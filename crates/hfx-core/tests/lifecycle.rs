// SPDX-License-Identifier: GPL-2.0-only

#[allow(dead_code)]
#[path = "../src/lifecycle.rs"]
mod lifecycle;

use hfx_domain::{
    ActivityState, ApplyOutcome, ConnectionMode, ContactState, DeviceKind, EndpointId,
    EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId, LogicalDeviceId,
    MonotonicMs, PairingState, PowerState, PresenceState, ProductId, ReceiverLifecycleState,
    RouteKind, RouteState, SequenceNumber, SleepState,
};
use lifecycle::{
    ChildIdentity, EndpointIdentity, LifecycleError, LifecycleLimits, ObservationStamp,
    ReceiverLifecycleMachine,
};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identifier is canonical")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is canonical")
}

fn stamp(generation_id: u64, sequence: u64, observed_at_ms: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(generation_id),
        SequenceNumber::try_from(sequence).expect("test sequence is canonical"),
        MonotonicMs::try_from(observed_at_ms).expect("test time is canonical"),
        EvidenceConfidence::Observed,
        text(&format!(
            "claim-{generation_id}-{sequence}-{observed_at_ms}"
        )),
    )
    .expect("test stamp has explicit evidence")
}

fn child(id: &str, kind: DeviceKind, product_id: u16) -> ChildIdentity {
    ChildIdentity::new(
        text(id),
        kind,
        ProductId::try_from(product_id).expect("test product id is canonical"),
    )
    .expect("test child kind is valid")
}

fn endpoint(id: &str, kind: RouteKind) -> EndpointIdentity {
    let mode = match kind {
        RouteKind::HyperfluxWireless => ConnectionMode::Hyperflux24ghz,
        RouteKind::DirectUsb => ConnectionMode::DirectUsb,
        RouteKind::Bluetooth => ConnectionMode::Bluetooth,
    };
    EndpointIdentity::new(text(id), kind, mode).expect("test endpoint is consistent")
}

fn machine_with_limits(max_devices: usize, max_endpoints: usize) -> ReceiverLifecycleMachine {
    ReceiverLifecycleMachine::new(
        text("receiver-1"),
        LifecycleLimits::new(max_devices, max_endpoints).expect("test limits are valid"),
    )
    .expect("machine construction succeeds")
}

fn discovered_machine() -> ReceiverLifecycleMachine {
    let mut machine = machine_with_limits(8, 4);
    assert_eq!(machine.discover(stamp(1, 1, 1)), ApplyOutcome::Applied);
    machine
}

fn register_wireless_child(
    machine: &mut ReceiverLifecycleMachine,
    id: &str,
    kind: DeviceKind,
    base_sequence: u64,
) -> (LogicalDeviceId, EndpointId) {
    let device_id = text(id);
    let endpoint_id = text(&format!("{id}-wireless"));
    machine
        .register_device(
            child(
                id,
                kind,
                if kind == DeviceKind::Mouse {
                    0x00cd
                } else {
                    0x0296
                },
            ),
            stamp(1, base_sequence, base_sequence),
        )
        .expect("child registration is structurally valid");
    assert_eq!(
        machine.observe_pairing(
            &device_id,
            PairingState::Paired,
            stamp(1, base_sequence + 1, base_sequence + 1),
        ),
        ApplyOutcome::Applied
    );
    machine
        .register_endpoint(
            &device_id,
            endpoint(&format!("{id}-wireless"), RouteKind::HyperfluxWireless),
            stamp(1, base_sequence + 2, base_sequence + 2),
        )
        .expect("endpoint registration is structurally valid");
    (device_id, endpoint_id)
}

#[test]
fn retired_generations_never_reactivate_or_retain_children() {
    let mut machine = discovered_machine();
    let (mouse_id, mouse_endpoint) =
        register_wireless_child(&mut machine, "mouse-1", DeviceKind::Mouse, 2);
    assert_eq!(
        machine
            .current()
            .expect("generation is current")
            .devices()
            .len(),
        1
    );
    machine
        .observe_route(
            &mouse_id,
            &mouse_endpoint,
            RouteState::Available,
            stamp(1, 50, 50),
        )
        .expect("new child evidence succeeds");
    assert_eq!(
        machine.replace_generation(stamp(2, 20, 20)),
        ApplyOutcome::IgnoredOlderObservation
    );
    assert_eq!(
        machine
            .current()
            .expect("delayed replacement cannot retire the generation")
            .generation_id(),
        generation(1)
    );

    assert_eq!(
        machine.replace_generation(stamp(2, 60, 60)),
        ApplyOutcome::Applied
    );
    let replacement = machine.current().expect("replacement is current");
    assert_eq!(replacement.generation_id(), generation(2));
    assert_eq!(replacement.devices().len(), 0);
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Suspended, stamp(1, 61, 61)),
        ApplyOutcome::RejectedStaleGeneration
    );
    assert_eq!(
        machine.discover(stamp(1, 62, 62)),
        ApplyOutcome::RejectedStaleGeneration
    );
    assert_eq!(
        machine.observe_pairing(&mouse_id, PairingState::Unpaired, stamp(1, 63, 63)),
        ApplyOutcome::RejectedStaleGeneration
    );

    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Disconnecting, stamp(2, 64, 64)),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine.complete_disconnect(stamp(2, 65, 65)),
        ApplyOutcome::Applied
    );
    assert!(machine.current().is_none());
    assert_eq!(
        machine.discover(stamp(2, 66, 66)),
        ApplyOutcome::RejectedStaleGeneration
    );
    assert_eq!(machine.discover(stamp(3, 67, 67)), ApplyOutcome::Applied);
    assert_eq!(machine.highest_generation(), Some(generation(3)));
}

#[test]
fn receiver_transitions_are_explicit_ordered_and_fail_closed() {
    let mut machine = discovered_machine();
    assert_eq!(
        machine.complete_disconnect(stamp(1, 2, 2)),
        ApplyOutcome::RejectedInvalidTransition
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Suspended, stamp(1, 3, 3)),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Active, stamp(1, 2, 2)),
        ApplyOutcome::IgnoredOlderObservation
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::PartiallySuspended, stamp(1, 4, 4)),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Active, stamp(1, 5, 5)),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Unknown, stamp(1, 6, 6)),
        ApplyOutcome::RejectedInvalidTransition
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Disconnecting, stamp(1, 7, 7)),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine.transition_receiver(ReceiverLifecycleState::Active, stamp(1, 8, 8)),
        ApplyOutcome::RejectedInvalidTransition
    );
    assert_eq!(
        machine
            .current()
            .expect("generation remains present")
            .lifecycle()
            .value(),
        ReceiverLifecycleState::Disconnecting
    );
}

#[test]
fn device_facts_remain_separate_while_presence_is_derived() {
    let mut machine = discovered_machine();
    let (mouse_id, endpoint_id) =
        register_wireless_child(&mut machine, "mouse-1", DeviceKind::Mouse, 2);
    assert_eq!(
        machine
            .observe_route(
                &mouse_id,
                &endpoint_id,
                RouteState::Available,
                stamp(1, 5, 5),
            )
            .expect("route observation succeeds"),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&mouse_id)
            .expect("mouse exists")
            .presence(),
        PresenceState::Available
    );

    machine
        .observe_sleep(&mouse_id, &endpoint_id, SleepState::Asleep, stamp(1, 6, 6))
        .expect("sleep observation succeeds");
    machine
        .observe_activity(&mouse_id, &endpoint_id, ActivityState::Idle, stamp(1, 7, 7))
        .expect("idle observation succeeds");
    machine
        .observe_contact(
            &mouse_id,
            &endpoint_id,
            ContactState::OffMat,
            stamp(1, 8, 8),
        )
        .expect("mouse contact observation succeeds");
    let mouse = machine
        .current()
        .expect("receiver exists")
        .device(&mouse_id)
        .expect("mouse exists");
    assert_eq!(mouse.presence(), PresenceState::Sleeping);
    assert_eq!(mouse.pairing().value(), PairingState::Paired);
    let route = mouse.endpoint(&endpoint_id).expect("wireless route exists");
    assert_eq!(route.route().value(), RouteState::Available);
    assert_eq!(route.sleep().value(), SleepState::Asleep);
    assert_eq!(route.activity().value(), ActivityState::Idle);
    assert_eq!(route.contact().value(), ContactState::OffMat);
}

#[test]
fn newer_power_wake_and_freshness_evidence_rederive_presence() {
    let mut machine = discovered_machine();
    let (mouse_id, endpoint_id) =
        register_wireless_child(&mut machine, "mouse-1", DeviceKind::Mouse, 2);
    machine
        .observe_route(
            &mouse_id,
            &endpoint_id,
            RouteState::Available,
            stamp(1, 5, 5),
        )
        .expect("route observation succeeds");
    machine
        .observe_power(&mouse_id, &endpoint_id, PowerState::Off, stamp(1, 9, 9))
        .expect("power-off observation succeeds");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&mouse_id)
            .expect("mouse exists")
            .presence(),
        PresenceState::Unavailable
    );
    machine
        .observe_power(&mouse_id, &endpoint_id, PowerState::On, stamp(1, 10, 10))
        .expect("power-on observation succeeds");
    machine
        .observe_sleep(&mouse_id, &endpoint_id, SleepState::Awake, stamp(1, 11, 11))
        .expect("wake observation succeeds");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&mouse_id)
            .expect("mouse exists")
            .presence(),
        PresenceState::Available
    );

    machine
        .observe_freshness(
            &mouse_id,
            &endpoint_id,
            FreshnessState::Stale,
            stamp(1, 12, 12),
        )
        .expect("stale projection observation succeeds");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&mouse_id)
            .expect("mouse exists")
            .presence(),
        PresenceState::Unknown
    );
    machine
        .observe_route(
            &mouse_id,
            &endpoint_id,
            RouteState::Available,
            stamp(1, 13, 13),
        )
        .expect("new route evidence supersedes stale projection");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&mouse_id)
            .expect("mouse exists")
            .presence(),
        PresenceState::Available
    );
}

#[test]
fn delayed_observations_cannot_overwrite_newer_device_evidence() {
    let mut machine = discovered_machine();
    let (mouse_id, mouse_endpoint) =
        register_wireless_child(&mut machine, "mouse-1", DeviceKind::Mouse, 2);
    let (keyboard_id, keyboard_endpoint) =
        register_wireless_child(&mut machine, "keyboard-1", DeviceKind::Keyboard, 2);

    assert_eq!(
        machine
            .observe_route(
                &mouse_id,
                &mouse_endpoint,
                RouteState::Available,
                stamp(1, 10, 100),
            )
            .expect("new route observation succeeds"),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine
            .observe_route(
                &mouse_id,
                &mouse_endpoint,
                RouteState::Unavailable,
                stamp(1, 9, 99),
            )
            .expect("older route is a typed no-op"),
        ApplyOutcome::IgnoredOlderObservation
    );
    assert_eq!(
        machine
            .observe_route(
                &mouse_id,
                &mouse_endpoint,
                RouteState::Unavailable,
                stamp(1, 11, 90),
            )
            .expect("time-regressing route is a typed no-op"),
        ApplyOutcome::IgnoredOlderObservation
    );
    assert_eq!(
        machine.observe_pairing(&mouse_id, PairingState::Unpaired, stamp(1, 9, 99)),
        ApplyOutcome::IgnoredOlderObservation
    );
    let mouse = machine
        .current()
        .expect("receiver exists")
        .device(&mouse_id)
        .expect("mouse exists");
    assert_eq!(
        mouse
            .endpoint(&mouse_endpoint)
            .expect("mouse route exists")
            .route()
            .value(),
        RouteState::Available
    );
    assert_eq!(mouse.pairing().value(), PairingState::Paired);

    let shared_report = stamp(1, 5, 5);
    assert_eq!(
        machine
            .observe_route(
                &keyboard_id,
                &keyboard_endpoint,
                RouteState::Available,
                shared_report.clone(),
            )
            .expect("independent keyboard accepts its own current evidence"),
        ApplyOutcome::Applied
    );
    assert_eq!(
        machine
            .observe_power(
                &keyboard_id,
                &keyboard_endpoint,
                PowerState::On,
                shared_report,
            )
            .expect("one report may establish multiple independent facts"),
        ApplyOutcome::Applied
    );
    let keyboard_route = machine
        .current()
        .expect("receiver exists")
        .device(&keyboard_id)
        .expect("keyboard exists")
        .endpoint(&keyboard_endpoint)
        .expect("keyboard route exists");
    assert_eq!(keyboard_route.route().value(), RouteState::Available);
    assert_eq!(keyboard_route.power().value(), PowerState::On);
}

#[test]
fn mouse_keyboard_and_unknown_children_evolve_independently() {
    let mut machine = discovered_machine();
    let (mouse_id, mouse_endpoint) =
        register_wireless_child(&mut machine, "mouse-1", DeviceKind::Mouse, 2);
    let (keyboard_id, keyboard_endpoint) =
        register_wireless_child(&mut machine, "keyboard-1", DeviceKind::Keyboard, 2);
    let (unknown_id, unknown_endpoint) =
        register_wireless_child(&mut machine, "unknown-1", DeviceKind::Unknown, 2);

    machine
        .observe_route(
            &mouse_id,
            &mouse_endpoint,
            RouteState::Available,
            stamp(1, 5, 5),
        )
        .expect("mouse route succeeds");
    machine
        .observe_route(
            &keyboard_id,
            &keyboard_endpoint,
            RouteState::Available,
            stamp(1, 5, 5),
        )
        .expect("keyboard route succeeds");
    machine
        .observe_sleep(
            &keyboard_id,
            &keyboard_endpoint,
            SleepState::Asleep,
            stamp(1, 6, 6),
        )
        .expect("keyboard sleep succeeds");
    machine
        .observe_route(
            &unknown_id,
            &unknown_endpoint,
            RouteState::Unavailable,
            stamp(1, 5, 5),
        )
        .expect("unknown child route succeeds");

    let current = machine.current().expect("receiver exists");
    assert_eq!(current.devices().len(), 3);
    assert_eq!(
        current.device(&mouse_id).expect("mouse exists").presence(),
        PresenceState::Available
    );
    assert_eq!(
        current
            .device(&keyboard_id)
            .expect("keyboard exists")
            .presence(),
        PresenceState::Sleeping
    );
    assert_eq!(
        current
            .device(&unknown_id)
            .expect("unknown child exists")
            .presence(),
        PresenceState::Unavailable
    );
    assert_eq!(
        current
            .device(&unknown_id)
            .expect("unknown child exists")
            .identity()
            .device_kind(),
        DeviceKind::Unknown
    );

    for kind in [DeviceKind::Mouse, DeviceKind::Keyboard] {
        let mut single = discovered_machine();
        let id = if kind == DeviceKind::Mouse {
            "only-mouse"
        } else {
            "only-keyboard"
        };
        let (device_id, endpoint_id) = register_wireless_child(&mut single, id, kind, 2);
        single
            .observe_route(
                &device_id,
                &endpoint_id,
                RouteState::Available,
                stamp(1, 5, 5),
            )
            .expect("single-child route succeeds");
        assert_eq!(
            single
                .current()
                .expect("receiver exists")
                .device(&device_id)
                .expect("single child exists")
                .presence(),
            PresenceState::Available
        );
    }
}

#[test]
fn multiple_routes_do_not_collapse_pairing_into_presence() {
    let mut machine = discovered_machine();
    let keyboard_id: LogicalDeviceId = text("keyboard-1");
    let wireless_id: EndpointId = text("keyboard-wireless");
    let usb_id: EndpointId = text("keyboard-usb");
    machine
        .register_device(
            child("keyboard-1", DeviceKind::Keyboard, 0x0296),
            stamp(1, 2, 2),
        )
        .expect("keyboard registration succeeds");
    assert_eq!(
        machine.observe_pairing(&keyboard_id, PairingState::Unpaired, stamp(1, 3, 3)),
        ApplyOutcome::Applied
    );
    machine
        .register_endpoint(
            &keyboard_id,
            endpoint("keyboard-wireless", RouteKind::HyperfluxWireless),
            stamp(1, 4, 4),
        )
        .expect("wireless endpoint succeeds");
    machine
        .observe_route(
            &keyboard_id,
            &wireless_id,
            RouteState::Unavailable,
            stamp(1, 5, 5),
        )
        .expect("wireless route loss succeeds");
    machine
        .register_endpoint(
            &keyboard_id,
            endpoint("keyboard-usb", RouteKind::DirectUsb),
            stamp(1, 6, 6),
        )
        .expect("USB endpoint succeeds");
    machine
        .observe_route(&keyboard_id, &usb_id, RouteState::Available, stamp(1, 7, 7))
        .expect("USB route succeeds");

    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&keyboard_id)
            .expect("keyboard exists")
            .presence(),
        PresenceState::Available
    );
    machine
        .observe_sleep(&keyboard_id, &usb_id, SleepState::Asleep, stamp(1, 8, 8))
        .expect("USB sleep succeeds");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&keyboard_id)
            .expect("keyboard exists")
            .presence(),
        PresenceState::Sleeping
    );
    machine
        .observe_activity(&keyboard_id, &usb_id, ActivityState::Active, stamp(1, 9, 9))
        .expect("new USB activity succeeds");
    assert_eq!(
        machine
            .current()
            .expect("receiver exists")
            .device(&keyboard_id)
            .expect("keyboard exists")
            .presence(),
        PresenceState::Available
    );
}

#[test]
fn capacity_and_identity_failures_leave_existing_state_intact() {
    assert_eq!(
        LifecycleLimits::new(0, 1),
        Err(LifecycleError::InvalidLimits)
    );
    let mut machine = machine_with_limits(2, 1);
    assert_eq!(machine.discover(stamp(1, 1, 1)), ApplyOutcome::Applied);
    let mouse = child("mouse-1", DeviceKind::Mouse, 0x00cd);
    let mouse_id = mouse.device_id().clone();
    machine
        .register_device(mouse, stamp(1, 2, 2))
        .expect("first device fits");
    machine
        .register_device(
            child("keyboard-1", DeviceKind::Keyboard, 0x0296),
            stamp(1, 2, 2),
        )
        .expect("second device fits");
    assert_eq!(
        machine.register_device(
            child("unknown-1", DeviceKind::Unknown, 0xffff),
            stamp(1, 2, 2),
        ),
        Err(LifecycleError::DeviceCapacity)
    );
    assert_eq!(
        machine.register_device(
            child("mouse-1", DeviceKind::Keyboard, 0x0296),
            stamp(1, 3, 3),
        ),
        Err(LifecycleError::DeviceIdentityConflict(mouse_id.clone()))
    );

    machine
        .register_endpoint(
            &mouse_id,
            endpoint("mouse-wireless", RouteKind::HyperfluxWireless),
            stamp(1, 4, 4),
        )
        .expect("first endpoint fits");
    assert_eq!(
        machine.register_endpoint(
            &mouse_id,
            endpoint("mouse-usb", RouteKind::DirectUsb),
            stamp(1, 5, 5),
        ),
        Err(LifecycleError::EndpointCapacity(mouse_id.clone()))
    );
    let current = machine.current().expect("receiver exists");
    assert_eq!(current.devices().len(), 2);
    assert_eq!(
        current
            .device(&mouse_id)
            .expect("mouse exists")
            .endpoints()
            .len(),
        1
    );
}

#[test]
fn contact_semantics_and_observation_provenance_are_fail_closed() {
    assert_eq!(
        ObservationStamp::new(
            generation(1),
            SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            MonotonicMs::try_from(1_u64).expect("time is valid"),
            EvidenceConfidence::Unknown,
            text::<EvidenceClaimId>("unknown-confidence"),
        ),
        Err(LifecycleError::UnknownEvidenceConfidence)
    );
    assert!(matches!(
        ChildIdentity::new(
            text("not-a-child"),
            DeviceKind::Receiver,
            ProductId::try_from(0x00cf_u16).expect("product id is valid"),
        ),
        Err(LifecycleError::InvalidChildKind(DeviceKind::Receiver))
    ));
    assert!(matches!(
        EndpointIdentity::new(
            text("bad-route"),
            RouteKind::DirectUsb,
            ConnectionMode::Bluetooth,
        ),
        Err(LifecycleError::InvalidEndpointMode { .. })
    ));

    let mut machine = discovered_machine();
    let (keyboard_id, keyboard_endpoint) =
        register_wireless_child(&mut machine, "keyboard-1", DeviceKind::Keyboard, 2);
    assert_eq!(
        machine
            .observe_contact(
                &keyboard_id,
                &keyboard_endpoint,
                ContactState::OnMat,
                stamp(1, 5, 5),
            )
            .expect("invalid semantics are a typed outcome"),
        ApplyOutcome::RejectedInvalidTransition
    );
    let contact = machine
        .current()
        .expect("receiver exists")
        .device(&keyboard_id)
        .expect("keyboard exists")
        .endpoint(&keyboard_endpoint)
        .expect("keyboard route exists")
        .contact();
    assert_eq!(contact.value(), ContactState::NotApplicable);
    assert!(!contact.is_observed());

    machine.transition_receiver(ReceiverLifecycleState::Disconnecting, stamp(1, 6, 6));
    assert_eq!(
        machine
            .observe_route(
                &keyboard_id,
                &keyboard_endpoint,
                RouteState::Available,
                stamp(1, 7, 7),
            )
            .expect("disconnecting receiver is a typed outcome"),
        ApplyOutcome::RejectedReceiverAbsent
    );
}
