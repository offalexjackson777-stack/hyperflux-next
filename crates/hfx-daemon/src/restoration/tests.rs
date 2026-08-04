// SPDX-License-Identifier: GPL-2.0-only

use super::schedule::deferred_due;
use super::*;
use crate::ProductionRestoration;
use hfx_bridge::{
    AtomicFileCommitter, CoreBridgeBackend, CoreBridgeConfig, DesiredEffectReplayStatus,
    DurableRestorationRuntime, FilePersistenceConfig, FilePersistenceError, FilePersistenceStore,
    PersistenceCommitter, RestorationRuntime, RuntimeProfileAuthority, SessionIdentityError,
    SessionIdentitySource, SessionRegistry,
};
use hfx_core::{
    ChildIdentity, Clock, CompletedTransaction, EndpointIdentity, EventDelivery, EventSink,
    LifecycleLimits, ObservationStamp, PersistenceStore, ProfileRegistry, ReceiverLifecycleMachine,
    ReceiverLifecycleRegistry, ReceiverTransport, RestorationCoordinator, RestoreAdvanceResult,
    RestoreCompletion, RestoreDeferred, RestorePlanResult, RestoreRecord, RestoreRecordStatus,
    StableCommitOutcome, TransportDispatch, TransportFailure, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal, WallClock,
    canonical_request_digest,
};
use hfx_domain::{
    ActivityState, ApplyOutcome, AuthorizationEpoch, ColorChannel, ConnectionMode,
    DeliveredFrameCount, DeviceApplicationState, DeviceKind, EvidenceClaimId, EvidenceConfidence,
    FrameCount, FrameIndex, FreshnessState, IntentRevision, LogicalDeviceId, PairingState,
    PersistenceRevision, PersistenceSchemaVersion, PowerState, ProductId, ProjectionRevision,
    ProtocolVersion, QueueCapacity, ResourceKind, RestoreAttemptNumber, RestoreDeferReason,
    RouteKind, RouteState, SessionId, SideEffectCertainty, SleepState, StableLightingMode,
    StreamEpoch, StreamId, TransactionClass, TransactionState, VendorId, WallClockUnixMs,
};
use hfx_protocol::{
    DeviceProfileBinding, LightingFrame, ResourceKey, RgbColor, StableLightingIntent,
    TransactionRequest, TransactionTerminal,
};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct FakeController {
    now: MonotonicMs,
    plans: Vec<Result<RestorePlanResult, RestorationError>>,
    pending: Vec<RestoreRecord>,
    advances: Vec<Result<RestoreAdvanceResult, RestorationError>>,
    dispatches: Vec<Result<RestoreRecord, RestorationError>>,
    policy_calls: usize,
    advance_calls: usize,
    dispatch_calls: usize,
}

impl RestorationController for FakeController {
    fn now(&self) -> MonotonicMs {
        self.now
    }

    fn set_enabled(
        &mut self,
        _receiver_id: &ReceiverId,
        _enabled: bool,
    ) -> Result<(), RestorationError> {
        self.policy_calls += 1;
        Ok(())
    }

    fn pending_records(
        &self,
        _receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        Ok(self.pending.clone())
    }

    fn plan_restore(
        &mut self,
        _trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        let result = self.plans.remove(0);
        if let Ok(RestorePlanResult::Planned(records)) = &result {
            for record in records {
                self.pending.retain(|pending| {
                    pending.device_id != record.device_id || pending.claim_id == record.claim_id
                });
                if !self
                    .pending
                    .iter()
                    .any(|pending| pending.claim_id == record.claim_id)
                {
                    self.pending.push(record.clone());
                }
            }
        }
        result
    }

    fn advance_claim(
        &mut self,
        _claim_id: &RestoreClaimId,
        _session: &AuthorizedSession,
        _sessions: &SessionRegistry,
        _lease_duration_ms: LeaseDurationMs,
        _authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        self.advance_calls += 1;
        self.advances.remove(0)
    }

    fn dispatch_claim(
        &mut self,
        _claim_id: &RestoreClaimId,
        _sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError> {
        self.dispatch_calls += 1;
        self.dispatches.remove(0)
    }
}

struct MigratingController {
    inner: FakeController,
    migrations: Vec<Result<usize, RestorationError>>,
}

struct EffectController {
    inner: FakeController,
    replays: Vec<DesiredEffectReplayStatus>,
}

impl RestorationController for EffectController {
    fn now(&self) -> MonotonicMs {
        self.inner.now()
    }

    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        self.inner.set_enabled(receiver_id, enabled)
    }

    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        self.inner.pending_records(receiver_id)
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        self.inner.plan_restore(trigger)
    }

    fn queue_desired_effect_replay(
        &mut self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
        _device_id: &LogicalDeviceId,
        _sessions: &SessionRegistry,
        _authority_window_ms: u64,
    ) -> Result<DesiredEffectReplayStatus, RestorationScheduleError> {
        Ok(self.replays.remove(0))
    }

    fn advance_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        session: &AuthorizedSession,
        sessions: &SessionRegistry,
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        self.inner.advance_claim(
            claim_id,
            session,
            sessions,
            lease_duration_ms,
            authority_window_ms,
        )
    }

    fn dispatch_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError> {
        self.inner.dispatch_claim(claim_id, sessions)
    }
}

impl RestorationController for MigratingController {
    fn now(&self) -> MonotonicMs {
        self.inner.now()
    }

    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        self.inner.set_enabled(receiver_id, enabled)
    }

    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        self.inner.pending_records(receiver_id)
    }

    fn rebind_compatible_stable_intents(
        &mut self,
        _trigger: &RestoreTrigger,
    ) -> Result<usize, RestorationError> {
        self.migrations.remove(0)
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        self.inner.plan_restore(trigger)
    }

    fn advance_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        session: &AuthorizedSession,
        sessions: &SessionRegistry,
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        self.inner.advance_claim(
            claim_id,
            session,
            sessions,
            lease_duration_ms,
            authority_window_ms,
        )
    }

    fn dispatch_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError> {
        self.inner.dispatch_claim(claim_id, sessions)
    }
}

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test id")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("generation")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("time")
}

fn stamp(sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(1),
        SequenceNumber::try_from(sequence).expect("sequence"),
        time(sequence),
        EvidenceConfidence::Observed,
        id::<EvidenceClaimId>(&format!("evidence-{sequence}")),
    )
    .expect("observation stamp")
}

fn record(claim: &str, status: RestoreRecordStatus) -> RestoreRecord {
    record_for_device(claim, "mouse-1", status)
}

fn record_for_device(claim: &str, device_id: &str, status: RestoreRecordStatus) -> RestoreRecord {
    RestoreRecord {
        schema_version: PersistenceSchemaVersion::try_from(1_u16).expect("schema"),
        claim_id: id(claim),
        trigger_id: id("trigger-1"),
        trigger_kind: RestoreTriggerKind::ServiceStart,
        receiver_id: id("receiver-1"),
        generation_id: generation(1),
        device_id: id(device_id),
        intent_revision: IntentRevision::try_from(1_u64).expect("intent revision"),
        intent_digest: id(&"11".repeat(32)),
        revision: PersistenceRevision::try_from(1_u64).expect("revision"),
        last_attempt: None,
        status,
    }
}

fn delivered_record(claim: &str, device_id: &str) -> RestoreRecord {
    record_for_device(
        claim,
        device_id,
        RestoreRecordStatus::Succeeded(RestoreCompletion {
            attempt_number: RestoreAttemptNumber::try_from(1_u32).expect("attempt"),
            transaction_id: id(&format!("transaction-{claim}")),
            request_digest: id(&"22".repeat(32)),
            state: TransactionState::Succeeded,
            delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("frame count"),
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry: false,
            device_application: DeviceApplicationState::Unverified,
            error_kind: None,
        }),
    )
}

fn session() -> AuthorizedSession {
    AuthorizedSession {
        client_id: id("bridge-restoration"),
        selected_version: ProtocolVersion::try_from(5_u16).expect("protocol"),
        session_id: SessionId::try_from("restore-session-test").expect("session"),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("epoch"),
    }
}

fn scheduler() -> RestorationScheduler {
    RestorationScheduler::new(
        true,
        Some(session()),
        [0x55; 32],
        LeaseDurationMs::try_from(10_000_u32).expect("lease"),
        30_000,
        1_000,
        4,
        8,
        16,
    )
    .expect("scheduler")
}

fn epoch_scheduler(max_triggers: usize) -> RestorationScheduler {
    RestorationScheduler::new(
        true,
        Some(session()),
        [0x55; 32],
        LeaseDurationMs::try_from(10_000_u32).expect("lease"),
        30_000,
        1_000,
        4,
        max_triggers,
        max_triggers.max(32),
    )
    .expect("epoch scheduler")
}

const RETURN_RETRIES: &[u64] = &[
    0, 100, 200, 300, 500, 750, 1_000, 1_500, 2_250, 3_250, 4_500, 6_000,
];

fn sessions() -> SessionRegistry {
    let mut sessions =
        SessionRegistry::new(hfx_domain::QueueCapacity::try_from(2_u16).expect("capacity"));
    sessions.register(session()).expect("session registers");
    sessions
}

#[derive(Clone, Copy, Debug)]
struct TestClock(MonotonicMs);

impl Clock for TestClock {
    fn now(&self) -> MonotonicMs {
        self.0
    }
}

#[derive(Clone, Copy, Debug)]
struct TestWallClock(WallClockUnixMs);

impl WallClock for TestWallClock {
    fn now_unix_ms(&self) -> WallClockUnixMs {
        self.0
    }
}

#[derive(Clone, Debug)]
struct TestTransport {
    dispatches: Vec<TransportDispatch>,
}

#[derive(Clone, Copy, Debug)]
struct TestTransportError;

impl TransportFailure for TestTransportError {
    fn facts(&self) -> TransportFailureFacts {
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("zero frames"),
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
        (receiver_id == &id::<ReceiverId>("receiver-1")).then_some(generation(1))
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        TransportReconciliation::NotObserved
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.dispatches.push(dispatch.clone());
        Ok(TransportReceipt {
            terminal: TransportTerminal::Delivered,
            delivered_frames: DeliveredFrameCount::try_from(
                u16::try_from(dispatch.frames.len()).expect("frame count fits"),
            )
            .expect("delivered frames"),
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry_safe: false,
            device_application: DeviceApplicationState::Confirmed,
        })
    }
}

#[derive(Debug, Default)]
struct TestEventSink(Vec<hfx_protocol::BridgeEvent>);

impl EventSink for TestEventSink {
    fn try_emit(&mut self, event: &hfx_protocol::BridgeEvent) -> EventDelivery {
        self.0.push(event.clone());
        EventDelivery::Accepted
    }
}

#[derive(Debug)]
struct DeterministicIdentities(u8);

impl SessionIdentitySource for DeterministicIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.0;
            self.0 = self.0.wrapping_add(1);
        }
        Ok(())
    }
}

fn runtime_state() -> (ReceiverLifecycleRegistry, RuntimeProfileAuthority) {
    let mut machine = ReceiverLifecycleMachine::new(id("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle initializes");
    machine.discover(stamp(1));
    let device_id: LogicalDeviceId = id("mouse-1");
    machine
        .register_device(
            ChildIdentity::new(
                device_id.clone(),
                DeviceKind::Mouse,
                ProductId::try_from(0x00cd_u16).expect("product id"),
            )
            .expect("mouse identity"),
            stamp(2),
        )
        .expect("mouse registers");
    assert_eq!(
        machine.observe_pairing(&device_id, PairingState::Paired, stamp(3)),
        ApplyOutcome::Applied
    );
    machine
        .register_endpoint(
            &device_id,
            EndpointIdentity::new(
                id("mouse-hyperflux"),
                RouteKind::HyperfluxWireless,
                ConnectionMode::Hyperflux24ghz,
            )
            .expect("endpoint identity"),
            stamp(4),
        )
        .expect("endpoint registers");
    machine
        .observe_route(
            &device_id,
            &id("mouse-hyperflux"),
            RouteState::Available,
            stamp(5),
        )
        .expect("route becomes available");
    machine
        .observe_power(&device_id, &id("mouse-hyperflux"), PowerState::On, stamp(6))
        .expect("power observation");
    machine
        .observe_sleep(
            &device_id,
            &id("mouse-hyperflux"),
            SleepState::Awake,
            stamp(7),
        )
        .expect("sleep observation");
    machine
        .observe_activity(
            &device_id,
            &id("mouse-hyperflux"),
            ActivityState::Active,
            stamp(8),
        )
        .expect("activity observation");
    machine
        .observe_freshness(
            &device_id,
            &id("mouse-hyperflux"),
            FreshnessState::Fresh,
            stamp(9),
        )
        .expect("freshness observation");
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver registers");
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    profiles
        .bind_receiver(
            id("receiver-1"),
            generation(1),
            VendorId::try_from(0x1532_u16).expect("vendor id"),
            ProductId::try_from(0x00cf_u16).expect("receiver product id"),
        )
        .expect("receiver profile binds");
    (receivers, profiles)
}

fn stable_completion(
    receivers: &ReceiverLifecycleRegistry,
    profiles: &RuntimeProfileAuthority,
) -> CompletedTransaction {
    let resource = ResourceKey {
        receiver_id: id("receiver-1"),
        generation_id: generation(1),
        device_id: id("mouse-1"),
        kind: ResourceKind::Lighting,
    };
    let view = profiles.view(receivers);
    let receiver = view
        .receiver_profile(&resource.receiver_id, resource.generation_id)
        .expect("receiver profile");
    let device = view.device_profile(&resource).expect("device profile");
    let request = TransactionRequest {
        request_id: id("stable-request-1"),
        transaction_id: id("stable-transaction-1"),
        client_id: id("client-1"),
        lease_id: id("lease-1"),
        receiver_id: resource.receiver_id.clone(),
        generation_id: resource.generation_id,
        receiver_profile_id: receiver.profile_id,
        receiver_profile_digest: receiver.profile_digest,
        device_profiles: vec![DeviceProfileBinding {
            device_id: resource.device_id.clone(),
            profile_id: device.profile_id,
            profile_digest: device.profile_digest,
            application_slot_count: device.application_slot_count,
        }],
        transaction_class: TransactionClass::StaticLighting,
        stable_intents: vec![StableLightingIntent {
            device_id: resource.device_id.clone(),
            mode: StableLightingMode::Static,
        }],
        deadline_ms: time(1_000),
        resources: vec![resource],
        frames: vec![LightingFrame {
            device_id: id("mouse-1"),
            frame_index: FrameIndex::try_from(0_u32).expect("frame index"),
            colors: (0..device.application_slot_count.get())
                .map(|_| RgbColor {
                    red: ColorChannel::try_from(0_u8).expect("red"),
                    green: ColorChannel::try_from(0_u8).expect("green"),
                    blue: ColorChannel::try_from(255_u8).expect("blue"),
                })
                .collect(),
        }],
    };
    let terminal = TransactionTerminal {
        request_id: request.request_id.clone(),
        request_digest: canonical_request_digest(&request).expect("request digest"),
        transaction_id: request.transaction_id.clone(),
        receiver_id: request.receiver_id.clone(),
        generation_id: request.generation_id,
        state: TransactionState::Succeeded,
        declared_frames: FrameCount::try_from(1_u16).expect("declared frames"),
        delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("delivered frames"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Confirmed,
        terminal_sequence: SequenceNumber::try_from(1_u64).expect("terminal sequence"),
        error_kind: None,
        superseded_by: None,
    };
    CompletedTransaction { request, terminal }
}

fn temporary_directory() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "hfx-restoration-integration-{}-{nonce}",
        std::process::id()
    ))
}

type DurableTestBackend<R> =
    CoreBridgeBackend<TestClock, TestWallClock, TestTransport, R, TestEventSink>;

fn compose_backend<R: RestorationRuntime>(
    restoration: R,
    identity_seed: u8,
) -> DurableTestBackend<R> {
    let capacity = QueueCapacity::try_from(16_u16).expect("capacity");
    let (receivers, profiles) = runtime_state();
    CoreBridgeBackend::new(
        CoreBridgeConfig {
            lifecycle_limits: LifecycleLimits::default(),
            lease_capacity: capacity,
            lease_history_capacity: capacity,
            transaction_capacity: capacity,
            event_capacity: capacity,
            diagnostic_capacity: capacity,
            subscription_capacity: capacity,
            stream_id: id::<StreamId>("stream-1"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("projection revision"),
        },
        TestClock(time(100)),
        TestWallClock(WallClockUnixMs::try_from(20_u64).expect("wall clock")),
        TestTransport {
            dispatches: Vec::new(),
        },
        restoration,
        &mut DeterministicIdentities(identity_seed),
        receivers,
        profiles,
        TestEventSink::default(),
    )
    .expect("backend composes")
}

fn durable_with_stable<S: PersistenceStore>(store: S) -> DurableRestorationRuntime<S> {
    let (receivers, profiles) = runtime_state();
    let completion = stable_completion(&receivers, &profiles);
    let mut restoration = DurableRestorationRuntime::new(store);
    assert!(matches!(
        restoration
            .capture_completed(
                &completion,
                WallClockUnixMs::try_from(10_u64).expect("capture time")
            )
            .expect("stable intent captures"),
        StableCommitOutcome::Captured(ref intents) if intents.len() == 1
    ));
    restoration
}

fn plan_durable_restore<R: RestorationRuntime>(
    backend: &mut DurableTestBackend<R>,
) -> RestoreClaimId {
    backend
        .set_restoration_enabled(&id("receiver-1"), true)
        .expect("restoration enables");
    let trigger = RestoreTrigger {
        trigger_id: id("restore-trigger-crash-recovery"),
        kind: RestoreTriggerKind::ServiceStart,
        receiver_id: id("receiver-1"),
        generation_id: generation(1),
        target_device_id: None,
    };
    let RestorePlanResult::Planned(records) =
        backend.plan_restoration(&trigger).expect("restore plans")
    else {
        panic!("stable state must produce one restore claim")
    };
    assert_eq!(records.len(), 1);
    records[0].claim_id.clone()
}

fn advance_durable_restore<R: RestorationRuntime>(
    backend: &mut DurableTestBackend<R>,
    claim_id: &RestoreClaimId,
) -> Result<RestoreAdvanceResult, RestorationError> {
    backend.advance_restoration(
        claim_id,
        &session(),
        &sessions(),
        LeaseDurationMs::try_from(10_000_u32).expect("lease"),
        30_000,
    )
}

fn recover_file_checkpoint(state_path: &Path) {
    let store = FilePersistenceStore::open(FilePersistenceConfig::new(state_path))
        .expect("checkpoint reopens");
    let mut backend = compose_backend(DurableRestorationRuntime::new(store), 1);
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("recovery trigger schedules");
    let report = scheduler
        .tick_with(&mut backend, &sessions())
        .expect("checkpoint recovers");
    assert_eq!(report.claims_recovered, 1);
    assert_eq!(report.claims_dispatched, 1);
    assert_eq!(report.claims_terminal, 1);
    assert_eq!(backend.transport().dispatches.len(), 1);
    assert!(
        backend
            .pending_restoration_records(&id("receiver-1"))
            .expect("pending records load")
            .is_empty()
    );
    drop(backend);

    let reopened = FilePersistenceStore::open(FilePersistenceConfig::new(state_path))
        .expect("terminal state reopens");
    let records = reopened
        .restore_records(&id("receiver-1"))
        .expect("terminal record reloads");
    assert_eq!(records.len(), 1);
    assert!(matches!(
        records[0].status,
        RestoreRecordStatus::Succeeded(_)
    ));
}

#[derive(Debug)]
struct FailNthCommitter {
    inner: AtomicFileCommitter,
    call: usize,
    fail_on: usize,
}

impl FailNthCommitter {
    fn new(fail_on: usize) -> Self {
        Self {
            inner: AtomicFileCommitter::default(),
            call: 0,
            fail_on,
        }
    }
}

impl PersistenceCommitter for FailNthCommitter {
    fn commit(&mut self, path: &Path, bytes: &[u8]) -> Result<(), FilePersistenceError> {
        self.call += 1;
        if self.call == self.fail_on {
            Err(FilePersistenceError::Capacity)
        } else {
            self.inner.commit(path, bytes)
        }
    }
}

#[test]
fn one_trigger_plans_and_dispatches_one_claim_on_the_actor_tick() {
    let planned = record("claim-1", RestoreRecordStatus::Planned);
    let terminal = record(
        "claim-1",
        RestoreRecordStatus::Invalidated(hfx_core::RestoreInvalidation {
            reason: hfx_domain::RestoreInvalidationReason::IntentChanged,
        }),
    );
    let mut controller = FakeController {
        now: MonotonicMs::try_from(10_u64).expect("time"),
        plans: vec![Ok(RestorePlanResult::Planned(vec![planned.clone()]))],
        pending: Vec::new(),
        advances: vec![Ok(RestoreAdvanceResult::Queued(planned))],
        dispatches: vec![Ok(terminal)],
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("trigger schedules");
    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("tick succeeds");
    assert_eq!(report.triggers_planned, 1);
    assert_eq!(report.claims_dispatched, 1);
    assert_eq!(report.claims_terminal, 1);
    assert_eq!(controller.policy_calls, 1);
    assert_eq!(controller.advance_calls, 1);
    assert_eq!(controller.dispatch_calls, 1);
}

#[test]
fn pending_claim_is_recovered_before_a_fresh_trigger_is_planned() {
    let prior = record("claim-prior", RestoreRecordStatus::Planned);
    let terminal = RestoreAdvanceResult::Terminal(record(
        "claim-prior",
        RestoreRecordStatus::Invalidated(hfx_core::RestoreInvalidation {
            reason: hfx_domain::RestoreInvalidationReason::StaleGeneration,
        }),
    ));
    let mut controller = FakeController {
        now: MonotonicMs::try_from(10_u64).expect("time"),
        plans: vec![Err(RestorationError::PriorClaimUnresolved)],
        pending: vec![prior],
        advances: vec![Ok(terminal)],
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("trigger schedules");
    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("barrier is reconciled");
    assert_eq!(report.claims_recovered, 1);
    assert_eq!(report.claims_terminal, 1);
    assert!(scheduler.triggers.is_empty());
    assert_eq!(controller.plans.len(), 1);
}

#[test]
fn compatible_rebind_replans_instead_of_recovering_a_stale_deferred_claim() {
    let prior = record(
        "claim-prior",
        RestoreRecordStatus::Deferred(RestoreDeferred {
            reason: RestoreDeferReason::DeviceSleeping,
            prior_outcome: None,
        }),
    );
    let current = record("claim-current", RestoreRecordStatus::Planned);
    let mut controller = MigratingController {
        inner: FakeController {
            now: time(10),
            plans: vec![Ok(RestorePlanResult::Planned(vec![current.clone()]))],
            pending: vec![prior.clone()],
            advances: Vec::new(),
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        },
        migrations: vec![Ok(1)],
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("service trigger schedules");
    let mut report = RestorationTickReport::default();

    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(10), &mut report)
        .expect("compatible stable state replans");

    assert_eq!(report.triggers_planned, 1);
    assert_eq!(report.claims_recovered, 0);
    assert!(controller.inner.plans.is_empty());
    assert!(scheduler.claims.contains_key(&current.claim_id));
    assert!(!scheduler.claims.contains_key(&prior.claim_id));
}

#[test]
fn child_identity_arriving_after_service_start_retries_compatible_rebind() {
    let prior = record(
        "claim-prior",
        RestoreRecordStatus::Deferred(RestoreDeferred {
            reason: RestoreDeferReason::DeviceUnknown,
            prior_outcome: None,
        }),
    );
    let current = record("claim-current", RestoreRecordStatus::Planned);
    let mut controller = MigratingController {
        inner: FakeController {
            now: time(10),
            plans: vec![Ok(RestorePlanResult::Planned(vec![current.clone()]))],
            pending: vec![prior.clone()],
            advances: Vec::new(),
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        },
        migrations: vec![Ok(0), Ok(1)],
    };
    let receiver_id: ReceiverId = id("receiver-1");
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(receiver_id.clone(), generation(1))
        .expect("receiver-only service trigger schedules");
    let mut startup_report = RestorationTickReport::default();
    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(10), &mut startup_report)
        .expect("receiver-only startup safely recovers pending state");
    assert_eq!(startup_report.claims_recovered, 1);
    assert!(scheduler.configured_receivers.contains(&receiver_id));

    scheduler
        .schedule_device_return_epoch_at(
            receiver_id,
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(2_u64).expect("sequence"),
            time(10),
            &[0, 1],
        )
        .expect("child identity trigger schedules");
    let mut return_report = RestorationTickReport::default();
    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(10), &mut return_report)
        .expect("late child identity retries compatible migration");

    assert_eq!(return_report.triggers_planned, 1);
    assert_eq!(return_report.claims_recovered, 0);
    assert!(controller.migrations.is_empty());
    assert!(controller.inner.plans.is_empty());
    assert!(scheduler.claims.contains_key(&current.claim_id));
}

#[test]
fn startup_reconciles_a_claim_before_retrying_profile_migration() {
    let prior = record(
        "claim-prior",
        RestoreRecordStatus::Deferred(RestoreDeferred {
            reason: RestoreDeferReason::DeviceUnknown,
            prior_outcome: None,
        }),
    );
    let current = record("claim-current", RestoreRecordStatus::Planned);
    let mut controller = MigratingController {
        inner: FakeController {
            now: time(10),
            plans: vec![Ok(RestorePlanResult::Planned(vec![current.clone()]))],
            pending: vec![prior.clone()],
            advances: vec![
                Ok(RestoreAdvanceResult::Terminal(delivered_record(
                    "claim-prior",
                    "mouse-1",
                ))),
                Ok(RestoreAdvanceResult::Terminal(delivered_record(
                    "claim-current",
                    "mouse-1",
                ))),
            ],
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        },
        migrations: vec![Err(RestorationError::PriorClaimUnresolved), Ok(1)],
    };
    let receiver_id: ReceiverId = id("receiver-1");
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(receiver_id.clone(), generation(1))
        .expect("service trigger schedules");

    let first = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("blocking claim is reconciled without stopping the runtime");

    assert_eq!(first.claims_recovered, 1);
    assert_eq!(first.claims_terminal, 1);
    assert_eq!(controller.inner.advance_calls, 1);
    assert!(scheduler.configured_receivers.contains(&receiver_id));
    assert!(
        scheduler
            .triggers
            .values()
            .any(|scheduled| scheduled.trigger.kind == RestoreTriggerKind::ServiceStart)
    );

    controller.inner.pending.clear();
    controller.inner.now = time(1_010);
    let second = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("compatible migration retries after reconciliation");

    assert_eq!(second.triggers_planned, 1);
    assert_eq!(second.claims_terminal, 1);
    assert!(controller.migrations.is_empty());
    assert!(controller.inner.plans.is_empty());
    assert!(controller.inner.advances.is_empty());
}

#[test]
fn return_envelope_prefers_live_effect_and_falls_back_only_after_invalidation() {
    let mut controller = EffectController {
        inner: FakeController {
            now: time(10),
            plans: vec![Ok(RestorePlanResult::NoStableIntents)],
            pending: Vec::new(),
            advances: Vec::new(),
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        },
        replays: vec![
            DesiredEffectReplayStatus::Queued,
            DesiredEffectReplayStatus::Deferred,
            DesiredEffectReplayStatus::Invalidated,
        ],
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(9_u64).expect("sequence"),
            time(10),
            &[0, 1, 2],
        )
        .expect("return envelope schedules");

    let mut first = RestorationTickReport::default();
    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(10), &mut first)
        .expect("live effect replay queues");
    assert_eq!(first.effect_replays_queued, 1);
    assert_eq!(first.triggers_planned, 0);
    assert_eq!(controller.inner.plans.len(), 1);

    let mut second = RestorationTickReport::default();
    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(11), &mut second)
        .expect("temporary readiness consumes only this attempt");
    assert_eq!(second.effect_replays_deferred, 1);
    assert_eq!(second.triggers_planned, 0);
    assert_eq!(controller.inner.plans.len(), 1);

    let mut third = RestorationTickReport::default();
    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(12), &mut third)
        .expect("stale owner falls back to stable authority");
    assert_eq!(third.triggers_planned, 1);
    assert!(controller.inner.plans.is_empty());
    assert!(controller.replays.is_empty());
}

#[test]
fn failed_compatible_rebind_leaves_receiver_unconfigured_for_retry() {
    let mut controller = MigratingController {
        inner: FakeController {
            now: time(10),
            plans: Vec::new(),
            pending: Vec::new(),
            advances: Vec::new(),
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        },
        migrations: vec![Err(RestorationError::Persistence(
            hfx_core::PersistenceOperation::SaveIntent,
        ))],
    };
    let receiver_id: ReceiverId = id("receiver-1");
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(receiver_id.clone(), generation(1))
        .expect("service trigger schedules");
    let mut report = RestorationTickReport::default();

    assert!(matches!(
        scheduler.process_due_trigger(&mut controller, &sessions(), time(10), &mut report),
        Err(RestorationScheduleError::Restoration(
            RestorationError::Persistence(hfx_core::PersistenceOperation::SaveIntent)
        ))
    ));
    assert!(!scheduler.configured_receivers.contains(&receiver_id));
    assert_eq!(controller.inner.policy_calls, 1);
}

#[test]
fn service_restart_preserves_a_pending_generation_claims_readiness_gate() {
    let mut prior = record("claim-prior", RestoreRecordStatus::Planned);
    prior.trigger_kind = RestoreTriggerKind::ReceiverGeneration;
    let mut controller = FakeController {
        now: time(10),
        plans: Vec::new(),
        pending: vec![prior.clone()],
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("service trigger schedules");

    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("pending generation claim recovers");

    assert_eq!(report.claims_recovered, 1);
    assert_eq!(controller.advance_calls, 0);
    assert_eq!(
        scheduler
            .claims
            .get(&prior.claim_id)
            .expect("claim remains parked")
            .due
            .get(),
        u64::MAX
    );
}

#[test]
fn advance_barrier_reloads_pending_claim_without_stopping_the_scheduler() {
    let planned = record("claim-prior", RestoreRecordStatus::Planned);
    let mut controller = FakeController {
        now: time(10),
        plans: vec![Ok(RestorePlanResult::Planned(vec![planned.clone()]))],
        pending: vec![planned],
        advances: vec![Err(RestorationError::PriorOutcomeUncertain)],
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler.configured_receivers.insert(id("receiver-1"));
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("trigger schedules");

    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("advance barrier is recoverable");

    assert_eq!(report.triggers_planned, 1);
    assert_eq!(report.claims_recovered, 1);
    assert_eq!(report.claims_advanced, 0);
    assert_eq!(scheduler.claims.len(), 1);
    assert_eq!(controller.advance_calls, 1);
}

#[test]
fn superseding_plan_resynchronizes_compacted_pending_claims() {
    let old_keyboard = record_for_device(
        "claim-keyboard-old",
        "keyboard-1",
        RestoreRecordStatus::Planned,
    );
    let current_keyboard = record_for_device(
        "claim-keyboard-current",
        "keyboard-1",
        RestoreRecordStatus::Planned,
    );
    let mouse = record_for_device("claim-mouse", "mouse-1", RestoreRecordStatus::Planned);
    let mut controller = FakeController {
        now: time(10),
        plans: vec![Ok(RestorePlanResult::Planned(vec![
            current_keyboard.clone(),
        ]))],
        pending: vec![old_keyboard.clone(), mouse.clone()],
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let receiver_id: ReceiverId = id("receiver-1");
    let mut scheduler = scheduler();
    scheduler.configured_receivers.insert(receiver_id.clone());
    scheduler
        .schedule_record(old_keyboard.clone(), time(20))
        .expect("old keyboard claim schedules");
    scheduler
        .schedule_record(mouse.clone(), time(30))
        .expect("mouse claim schedules");
    scheduler
        .schedule_device_return_epoch_at(
            receiver_id,
            generation(1),
            id("keyboard-1"),
            SequenceNumber::try_from(2_u64).expect("sequence"),
            time(10),
            &[0, 1],
        )
        .expect("keyboard return schedules");
    let mut report = RestorationTickReport::default();

    scheduler
        .process_due_trigger(&mut controller, &sessions(), time(10), &mut report)
        .expect("superseding plan resynchronizes scheduler claims");

    assert_eq!(report.triggers_planned, 1);
    assert!(!scheduler.claims.contains_key(&old_keyboard.claim_id));
    assert!(scheduler.claims.contains_key(&current_keyboard.claim_id));
    assert_eq!(
        scheduler
            .claims
            .get(&mouse.claim_id)
            .expect("sibling claim remains scheduled")
            .due,
        time(30)
    );
}

#[test]
fn compacted_volatile_claim_recovers_from_durable_authority() {
    let compacted = record_for_device(
        "claim-keyboard-compacted",
        "keyboard-1",
        RestoreRecordStatus::Planned,
    );
    let current = record_for_device(
        "claim-keyboard-current",
        "keyboard-1",
        RestoreRecordStatus::Planned,
    );
    let mut controller = FakeController {
        now: time(10),
        plans: Vec::new(),
        pending: vec![current.clone()],
        advances: vec![Err(RestorationError::UnknownClaim)],
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_record(compacted.clone(), time(10))
        .expect("compacted volatile claim schedules");
    let mut report = RestorationTickReport::default();

    assert!(
        scheduler
            .process_due_claim(&mut controller, &sessions(), time(10), &mut report)
            .expect("unknown volatile claim reloads durable authority")
    );

    assert_eq!(report.claims_recovered, 1);
    assert!(!scheduler.claims.contains_key(&compacted.claim_id));
    assert_eq!(
        scheduler
            .claims
            .get(&current.claim_id)
            .expect("current durable claim schedules")
            .due,
        time(1_010)
    );
}

#[test]
fn overlapping_resume_and_device_return_preserve_the_latest_exact_envelope() {
    let mut scheduler = scheduler();
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(1_u64).expect("sequence"),
            time(0),
            &[0, 100],
        )
        .expect("device return schedules");
    scheduler
        .schedule_system_resume(
            id("receiver-1"),
            generation(1),
            SequenceNumber::try_from(2_u64).expect("sequence"),
        )
        .expect("resume schedules");
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(3_u64).expect("sequence"),
            time(0),
            &[0, 100],
        )
        .expect("later device return coalesces");

    assert_eq!(scheduler.triggers.len(), 3);
    assert_eq!(
        scheduler
            .triggers
            .values()
            .filter(|scheduled| scheduled.trigger.kind == RestoreTriggerKind::SystemResume)
            .count(),
        1
    );
    assert_eq!(
        scheduler
            .triggers
            .values()
            .filter(|scheduled| matches!(
                scheduled.purpose,
                ScheduledTriggerPurpose::ReturnAttempt(_)
            ) && scheduled.sequence
                == Some(SequenceNumber::try_from(3_u64).unwrap()))
            .count(),
        2
    );
    assert!(scheduler.ready_devices.contains_key(&(
        id("receiver-1"),
        generation(1),
        id("mouse-1")
    )));
}

#[test]
fn return_epoch_queues_the_complete_bounded_profile_envelope() {
    let mut scheduler = epoch_scheduler(32);
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(10_u64).expect("sequence"),
            time(1_000),
            RETURN_RETRIES,
        )
        .expect("bounded return epoch schedules");

    let mut attempts = scheduler
        .triggers
        .values()
        .map(|scheduled| (scheduled.due.get(), scheduled.purpose))
        .collect::<Vec<_>>();
    attempts.sort_by_key(|(due, _)| *due);
    assert_eq!(attempts.len(), RETURN_RETRIES.len());
    for (index, ((due, purpose), offset)) in attempts.iter().zip(RETURN_RETRIES).enumerate() {
        assert_eq!(*due, 1_000 + offset);
        assert_eq!(
            *purpose,
            ScheduledTriggerPurpose::ReturnAttempt(u8::try_from(index).unwrap())
        );
    }
}

#[test]
fn incompatible_stable_intent_consumes_work_and_preserves_epoch_watermark() {
    let mut controller = FakeController {
        now: time(1_000),
        plans: vec![Ok(RestorePlanResult::NoCompatibleStableIntents)],
        pending: Vec::new(),
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = epoch_scheduler(32);
    let key: (ReceiverId, GenerationId, LogicalDeviceId) =
        (id("receiver-1"), generation(1), id("mouse-1"));
    let epoch_sequence = SequenceNumber::try_from(10_u64).expect("sequence");
    scheduler
        .schedule_device_return_epoch_at(
            key.0.clone(),
            key.1,
            key.2.clone(),
            epoch_sequence,
            time(1_000),
            RETURN_RETRIES,
        )
        .expect("bounded return epoch schedules");

    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("incompatible intent is consumed without a claim");

    assert_eq!(report.triggers_planned, 1);
    assert_eq!(report.claims_advanced, 0);
    assert_eq!(report.claims_dispatched, 0);
    assert_eq!(report.claims_terminal, 0);
    assert!(controller.plans.is_empty());
    assert_eq!(controller.advance_calls, 0);
    assert_eq!(controller.dispatch_calls, 0);
    assert!(scheduler.triggers.is_empty());
    assert!(!scheduler.ready_devices.contains_key(&key));
    assert_eq!(scheduler.return_epochs.get(&key), Some(&epoch_sequence));

    for replay_sequence in [
        epoch_sequence,
        SequenceNumber::try_from(9_u64).expect("stale sequence"),
    ] {
        scheduler
            .schedule_device_return_epoch_at(
                key.0.clone(),
                key.1,
                key.2.clone(),
                replay_sequence,
                time(2_000),
                RETURN_RETRIES,
            )
            .expect("duplicate and stale evidence are idempotent");
        assert!(scheduler.triggers.is_empty());
        assert!(!scheduler.ready_devices.contains_key(&key));
        assert_eq!(scheduler.return_epochs.get(&key), Some(&epoch_sequence));
    }

    let newer_sequence = SequenceNumber::try_from(11_u64).expect("newer sequence");
    scheduler
        .schedule_device_return_epoch_at(
            key.0.clone(),
            key.1,
            key.2.clone(),
            newer_sequence,
            time(3_000),
            RETURN_RETRIES,
        )
        .expect("newer evidence opens one new bounded epoch");
    assert_eq!(scheduler.triggers.len(), RETURN_RETRIES.len());
    assert_eq!(scheduler.return_epochs.get(&key), Some(&newer_sequence));

    scheduler.retire_generation(&key.0, key.1);
    assert!(scheduler.triggers.is_empty());
    assert!(!scheduler.return_epochs.contains_key(&key));
    let replacement = (key.0.clone(), generation(2), key.2.clone());
    scheduler
        .schedule_device_return_epoch_at(
            replacement.0.clone(),
            replacement.1,
            replacement.2.clone(),
            epoch_sequence,
            time(4_000),
            RETURN_RETRIES,
        )
        .expect("replacement generation has independent epoch authority");
    assert_eq!(scheduler.triggers.len(), RETURN_RETRIES.len());
    assert_eq!(
        scheduler.return_epochs.get(&replacement),
        Some(&epoch_sequence)
    );
}

#[test]
fn unavailable_profile_authority_preserves_remaining_return_offsets() {
    let mut controller = FakeController {
        now: time(1_000),
        plans: vec![Ok(RestorePlanResult::ProfileAuthorityUnavailable)],
        pending: Vec::new(),
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = epoch_scheduler(8);
    let key: (ReceiverId, GenerationId, LogicalDeviceId) =
        (id("receiver-1"), generation(1), id("mouse-1"));
    scheduler
        .schedule_device_return_epoch_at(
            key.0.clone(),
            key.1,
            key.2.clone(),
            SequenceNumber::try_from(10_u64).expect("sequence"),
            time(1_000),
            &[0, 100, 200],
        )
        .expect("bounded return epoch schedules");

    let report = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("unavailable authority remains read-only");

    assert_eq!(report.triggers_planned, 1);
    assert_eq!(report.claims_advanced, 0);
    assert_eq!(scheduler.triggers.len(), 2);
    assert!(scheduler.ready_devices.contains_key(&key));
    assert!(scheduler.return_epochs.contains_key(&key));
}

#[test]
fn terminal_receiver_delivery_does_not_cancel_the_remaining_bounded_attempts() {
    let planned = [
        record_for_device("claim-return-1", "mouse-1", RestoreRecordStatus::Planned),
        record_for_device("claim-return-2", "mouse-1", RestoreRecordStatus::Planned),
        record_for_device("claim-return-3", "mouse-1", RestoreRecordStatus::Planned),
    ];
    let mut controller = FakeController {
        now: time(1_000),
        plans: planned
            .iter()
            .cloned()
            .map(|record| Ok(RestorePlanResult::Planned(vec![record])))
            .collect(),
        pending: Vec::new(),
        advances: planned
            .iter()
            .cloned()
            .map(|record| Ok(RestoreAdvanceResult::Queued(record)))
            .collect(),
        dispatches: [
            delivered_record("claim-return-1", "mouse-1"),
            delivered_record("claim-return-2", "mouse-1"),
            delivered_record("claim-return-3", "mouse-1"),
        ]
        .into_iter()
        .map(Ok)
        .collect(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = epoch_scheduler(8);
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(10_u64).expect("sequence"),
            time(1_000),
            &[0, 100, 200],
        )
        .expect("return envelope schedules");

    for (index, now) in [1_000_u64, 1_100, 1_200].into_iter().enumerate() {
        controller.now = time(now);
        let report = scheduler
            .tick_with(&mut controller, &sessions())
            .expect("bounded attempt completes");
        assert_eq!(report.triggers_planned, 1);
        assert_eq!(report.claims_dispatched, 1);
        assert_eq!(report.claims_terminal, 1);
        assert_eq!(scheduler.triggers.len(), 2 - index);
    }
    assert_eq!(controller.dispatch_calls, 3);
}

#[test]
fn unresolved_attempt_does_not_extend_or_destroy_the_remaining_envelope() {
    let mut controller = FakeController {
        now: time(1_000),
        plans: vec![
            Err(RestorationError::PriorClaimUnresolved),
            Ok(RestorePlanResult::NoStableIntents),
        ],
        pending: Vec::new(),
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = epoch_scheduler(8);
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(10_u64).expect("sequence"),
            time(1_000),
            &[0, 100],
        )
        .expect("return envelope schedules");

    let first = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("unresolved attempt remains bounded");
    assert_eq!(first.claims_recovered, 0);
    assert_eq!(scheduler.triggers.len(), 1);

    controller.now = time(1_100);
    let second = scheduler
        .tick_with(&mut controller, &sessions())
        .expect("next bounded attempt remains available");
    assert_eq!(second.triggers_planned, 1);
    assert!(scheduler.triggers.is_empty());
}

#[test]
fn newer_return_epoch_replaces_only_the_same_endpoint_and_stale_epochs_are_ignored() {
    let mut scheduler = epoch_scheduler(40);
    for (device, sequence) in [("mouse-1", 10_u64), ("keyboard-1", 11_u64)] {
        scheduler
            .schedule_device_return_epoch_at(
                id("receiver-1"),
                generation(1),
                id(device),
                SequenceNumber::try_from(sequence).unwrap(),
                time(1_000),
                &[0, 100],
            )
            .expect("endpoint epoch schedules");
    }
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(12_u64).unwrap(),
            time(2_000),
            &[0, 200, 400],
        )
        .expect("newer mouse epoch replaces its predecessor");
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(9_u64).unwrap(),
            time(3_000),
            &[0, 100],
        )
        .expect("stale epoch is an idempotent no-op");

    let mouse = scheduler
        .triggers
        .values()
        .filter(|scheduled| scheduled.trigger.target_device_id.as_ref() == Some(&id("mouse-1")))
        .collect::<Vec<_>>();
    let keyboard = scheduler
        .triggers
        .values()
        .filter(|scheduled| scheduled.trigger.target_device_id.as_ref() == Some(&id("keyboard-1")))
        .collect::<Vec<_>>();
    assert_eq!(mouse.len(), 3);
    assert!(mouse.iter().all(|scheduled| {
        scheduled.sequence == Some(SequenceNumber::try_from(12_u64).unwrap())
    }));
    assert_eq!(keyboard.len(), 2);
    assert!(keyboard.iter().all(|scheduled| {
        scheduled.sequence == Some(SequenceNumber::try_from(11_u64).unwrap())
    }));
}

#[test]
fn return_epoch_capacity_failure_is_atomic() {
    let mut scheduler = epoch_scheduler(11);
    assert_eq!(
        scheduler.schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(10_u64).unwrap(),
            time(1_000),
            RETURN_RETRIES,
        ),
        Err(RestorationScheduleError::TriggerCapacity)
    );
    assert!(scheduler.triggers.is_empty());
    assert!(scheduler.ready_devices.is_empty());
    assert!(scheduler.return_epochs.is_empty());
}

#[test]
fn accelerator_moves_one_pending_attempt_earlier_without_touching_a_sibling() {
    let mut scheduler = epoch_scheduler(40);
    for (device, sequence) in [("mouse-1", 10_u64), ("keyboard-1", 11_u64)] {
        scheduler
            .schedule_device_return_epoch_at(
                id("receiver-1"),
                generation(1),
                id(device),
                SequenceNumber::try_from(sequence).unwrap(),
                time(1_000),
                &[0, 1_000, 2_000],
            )
            .expect("endpoint epoch schedules");
    }
    scheduler
        .accelerate_device_return_epoch_at(
            &id("receiver-1"),
            generation(1),
            &id("mouse-1"),
            SequenceNumber::try_from(10_u64).unwrap(),
            time(1_500),
        )
        .expect("mouse evidence accelerates one attempt");

    let mut mouse_due = scheduler
        .triggers
        .values()
        .filter(|scheduled| scheduled.trigger.target_device_id.as_ref() == Some(&id("mouse-1")))
        .map(|scheduled| scheduled.due.get())
        .collect::<Vec<_>>();
    let mut keyboard_due = scheduler
        .triggers
        .values()
        .filter(|scheduled| scheduled.trigger.target_device_id.as_ref() == Some(&id("keyboard-1")))
        .map(|scheduled| scheduled.due.get())
        .collect::<Vec<_>>();
    mouse_due.sort_unstable();
    keyboard_due.sort_unstable();
    assert_eq!(mouse_due, vec![1_000, 1_500, 3_000]);
    assert_eq!(keyboard_due, vec![1_000, 2_000, 3_000]);
}

#[test]
fn broad_resume_does_not_erase_an_endpoint_retry_envelope() {
    let mut scheduler = epoch_scheduler(32);
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(10_u64).unwrap(),
            time(1_000),
            &[0, 100, 200],
        )
        .expect("endpoint epoch schedules");
    scheduler
        .schedule_system_resume(
            id("receiver-1"),
            generation(1),
            SequenceNumber::try_from(11_u64).unwrap(),
        )
        .expect("resume coexists with the endpoint envelope");

    assert_eq!(
        scheduler
            .triggers
            .values()
            .filter(|scheduled| matches!(
                scheduled.purpose,
                ScheduledTriggerPurpose::ReturnAttempt(_)
            ))
            .count(),
        3
    );
}

#[test]
fn receiver_generation_parks_each_sibling_until_its_exact_activity() {
    let mouse = record_for_device("claim-mouse", "mouse-1", RestoreRecordStatus::Planned);
    let keyboard = record_for_device("claim-keyboard", "keyboard-1", RestoreRecordStatus::Planned);
    let mut controller = FakeController {
        now: time(10),
        plans: vec![Ok(RestorePlanResult::Planned(vec![
            mouse.clone(),
            keyboard.clone(),
        ]))],
        pending: Vec::new(),
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_receiver_generation(id("receiver-1"), generation(1))
        .expect("generation trigger schedules");
    scheduler
        .process_due_trigger(
            &mut controller,
            &sessions(),
            time(10),
            &mut RestorationTickReport::default(),
        )
        .expect("generation claims plan");

    assert_eq!(
        scheduler
            .claims
            .get(&mouse.claim_id)
            .expect("mouse claim")
            .due
            .get(),
        u64::MAX
    );
    assert_eq!(
        scheduler
            .claims
            .get(&keyboard.claim_id)
            .expect("keyboard claim")
            .due
            .get(),
        u64::MAX
    );

    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(4_u64).expect("sequence"),
            time(0),
            &[0, 1],
        )
        .expect("mouse readiness wakes its claim");
    assert_eq!(
        scheduler
            .claims
            .get(&mouse.claim_id)
            .expect("mouse claim")
            .due
            .get(),
        0
    );
    assert_eq!(
        scheduler
            .claims
            .get(&keyboard.claim_id)
            .expect("keyboard claim")
            .due
            .get(),
        u64::MAX
    );
}

#[test]
fn activity_before_generation_planning_is_retained_without_waking_a_sibling() {
    let mouse = record_for_device("claim-mouse", "mouse-1", RestoreRecordStatus::Planned);
    let keyboard = record_for_device("claim-keyboard", "keyboard-1", RestoreRecordStatus::Planned);
    let mut controller = FakeController {
        now: time(10),
        plans: vec![Ok(RestorePlanResult::Planned(vec![
            mouse.clone(),
            keyboard.clone(),
        ]))],
        pending: Vec::new(),
        advances: Vec::new(),
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_receiver_generation(id("receiver-1"), generation(1))
        .expect("generation trigger schedules");
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(4_u64).expect("sequence"),
            time(10),
            &[0, 1],
        )
        .expect("early mouse readiness is retained");
    scheduler
        .process_due_trigger(
            &mut controller,
            &sessions(),
            time(10),
            &mut RestorationTickReport::default(),
        )
        .expect("generation claims plan");

    assert_eq!(
        scheduler
            .claims
            .get(&mouse.claim_id)
            .expect("mouse claim")
            .due,
        time(10)
    );
    assert_eq!(
        scheduler
            .claims
            .get(&keyboard.claim_id)
            .expect("keyboard claim")
            .due
            .get(),
        u64::MAX
    );
}

#[test]
fn suspend_clears_old_readiness_but_resume_keeps_post_suspend_activity() {
    let claim = record("claim-mouse", RestoreRecordStatus::Planned);
    let mut scheduler = scheduler();
    scheduler
        .schedule_record(claim.clone(), time(0))
        .expect("claim schedules");
    scheduler
        .ready_devices
        .insert((id("receiver-1"), generation(1), id("mouse-1")), time(0));

    scheduler
        .schedule_system_suspend(&id("receiver-1"), generation(1))
        .expect("suspend boundary records");

    assert!(scheduler.ready_devices.is_empty());
    assert_eq!(
        scheduler
            .claims
            .get(&claim.claim_id)
            .expect("claim remains parked")
            .due
            .get(),
        u64::MAX
    );
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(6_u64).expect("sequence"),
            time(0),
            &[0, 1],
        )
        .expect("fresh activity wakes the claim");
    scheduler
        .schedule_system_resume(
            id("receiver-1"),
            generation(1),
            SequenceNumber::try_from(7_u64).expect("sequence"),
        )
        .expect("resume trigger schedules");
    assert!(scheduler.ready_devices.contains_key(&(
        id("receiver-1"),
        generation(1),
        id("mouse-1")
    )));
    assert_eq!(
        scheduler
            .claims
            .get(&claim.claim_id)
            .expect("claim wakes")
            .due
            .get(),
        0
    );
}

#[test]
fn repeated_device_return_epochs_replace_without_trigger_overflow() {
    let mut scheduler = scheduler();
    for sequence in 1_u64..=65 {
        scheduler
            .schedule_device_return_epoch_at(
                id("receiver-1"),
                generation(1),
                id("mouse-1"),
                SequenceNumber::try_from(sequence).expect("sequence"),
                time(0),
                &[0, 1],
            )
            .expect("same-scope return coalesces");
    }

    assert_eq!(scheduler.triggers.len(), 2);
    assert!(scheduler.triggers.values().all(|scheduled| {
        scheduled.sequence == Some(SequenceNumber::try_from(65_u64).expect("sequence"))
    }));
}
#[test]
fn deferred_claim_waits_for_backoff_but_device_return_wakes_it() {
    let deferred = record(
        "claim-deferred",
        RestoreRecordStatus::Deferred(RestoreDeferred {
            reason: RestoreDeferReason::DeviceSleeping,
            prior_outcome: None,
        }),
    );
    let mut controller = FakeController {
        now: MonotonicMs::try_from(10_u64).expect("time"),
        plans: vec![Ok(RestorePlanResult::Planned(vec![deferred.clone()]))],
        pending: Vec::new(),
        advances: vec![Ok(RestoreAdvanceResult::Deferred(deferred.clone()))],
        dispatches: Vec::new(),
        policy_calls: 0,
        advance_calls: 0,
        dispatch_calls: 0,
    };
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("trigger schedules");
    scheduler
        .tick_with(&mut controller, &sessions())
        .expect("claim defers");
    assert_eq!(controller.advance_calls, 1);
    assert_eq!(
        scheduler
            .claims
            .get(&id("claim-deferred"))
            .expect("claim retained")
            .due
            .get(),
        u64::MAX
    );
    scheduler
        .schedule_device_return_epoch_at(
            id("receiver-1"),
            generation(1),
            id("mouse-1"),
            SequenceNumber::try_from(9_u64).expect("sequence"),
            time(0),
            &[0, 1],
        )
        .expect("return schedules");
    assert_eq!(
        scheduler
            .claims
            .get(&id("claim-deferred"))
            .expect("claim retained")
            .due
            .get(),
        0
    );
}

#[test]
fn transient_deferral_uses_bounded_exponential_backoff() {
    assert_eq!(
        deferred_due(time(10), RestoreDeferReason::OwnershipConflict, 1_000, 1)
            .expect("first retry")
            .get(),
        1_010
    );
    assert_eq!(
        deferred_due(time(10), RestoreDeferReason::OwnershipConflict, 1_000, 2)
            .expect("second retry")
            .get(),
        2_010
    );
    assert_eq!(
        deferred_due(
            time(10),
            RestoreDeferReason::OwnershipConflict,
            1_000,
            u8::MAX
        )
        .expect("bounded retry")
        .get(),
        64_010
    );
}

#[test]
fn disabled_scheduler_has_no_session_and_no_work() {
    let scheduler = RestorationScheduler::new(
        false,
        None,
        [0; 32],
        LeaseDurationMs::try_from(10_000_u32).expect("lease"),
        30_000,
        1_000,
        4,
        8,
        16,
    )
    .expect("disabled scheduler");
    assert!(!scheduler.is_enabled());
}

#[test]
#[allow(clippy::too_many_lines)]
fn durable_service_start_restore_uses_the_normal_backend_and_survives_reopen() {
    let root = temporary_directory();
    fs::create_dir(&root).expect("state directory creates");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
        .expect("state directory is private");
    let state_path = root.join("bridge-state.json");
    let (receivers, profiles) = runtime_state();
    let completion = stable_completion(&receivers, &profiles);
    let mut restoration =
        ProductionRestoration::durable(&state_path, 4, 1024 * 1024).expect("durable runtime opens");
    assert!(matches!(
        restoration
            .capture_completed(
                &completion,
                WallClockUnixMs::try_from(10_u64).expect("capture time")
            )
            .expect("stable intent captures"),
        StableCommitOutcome::Captured(ref intents) if intents.len() == 1
    ));

    let capacity = QueueCapacity::try_from(16_u16).expect("capacity");
    let mut identities = DeterministicIdentities(1);
    let mut backend = CoreBridgeBackend::new(
        CoreBridgeConfig {
            lifecycle_limits: LifecycleLimits::default(),
            lease_capacity: capacity,
            lease_history_capacity: capacity,
            transaction_capacity: capacity,
            event_capacity: capacity,
            diagnostic_capacity: capacity,
            subscription_capacity: capacity,
            stream_id: id::<StreamId>("stream-1"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("projection revision"),
        },
        TestClock(time(100)),
        TestWallClock(WallClockUnixMs::try_from(20_u64).expect("wall clock")),
        TestTransport {
            dispatches: Vec::new(),
        },
        restoration,
        &mut identities,
        receivers,
        profiles,
        TestEventSink::default(),
    )
    .expect("backend composes");
    let mut scheduler = scheduler();
    scheduler
        .schedule_service_start(id("receiver-1"), generation(1))
        .expect("service-start trigger schedules");
    let report = scheduler
        .tick_with(&mut backend, &sessions())
        .expect("restore tick succeeds");
    assert_eq!(
        report,
        RestorationTickReport {
            triggers_planned: 1,
            claims_recovered: 0,
            claims_advanced: 1,
            claims_dispatched: 1,
            claims_deferred: 0,
            claims_terminal: 1,
            ..RestorationTickReport::default()
        }
    );
    assert_eq!(backend.transport().dispatches.len(), 1);
    assert_eq!(backend.transport().dispatches[0].frames.len(), 1);
    assert!(
        backend.transport().dispatches[0].frames[0]
            .colors
            .iter()
            .all(|color| color.blue.get() == 255)
    );
    assert!(
        backend
            .pending_restoration_records(&id("receiver-1"))
            .expect("pending records load")
            .is_empty()
    );
    drop(backend);

    let reopened =
        hfx_bridge::FilePersistenceStore::open(hfx_bridge::FilePersistenceConfig::new(&state_path))
            .expect("state reopens after process boundary");
    let records = reopened
        .restore_records(&id("receiver-1"))
        .expect("records reload");
    assert_eq!(records.len(), 1);
    assert!(matches!(
        records[0].status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(
        reopened
            .stable_entries(&id("receiver-1"))
            .expect("stable intent reloads")
            .len(),
        1
    );
    drop(reopened);
    fs::remove_dir_all(root).expect("state directory removes");
}

#[test]
fn production_scheduler_recovers_prepared_queued_and_applying_file_checkpoints() {
    let prepared_root = temporary_directory();
    fs::create_dir(&prepared_root).expect("prepared directory creates");
    fs::set_permissions(&prepared_root, fs::Permissions::from_mode(0o700))
        .expect("prepared directory is private");
    let prepared_path = prepared_root.join("bridge-state.json");
    let prepared_store = FilePersistenceStore::open_with_committer(
        FilePersistenceConfig::new(&prepared_path),
        FailNthCommitter::new(5),
    )
    .expect("prepared store opens");
    let mut prepared_backend = compose_backend(durable_with_stable(prepared_store), 1);
    let prepared_claim = plan_durable_restore(&mut prepared_backend);
    assert!(matches!(
        advance_durable_restore(&mut prepared_backend, &prepared_claim),
        Err(RestorationError::Persistence(
            hfx_core::PersistenceOperation::SaveRestore
        ))
    ));
    assert!(matches!(
        prepared_backend
            .restoration()
            .store()
            .restore_record(&prepared_claim)
            .expect("prepared record reads")
            .expect("prepared record exists")
            .status,
        RestoreRecordStatus::Prepared(_)
    ));
    assert!(prepared_backend.transport().dispatches.is_empty());
    drop(prepared_backend);
    recover_file_checkpoint(&prepared_path);
    fs::remove_dir_all(prepared_root).expect("prepared directory removes");

    for checkpoint in ["queued", "applying"] {
        let root = temporary_directory().with_extension(checkpoint);
        fs::create_dir(&root).expect("checkpoint directory creates");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
            .expect("checkpoint directory is private");
        let state_path = root.join("bridge-state.json");
        let store = FilePersistenceStore::open(FilePersistenceConfig::new(&state_path))
            .expect("checkpoint store opens");
        let mut backend = compose_backend(durable_with_stable(store), 1);
        let claim_id = plan_durable_restore(&mut backend);
        assert!(matches!(
            advance_durable_restore(&mut backend, &claim_id),
            Ok(RestoreAdvanceResult::Queued(_))
        ));
        if checkpoint == "applying" {
            RestorationCoordinator
                .mark_applying(&claim_id, backend.restoration_mut().store_mut())
                .expect("applying checkpoint persists");
        }
        let record = backend
            .restoration()
            .store()
            .restore_record(&claim_id)
            .expect("checkpoint record reads")
            .expect("checkpoint record exists");
        assert!(
            matches!(&record.status, RestoreRecordStatus::Queued(_)) == (checkpoint == "queued")
        );
        assert!(
            matches!(&record.status, RestoreRecordStatus::Applying(_))
                == (checkpoint == "applying")
        );
        assert!(backend.transport().dispatches.is_empty());
        drop(backend);

        recover_file_checkpoint(&state_path);
        fs::remove_dir_all(root).expect("checkpoint directory removes");
    }
}
