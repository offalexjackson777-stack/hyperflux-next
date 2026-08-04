// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{RuntimeProfileAuthority, project_native_routes};
use hfx_core::{
    ChildIdentity, EndpointIdentity, LifecycleLimits, ObservationStamp, ReceiverLifecycleMachine,
    ReceiverLifecycleRegistry,
};
use hfx_domain::{
    ConnectionMode, DeviceKind, EndpointId, EvidenceClaimId, EvidenceConfidence, GenerationId,
    LogicalDeviceId, MonotonicMs, NativeRouteAccess, NativeRouteState, ProductId, RouteKind,
    RouteState, SequenceNumber, SleepState, VendorId,
};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test value is valid")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("generation is valid")
}

fn stamp(sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(1),
        SequenceNumber::try_from(sequence).expect("sequence is valid"),
        MonotonicMs::try_from(sequence).expect("timestamp is valid"),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{sequence}")),
    )
    .expect("stamp is valid")
}

fn register_child(
    machine: &mut ReceiverLifecycleMachine,
    id: &str,
    kind: DeviceKind,
    product_id: u16,
    route: RouteKind,
    sequence: u64,
    route_state: Option<RouteState>,
) {
    let device_id: LogicalDeviceId = text(id);
    machine
        .register_device(
            ChildIdentity::new(
                device_id.clone(),
                kind,
                ProductId::try_from(product_id).expect("product is valid"),
            )
            .expect("identity is valid"),
            stamp(sequence),
        )
        .expect("device registers");
    let endpoint_id: EndpointId = text(&format!("{id}-endpoint"));
    machine
        .register_endpoint(
            &device_id,
            EndpointIdentity::new(
                endpoint_id.clone(),
                route,
                match route {
                    RouteKind::HyperfluxWireless => ConnectionMode::Hyperflux24ghz,
                    RouteKind::DirectUsb => ConnectionMode::DirectUsb,
                    RouteKind::Bluetooth => ConnectionMode::Bluetooth,
                },
            )
            .expect("endpoint is valid"),
            stamp(sequence + 1),
        )
        .expect("endpoint registers");
    if let Some(route_state) = route_state {
        machine
            .observe_route(&device_id, &endpoint_id, route_state, stamp(sequence + 2))
            .expect("route state is observed");
    } else {
        machine
            .observe_sleep(
                &device_id,
                &endpoint_id,
                SleepState::Asleep,
                stamp(sequence + 2),
            )
            .expect("sleep is observed");
    }
}

fn universality_receiver_registry() -> ReceiverLifecycleRegistry {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle starts");
    machine.discover(stamp(1));
    for (id, kind, product_id, sequence, route_state) in [
        (
            "known-mouse",
            DeviceKind::Mouse,
            0x00cd,
            2,
            Some(RouteState::Available),
        ),
        (
            "automatic-original-basilisk",
            DeviceKind::Mouse,
            0x00ab,
            6,
            Some(RouteState::Available),
        ),
        ("unknown-keyboard", DeviceKind::Keyboard, 0x7777, 10, None),
        (
            "kind-mismatch",
            DeviceKind::Keyboard,
            0x00ab,
            13,
            Some(RouteState::Available),
        ),
        (
            "known-unavailable",
            DeviceKind::Mouse,
            0x00cd,
            16,
            Some(RouteState::Unavailable),
        ),
        (
            "backend-required",
            DeviceKind::Mouse,
            0x00db,
            19,
            Some(RouteState::Available),
        ),
        (
            "nonofficial-upstream",
            DeviceKind::Mouse,
            0x003f,
            22,
            Some(RouteState::Available),
        ),
    ] {
        register_child(
            &mut machine,
            id,
            kind,
            product_id,
            RouteKind::HyperfluxWireless,
            sequence,
            route_state,
        );
    }
    register_child(
        &mut machine,
        "direct-only",
        DeviceKind::Mouse,
        0x00cd,
        RouteKind::DirectUsb,
        25,
        Some(RouteState::Available),
    );
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver fits");
    receivers
}

fn bound_receiver_profiles() -> RuntimeProfileAuthority {
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    profiles
        .bind_receiver(
            text("receiver-1"),
            generation(1),
            VendorId::try_from(0x1532_u16).expect("vendor is valid"),
            ProductId::try_from(0x00cf_u16).expect("receiver product is valid"),
        )
        .expect("receiver binds");
    profiles
}

#[test]
fn receiver_native_backend_uses_exact_source_validated_identity_without_hardware_promotion() {
    let receivers = universality_receiver_registry();
    let profiles = bound_receiver_profiles();

    let source_validated = hfx_integration_model::SourceValidatedDeviceCatalog::load()
        .expect("source-validated catalog loads");
    let routes =
        project_native_routes(&receivers, &profiles, &source_validated).expect("routes project");
    assert_eq!(routes.len(), 7);
    let route = |id: &str| {
        routes
            .iter()
            .find(|route| route.device_id.as_str() == id)
            .expect("route exists")
    };
    let known = route("known-mouse");
    assert_eq!(known.route_state, NativeRouteState::Available);
    assert_eq!(known.access, NativeRouteAccess::Qualified);
    assert_eq!(known.vendor_id.get(), 0x1532);
    assert_eq!(known.product_id.get(), 0x00cd);
    assert_eq!(known.protocol_family.as_str(), "razer-feature-report-v1");
    assert_eq!(known.report_bytes.get(), 90);
    assert!(known.route_token.is_none());
    assert!(known.resource.is_none());

    let unavailable = route("known-unavailable");
    assert_eq!(
        unavailable.route_state,
        NativeRouteState::TemporarilyUnavailable
    );
    assert_eq!(unavailable.access, NativeRouteAccess::Qualified);

    let automatic = route("automatic-original-basilisk");
    assert_eq!(automatic.product_id.get(), 0x00ab);
    assert_eq!(automatic.access, NativeRouteAccess::Qualified);

    let unknown = route("unknown-keyboard");
    assert_eq!(unknown.product_id.get(), 0x7777);
    assert_eq!(
        unknown.route_state,
        NativeRouteState::TemporarilyUnavailable
    );
    assert_eq!(unknown.access, NativeRouteAccess::Candidate);
    assert_eq!(route("kind-mismatch").access, NativeRouteAccess::Candidate);
    assert_eq!(
        route("nonofficial-upstream").access,
        NativeRouteAccess::Candidate
    );
    assert_eq!(
        route("backend-required").access,
        NativeRouteAccess::BackendRequired
    );
}
