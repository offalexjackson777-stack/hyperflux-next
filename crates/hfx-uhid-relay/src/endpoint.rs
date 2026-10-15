// SPDX-License-Identifier: GPL-2.0-only

use crate::NativeCommandPolicy;
use crate::bridge::{BridgeExchange, BridgeExchangeResult};
use crate::catalog::SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS;
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BatteryExchangeDiagnostic {
    Matched {
        transaction_id: u8,
        reply_transaction_id: u8,
        status: u8,
        raw_value: u8,
        accepted: bool,
        attempts: u8,
    },
    NotSent {
        transaction_id: u8,
        attempts: u8,
    },
    MayHaveBeenSent {
        transaction_id: u8,
        attempts: u8,
    },
}

const RAZER_STATUS_BUSY: u8 = 0x01;
const RAZER_STATUS_SUCCESS: u8 = 0x02;
pub(crate) const RAZER_STATUS_TIMEOUT: u8 = 0x04;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RetainedRouteDeparture {
    None,
    AwaitingGet,
    ReplyIssued,
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
    command_policy: NativeCommandPolicy,
    state: FeatureEndpointState,
    pending_response: Option<[u8; FEATURE_REPORT_BYTES]>,
    pending_battery_diagnostic: Option<BatteryExchangeDiagnostic>,
    retained_route_departure: RetainedRouteDeparture,
    virtual_device_mode: [u8; 2],
}

impl<B: BridgeExchange> FeatureEndpoint<B> {
    #[must_use]
    pub fn new(bridge: B, command_policy: NativeCommandPolicy) -> Self {
        Self {
            bridge,
            command_policy,
            state: FeatureEndpointState::Created,
            pending_response: None,
            pending_battery_diagnostic: None,
            retained_route_departure: RetainedRouteDeparture::None,
            virtual_device_mode: [0, 0],
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

    pub(crate) fn take_battery_diagnostic(&mut self) -> Option<BatteryExchangeDiagnostic> {
        self.pending_battery_diagnostic.take()
    }

    pub(crate) fn bridge_mut(&mut self) -> &mut B {
        &mut self.bridge
    }

    pub(crate) fn into_bridge(self) -> B {
        self.bridge
    }

    pub(crate) const fn retained_route_departure_ready(&self) -> bool {
        matches!(
            self.retained_route_departure,
            RetainedRouteDeparture::ReplyIssued
        )
    }

    pub(crate) fn prepare_withdrawal(&mut self) {
        self.state = FeatureEndpointState::Stopped;
        self.pending_response = None;
        self.pending_battery_diagnostic = None;
        self.retained_route_departure = RetainedRouteDeparture::None;
        self.virtual_device_mode = [0, 0];
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
                self.prepare_withdrawal();
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
            || self.retained_route_departure != RetainedRouteDeparture::None
        {
            return UhidReply::SetReport { id, success: false };
        }
        let mut report = [0_u8; FEATURE_REPORT_BYTES];
        report.copy_from_slice(data);
        if !self.command_policy.allows(&report) {
            return UhidReply::SetReport { id, success: false };
        }
        let response = if let Some(response) = self.virtualized_response(&report) {
            response
        } else {
            let (result, attempts) = self.exchange(report);
            if is_battery_request(&report) {
                self.pending_battery_diagnostic = Some(match result {
                    BridgeExchangeResult::MatchedResponse(response) => {
                        BatteryExchangeDiagnostic::Matched {
                            transaction_id: report[1],
                            reply_transaction_id: response[1],
                            status: response[0],
                            raw_value: response[9],
                            accepted: battery_reply_accepted(&report, &response),
                            attempts,
                        }
                    }
                    BridgeExchangeResult::NotSent(_) => BatteryExchangeDiagnostic::NotSent {
                        transaction_id: report[1],
                        attempts,
                    },
                    BridgeExchangeResult::MayHaveBeenSent(_) => {
                        BatteryExchangeDiagnostic::MayHaveBeenSent {
                            transaction_id: report[1],
                            attempts,
                        }
                    }
                });
            }
            match result {
                BridgeExchangeResult::MatchedResponse(response) if is_battery_request(&report) => {
                    // A retained receiver route can outlive a powered-off
                    // child. Its in-flight battery read still completes, but
                    // as unknown: no earlier value is replayed as current.
                    if response[0] == RAZER_STATUS_TIMEOUT {
                        self.retained_route_departure = RetainedRouteDeparture::AwaitingGet;
                    }
                    if battery_reply_accepted(&report, &response) {
                        response
                    } else {
                        encode_transport_envelope(TransportEnvelopeKind::MayHaveBeenSent)
                    }
                }
                BridgeExchangeResult::MatchedResponse(response)
                    if response[0] == RAZER_STATUS_TIMEOUT =>
                {
                    self.retained_route_departure = RetainedRouteDeparture::AwaitingGet;
                    response
                }
                BridgeExchangeResult::MatchedResponse(response) => response,
                BridgeExchangeResult::NotSent(_) => {
                    encode_transport_envelope(TransportEnvelopeKind::NotSent)
                }
                BridgeExchangeResult::MayHaveBeenSent(_) => {
                    encode_transport_envelope(TransportEnvelopeKind::MayHaveBeenSent)
                }
            }
        };
        self.pending_response = Some(response);
        UhidReply::SetReport { id, success: true }
    }

    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> (BridgeExchangeResult, u8) {
        let maximum_attempts = if is_battery_request(&report) {
            SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS
        } else {
            1
        };
        for attempt in 1..=maximum_attempts {
            let result = self.bridge.exchange(report);
            if matches!(
                result,
                BridgeExchangeResult::MatchedResponse(response)
                    if response[0] == RAZER_STATUS_BUSY
                        || !reply_identity_matches(&report, &response)
            ) && attempt < maximum_attempts
            {
                continue;
            }
            return (result, attempt);
        }
        unreachable!("source BUSY retry bound is nonzero")
    }

    fn virtualized_response(
        &mut self,
        request: &[u8; FEATURE_REPORT_BYTES],
    ) -> Option<[u8; FEATURE_REPORT_BYTES]> {
        if request[5] != 2 || request[6] != 0 {
            return None;
        }
        match request[7] {
            0x04 => self.virtual_device_mode.copy_from_slice(&request[8..10]),
            0x84 => {}
            _ => return None,
        }
        let mut response = *request;
        response[0] = 0x02;
        response[8..10].copy_from_slice(&self.virtual_device_mode);
        response[88] = response[2..88]
            .iter()
            .fold(0_u8, |checksum, byte| checksum ^ byte);
        Some(response)
    }

    fn handle_get(&mut self, id: u32, report_number: u8, report_type: u8) -> UhidReply {
        if self.state != FeatureEndpointState::Started
            || !is_feature_request(report_number, report_type)
        {
            return UhidReply::GetReport { id, data: None };
        }
        let data = self
            .pending_response
            .take()
            .map(|response| response.to_vec());
        if data.is_some() && self.retained_route_departure == RetainedRouteDeparture::AwaitingGet {
            self.retained_route_departure = RetainedRouteDeparture::ReplyIssued;
        }
        UhidReply::GetReport { id, data }
    }
}

const fn is_battery_request(report: &[u8; FEATURE_REPORT_BYTES]) -> bool {
    report[5] == 2 && report[6] == 0x07 && report[7] == 0x80 && report[8] == 0 && report[9] == 0
}

// `OpenRazer` checks a reply's command class and ID but not its transaction
// ID, so a late reply for another lane on the shared receiver would otherwise
// be read as this device's battery level.
const fn reply_identity_matches(
    request: &[u8; FEATURE_REPORT_BYTES],
    response: &[u8; FEATURE_REPORT_BYTES],
) -> bool {
    response[1] == request[1] && response[6] == request[6] && response[7] == request[7]
}

const fn battery_reply_accepted(
    request: &[u8; FEATURE_REPORT_BYTES],
    response: &[u8; FEATURE_REPORT_BYTES],
) -> bool {
    response[0] == RAZER_STATUS_SUCCESS && reply_identity_matches(request, response)
}

#[cfg(test)]
mod tests;
