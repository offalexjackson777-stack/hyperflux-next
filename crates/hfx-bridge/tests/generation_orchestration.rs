// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    DisabledRestorationSource, GenerationActivationOutcome, GenerationOrchestrationError,
    GenerationOrchestrator, GenerationQualification, GenerationRestorationRuntime,
    ReceiverDisconnectCompletionOutcome, ReceiverDisconnectObservation, ReceiverDisconnectOutcome,
    ReceiverGenerationObservation, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationSnapshotSource, RuntimeProfileAuthority,
};
use hfx_core::{
    BoundedEventLog, ChildIdentity, EndpointIdentity, EventDelivery, EventSink, LeaseManager,
    LifecycleLimits, ObservationStamp, OutcomeLookup, PersistenceOperation, ProfileRegistry,
    ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverTransport, RestorationError,
    RestoreGenerationRetirement, SessionAuthority, SubmissionBinding, TransactionCoordinator,
    TransportDispatch, TransportFailure, TransportFailureFacts, TransportReceipt,
    TransportReconciliation,
};
use hfx_domain::{
    ApplyOutcome, AuthorizationEpoch, ColorChannel, ConnectionMode, DeliveredFrameCount,
    DeviceApplicationState, DeviceKind, DispatchNonce, EventKind, EvidenceClaimId,
    EvidenceConfidence, FrameIndex, GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs,
    ProductId, ProjectionRevision, QueueAdmission, ReceiverId, ReceiverLifecycleState,
    ResourceKind, RestoreState, RouteKind, RouteState, SequenceNumber, SideEffectCertainty,
    StreamEpoch, TransactionClass, TransactionId, TransactionState, VendorId,
};
use hfx_protocol::{
    BridgeEvent, DeviceProfileBinding, LeaseRequest, LeaseResult, LightingFrame, ResourceKey,
    RgbColor, TransactionRequest, TransactionResult,
};

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

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("time is canonical")
}

fn stamp(generation_id: u64, sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(generation_id),
        SequenceNumber::try_from(sequence).expect("sequence is canonical"),
        time(sequence),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{generation_id}-{sequence}")),
    )
    .expect("stamp is canonical")
}

fn resource(generation_id: u64) -> ResourceKey {
    ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: generation(generation_id),
        device_id: text("mouse"),
        kind: ResourceKind::Lighting,
    }
}

fn qualified_runtime() -> (ReceiverLifecycleRegistry, RuntimeProfileAuthority) {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle initializes");
    machine.discover(stamp(1, 1));
    let mouse_id: LogicalDeviceId = text("mouse");
    machine
        .register_device(
            ChildIdentity::new(
                mouse_id.clone(),
                DeviceKind::Mouse,
                ProductId::try_from(0x00cd_u16).expect("product id is canonical"),
            )
            .expect("mouse identity is canonical"),
            stamp(1, 2),
        )
        .expect("mouse registers");
    machine
        .register_endpoint(
            &mouse_id,
            EndpointIdentity::new(
                text("mouse-hyperflux"),
                RouteKind::HyperfluxWireless,
                ConnectionMode::Hyperflux24ghz,
            )
            .expect("endpoint is canonical"),
            stamp(1, 3),
        )
        .expect("endpoint registers");
    machine
        .observe_route(
            &mouse_id,
            &text("mouse-hyperflux"),
            RouteState::Available,
            stamp(1, 4),
        )
        .expect("route becomes available");

    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver registers");
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    profiles
        .bind_receiver(
            text("receiver-1"),
            generation(1),
            VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
            ProductId::try_from(0x00cf_u16).expect("product id is canonical"),
        )
        .expect("receiver profile binds");
    (receivers, profiles)
}

#[derive(Clone, Debug)]
struct TestTransport {
    receiver_id: ReceiverId,
    generation_id: Option<GenerationId>,
    dispatches: Vec<TransportDispatch>,
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
            device_application: DeviceApplicationState::Unverified,
        }
    }
}

impl ReceiverTransport for TestTransport {
    type Error = TestTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        (receiver_id == &self.receiver_id)
            .then_some(self.generation_id)
            .flatten()
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        TransportReconciliation::NotObserved
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.dispatches.push(dispatch.clone());
        panic!("generation orchestration must never dispatch transport")
    }
}

#[derive(Clone, Debug)]
struct TestSessions;

impl SessionAuthority for TestSessions {
    fn authorizes(
        &self,
        session_id: &hfx_domain::SessionId,
        authorization_epoch: AuthorizationEpoch,
    ) -> bool {
        session_id.as_str() == "session-1" && authorization_epoch.get() == 1
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

#[derive(Debug)]
struct FailingRestoration;

impl RestorationSnapshotSource for FailingRestoration {
    fn restoration(
        &self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        Ok(ReceiverRestorationSnapshot {
            stable_restore_enabled: true,
            restore_state: RestoreState::Idle,
        })
    }
}

impl GenerationRestorationRuntime for FailingRestoration {
    fn retire_generation<T, E>(
        &mut self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
        _now: MonotonicMs,
        _transport: &T,
        _leases: &mut LeaseManager,
        _transactions: &TransactionCoordinator,
        _events: &mut BoundedEventLog,
        _sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink,
    {
        Err(RestorationError::Persistence(
            PersistenceOperation::SaveRestore,
        ))
    }
}

fn event_log() -> BoundedEventLog {
    BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("stream epoch is canonical"),
        ProjectionRevision::try_from(1_u32).expect("revision is canonical"),
        16,
    )
    .expect("event bounds are valid")
}

fn queue_generation_one_transaction(
    receivers: &ReceiverLifecycleRegistry,
    profiles: &RuntimeProfileAuthority,
    leases: &LeaseManager,
    transport: &TestTransport,
    transactions: &mut TransactionCoordinator,
    events: &mut BoundedEventLog,
    sink: &mut TestSink,
) {
    let view = profiles.view(receivers);
    let receiver_profile = view
        .receiver_profile(&text("receiver-1"), generation(1))
        .expect("receiver is qualified");
    let mouse_profile = view
        .device_profile(&resource(1))
        .expect("mouse is qualified");
    let result = transactions
        .submit(
            TransactionRequest {
                request_id: text("transaction-request-1"),
                transaction_id: text("transaction-1"),
                client_id: text("client-1"),
                lease_id: text("lease-1"),
                receiver_id: text("receiver-1"),
                generation_id: generation(1),
                receiver_profile_id: receiver_profile.profile_id,
                receiver_profile_digest: receiver_profile.profile_digest,
                device_profiles: vec![DeviceProfileBinding {
                    device_id: text("mouse"),
                    profile_id: mouse_profile.profile_id,
                    profile_digest: mouse_profile.profile_digest,
                    application_slot_count: mouse_profile.application_slot_count,
                }],
                transaction_class: TransactionClass::StaticLighting,
                deadline_ms: time(1_000),
                resources: vec![resource(1)],
                frames: vec![LightingFrame {
                    device_id: text("mouse"),
                    frame_index: FrameIndex::try_from(0_u32).expect("frame index is canonical"),
                    colors: (0..13)
                        .map(|_| RgbColor {
                            red: ColorChannel::try_from(0_u8).expect("color is canonical"),
                            green: ColorChannel::try_from(0_u8).expect("color is canonical"),
                            blue: ColorChannel::try_from(255_u8).expect("color is canonical"),
                        })
                        .collect(),
                }],
            },
            SubmissionBinding {
                session_id: text("session-1"),
                authorization_epoch: AuthorizationEpoch::try_from(1_u64)
                    .expect("epoch is canonical"),
                dispatch_nonce: DispatchNonce::try_from(1_u64).expect("nonce is canonical"),
            },
            time(10),
            &TestSessions,
            leases,
            &view,
            &view,
            transport,
            events,
            sink,
        )
        .expect("transaction is admitted");
    assert!(matches!(
        result,
        hfx_core::SubmissionResult::Queued(ref progress)
            if progress.admission == QueueAdmission::Enqueued
    ));
}

struct GenerationHarness {
    receivers: ReceiverLifecycleRegistry,
    profiles: RuntimeProfileAuthority,
    leases: LeaseManager,
    transactions: TransactionCoordinator,
    events: BoundedEventLog,
    sink: TestSink,
    transport: TestTransport,
    restoration: DisabledRestorationSource,
}

fn queued_generation_one_harness() -> GenerationHarness {
    let (receivers, profiles) = qualified_runtime();
    let mut leases = LeaseManager::new(4, 8).expect("lease bounds are valid");
    let acquired = leases
        .acquire(
            LeaseRequest {
                request_id: text("lease-request-1"),
                client_id: text("client-1"),
                resources: vec![resource(1)],
                duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is canonical"),
            },
            text("lease-1"),
            time(0),
        )
        .expect("lease is granted");
    assert!(matches!(acquired, LeaseResult::Granted(_)));
    let mut transactions = TransactionCoordinator::new(8).expect("transaction bounds are valid");
    let mut events = event_log();
    let mut sink = TestSink::default();
    let transport = TestTransport {
        receiver_id: text("receiver-1"),
        generation_id: Some(generation(1)),
        dispatches: Vec::new(),
    };
    queue_generation_one_transaction(
        &receivers,
        &profiles,
        &leases,
        &transport,
        &mut transactions,
        &mut events,
        &mut sink,
    );
    GenerationHarness {
        receivers,
        profiles,
        leases,
        transactions,
        events,
        sink,
        transport,
        restoration: DisabledRestorationSource,
    }
}

fn generation_two_observation() -> ReceiverGenerationObservation {
    ReceiverGenerationObservation {
        receiver_id: text("receiver-1"),
        vendor_id: VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
        product_id: ProductId::try_from(0x00cf_u16).expect("product id is canonical"),
        stamp: stamp(2, 20),
    }
}

fn disconnect_observation(sequence: u64) -> ReceiverDisconnectObservation {
    ReceiverDisconnectObservation {
        receiver_id: text("receiver-1"),
        stamp: stamp(1, sequence),
    }
}

#[test]
fn transport_generation_mismatch_changes_nothing() {
    let mut harness = queued_generation_one_harness();
    assert!(matches!(
        GenerationOrchestrator::activate(
            generation_two_observation(),
            LifecycleLimits::default(),
            &harness.transport,
            &mut harness.restoration,
            &mut harness.receivers,
            &mut harness.profiles,
            &mut harness.leases,
            &mut harness.transactions,
            &mut harness.events,
            &mut harness.sink,
        ),
        Err(GenerationOrchestrationError::TransportGenerationMismatch { .. })
    ));
    assert_eq!(harness.transactions.queued_len(), 1);
    assert!(harness.leases.owns(
        &text("client-1"),
        &text("lease-1"),
        &[resource(1)],
        time(20)
    ));
    assert!(harness.sink.0.is_empty());
}

#[test]
fn restoration_failure_commits_no_staged_generation_replacement() {
    let mut harness = queued_generation_one_harness();
    harness.transport.generation_id = Some(generation(2));
    let events_before = harness.sink.0.len();
    let result = GenerationOrchestrator::activate(
        generation_two_observation(),
        LifecycleLimits::default(),
        &harness.transport,
        &mut FailingRestoration,
        &mut harness.receivers,
        &mut harness.profiles,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    );
    assert!(matches!(
        result,
        Err(GenerationOrchestrationError::Restoration(
            RestorationError::Persistence(PersistenceOperation::SaveRestore)
        ))
    ));
    assert_eq!(
        harness
            .receivers
            .get(&text("receiver-1"))
            .and_then(ReceiverLifecycleMachine::current)
            .map(hfx_core::ReceiverGenerationLifecycle::generation_id),
        Some(generation(1))
    );
    assert_eq!(
        harness
            .profiles
            .binding(&text("receiver-1"))
            .map(|binding| binding.generation_id),
        Some(generation(1))
    );
    assert_eq!(harness.transactions.queued_len(), 1);
    assert!(harness.leases.owns(
        &text("client-1"),
        &text("lease-1"),
        &[resource(1)],
        time(20)
    ));
    assert_eq!(harness.sink.0.len(), events_before);
}

#[test]
fn replacement_atomically_revokes_old_authority_and_publishes_one_transition() {
    let mut harness = queued_generation_one_harness();
    let observation = generation_two_observation();
    harness.transport.generation_id = Some(generation(2));
    let applied = GenerationOrchestrator::activate(
        observation.clone(),
        LifecycleLimits::default(),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.profiles,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    )
    .expect("replacement is atomically applied");
    let GenerationActivationOutcome::Applied(applied) = applied else {
        panic!("new generation must be applied");
    };
    assert_eq!(applied.previous_generation, Some(generation(1)));
    assert!(matches!(
        applied.qualification,
        GenerationQualification::Qualified(ref binding)
            if binding.generation_id == generation(2)
    ));
    assert_eq!(applied.revoked_leases, vec![text("lease-1")]);
    assert_eq!(
        applied.revoked_transactions,
        vec![text::<TransactionId>("transaction-1")]
    );
    assert_eq!(harness.transactions.queued_len(), 0);
    assert!(!harness.leases.owns(
        &text("client-1"),
        &text("lease-1"),
        &[resource(1)],
        time(20)
    ));
    assert!(matches!(
        harness
            .transactions
            .outcome(&text("client-1"), &text("transaction-1")),
        OutcomeLookup::Retained(TransactionResult::Terminal(terminal))
            if terminal.state == TransactionState::Revoked
    ));
    let current = harness
        .receivers
        .get(&text("receiver-1"))
        .and_then(ReceiverLifecycleMachine::current)
        .expect("new generation is current");
    assert_eq!(current.generation_id(), generation(2));
    assert_eq!(current.devices().len(), 0);
    assert!(
        harness
            .profiles
            .view(&harness.receivers)
            .receiver_profile(&text("receiver-1"), generation(1))
            .is_none()
    );
    assert!(
        harness
            .profiles
            .view(&harness.receivers)
            .receiver_profile(&text("receiver-1"), generation(2))
            .is_some()
    );
    assert!(harness.transport.dispatches.is_empty());
    assert_eq!(harness.sink.0.len(), 3);
    assert_eq!(harness.sink.0[0].kind, EventKind::TransactionCompleted);
    assert_eq!(harness.sink.0[1].kind, EventKind::OwnershipChanged);
    assert_eq!(harness.sink.0[2].kind, EventKind::GenerationReplaced);

    let replay = GenerationOrchestrator::activate(
        observation,
        LifecycleLimits::default(),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.profiles,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    )
    .expect("stale replay is a typed observation outcome");
    assert_eq!(
        replay,
        GenerationActivationOutcome::Ignored(ApplyOutcome::RejectedStaleGeneration)
    );
    assert_eq!(harness.sink.0.len(), 3);
}

#[test]
fn unknown_receiver_generation_remains_visible_without_write_qualification() {
    let mut receivers = ReceiverLifecycleRegistry::default();
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    let mut leases = LeaseManager::new(4, 8).expect("lease bounds are valid");
    let mut transactions = TransactionCoordinator::new(8).expect("transaction bounds are valid");
    let mut events = event_log();
    let mut sink = TestSink::default();
    let transport = TestTransport {
        receiver_id: text("receiver-unknown"),
        generation_id: Some(generation(1)),
        dispatches: Vec::new(),
    };

    let result = GenerationOrchestrator::activate(
        ReceiverGenerationObservation {
            receiver_id: text("receiver-unknown"),
            vendor_id: VendorId::try_from(0xffff_u16).expect("vendor id is canonical"),
            product_id: ProductId::try_from(0xffff_u16).expect("product id is canonical"),
            stamp: stamp(1, 1),
        },
        LifecycleLimits::default(),
        &transport,
        &mut DisabledRestorationSource,
        &mut receivers,
        &mut profiles,
        &mut leases,
        &mut transactions,
        &mut events,
        &mut sink,
    )
    .expect("unknown receiver remains observable");
    assert!(matches!(
        result,
        GenerationActivationOutcome::Applied(ref activation)
            if activation.qualification == GenerationQualification::Unqualified
    ));
    assert!(
        receivers
            .get(&text("receiver-unknown"))
            .and_then(ReceiverLifecycleMachine::current)
            .is_some()
    );
    assert!(profiles.binding(&text("receiver-unknown")).is_none());
    assert_eq!(sink.0.len(), 1);
    assert_eq!(sink.0[0].kind, EventKind::ReceiverAvailable);
}

#[test]
fn disconnect_revokes_once_retires_later_and_reconnects_as_a_new_generation() {
    let mut harness = queued_generation_one_harness();
    harness.transport.generation_id = None;
    let began = GenerationOrchestrator::begin_disconnect(
        disconnect_observation(30),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    )
    .expect("disconnect begins atomically");
    let ReceiverDisconnectOutcome::Applied(began) = began else {
        panic!("current generation disconnect must begin");
    };
    assert_eq!(began.revoked_leases, vec![text("lease-1")]);
    assert_eq!(
        began.revoked_transactions,
        vec![text::<TransactionId>("transaction-1")]
    );
    let current = harness
        .receivers
        .get(&text("receiver-1"))
        .and_then(ReceiverLifecycleMachine::current)
        .expect("disconnecting generation remains inspectable");
    assert_eq!(
        current.lifecycle().value(),
        ReceiverLifecycleState::Disconnecting
    );
    assert!(harness.profiles.binding(&text("receiver-1")).is_some());
    assert_eq!(harness.sink.0.len(), 3);
    assert_eq!(harness.sink.0[0].kind, EventKind::ReceiverUnavailable);
    assert_eq!(harness.sink.0[1].kind, EventKind::TransactionCompleted);
    assert_eq!(harness.sink.0[2].kind, EventKind::OwnershipChanged);

    let duplicate = GenerationOrchestrator::begin_disconnect(
        disconnect_observation(31),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    )
    .expect("duplicate disconnect is a typed no-op");
    assert_eq!(
        duplicate,
        ReceiverDisconnectOutcome::Ignored(ApplyOutcome::RejectedInvalidTransition)
    );
    assert_eq!(harness.sink.0.len(), 3);

    let completed = GenerationOrchestrator::complete_disconnect(
        disconnect_observation(40),
        &harness.transport,
        &mut harness.receivers,
        &mut harness.profiles,
    )
    .expect("disconnect completes atomically");
    assert!(matches!(
        completed,
        ReceiverDisconnectCompletionOutcome::Applied(ref result) if result.profile_retired
    ));
    assert!(
        harness
            .receivers
            .get(&text("receiver-1"))
            .and_then(ReceiverLifecycleMachine::current)
            .is_none()
    );
    assert!(harness.profiles.binding(&text("receiver-1")).is_none());

    harness.transport.generation_id = Some(generation(2));
    let reconnected = GenerationOrchestrator::activate(
        ReceiverGenerationObservation {
            receiver_id: text("receiver-1"),
            vendor_id: VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
            product_id: ProductId::try_from(0x00cf_u16).expect("product id is canonical"),
            stamp: stamp(2, 50),
        },
        LifecycleLimits::default(),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.profiles,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    )
    .expect("new transport generation reconnects");
    assert!(matches!(
        reconnected,
        GenerationActivationOutcome::Applied(ref activation)
            if activation.previous_generation == Some(generation(1))
    ));
    assert_eq!(harness.sink.0.len(), 5);
    assert_eq!(harness.sink.0[3].kind, EventKind::GenerationReplaced);
    assert_eq!(harness.sink.0[4].kind, EventKind::ReceiverAvailable);
    assert!(harness.transport.dispatches.is_empty());
}

#[test]
fn disconnect_observation_is_rejected_while_transport_still_reports_present() {
    let mut harness = queued_generation_one_harness();
    let result = GenerationOrchestrator::begin_disconnect(
        disconnect_observation(30),
        &harness.transport,
        &mut harness.restoration,
        &mut harness.receivers,
        &mut harness.leases,
        &mut harness.transactions,
        &mut harness.events,
        &mut harness.sink,
    );
    assert!(matches!(
        result,
        Err(GenerationOrchestrationError::TransportStillPresent { .. })
    ));
    assert_eq!(harness.transactions.queued_len(), 1);
    assert!(harness.leases.owns(
        &text("client-1"),
        &text("lease-1"),
        &[resource(1)],
        time(30)
    ));
    assert!(harness.sink.0.is_empty());
}
