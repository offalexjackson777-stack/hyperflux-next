// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{ProfileBindingOutcome, RuntimeProfileAuthority, RuntimeProfileAuthorityError};
use hfx_core::{
    ChildIdentity, EndpointIdentity, LifecycleLimits, ObservationStamp, ProfileRegistry,
    ReceiverLifecycleMachine, ReceiverLifecycleRegistry,
};
use hfx_domain::{
    ConnectionMode, DeviceKind, EvidenceClaimId, EvidenceConfidence, GenerationId, LogicalDeviceId,
    MonotonicMs, ProductId, ResourceKind, RouteKind, SequenceNumber, VendorId,
};
use hfx_protocol::ResourceKey;

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity is canonical")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is canonical")
}

fn stamp(generation_id: u64, sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(generation_id),
        SequenceNumber::try_from(sequence).expect("test sequence is canonical"),
        MonotonicMs::try_from(sequence).expect("test time is canonical"),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{generation_id}-{sequence}")),
    )
    .expect("test observation is canonical")
}

fn lifecycle(route_kind: RouteKind) -> ReceiverLifecycleRegistry {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-a"), LifecycleLimits::default())
        .expect("lifecycle limits are valid");
    machine.discover(stamp(1, 1));
    register_child(
        &mut machine,
        "mouse",
        DeviceKind::Mouse,
        0x00cd,
        route_kind,
        2,
    );
    register_child(
        &mut machine,
        "keyboard",
        DeviceKind::Keyboard,
        0x0296,
        RouteKind::HyperfluxWireless,
        10,
    );
    register_child(
        &mut machine,
        "kind-mismatch",
        DeviceKind::Keyboard,
        0x00cd,
        RouteKind::HyperfluxWireless,
        20,
    );
    register_child(
        &mut machine,
        "unknown",
        DeviceKind::Unknown,
        0xffff,
        RouteKind::HyperfluxWireless,
        30,
    );
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver fits");
    receivers
}

fn register_child(
    machine: &mut ReceiverLifecycleMachine,
    device_id: &str,
    kind: DeviceKind,
    product_id: u16,
    route_kind: RouteKind,
    sequence: u64,
) {
    let logical_id: LogicalDeviceId = text(device_id);
    machine
        .register_device(
            ChildIdentity::new(
                logical_id.clone(),
                kind,
                ProductId::try_from(product_id).expect("product id is canonical"),
            )
            .expect("child identity is valid"),
            stamp(1, sequence),
        )
        .expect("child registration succeeds");
    machine
        .register_endpoint(
            &logical_id,
            EndpointIdentity::new(
                text(&format!("{device_id}-endpoint")),
                route_kind,
                if route_kind == RouteKind::HyperfluxWireless {
                    ConnectionMode::Hyperflux24ghz
                } else {
                    ConnectionMode::DirectUsb
                },
            )
            .expect("endpoint identity is valid"),
            stamp(1, sequence + 1),
        )
        .expect("endpoint registration succeeds");
}

fn resource(device_id: &str, kind: ResourceKind, generation_id: u64) -> ResourceKey {
    ResourceKey {
        receiver_id: text("receiver-a"),
        generation_id: generation(generation_id),
        device_id: text(device_id),
        kind,
    }
}

fn bind(authority: &mut RuntimeProfileAuthority, receiver_id: &str, generation_id: u64) {
    authority
        .bind_receiver(
            text(receiver_id),
            generation(generation_id),
            VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
            ProductId::try_from(0x00cf_u16).expect("product id is canonical"),
        )
        .expect("qualified receiver binds");
}

#[test]
fn exact_generation_composes_mouse_and_keyboard_independently() {
    let receivers = lifecycle(RouteKind::HyperfluxWireless);
    let mut authority = RuntimeProfileAuthority::load(4).expect("authority loads");
    bind(&mut authority, "receiver-a", 1);
    let view = authority.view(&receivers);

    let receiver = view
        .receiver_profile(&text("receiver-a"), generation(1))
        .expect("receiver profile is current");
    assert_eq!(
        receiver.profile_id.as_str(),
        "receiver.razer.hyperflux-v2.1532-00cf"
    );
    let mouse = view
        .device_profile(&resource("mouse", ResourceKind::Lighting, 1))
        .expect("mouse lighting is independently qualified");
    assert_eq!(mouse.application_slot_count.get(), 13);
    let keyboard = view
        .device_profile(&resource("keyboard", ResourceKind::Lighting, 1))
        .expect("keyboard lighting is independently qualified");
    assert_eq!(keyboard.application_slot_count.get(), 102);
}

#[test]
fn unknown_mismatched_incompatible_and_nonlighting_resources_get_no_authority() {
    let receivers = lifecycle(RouteKind::DirectUsb);
    let mut authority = RuntimeProfileAuthority::load(4).expect("authority loads");
    bind(&mut authority, "receiver-a", 1);
    let view = authority.view(&receivers);

    for resource in [
        resource("mouse", ResourceKind::Lighting, 1),
        resource("kind-mismatch", ResourceKind::Lighting, 1),
        resource("unknown", ResourceKind::Lighting, 1),
        resource("keyboard", ResourceKind::Settings, 1),
        resource("keyboard", ResourceKind::Pairing, 1),
        resource("keyboard", ResourceKind::Lighting, 2),
    ] {
        assert!(!view.supports(&resource));
        assert!(view.device_profile(&resource).is_none());
    }
    assert!(
        view.device_profile(&resource("keyboard", ResourceKind::Lighting, 1))
            .is_some()
    );
}

#[test]
fn binding_is_idempotent_bounded_and_strictly_generation_ordered() {
    let mut authority = RuntimeProfileAuthority::load(1).expect("authority loads");
    let vendor_id = VendorId::try_from(0x1532_u16).expect("vendor id is canonical");
    let product_id = ProductId::try_from(0x00cf_u16).expect("product id is canonical");
    let first = authority
        .bind_receiver(text("receiver-a"), generation(1), vendor_id, product_id)
        .expect("first generation binds");
    assert_eq!(first, ProfileBindingOutcome::Bound);
    let replay = authority
        .bind_receiver(text("receiver-a"), generation(1), vendor_id, product_id)
        .expect("exact replay is idempotent");
    assert_eq!(replay, ProfileBindingOutcome::Unchanged);
    assert_eq!(
        authority.bind_receiver(text("receiver-b"), generation(1), vendor_id, product_id),
        Err(RuntimeProfileAuthorityError::CapacityExhausted)
    );
    assert_eq!(
        authority
            .bind_receiver(text("receiver-a"), generation(2), vendor_id, product_id)
            .expect("newer generation replaces"),
        ProfileBindingOutcome::Replaced {
            previous_generation: generation(1)
        }
    );
    assert_eq!(
        authority.bind_receiver(text("receiver-a"), generation(1), vendor_id, product_id),
        Err(RuntimeProfileAuthorityError::StaleGeneration {
            receiver_id: text("receiver-a"),
            active_generation: generation(2),
            requested_generation: generation(1),
        })
    );
    assert!(!authority.retire(&text("receiver-a"), generation(1)));
    assert!(authority.retire(&text("receiver-a"), generation(2)));
}

#[test]
fn unsupported_receiver_never_consumes_binding_capacity() {
    let mut authority = RuntimeProfileAuthority::load(1).expect("authority loads");
    let error = authority.bind_receiver(
        text("unknown-receiver"),
        generation(1),
        VendorId::try_from(0xffff_u16).expect("vendor id is canonical"),
        ProductId::try_from(0xffff_u16).expect("product id is canonical"),
    );
    assert!(matches!(
        error,
        Err(RuntimeProfileAuthorityError::UnsupportedReceiver(_, _))
    ));
    bind(&mut authority, "receiver-a", 1);
}
