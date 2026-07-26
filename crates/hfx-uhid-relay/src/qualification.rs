// SPDX-License-Identifier: GPL-2.0-only

use crate::{BridgeExchange, BridgeExchangeResult, FEATURE_REPORT_BYTES, NativeQualificationProbe};
use hfx_kernel_transport::{
    KernelIo, KernelReceiverTransport, KernelTransportError, NativeEndpointLane,
    NativeExchangeCertainty, NativeExchangeOutcome, NativeExchangeRequest,
};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

const QUALIFICATION_TIMEOUT: Duration = Duration::from_secs(1);
const MAX_QUALIFICATION_EVENTS: usize = 128;
const DEVICE_STATUS_BUSY: u8 = 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum QualificationExchangeCertainty {
    NotSent,
    MayHaveBeenSent,
    MatchedResponse,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QualificationExchangeRecord {
    pub sequence: u64,
    pub method: Option<String>,
    pub attempt: u8,
    pub request: [u8; FEATURE_REPORT_BYTES],
    pub certainty: QualificationExchangeCertainty,
    pub transport_errno: i32,
    pub response: Option<[u8; FEATURE_REPORT_BYTES]>,
}

pub(crate) trait QualificationNativeTransport {
    fn exchange(
        &mut self,
        request: &NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, KernelTransportError>;
}

impl<I: KernelIo> QualificationNativeTransport for KernelReceiverTransport<I> {
    fn exchange(
        &mut self,
        request: &NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, KernelTransportError> {
        self.native_exchange(request)
    }
}

/// Temporary candidate-route transport used only by the supervised parity tool.
///
/// The production bridge remains the sole issuer of native route tokens. This
/// transport instead accepts only exact read signatures embedded from the
/// pinned `OpenRazer` source and binds every exchange to one endpoint lane and
/// one short-lived kernel writer session.
pub(crate) struct QualificationBridge<T> {
    transport: T,
    endpoint: NativeEndpointLane,
    route_token_digest: [u8; 32],
    probes: Vec<NativeQualificationProbe>,
    maximum_attempts: u8,
    attempts: BTreeMap<String, u8>,
    terminal_methods: BTreeSet<String>,
    next_nonce: u64,
    records: Vec<QualificationExchangeRecord>,
}

impl<T: QualificationNativeTransport> QualificationBridge<T> {
    #[must_use]
    pub fn new(
        transport: T,
        endpoint: NativeEndpointLane,
        route_token_digest: [u8; 32],
        probes: Vec<NativeQualificationProbe>,
        maximum_attempts: u8,
    ) -> Option<Self> {
        (!probes.is_empty()
            && (1..=5).contains(&maximum_attempts)
            && route_token_digest.iter().any(|byte| *byte != 0))
        .then_some(Self {
            transport,
            endpoint,
            route_token_digest,
            probes,
            maximum_attempts,
            attempts: BTreeMap::new(),
            terminal_methods: BTreeSet::new(),
            next_nonce: 1,
            records: Vec::with_capacity(MAX_QUALIFICATION_EVENTS),
        })
    }

    #[must_use]
    pub fn records(&self) -> &[QualificationExchangeRecord] {
        &self.records
    }

    #[must_use]
    pub fn all_methods_observed(&self) -> bool {
        self.probes.iter().all(|probe| {
            self.records.iter().any(|record| {
                record.method.as_deref() == Some(probe.method.as_str())
                    && record.certainty == QualificationExchangeCertainty::MatchedResponse
                    && record
                        .response
                        .as_ref()
                        .is_some_and(|response| response[0] != DEVICE_STATUS_BUSY)
            })
        })
    }

    fn record(
        &mut self,
        method: Option<String>,
        attempt: u8,
        request: [u8; FEATURE_REPORT_BYTES],
        certainty: QualificationExchangeCertainty,
        transport_errno: i32,
        response: Option<[u8; FEATURE_REPORT_BYTES]>,
    ) {
        if self.records.len() >= MAX_QUALIFICATION_EVENTS {
            return;
        }
        let sequence = u64::try_from(self.records.len())
            .unwrap_or(u64::MAX)
            .saturating_add(1);
        self.records.push(QualificationExchangeRecord {
            sequence,
            method,
            attempt,
            request,
            certainty,
            transport_errno,
            response,
        });
    }

    fn matching_method(&self, report: &[u8; FEATURE_REPORT_BYTES]) -> Option<String> {
        let mut matches = self.probes.iter().filter(|probe| probe.matches(report));
        let method = matches.next()?.method.clone();
        matches.next().is_none().then_some(method)
    }

    fn prepare_exchange(
        &mut self,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> Option<(String, u8, NativeExchangeRequest)> {
        if self.records.len() >= MAX_QUALIFICATION_EVENTS {
            return None;
        }
        let Some(method) = self.matching_method(&report) else {
            self.record(
                None,
                0,
                report,
                QualificationExchangeCertainty::NotSent,
                0,
                None,
            );
            return None;
        };
        let previous_attempt = self.attempts.get(&method).copied().unwrap_or(0);
        if self.terminal_methods.contains(&method) {
            self.record(
                Some(method),
                previous_attempt.saturating_add(1),
                report,
                QualificationExchangeCertainty::NotSent,
                0,
                None,
            );
            return None;
        }
        if previous_attempt >= self.maximum_attempts {
            self.record(
                Some(method),
                previous_attempt.saturating_add(1),
                report,
                QualificationExchangeCertainty::NotSent,
                0,
                None,
            );
            return None;
        }
        let attempt = previous_attempt.saturating_add(1);
        self.attempts.insert(method.clone(), attempt);
        let nonce = self.next_nonce;
        let Some(next_nonce) = nonce.checked_add(1) else {
            self.record(
                Some(method),
                attempt,
                report,
                QualificationExchangeCertainty::NotSent,
                0,
                None,
            );
            return None;
        };
        self.next_nonce = next_nonce;
        let request = NativeExchangeRequest {
            dispatch_nonce: nonce,
            endpoint: self.endpoint,
            route_token_digest: self.route_token_digest,
            report,
            timeout: QUALIFICATION_TIMEOUT,
        };
        Some((method, attempt, request))
    }

    fn complete_exchange(
        &mut self,
        method: String,
        attempt: u8,
        report: [u8; FEATURE_REPORT_BYTES],
        outcome: Result<NativeExchangeOutcome, KernelTransportError>,
    ) -> BridgeExchangeResult {
        let (certainty, transport_errno, response, result) = match outcome {
            Ok(outcome) => match (outcome.certainty, outcome.response) {
                (NativeExchangeCertainty::MatchedResponse, Some(response)) => (
                    QualificationExchangeCertainty::MatchedResponse,
                    outcome.transport_errno,
                    Some(response),
                    BridgeExchangeResult::MatchedResponse(response),
                ),
                (NativeExchangeCertainty::NotSent, None) => (
                    QualificationExchangeCertainty::NotSent,
                    outcome.transport_errno,
                    None,
                    BridgeExchangeResult::NotSent,
                ),
                _ => (
                    QualificationExchangeCertainty::MayHaveBeenSent,
                    outcome.transport_errno,
                    None,
                    BridgeExchangeResult::MayHaveBeenSent,
                ),
            },
            Err(error) if error.failure_facts().automatic_retry_safe => (
                QualificationExchangeCertainty::NotSent,
                0,
                None,
                BridgeExchangeResult::NotSent,
            ),
            Err(_) => (
                QualificationExchangeCertainty::MayHaveBeenSent,
                0,
                None,
                BridgeExchangeResult::MayHaveBeenSent,
            ),
        };
        let retry_allowed = certainty == QualificationExchangeCertainty::MatchedResponse
            && response
                .as_ref()
                .is_some_and(|value| value[0] == DEVICE_STATUS_BUSY);
        if !retry_allowed {
            self.terminal_methods.insert(method.clone());
        }
        self.record(
            Some(method),
            attempt,
            report,
            certainty,
            transport_errno,
            response,
        );
        result
    }
}

impl<T: QualificationNativeTransport> BridgeExchange for QualificationBridge<T> {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
        let Some((method, attempt, request)) = self.prepare_exchange(report) else {
            return BridgeExchangeResult::NotSent;
        };
        let outcome = self.transport.exchange(&request);
        self.complete_exchange(method, attempt, report, outcome)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct FakeTransport {
        outcomes: VecDeque<NativeExchangeOutcome>,
        requests: Vec<NativeExchangeRequest>,
    }

    impl QualificationNativeTransport for FakeTransport {
        fn exchange(
            &mut self,
            request: &NativeExchangeRequest,
        ) -> Result<NativeExchangeOutcome, KernelTransportError> {
            self.requests.push(request.clone());
            Ok(self.outcomes.pop_front().expect("an outcome is configured"))
        }
    }

    fn probe() -> NativeQualificationProbe {
        NativeQualificationProbe {
            method: "get_firmware".to_owned(),
            privacy: "summary-only".to_owned(),
            attribute: "firmware_version".to_owned(),
            response_summary: "firmware-version".to_owned(),
            comparison: "exact-semantic".to_owned(),
            command_class: 0,
            command_id: 0x81,
            data_size: 2,
            arguments: vec![0, 0],
            normalized_sha256: "0".repeat(64),
        }
    }

    fn report() -> [u8; FEATURE_REPORT_BYTES] {
        let mut report = [0_u8; FEATURE_REPORT_BYTES];
        report[1] = 0x1f;
        report[5] = 2;
        report[7] = 0x81;
        report[88] = report[2..88]
            .iter()
            .fold(0_u8, |checksum, byte| checksum ^ byte);
        report
    }

    fn outcome(response: [u8; FEATURE_REPORT_BYTES]) -> NativeExchangeOutcome {
        NativeExchangeOutcome {
            authorization_epoch: 1,
            kernel_sequence: 1,
            dispatch_nonce: 1,
            endpoint: NativeEndpointLane::Pointer,
            route_token_digest: [0x44; 32],
            request_digest: [0x55; 32],
            certainty: NativeExchangeCertainty::MatchedResponse,
            transport_errno: 0,
            response: Some(response),
        }
    }

    #[test]
    fn exact_read_report_crosses_transport_once_and_is_recorded() {
        let response = [0x22; FEATURE_REPORT_BYTES];
        let transport = FakeTransport {
            outcomes: vec![outcome(response)].into(),
            requests: Vec::new(),
        };
        let mut bridge = QualificationBridge::new(
            transport,
            NativeEndpointLane::Pointer,
            [0x44; 32],
            vec![probe()],
            5,
        )
        .expect("bridge is valid");
        assert_eq!(
            bridge.exchange(report()),
            BridgeExchangeResult::MatchedResponse(response)
        );
        assert_eq!(bridge.transport.requests.len(), 1);
        assert_eq!(bridge.records().len(), 1);
        assert_eq!(
            bridge.records()[0].certainty,
            QualificationExchangeCertainty::MatchedResponse
        );
    }

    #[test]
    fn unknown_or_sixth_report_never_reaches_kernel_transport() {
        let transport = FakeTransport {
            outcomes: (0..5).map(|_| outcome([DEVICE_STATUS_BUSY; 90])).collect(),
            requests: Vec::new(),
        };
        let mut bridge = QualificationBridge::new(
            transport,
            NativeEndpointLane::Pointer,
            [0x44; 32],
            vec![probe()],
            5,
        )
        .expect("bridge is valid");
        let mut unknown = report();
        unknown[7] = 0x01;
        unknown[88] = unknown[2..88]
            .iter()
            .fold(0_u8, |checksum, byte| checksum ^ byte);
        assert_eq!(bridge.exchange(unknown), BridgeExchangeResult::NotSent);
        for _ in 0..5 {
            assert!(matches!(
                bridge.exchange(report()),
                BridgeExchangeResult::MatchedResponse(_)
            ));
        }
        assert_eq!(bridge.exchange(report()), BridgeExchangeResult::NotSent);
        assert_eq!(bridge.transport.requests.len(), 5);
    }

    #[test]
    fn only_busy_response_permits_one_more_transport_attempt() {
        let mut success = [0_u8; FEATURE_REPORT_BYTES];
        success[0] = 2;
        let transport = FakeTransport {
            outcomes: vec![outcome([DEVICE_STATUS_BUSY; 90]), outcome(success)].into(),
            requests: Vec::new(),
        };
        let mut bridge = QualificationBridge::new(
            transport,
            NativeEndpointLane::Pointer,
            [0x44; 32],
            vec![probe()],
            5,
        )
        .expect("bridge is valid");

        assert!(matches!(
            bridge.exchange(report()),
            BridgeExchangeResult::MatchedResponse(response)
                if response[0] == DEVICE_STATUS_BUSY
        ));
        assert!(matches!(
            bridge.exchange(report()),
            BridgeExchangeResult::MatchedResponse(response) if response[0] == 2
        ));
        assert_eq!(bridge.exchange(report()), BridgeExchangeResult::NotSent);
        assert_eq!(bridge.transport.requests.len(), 2);
        assert_eq!(
            bridge.records()[2].certainty,
            QualificationExchangeCertainty::NotSent
        );
    }

    #[test]
    fn uncertain_outcome_permanently_blocks_automatic_retry() {
        let transport = FakeTransport {
            outcomes: vec![NativeExchangeOutcome {
                authorization_epoch: 1,
                kernel_sequence: 1,
                dispatch_nonce: 1,
                endpoint: NativeEndpointLane::Pointer,
                route_token_digest: [0x44; 32],
                request_digest: [0x55; 32],
                certainty: NativeExchangeCertainty::MayHaveBeenSent,
                transport_errno: 0,
                response: None,
            }]
            .into(),
            requests: Vec::new(),
        };
        let mut bridge = QualificationBridge::new(
            transport,
            NativeEndpointLane::Pointer,
            [0x44; 32],
            vec![probe()],
            5,
        )
        .expect("bridge is valid");

        assert_eq!(
            bridge.exchange(report()),
            BridgeExchangeResult::MayHaveBeenSent
        );
        assert_eq!(bridge.exchange(report()), BridgeExchangeResult::NotSent);
        assert_eq!(bridge.transport.requests.len(), 1);
    }
}
