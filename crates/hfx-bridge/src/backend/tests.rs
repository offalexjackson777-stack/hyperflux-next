// SPDX-License-Identifier: GPL-2.0-only

use super::native::{
    native_command_allowed, native_route_invalidation, native_uncertain_result,
    validated_native_transport_result,
};
use super::*;
use crate::NativeExchangeRejection;
use hfx_core::{
    NATIVE_REPORT_BYTES, NativeEndpointLane, NativeTransportDispatch, NativeTransportOutcome,
    NativeTransportResult,
};
use hfx_domain::{
    AuthorizationEpoch, DeviceKind, DispatchNonce, EndpointId, GenerationId, LogicalDeviceId,
    NativeExchangeTimeoutMs, NativeProtocolFamily, NativeReportByteCount, NativeRouteAccess,
    NativeRouteState, ProductId, SessionId, VendorId,
};
use hfx_protocol::{NativeExchangeResult, NativeRoute};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test value is canonical")
}

fn route(generation: u64, access: NativeRouteAccess, route_state: NativeRouteState) -> NativeRoute {
    NativeRoute {
        receiver_id: text::<ReceiverId>("receiver-1"),
        generation_id: GenerationId::try_from(generation).expect("generation is canonical"),
        device_id: text::<LogicalDeviceId>("mouse-1"),
        endpoint_id: text::<EndpointId>("pointer"),
        device_kind: DeviceKind::Mouse,
        vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is canonical"),
        product_id: ProductId::try_from(0x00cd_u16).expect("product is canonical"),
        route_state,
        access,
        protocol_family: text::<NativeProtocolFamily>("razer-feature-report-v1"),
        report_bytes: NativeReportByteCount::try_from(90_u16).expect("report size is canonical"),
        route_token: None,
        resource: None,
    }
}

fn native_report(status: u8) -> [u8; NATIVE_REPORT_BYTES] {
    let mut report = [0_u8; NATIVE_REPORT_BYTES];
    report[0] = status;
    report[5] = 2;
    report[6] = 0x1f;
    report[7] = 0x03;
    report[8] = 0xaa;
    report[9] = 0x55;
    report[88] = report[2..88].iter().fold(0_u8, |value, byte| value ^ byte);
    report
}

fn native_dispatch() -> NativeTransportDispatch {
    NativeTransportDispatch {
        session_id: text::<SessionId>("session-1"),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64)
            .expect("authorization epoch is canonical"),
        dispatch_nonce: DispatchNonce::try_from(1_u64).expect("nonce is canonical"),
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is canonical"),
        endpoint: NativeEndpointLane::Pointer,
        route_token_digest: [0x5a; 32],
        report: native_report(0),
        timeout_ms: NativeExchangeTimeoutMs::try_from(100_u16).expect("timeout is canonical"),
    }
}

fn allowed_native_command() -> [u8; NATIVE_REPORT_BYTES] {
    let mut report = [0_u8; NATIVE_REPORT_BYTES];
    report[1] = 0x1f;
    report[5] = 1;
    report[6] = 0;
    report[7] = 5;
    report[8] = 1;
    report[88] = report[2..88].iter().fold(0_u8, |value, byte| value ^ byte);
    report
}

#[test]
fn bridge_uses_complete_device_union_without_application_name_authority() {
    let source_validated =
        SourceValidatedDeviceCatalog::load().expect("source-validated catalog loads");
    let mut route = route(1, NativeRouteAccess::Qualified, NativeRouteState::Available);
    route.product_id = ProductId::try_from(0x00ab_u16).expect("product is canonical");
    let device = source_validated
        .device(route.vendor_id, route.product_id, route.device_kind)
        .expect("source-validated route exists");
    let openrazer = device
        .openrazer_command_policy
        .as_ref()
        .expect("OpenRazer policy exists");
    let openrgb = device
        .openrgb_command_policy
        .as_ref()
        .expect("OpenRGB policy exists");

    let openrazer_only = allowed_native_command();
    assert!(openrazer.allows(&openrazer_only));
    assert!(!openrgb.allows(&openrazer_only));
    assert!(native_command_allowed(
        &source_validated,
        &route,
        &openrazer_only
    ));

    let mut openrgb_only = [0_u8; NATIVE_REPORT_BYTES];
    let arguments = [0, 0, 2, 2, 0, 2, 0xff, 0, 0, 0, 0, 0xff];
    openrgb_only[1] = 0x1f;
    openrgb_only[5] = u8::try_from(arguments.len()).expect("arguments are bounded");
    openrgb_only[6] = 0x0f;
    openrgb_only[7] = 0x02;
    openrgb_only[8..8 + arguments.len()].copy_from_slice(&arguments);
    openrgb_only[88] = openrgb_only[2..88]
        .iter()
        .fold(0_u8, |value, byte| value ^ byte);
    assert!(!openrazer.allows(&openrgb_only));
    assert!(openrgb.allows(&openrgb_only));
    assert!(native_command_allowed(
        &source_validated,
        &route,
        &openrgb_only
    ));

    let mut unknown = openrazer_only;
    unknown[6] = 0x7e;
    unknown[7] = 0x01;
    unknown[88] = unknown[2..88].iter().fold(0_u8, |value, byte| value ^ byte);
    assert!(!native_command_allowed(&source_validated, &route, &unknown));
}

#[test]
fn route_invalidation_preserves_generation_availability_and_authority_causes() {
    let previous = route(1, NativeRouteAccess::Qualified, NativeRouteState::Available);
    assert_eq!(
        native_route_invalidation(
            &previous,
            &[route(
                2,
                NativeRouteAccess::Qualified,
                NativeRouteState::Available,
            )],
        ),
        NativeExchangeRejection::StaleGeneration
    );
    assert_eq!(
        native_route_invalidation(
            &previous,
            &[route(
                1,
                NativeRouteAccess::Qualified,
                NativeRouteState::TemporarilyUnavailable,
            )],
        ),
        NativeExchangeRejection::RouteUnavailable
    );
    assert_eq!(
        native_route_invalidation(
            &previous,
            &[route(
                1,
                NativeRouteAccess::Candidate,
                NativeRouteState::Available,
            )],
        ),
        NativeExchangeRejection::RouteWithdrawn
    );
    assert_eq!(
        native_route_invalidation(&previous, &[]),
        NativeExchangeRejection::RouteWithdrawn
    );
}

#[test]
fn bridge_accepts_only_structurally_valid_matching_native_responses() {
    let catalog = hfx_profiles::RuntimeProfileCatalog::load().expect("profiles are valid");
    let profile = catalog
        .receiver(
            VendorId::try_from(0x1532_u16).expect("vendor is canonical"),
            ProductId::try_from(0x00cf_u16).expect("product is canonical"),
        )
        .and_then(|receiver| receiver.native_exchange)
        .expect("receiver native profile exists");
    let dispatch = native_dispatch();
    let matched = NativeTransportOutcome {
        dispatch_nonce: dispatch.dispatch_nonce,
        route_token_digest: dispatch.route_token_digest,
        result: NativeTransportResult::MatchedResponse(native_report(2)),
    };
    assert!(matches!(
        validated_native_transport_result(&profile, &dispatch.report, &dispatch, &matched),
        Ok(NativeExchangeResult::MatchedResponse(_))
    ));

    let mut malformed = native_report(2);
    malformed[88] ^= 1;
    let malformed = NativeTransportOutcome {
        result: NativeTransportResult::MatchedResponse(malformed),
        ..matched.clone()
    };
    assert_eq!(
        validated_native_transport_result(&profile, &dispatch.report, &dispatch, &malformed),
        Ok(native_uncertain_result())
    );

    let mut unrelated = native_report(2);
    unrelated[7] ^= 1;
    unrelated[88] = unrelated[2..88]
        .iter()
        .fold(0_u8, |value, byte| value ^ byte);
    let unrelated = NativeTransportOutcome {
        result: NativeTransportResult::MatchedResponse(unrelated),
        ..matched
    };
    assert_eq!(
        validated_native_transport_result(&profile, &dispatch.report, &dispatch, &unrelated),
        Ok(native_uncertain_result())
    );
}
