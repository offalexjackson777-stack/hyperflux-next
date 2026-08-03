// SPDX-License-Identifier: GPL-2.0-only

//! Native route reconciliation and exchange helpers.

use super::CoreBridgeBackend;
use super::failures::{
    finding, native_exchange_failure, native_projection_failure, native_route_failure,
    request_failure,
};
use crate::{
    NativeExchangeRejection, RestorationRuntime, RpcFailure, RuntimeProfileAuthority,
    project_native_routes,
};
use hfx_core::{
    Clock, EventSink, NATIVE_REPORT_BYTES, NativeTransportDispatch, NativeTransportOutcome,
    NativeTransportResult, ReceiverLifecycleRegistry, ReceiverTransport, WallClock,
};
use hfx_domain::{
    FindingId, NativeReportHex, NativeRouteAccess, NativeRouteState, ProtocolErrorKind,
};
use hfx_errors::ErrorCode;
use hfx_integration_model::SourceValidatedDeviceCatalog;
use hfx_profiles::{NativeExchangeRecord, native_response_matches};
use hfx_protocol::{
    NativeExchangeMatchedResponse, NativeExchangeNotSent, NativeExchangeOutcome,
    NativeExchangeRequest, NativeExchangeResult, NativeExchangeUncertain, NativeRoute,
};
use sha2::{Digest as _, Sha256};

impl<C, W, T, R, S> CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
    S: EventSink,
{
    pub(super) fn synchronize_native_routes(&mut self) -> Result<(), RpcFailure> {
        let previous = self.native_routes.inventory().routes;
        let mut routes = project_native_routes(
            &self.receivers,
            &self.profiles,
            &self.source_validated_devices,
        )
        .map_err(native_projection_failure)?;
        for route in &mut routes {
            if route.route_state == NativeRouteState::Available
                && !self
                    .transport
                    .native_exchange_available(&route.receiver_id, route.generation_id)
            {
                route.route_state = NativeRouteState::TemporarilyUnavailable;
            }
        }
        self.native_routes
            .synchronize(routes, &mut self.identities)
            .map_err(native_route_failure)?;
        let current = self.native_routes.inventory().routes;

        for route in &current {
            if let Some(route_token) = &route.route_token {
                self.native_invalidations.retire(route_token);
            }
        }
        for route in previous {
            let Some(route_token) = route.route_token.as_ref() else {
                continue;
            };
            if current
                .iter()
                .any(|candidate| candidate.route_token.as_ref() == Some(route_token))
            {
                continue;
            }
            let rejection = native_route_invalidation(&route, &current);
            let finding_id = native_finding(rejection.error_kind(), false);
            self.native_exchanges.invalidate_route(
                route_token,
                rejection.error_kind(),
                &finding_id,
            );
            self.native_invalidations
                .invalidate(route_token.clone(), rejection);
        }
        Ok(())
    }

    pub(super) fn complete_native_exchange(
        &mut self,
        request: NativeExchangeRequest,
        result: NativeExchangeResult,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        if let Some(outcome) = self
            .native_exchanges
            .retained(&request)
            .map_err(native_exchange_failure)?
        {
            return Ok(outcome);
        }
        let outcome = NativeExchangeOutcome {
            request_id: request.request_id.clone(),
            route_token: request.route_token.clone(),
            result,
        };
        self.native_exchanges
            .remember(request, outcome.clone())
            .map_err(native_exchange_failure)?;
        Ok(outcome)
    }

    pub(super) fn reject_native_before_set(
        &mut self,
        request: NativeExchangeRequest,
        error_kind: ProtocolErrorKind,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        self.complete_native_exchange(
            request,
            NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind,
                finding_id: native_finding(error_kind, false),
            }),
        )
    }
}

pub(super) fn decode_native_report(
    value: &NativeReportHex,
) -> Result<[u8; NATIVE_REPORT_BYTES], RpcFailure> {
    let bytes = value.as_str().as_bytes();
    if bytes.len() != NATIVE_REPORT_BYTES * 2 {
        return Err(request_failure());
    }
    let mut report = [0_u8; NATIVE_REPORT_BYTES];
    for (index, pair) in bytes.chunks_exact(2).enumerate() {
        let high = decode_lower_hex(pair[0]).ok_or_else(request_failure)?;
        let low = decode_lower_hex(pair[1]).ok_or_else(request_failure)?;
        report[index] = (high << 4) | low;
    }
    Ok(report)
}

const fn decode_lower_hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
}

fn encode_native_report(report: &[u8; NATIVE_REPORT_BYTES]) -> Result<NativeReportHex, RpcFailure> {
    const LOWER_HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(NATIVE_REPORT_BYTES * 2);
    for byte in report {
        encoded.push(char::from(LOWER_HEX[usize::from(byte >> 4)]));
        encoded.push(char::from(LOWER_HEX[usize::from(byte & 0x0f)]));
    }
    NativeReportHex::try_from(encoded).map_err(|_| RpcFailure::internal())
}

pub(super) fn native_route_token_digest(token: &hfx_domain::NativeRouteToken) -> [u8; 32] {
    Sha256::digest(token.as_str().as_bytes()).into()
}

pub(super) fn native_profile_for_route(
    profiles: &RuntimeProfileAuthority,
    receivers: &ReceiverLifecycleRegistry,
    route: &NativeRoute,
) -> Result<NativeExchangeRecord, ProtocolErrorKind> {
    let profile = profiles
        .view(receivers)
        .native_exchange(&route.receiver_id, route.generation_id)
        .ok_or(ProtocolErrorKind::UnsupportedFeature)?;
    if profile.protocol_family != route.protocol_family.as_str()
        || profile.report_bytes != route.report_bytes.get()
    {
        return Err(ProtocolErrorKind::InvalidRequest);
    }
    Ok(profile)
}

pub(super) fn native_command_allowed(
    source_validated: &SourceValidatedDeviceCatalog,
    route: &NativeRoute,
    report: &[u8; NATIVE_REPORT_BYTES],
) -> bool {
    // Native authorization is deliberately application-neutral: the session,
    // route token, and lease are trusted boundaries; a client-chosen app name
    // is not. Each authenticated local native client receives the complete
    // source-proven safe union for this exact device identity.
    source_validated
        .device(route.vendor_id, route.product_id, route.device_kind)
        .and_then(|device| device.command_policy.as_ref())
        .is_some_and(|policy| policy.allows(report))
}

pub(super) fn validated_native_transport_result(
    profile: &NativeExchangeRecord,
    request: &[u8; NATIVE_REPORT_BYTES],
    dispatch: &NativeTransportDispatch,
    outcome: &NativeTransportOutcome,
) -> Result<NativeExchangeResult, RpcFailure> {
    if outcome.dispatch_nonce != dispatch.dispatch_nonce
        || outcome.route_token_digest != dispatch.route_token_digest
    {
        return Ok(native_uncertain_result());
    }
    if let NativeTransportResult::MatchedResponse(response) = &outcome.result
        && native_response_matches(profile, request, response) != Ok(true)
    {
        return Ok(native_uncertain_result());
    }
    protocol_native_result(&outcome.result)
}

pub(super) fn native_uncertain_result() -> NativeExchangeResult {
    NativeExchangeResult::MayHaveBeenSent(NativeExchangeUncertain {
        error_kind: ProtocolErrorKind::OutcomeUnknown,
        finding_id: finding(ErrorCode::HfxTransport002),
    })
}

fn protocol_native_result(
    result: &NativeTransportResult,
) -> Result<NativeExchangeResult, RpcFailure> {
    Ok(match result {
        NativeTransportResult::NotSent { error_kind } => {
            NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: *error_kind,
                finding_id: native_finding(*error_kind, false),
            })
        }
        NativeTransportResult::MayHaveBeenSent { error_kind } => {
            NativeExchangeResult::MayHaveBeenSent(NativeExchangeUncertain {
                error_kind: *error_kind,
                finding_id: native_finding(*error_kind, true),
            })
        }
        NativeTransportResult::MatchedResponse(response) => {
            NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: encode_native_report(response)?,
            })
        }
    })
}

fn native_finding(error_kind: ProtocolErrorKind, uncertain: bool) -> FindingId {
    if uncertain {
        return finding(ErrorCode::HfxTransport002);
    }
    finding(match error_kind {
        ProtocolErrorKind::IncompatibleVersion => ErrorCode::HfxProtocol001,
        ProtocolErrorKind::UnsupportedFeature => ErrorCode::HfxProfile001,
        ProtocolErrorKind::InvalidRequest => ErrorCode::HfxRequest001,
        ProtocolErrorKind::OwnershipConflict => ErrorCode::HfxOwnership002,
        ProtocolErrorKind::StaleGeneration => ErrorCode::HfxGeneration001,
        ProtocolErrorKind::DeadlineExceeded => ErrorCode::HfxDeadline001,
        ProtocolErrorKind::QueueFull => ErrorCode::HfxQueue001,
        ProtocolErrorKind::TransportFailure => ErrorCode::HfxTransport001,
        ProtocolErrorKind::OutcomeUnknown => ErrorCode::HfxOutcome001,
        ProtocolErrorKind::OutcomeEvicted => ErrorCode::HfxOutcome002,
        ProtocolErrorKind::InternalFailure => ErrorCode::HfxInternal001,
    })
}

pub(super) fn native_route_invalidation(
    previous: &NativeRoute,
    current: &[NativeRoute],
) -> NativeExchangeRejection {
    let replacement = current.iter().find(|candidate| {
        candidate.receiver_id == previous.receiver_id
            && candidate.device_id == previous.device_id
            && candidate.endpoint_id == previous.endpoint_id
    });
    match replacement {
        Some(route) if route.generation_id != previous.generation_id => {
            NativeExchangeRejection::StaleGeneration
        }
        Some(route) if route.access != NativeRouteAccess::Qualified => {
            NativeExchangeRejection::RouteWithdrawn
        }
        Some(route) if route.route_state == NativeRouteState::TemporarilyUnavailable => {
            NativeExchangeRejection::RouteUnavailable
        }
        Some(route) if route.route_state == NativeRouteState::Withdrawn => {
            NativeExchangeRejection::RouteWithdrawn
        }
        Some(_) => NativeExchangeRejection::RouteUnavailable,
        None => NativeExchangeRejection::RouteWithdrawn,
    }
}
