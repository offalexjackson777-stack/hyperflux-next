// SPDX-License-Identifier: GPL-2.0-only

use crate::bridge::{BridgeExchange, BridgeExchangeResult};
use crate::uhid::{
    FEATURE_REPORT_BYTES, TransportEnvelopeKind, UhidEvent, UhidReply, encode_transport_envelope,
    is_feature_request,
};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FeatureEndpointState {
    Created,
    Started,
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EndpointError {
    InvalidState,
    PendingResponse,
    InvalidFeatureRequest,
}

impl fmt::Display for EndpointError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidState => "feature endpoint is not started",
            Self::PendingResponse => "a native response is still pending",
            Self::InvalidFeatureRequest => "UHID request is not the exact native Feature report",
        })
    }
}

impl std::error::Error for EndpointError {}

/// One lossless SET-then-GET Feature-report exchange.
///
/// `OpenRazer` writes one exact 90-byte Razer request and then reads its matching
/// response. A transport failure is encoded as an invalid Razer response so a
/// source-bound `OpenRazer` adapter can retain the bridge's retry certainty.
pub struct FeatureEndpoint<B> {
    bridge: B,
    state: FeatureEndpointState,
    pending_response: Option<[u8; FEATURE_REPORT_BYTES]>,
}

impl<B: BridgeExchange> FeatureEndpoint<B> {
    #[must_use]
    pub const fn new(bridge: B) -> Self {
        Self {
            bridge,
            state: FeatureEndpointState::Created,
            pending_response: None,
        }
    }

    #[must_use]
    pub const fn state(&self) -> FeatureEndpointState {
        self.state
    }

    #[must_use]
    pub const fn has_pending_response(&self) -> bool {
        self.pending_response.is_some()
    }

    pub(crate) fn bridge_mut(&mut self) -> &mut B {
        &mut self.bridge
    }

    /// Performs one relay-owned read-only management exchange only when no
    /// `OpenRazer` response is pending. This keeps direct-route identity probing
    /// serialized with the same ephemeral native-control lease discipline as
    /// normal Feature traffic.
    pub fn management_exchange(
        &mut self,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> Option<BridgeExchangeResult> {
        self.pending_response
            .is_none()
            .then(|| self.bridge.exchange(report))
    }

    /// Handles a decoded UHID event without exposing input collections.
    ///
    /// # Errors
    ///
    /// Returns a typed local error for invalid sequencing. Rejected GET/SET
    /// requests still receive an EIO reply and never reach the bridge.
    pub fn handle(&mut self, event: UhidEvent) -> Result<Option<UhidReply>, EndpointError> {
        match event {
            UhidEvent::Start { flags } => {
                if flags & 0x04 != 0 {
                    return Err(EndpointError::InvalidFeatureRequest);
                }
                self.state = FeatureEndpointState::Started;
                Ok(None)
            }
            UhidEvent::Stop => {
                self.state = FeatureEndpointState::Stopped;
                self.pending_response = None;
                Ok(None)
            }
            UhidEvent::Open | UhidEvent::Close | UhidEvent::Output | UhidEvent::Unknown { .. } => {
                Ok(None)
            }
            UhidEvent::SetReport {
                id,
                report_number,
                report_type,
                data,
            } => Ok(Some(self.handle_set(id, report_number, report_type, &data))),
            UhidEvent::GetReport {
                id,
                report_number,
                report_type,
            } => Ok(Some(self.handle_get(id, report_number, report_type))),
        }
    }

    fn handle_set(
        &mut self,
        id: u32,
        report_number: u8,
        report_type: u8,
        data: &[u8],
    ) -> UhidReply {
        if self.state != FeatureEndpointState::Started
            || !is_feature_request(report_number, report_type)
            || data.len() != FEATURE_REPORT_BYTES
            || self.pending_response.is_some()
        {
            return UhidReply::SetReport { id, success: false };
        }
        let mut report = [0_u8; FEATURE_REPORT_BYTES];
        report.copy_from_slice(data);
        let response = match self.bridge.exchange(report) {
            BridgeExchangeResult::MatchedResponse(response) => response,
            BridgeExchangeResult::NotSent => {
                encode_transport_envelope(TransportEnvelopeKind::NotSent)
            }
            BridgeExchangeResult::MayHaveBeenSent => {
                encode_transport_envelope(TransportEnvelopeKind::MayHaveBeenSent)
            }
        };
        self.pending_response = Some(response);
        UhidReply::SetReport { id, success: true }
    }

    fn handle_get(&mut self, id: u32, report_number: u8, report_type: u8) -> UhidReply {
        if self.state != FeatureEndpointState::Started
            || !is_feature_request(report_number, report_type)
        {
            return UhidReply::GetReport { id, data: None };
        }
        UhidReply::GetReport {
            id,
            data: self
                .pending_response
                .take()
                .map(|response| response.to_vec()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::uhid::decode_transport_envelope;
    use std::collections::VecDeque;

    struct FakeBridge {
        results: VecDeque<BridgeExchangeResult>,
        requests: Vec<[u8; FEATURE_REPORT_BYTES]>,
    }

    impl BridgeExchange for FakeBridge {
        fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
            self.requests.push(report);
            self.results.pop_front().expect("test bridge has a result")
        }
    }

    fn endpoint(results: Vec<BridgeExchangeResult>) -> FeatureEndpoint<FakeBridge> {
        let mut endpoint = FeatureEndpoint::new(FakeBridge {
            results: results.into(),
            requests: Vec::new(),
        });
        endpoint
            .handle(UhidEvent::Start { flags: 0 })
            .expect("endpoint starts");
        endpoint
    }

    fn set(id: u32, byte: u8) -> UhidEvent {
        UhidEvent::SetReport {
            id,
            report_number: 0,
            report_type: 0,
            data: vec![byte; FEATURE_REPORT_BYTES],
        }
    }

    fn get(id: u32) -> UhidEvent {
        UhidEvent::GetReport {
            id,
            report_number: 0,
            report_type: 0,
        }
    }

    #[test]
    fn matched_response_is_returned_untouched_and_consumed_once() {
        let expected = [0xa5; FEATURE_REPORT_BYTES];
        let mut endpoint = endpoint(vec![BridgeExchangeResult::MatchedResponse(expected)]);
        assert_eq!(
            endpoint.handle(set(10, 0x42)).expect("set succeeds"),
            Some(UhidReply::SetReport {
                id: 10,
                success: true
            })
        );
        assert_eq!(endpoint.bridge.requests, vec![[0x42; FEATURE_REPORT_BYTES]]);
        assert_eq!(
            endpoint.handle(get(11)).expect("get succeeds"),
            Some(UhidReply::GetReport {
                id: 11,
                data: Some(expected.to_vec())
            })
        );
        assert_eq!(
            endpoint.handle(get(12)).expect("empty get is answered"),
            Some(UhidReply::GetReport { id: 12, data: None })
        );
    }

    #[test]
    fn transport_certainty_is_preserved_in_the_response_channel() {
        for (result, expected) in [
            (
                BridgeExchangeResult::NotSent,
                TransportEnvelopeKind::NotSent,
            ),
            (
                BridgeExchangeResult::MayHaveBeenSent,
                TransportEnvelopeKind::MayHaveBeenSent,
            ),
        ] {
            let mut endpoint = endpoint(vec![result]);
            endpoint.handle(set(1, 0x11)).expect("set succeeds");
            let Some(UhidReply::GetReport {
                data: Some(response),
                ..
            }) = endpoint.handle(get(2)).expect("get succeeds")
            else {
                panic!("response is present");
            };
            assert_eq!(decode_transport_envelope(&response), Some(expected));
        }
    }

    #[test]
    fn invalid_and_overlapping_requests_never_reach_the_bridge() {
        let mut endpoint = endpoint(vec![BridgeExchangeResult::NotSent]);
        assert_eq!(
            endpoint
                .handle(UhidEvent::SetReport {
                    id: 1,
                    report_number: 1,
                    report_type: 0,
                    data: vec![0; FEATURE_REPORT_BYTES],
                })
                .expect("invalid set is answered"),
            Some(UhidReply::SetReport {
                id: 1,
                success: false
            })
        );
        assert!(endpoint.bridge.requests.is_empty());

        endpoint.handle(set(2, 0x22)).expect("first set succeeds");
        assert_eq!(
            endpoint.handle(set(3, 0x33)).expect("overlap is answered"),
            Some(UhidReply::SetReport {
                id: 3,
                success: false
            })
        );
        assert_eq!(endpoint.bridge.requests, vec![[0x22; FEATURE_REPORT_BYTES]]);
    }

    #[test]
    fn stop_discards_pending_data_and_blocks_exchange_until_restart() {
        let mut endpoint = endpoint(vec![BridgeExchangeResult::NotSent]);
        endpoint.handle(set(1, 0x44)).expect("set succeeds");
        endpoint.handle(UhidEvent::Stop).expect("stop succeeds");
        assert_eq!(endpoint.state(), FeatureEndpointState::Stopped);
        assert!(!endpoint.has_pending_response());
        assert_eq!(
            endpoint.handle(get(2)).expect("stopped get is answered"),
            Some(UhidReply::GetReport { id: 2, data: None })
        );
    }

    #[test]
    fn numbered_feature_start_is_rejected() {
        let mut endpoint = FeatureEndpoint::new(FakeBridge {
            results: VecDeque::new(),
            requests: Vec::new(),
        });
        assert_eq!(
            endpoint.handle(UhidEvent::Start { flags: 0x04 }),
            Err(EndpointError::InvalidFeatureRequest)
        );
        assert_eq!(endpoint.state(), FeatureEndpointState::Created);
    }

    #[test]
    fn management_exchange_never_interleaves_with_a_pending_openrazer_response() {
        let expected = [0x55; FEATURE_REPORT_BYTES];
        let mut endpoint = endpoint(vec![
            BridgeExchangeResult::MatchedResponse(expected),
            BridgeExchangeResult::NotSent,
        ]);
        endpoint.handle(set(1, 0x11)).expect("set succeeds");
        assert_eq!(
            endpoint.management_exchange([0x22; FEATURE_REPORT_BYTES]),
            None
        );
        assert_eq!(endpoint.bridge.requests, vec![[0x11; FEATURE_REPORT_BYTES]]);
        endpoint.handle(get(2)).expect("get consumes response");
        assert_eq!(
            endpoint.management_exchange([0x22; FEATURE_REPORT_BYTES]),
            Some(BridgeExchangeResult::NotSent)
        );
        assert_eq!(
            endpoint.bridge.requests,
            vec![[0x11; FEATURE_REPORT_BYTES], [0x22; FEATURE_REPORT_BYTES]]
        );
    }
}
