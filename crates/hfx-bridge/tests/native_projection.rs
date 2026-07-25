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

#[test]
fn receiver_native_backend_projects_known_and_unknown_wireless_children_as_candidates() {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle starts");
    machine.discover(stamp(1));
    register_child(
        &mut machine,
        "known-mouse",
        DeviceKind::Mouse,
        0x00cd,
        RouteKind::HyperfluxWireless,
        2,
        Some(RouteState::Available),
    );
    register_child(
        &mut machine,
        "unknown-keyboard",
        DeviceKind::Keyboard,
        0x7777,
        RouteKind::HyperfluxWireless,
        10,
        None,
    );
    register_child(
        &mut machine,
        "known-unavailable",
        DeviceKind::Mouse,
        0x00cd,
        RouteKind::HyperfluxWireless,
        16,
        Some(RouteState::Unavailable),
    );
    register_child(
        &mut machine,
        "direct-only",
        DeviceKind::Mouse,
        0x00cd,
        RouteKind::DirectUsb,
        20,
        Some(RouteState::Available),
    );
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver fits");
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    profiles
        .bind_receiver(
            text("receiver-1"),
            generation(1),
            VendorId::try_from(0x1532_u16).expect("vendor is valid"),
            ProductId::try_from(0x00cf_u16).expect("receiver product is valid"),
        )
        .expect("receiver binds");

    let routes = project_native_routes(&receivers, &profiles).expect("routes project");
    assert_eq!(routes.len(), 3);
    assert_eq!(routes[0].device_id.as_str(), "known-mouse");
    assert_eq!(routes[0].route_state, NativeRouteState::Available);
    assert_eq!(routes[0].access, NativeRouteAccess::Candidate);
    assert_eq!(routes[0].vendor_id.get(), 0x1532);
    assert_eq!(routes[0].product_id.get(), 0x00cd);
    assert_eq!(
        routes[0].protocol_family.as_str(),
        "razer-feature-report-v1"
    );
    assert_eq!(routes[0].report_bytes.get(), 90);
    assert!(routes[0].route_token.is_none());
    assert!(routes[0].resource.is_none());

    assert_eq!(routes[1].device_id.as_str(), "known-unavailable");
    assert_eq!(
        routes[1].route_state,
        NativeRouteState::TemporarilyUnavailable
    );
    assert_eq!(routes[1].access, NativeRouteAccess::Candidate);

    assert_eq!(routes[2].device_id.as_str(), "unknown-keyboard");
    assert_eq!(routes[2].product_id.get(), 0x7777);
    assert_eq!(
        routes[2].route_state,
        NativeRouteState::TemporarilyUnavailable
    );
    assert_eq!(routes[2].access, NativeRouteAccess::Candidate);
}
