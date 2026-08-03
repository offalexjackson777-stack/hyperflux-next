// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    LifecycleObservation, LifecycleObservationError, LifecycleObservationKind,
    LifecycleObservationOrchestrator, LifecycleObservationOutcome,
};
use hfx_core::{
    BoundedEventLog, ChildIdentity, EndpointIdentity, EventDelivery, EventSink, LifecycleLimits,
    ObservationStamp, ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverTransport,
    TransportDispatch, TransportFailure, TransportFailureFacts, TransportReceipt,
    TransportReconciliation,
};
use hfx_domain::{
    ActivityState, ApplyOutcome, BatteryPercent, ConnectionMode, ContactState, DeliveredFrameCount,
    DeviceKind, EventKind, EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId,
    LogicalDeviceId, MonotonicMs, PairingState, PowerState, ProductId, ProjectionRevision,
    ReceiverId, ReceiverLifecycleState, RouteKind, RouteState, SequenceNumber, SideEffectCertainty,
    SleepState, StreamEpoch,
};
use hfx_protocol::BridgeEvent;

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity is canonical")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("generation is canonical")
}

fn stamp(sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(1),
        SequenceNumber::try_from(sequence).expect("sequence is canonical"),
        MonotonicMs::try_from(sequence).expect("time is canonical"),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{sequence}")),
    )
    .expect("stamp is canonical")
}

fn registry() -> ReceiverLifecycleRegistry {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle initializes");
    assert_eq!(machine.discover(stamp(1)), ApplyOutcome::Applied);
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver registers");
    receivers
}

fn event_log() -> BoundedEventLog {
    BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("stream epoch is canonical"),
        ProjectionRevision::try_from(1_u32).expect("revision is canonical"),
        32,
    )
    .expect("event log is bounded")
}

#[derive(Clone, Debug)]
struct TestTransport {
    generation_id: Option<GenerationId>,
}

#[derive(Clone, Copy, Debug)]
struct TestTransportError;

impl TransportFailure for TestTransportError {
    fn facts(&self) -> TransportFailureFacts {
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("zero is canonical"),
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: hfx_domain::DeviceApplicationState::Unverified,
        }
    }
}

impl ReceiverTransport for TestTransport {
    type Error = TestTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        (receiver_id.as_str() == "receiver-1")
            .then_some(self.generation_id)
            .flatten()
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        TransportReconciliation::NotObserved
    }

    fn dispatch(&mut self, _dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        panic!("passive observation must never dispatch transport")
    }
}

#[derive(Clone, Debug, Default)]
struct TestSink(Vec<BridgeEvent>);

impl EventSink for TestSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.0.push(event.clone());
        EventDelivery::Accepted
    }
}

struct ObservationHarness {
    receivers: ReceiverLifecycleRegistry,
    events: BoundedEventLog,
    sink: TestSink,
}

impl ObservationHarness {
    fn new() -> Self {
        Self {
            receivers: registry(),
            events: event_log(),
            sink: TestSink::default(),
        }
    }

    fn apply(
        &mut self,
        sequence: u64,
        kind: LifecycleObservationKind,
    ) -> LifecycleObservationOutcome {
        LifecycleObservationOrchestrator::apply(
            LifecycleObservation {
                receiver_id: text("receiver-1"),
                stamp: stamp(sequence),
                kind,
            },
            &TestTransport {
                generation_id: Some(generation(1)),
            },
            &mut self.receivers,
            &mut self.events,
            &mut self.sink,
        )
        .expect("observation applies without partial failure")
    }
}

fn register_mouse(
    harness: &mut ObservationHarness,
    mouse: &LogicalDeviceId,
    endpoint: &hfx_domain::EndpointId,
) {
    let registered = harness.apply(
        2,
        LifecycleObservationKind::RegisterDevice(
            ChildIdentity::new(
                mouse.clone(),
                DeviceKind::Mouse,
                ProductId::try_from(0x00cd_u16).expect("product id is canonical"),
            )
            .expect("child identity is canonical"),
        ),
    );
    assert!(matches!(
        registered,
        LifecycleObservationOutcome::Applied(ref applied)
            if applied.events == vec![EventKind::DeviceUnknown]
    ));
    assert!(matches!(
        harness.apply(
            3,
            LifecycleObservationKind::RegisterEndpoint {
                device_id: mouse.clone(),
                identity: EndpointIdentity::new(
                    endpoint.clone(),
                    RouteKind::HyperfluxWireless,
                    ConnectionMode::Hyperflux24ghz,
                )
                .expect("endpoint identity is canonical"),
            },
        ),
        LifecycleObservationOutcome::Applied(ref applied) if applied.events.is_empty()
    ));
}

fn observe_mouse_presence(
    harness: &mut ObservationHarness,
    mouse: &LogicalDeviceId,
    endpoint: &hfx_domain::EndpointId,
) {
    for (sequence, kind) in [
        (
            4,
            LifecycleObservationKind::Pairing {
                device_id: mouse.clone(),
                value: PairingState::Paired,
            },
        ),
        (
            5,
            LifecycleObservationKind::Route {
                device_id: mouse.clone(),
                endpoint_id: endpoint.clone(),
                value: RouteState::Available,
            },
        ),
        (
            6,
            LifecycleObservationKind::Sleep {
                device_id: mouse.clone(),
                endpoint_id: endpoint.clone(),
                value: SleepState::Asleep,
            },
        ),
        (
            7,
            LifecycleObservationKind::Activity {
                device_id: mouse.clone(),
                endpoint_id: endpoint.clone(),
                value: ActivityState::Active,
            },
        ),
        (
            8,
            LifecycleObservationKind::Power {
                device_id: mouse.clone(),
                endpoint_id: endpoint.clone(),
                value: PowerState::Off,
            },
        ),
        (
            9,
            LifecycleObservationKind::Freshness {
                device_id: mouse.clone(),
                endpoint_id: endpoint.clone(),
                value: FreshnessState::Stale,
            },
        ),
    ] {
        let _ = harness.apply(sequence, kind);
    }
    let contact = harness.apply(
        10,
        LifecycleObservationKind::Contact {
            device_id: mouse.clone(),
            endpoint_id: endpoint.clone(),
            value: ContactState::OnMat,
        },
    );
    assert!(matches!(
        contact,
        LifecycleObservationOutcome::Applied(ref applied) if applied.events.is_empty()
    ));
}

fn observe_mouse_battery(harness: &mut ObservationHarness, mouse: &LogicalDeviceId) {
    let _ = harness.apply(
        11,
        LifecycleObservationKind::BatteryReported {
            device_id: mouse.clone(),
            percentage: BatteryPercent::try_from(0_u8).expect("zero is canonical"),
        },
    );
    let _ = harness.apply(
        12,
        LifecycleObservationKind::BatteryUnavailable {
            device_id: mouse.clone(),
        },
    );
    let _ = harness.apply(
        13,
        LifecycleObservationKind::BatteryStale {
            device_id: mouse.clone(),
        },
    );
}

#[test]
fn child_facts_emit_only_truthful_presence_and_battery_transitions() {
    let mut harness = ObservationHarness::new();
    let mouse: LogicalDeviceId = text("mouse");
    let endpoint: hfx_domain::EndpointId = text("mouse-hyperflux");

    register_mouse(&mut harness, &mouse, &endpoint);
    observe_mouse_presence(&mut harness, &mouse, &endpoint);
    observe_mouse_battery(&mut harness, &mouse);

    assert_eq!(
        harness
            .sink
            .0
            .iter()
            .map(|event| event.kind)
            .collect::<Vec<_>>(),
        vec![
            EventKind::DeviceUnknown,
            EventKind::DeviceAvailable,
            EventKind::DeviceSleeping,
            EventKind::DeviceAvailable,
            EventKind::DeviceUnavailable,
            EventKind::DeviceUnknown,
            EventKind::BatteryUpdated,
            EventKind::BatteryUpdated,
            EventKind::BatteryUpdated,
        ]
    );
}

#[test]
fn receiver_suspend_resume_is_typed_and_disconnect_cannot_bypass_revocation() {
    let mut harness = ObservationHarness::new();
    let suspended = harness.apply(
        2,
        LifecycleObservationKind::ReceiverState(ReceiverLifecycleState::Suspended),
    );
    assert!(matches!(
        suspended,
        LifecycleObservationOutcome::Applied(ref applied)
            if applied.events == vec![EventKind::ReceiverSuspended]
    ));
    let resumed = harness.apply(
        3,
        LifecycleObservationKind::ReceiverState(ReceiverLifecycleState::Active),
    );
    assert!(matches!(
        resumed,
        LifecycleObservationOutcome::Applied(ref applied)
            if applied.events == vec![EventKind::ReceiverAvailable]
    ));

    let restricted = LifecycleObservationOrchestrator::apply(
        LifecycleObservation {
            receiver_id: text("receiver-1"),
            stamp: stamp(4),
            kind: LifecycleObservationKind::ReceiverState(ReceiverLifecycleState::Disconnecting),
        },
        &TestTransport {
            generation_id: Some(generation(1)),
        },
        &mut harness.receivers,
        &mut harness.events,
        &mut harness.sink,
    );
    assert_eq!(
        restricted,
        Err(LifecycleObservationError::RestrictedReceiverTransition(
            ReceiverLifecycleState::Disconnecting
        ))
    );
    assert_eq!(harness.sink.0.len(), 2);
    assert_eq!(
        harness
            .receivers
            .get(&text("receiver-1"))
            .and_then(ReceiverLifecycleMachine::current)
            .expect("receiver remains active")
            .lifecycle()
            .value(),
        ReceiverLifecycleState::Active
    );
}

#[test]
fn mismatched_generation_and_identity_conflict_change_nothing() {
    let mut harness = ObservationHarness::new();
    let mismatched = LifecycleObservationOrchestrator::apply(
        LifecycleObservation {
            receiver_id: text("receiver-1"),
            stamp: stamp(2),
            kind: LifecycleObservationKind::RegisterDevice(
                ChildIdentity::new(
                    text("mouse"),
                    DeviceKind::Mouse,
                    ProductId::try_from(0x00cd_u16).expect("product id is canonical"),
                )
                .expect("child identity is canonical"),
            ),
        },
        &TestTransport {
            generation_id: Some(generation(2)),
        },
        &mut harness.receivers,
        &mut harness.events,
        &mut harness.sink,
    );
    assert!(matches!(
        mismatched,
        Err(LifecycleObservationError::TransportGenerationMismatch { .. })
    ));
    assert!(
        harness
            .receivers
            .get(&text("receiver-1"))
            .and_then(ReceiverLifecycleMachine::current)
            .expect("generation remains active")
            .devices()
            .next()
            .is_none()
    );
    assert!(harness.sink.0.is_empty());

    let _ = harness.apply(
        2,
        LifecycleObservationKind::RegisterDevice(
            ChildIdentity::new(
                text("mouse"),
                DeviceKind::Mouse,
                ProductId::try_from(0x00cd_u16).expect("product id is canonical"),
            )
            .expect("child identity is canonical"),
        ),
    );
    let conflict = LifecycleObservationOrchestrator::apply(
        LifecycleObservation {
            receiver_id: text("receiver-1"),
            stamp: stamp(3),
            kind: LifecycleObservationKind::RegisterDevice(
                ChildIdentity::new(
                    text("mouse"),
                    DeviceKind::Keyboard,
                    ProductId::try_from(0x0296_u16).expect("product id is canonical"),
                )
                .expect("child identity is canonical"),
            ),
        },
        &TestTransport {
            generation_id: Some(generation(1)),
        },
        &mut harness.receivers,
        &mut harness.events,
        &mut harness.sink,
    );
    assert!(matches!(
        conflict,
        Err(LifecycleObservationError::Lifecycle(
            hfx_core::LifecycleError::DeviceIdentityConflict(_)
        ))
    ));
    let device = harness
        .receivers
        .get(&text("receiver-1"))
        .and_then(ReceiverLifecycleMachine::current)
        .and_then(|generation| generation.device(&text("mouse")))
        .expect("original child remains");
    assert_eq!(device.identity().device_kind(), DeviceKind::Mouse);
    assert_eq!(harness.sink.0.len(), 1);
}
