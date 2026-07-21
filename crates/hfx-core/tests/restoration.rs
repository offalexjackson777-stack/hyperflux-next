// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{generation, text, transaction_request};
use hfx_core::{
    BoundedEventLog, DeviceStateAuthority, EventDelivery, EventSink, LeaseManager,
    PersistedRestorePolicy, PersistedStableEntry, PersistenceCasOutcome, PersistenceOperation,
    PersistenceStore, ProfileRegistry, QualifiedDeviceProfile, QualifiedReceiverProfile,
    ReceiverTransport, RestorationAuthority, RestorationCoordinator, RestorationError,
    RestoreAdvanceResult, RestoreGenerationRetirement, RestorePlanResult, RestoreRecord,
    RestoreRecordChange, RestoreRecordStatus, RestoreTrigger, SessionAuthority,
    StableCommitOutcome, StableIntentCapture, StableIntentChange, StableLighting,
    SubmissionBinding, TransactionCoordinator, TransportDispatch, TransportFailure,
    TransportFailureFacts, TransportReceipt, TransportReconciliation, TransportTerminal,
    canonical_request_digest,
};
use hfx_domain::{
    AuthorizationEpoch, ColorChannel, DeliveredFrameCount, DeviceApplicationState,
    DeviceWriteReadiness, DispatchNonce, FrameCount, IntentRevision, LeaseDurationMs, LedCount,
    LogicalDeviceId, MonotonicMs, PersistenceRevision, ProjectionRevision, ProtocolErrorKind,
    ReceiverId, ResourceKind, RestoreClaimId, RestoreDeferReason, RestoreInvalidationReason,
    RestoreTriggerKind, SequenceNumber, SideEffectCertainty, StreamEpoch, TransactionClass,
    TransactionState, WallClockUnixMs,
};
use hfx_protocol::{BridgeEvent, ResourceKey, RgbColor, TransactionRequest, TransactionTerminal};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::convert::Infallible;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct DurableState {
    policies: BTreeMap<ReceiverId, PersistedRestorePolicy>,
    stable_entries: BTreeMap<(ReceiverId, LogicalDeviceId), PersistedStableEntry>,
    restore_records: BTreeMap<RestoreClaimId, RestoreRecord>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PolicyCasCall {
    expected_revision: Option<PersistenceRevision>,
    policy: PersistedRestorePolicy,
}

#[derive(Debug, Default)]
struct MemoryPersistenceStore {
    durable: DurableState,
    policy_cas_calls: Vec<PolicyCasCall>,
    stable_cas_calls: Vec<Vec<StableIntentChange>>,
    stable_read_override: Option<Vec<PersistedStableEntry>>,
    conflict_next_policy_cas: bool,
    conflict_next_stable_cas: bool,
    restore_cas_calls: usize,
    conflict_restore_cas_call: Option<usize>,
}

impl MemoryPersistenceStore {
    fn durable_snapshot(&self) -> DurableState {
        self.durable.clone()
    }

    fn persisted_entries(&self, receiver_id: &ReceiverId) -> Vec<PersistedStableEntry> {
        self.durable
            .stable_entries
            .iter()
            .filter(|((stored_receiver_id, _), _)| stored_receiver_id == receiver_id)
            .map(|(_, entry)| entry.clone())
            .collect()
    }

    fn persisted_entry(
        &self,
        receiver_id: &ReceiverId,
        device_id: &LogicalDeviceId,
    ) -> Option<&PersistedStableEntry> {
        self.durable
            .stable_entries
            .get(&(receiver_id.clone(), device_id.clone()))
    }
}

impl PersistenceStore for MemoryPersistenceStore {
    type Error = Infallible;

    fn restore_policy(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Option<PersistedRestorePolicy>, Self::Error> {
        Ok(self.durable.policies.get(receiver_id).cloned())
    }

    fn compare_and_set_restore_policy(
        &mut self,
        expected_revision: Option<PersistenceRevision>,
        policy: &PersistedRestorePolicy,
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        self.policy_cas_calls.push(PolicyCasCall {
            expected_revision,
            policy: policy.clone(),
        });
        if std::mem::take(&mut self.conflict_next_policy_cas) {
            return Ok(PersistenceCasOutcome::Conflict);
        }

        let actual_revision = self
            .durable
            .policies
            .get(&policy.receiver_id)
            .map(|current| current.revision);
        if actual_revision != expected_revision {
            return Ok(PersistenceCasOutcome::Conflict);
        }

        self.durable
            .policies
            .insert(policy.receiver_id.clone(), policy.clone());
        Ok(PersistenceCasOutcome::Applied)
    }

    fn stable_entries(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<PersistedStableEntry>, Self::Error> {
        Ok(self
            .stable_read_override
            .clone()
            .unwrap_or_else(|| self.persisted_entries(receiver_id)))
    }

    fn compare_and_set_stable_entries(
        &mut self,
        changes: &[StableIntentChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        self.stable_cas_calls.push(changes.to_vec());
        if std::mem::take(&mut self.conflict_next_stable_cas) {
            return Ok(PersistenceCasOutcome::Conflict);
        }

        let revisions_match = changes.iter().all(|change| {
            let key = (
                change.entry.receiver_id().clone(),
                change.entry.device_id().clone(),
            );
            self.durable
                .stable_entries
                .get(&key)
                .map(PersistedStableEntry::revision)
                == change.expected_revision
        });
        if !revisions_match {
            return Ok(PersistenceCasOutcome::Conflict);
        }

        for change in changes {
            let key = (
                change.entry.receiver_id().clone(),
                change.entry.device_id().clone(),
            );
            self.durable
                .stable_entries
                .insert(key, change.entry.clone());
        }
        Ok(PersistenceCasOutcome::Applied)
    }

    fn restore_records(&self, receiver_id: &ReceiverId) -> Result<Vec<RestoreRecord>, Self::Error> {
        Ok(self
            .durable
            .restore_records
            .values()
            .filter(|record| &record.receiver_id == receiver_id)
            .cloned()
            .collect())
    }

    fn restore_record(
        &self,
        claim_id: &RestoreClaimId,
    ) -> Result<Option<RestoreRecord>, Self::Error> {
        Ok(self.durable.restore_records.get(claim_id).cloned())
    }

    fn compare_and_set_restore_records(
        &mut self,
        changes: &[RestoreRecordChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        self.restore_cas_calls += 1;
        if self.conflict_restore_cas_call == Some(self.restore_cas_calls) {
            return Ok(PersistenceCasOutcome::Conflict);
        }
        let revisions_match = changes.iter().all(|change| {
            self.durable
                .restore_records
                .get(&change.record.claim_id)
                .map(|current| current.revision)
                == change.expected_revision
        });
        if !revisions_match {
            return Ok(PersistenceCasOutcome::Conflict);
        }

        for change in changes {
            self.durable
                .restore_records
                .insert(change.record.claim_id.clone(), change.record.clone());
        }
        Ok(PersistenceCasOutcome::Applied)
    }
}

fn stable_request(id: &str, devices: &[&str]) -> TransactionRequest {
    transaction_request(id, TransactionClass::StaticLighting, 10_000, devices)
}

fn successful_terminal(request: &TransactionRequest) -> TransactionTerminal {
    let frame_count = u16::try_from(request.frames.len()).expect("test frame count fits");
    TransactionTerminal {
        request_id: request.request_id.clone(),
        request_digest: canonical_request_digest(request).expect("test request is canonical"),
        transaction_id: request.transaction_id.clone(),
        receiver_id: request.receiver_id.clone(),
        generation_id: request.generation_id,
        state: TransactionState::Succeeded,
        declared_frames: FrameCount::try_from(frame_count).expect("test frame count is valid"),
        delivered_frames: DeliveredFrameCount::try_from(frame_count)
            .expect("test delivered count is valid"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Confirmed,
        terminal_sequence: SequenceNumber::try_from(1_u64).expect("test sequence is valid"),
        error_kind: None,
        superseded_by: None,
    }
}

fn static_captures(request: &TransactionRequest) -> Vec<StableIntentCapture> {
    request
        .frames
        .iter()
        .map(|frame| StableIntentCapture {
            device_id: frame.device_id.clone(),
            lighting: StableLighting::Static(frame.colors.clone()),
        })
        .collect()
}

fn wall_time(value: u64) -> WallClockUnixMs {
    WallClockUnixMs::try_from(value).expect("test wall-clock time is valid")
}

fn zero_color() -> RgbColor {
    RgbColor {
        red: ColorChannel::try_from(0_u8).expect("test color is valid"),
        green: ColorChannel::try_from(0_u8).expect("test color is valid"),
        blue: ColorChannel::try_from(0_u8).expect("test color is valid"),
    }
}

fn commit_static(
    coordinator: RestorationCoordinator,
    request: &TransactionRequest,
    captured_at: u64,
    store: &mut MemoryPersistenceStore,
) {
    coordinator
        .commit_stable_transaction(
            request,
            &successful_terminal(request),
            &static_captures(request),
            wall_time(captured_at),
            store,
        )
        .expect("definitive static transaction is persisted");
}

#[derive(Clone, Debug)]
struct FakeTransportError(TransportFailureFacts);

impl TransportFailure for FakeTransportError {
    fn facts(&self) -> TransportFailureFacts {
        self.0
    }
}

#[derive(Clone, Debug)]
enum PlannedTransport {
    Receipt(TransportReceipt),
    Failure(TransportFailureFacts),
}

#[derive(Clone, Debug)]
struct FakeTransport {
    receiver_id: ReceiverId,
    generation_id: hfx_domain::GenerationId,
    present: bool,
    forced_reconciliation: Option<TransportReconciliation>,
    retained: Option<(TransportDispatch, TransportReconciliation)>,
    reconcile_calls: RefCell<Vec<TransportDispatch>>,
    dispatches: Vec<TransportDispatch>,
    physical_writes: usize,
    plan: PlannedTransport,
}

impl ReceiverTransport for FakeTransport {
    type Error = FakeTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<hfx_domain::GenerationId> {
        (self.present && receiver_id == &self.receiver_id).then_some(self.generation_id)
    }

    fn reconcile(&self, dispatch: &TransportDispatch) -> TransportReconciliation {
        self.reconcile_calls.borrow_mut().push(dispatch.clone());
        self.forced_reconciliation.unwrap_or_else(|| {
            self.retained.as_ref().map_or(
                TransportReconciliation::NotObserved,
                |(retained_dispatch, outcome)| {
                    if retained_dispatch == dispatch {
                        *outcome
                    } else {
                        TransportReconciliation::NotObserved
                    }
                },
            )
        })
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.dispatches.push(dispatch.clone());
        match self.plan {
            PlannedTransport::Receipt(receipt) => {
                if receipt.live_write_executed
                    || receipt.delivered_frames.get() > 0
                    || receipt.side_effect_certainty != SideEffectCertainty::None
                {
                    self.physical_writes += 1;
                }
                self.retained =
                    Some((dispatch.clone(), TransportReconciliation::Retained(receipt)));
                Ok(receipt)
            }
            PlannedTransport::Failure(facts) => {
                if facts.live_write_executed
                    || facts.delivered_frames.get() > 0
                    || facts.side_effect_certainty != SideEffectCertainty::None
                {
                    self.physical_writes += 1;
                }
                self.retained = Some((
                    dispatch.clone(),
                    TransportReconciliation::RetainedFailure(facts),
                ));
                Err(FakeTransportError(facts))
            }
        }
    }
}

#[derive(Clone, Debug)]
struct FakeSessions {
    session_id: hfx_domain::SessionId,
    epoch: AuthorizationEpoch,
    live: bool,
}

impl SessionAuthority for FakeSessions {
    fn authorizes(
        &self,
        session_id: &hfx_domain::SessionId,
        authorization_epoch: AuthorizationEpoch,
    ) -> bool {
        self.live && session_id == &self.session_id && authorization_epoch == self.epoch
    }
}

#[derive(Clone, Debug, Default)]
struct FakeDevices {
    readiness: BTreeMap<LogicalDeviceId, DeviceWriteReadiness>,
}

impl DeviceStateAuthority for FakeDevices {
    fn write_readiness(&self, resource: &ResourceKey) -> DeviceWriteReadiness {
        self.readiness
            .get(&resource.device_id)
            .copied()
            .unwrap_or(DeviceWriteReadiness::Unknown)
    }
}

#[derive(Clone, Copy, Debug)]
struct FakeProfiles {
    valid: bool,
}

impl ProfileRegistry for FakeProfiles {
    fn supports(&self, resource: &ResourceKey) -> bool {
        self.valid
            && resource.receiver_id.as_str() == "receiver-1"
            && resource.generation_id == generation(1)
            && resource.kind == ResourceKind::Lighting
    }

    fn receiver_profile(
        &self,
        receiver_id: &ReceiverId,
        generation_id: hfx_domain::GenerationId,
    ) -> Option<QualifiedReceiverProfile> {
        (self.valid && receiver_id.as_str() == "receiver-1" && generation_id == generation(1)).then(
            || QualifiedReceiverProfile {
                profile_id: common::receiver_profile_id(),
                profile_digest: common::receiver_profile_digest(),
            },
        )
    }

    fn device_profile(&self, resource: &ResourceKey) -> Option<QualifiedDeviceProfile> {
        self.supports(resource).then(|| QualifiedDeviceProfile {
            profile_id: common::device_profile_id(resource.device_id.as_str()),
            profile_digest: common::device_profile_digest(),
            application_slot_count: LedCount::try_from(1_u16).expect("test LED count is valid"),
        })
    }
}

#[derive(Debug, Default)]
struct FakeSink {
    events: Vec<BridgeEvent>,
}

impl EventSink for FakeSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.events.push(event.clone());
        EventDelivery::Accepted
    }
}

fn monotonic(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("test monotonic time is valid")
}

fn sessions() -> FakeSessions {
    FakeSessions {
        session_id: text("restore-session-1"),
        epoch: AuthorizationEpoch::try_from(1_u64).expect("test epoch is valid"),
        live: true,
    }
}

fn authority(nonce: u64) -> RestorationAuthority {
    RestorationAuthority {
        client_id: text("restore-client-1"),
        submission: SubmissionBinding {
            session_id: text("restore-session-1"),
            authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("test epoch is valid"),
            dispatch_nonce: DispatchNonce::try_from(nonce).expect("test nonce is valid"),
        },
        lease_duration_ms: LeaseDurationMs::try_from(10_000_u32)
            .expect("test lease duration is valid"),
        deadline_ms: monotonic(50_000),
    }
}

fn devices(states: &[(&str, DeviceWriteReadiness)]) -> FakeDevices {
    FakeDevices {
        readiness: states
            .iter()
            .map(|(device, readiness)| (text(device), *readiness))
            .collect(),
    }
}

fn successful_receipt() -> TransportReceipt {
    TransportReceipt {
        terminal: TransportTerminal::Delivered,
        delivered_frames: DeliveredFrameCount::try_from(1_u16)
            .expect("test delivered count is valid"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry_safe: false,
        device_application: DeviceApplicationState::Unverified,
    }
}

fn fake_transport() -> FakeTransport {
    FakeTransport {
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        present: true,
        forced_reconciliation: None,
        retained: None,
        reconcile_calls: RefCell::new(Vec::new()),
        dispatches: Vec::new(),
        physical_writes: 0,
        plan: PlannedTransport::Receipt(successful_receipt()),
    }
}

fn event_log() -> BoundedEventLog {
    BoundedEventLog::new(
        text("restore-stream-1"),
        StreamEpoch::try_from(1_u64).expect("test stream epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("test projection revision is valid"),
        64,
    )
    .expect("test event log is valid")
}

fn plan_claims(
    coordinator: RestorationCoordinator,
    store: &mut MemoryPersistenceStore,
    trigger_id: &str,
    kind: RestoreTriggerKind,
    target_device_id: Option<&str>,
) -> Vec<RestoreRecord> {
    let trigger = RestoreTrigger {
        trigger_id: text(trigger_id),
        kind,
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        target_device_id: target_device_id.map(text),
    };
    let RestorePlanResult::Planned(records) = coordinator
        .plan_restore(&trigger, store)
        .expect("restore trigger is planned")
    else {
        panic!("stable intents produce restore claims")
    };
    records
}

fn seed_restore(
    coordinator: RestorationCoordinator,
    store: &mut MemoryPersistenceStore,
    devices: &[&str],
) -> Vec<RestoreRecord> {
    let request = stable_request("restore-seed", devices);
    commit_static(coordinator, &request, 1, store);
    coordinator
        .set_restore_enabled(&request.receiver_id, true, store)
        .expect("restore policy is enabled");
    plan_claims(
        coordinator,
        store,
        "restore-trigger-start",
        RestoreTriggerKind::ServiceStart,
        None,
    )
}

#[derive(Debug)]
struct RestoreRuntime {
    store: MemoryPersistenceStore,
    sessions: FakeSessions,
    devices: FakeDevices,
    profiles: FakeProfiles,
    transport: FakeTransport,
    leases: LeaseManager,
    transactions: TransactionCoordinator,
    events: BoundedEventLog,
    sink: FakeSink,
}

impl RestoreRuntime {
    fn new(store: MemoryPersistenceStore, devices: FakeDevices) -> Self {
        Self {
            store,
            sessions: sessions(),
            devices,
            profiles: FakeProfiles { valid: true },
            transport: fake_transport(),
            leases: LeaseManager::new(16, 32).expect("test lease bounds are valid"),
            transactions: TransactionCoordinator::new(16)
                .expect("test transaction bounds are valid"),
            events: event_log(),
            sink: FakeSink::default(),
        }
    }

    fn advance(
        &mut self,
        claim_id: &RestoreClaimId,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        self.advance_with(claim_id, &authority(10))
    }

    fn advance_with(
        &mut self,
        claim_id: &RestoreClaimId,
        restore_authority: &RestorationAuthority,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        RestorationCoordinator.advance_claim(
            claim_id,
            restore_authority,
            monotonic(10),
            &self.sessions,
            &self.devices,
            &self.profiles,
            &self.transport,
            &mut self.store,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.sink,
        )
    }

    fn dispatch(&mut self, claim_id: &RestoreClaimId) -> Result<RestoreRecord, RestorationError> {
        RestorationCoordinator.dispatch_claim(
            claim_id,
            monotonic(11),
            &self.sessions,
            &self.devices,
            &self.profiles,
            &mut self.transport,
            &mut self.store,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.sink,
        )
    }

    fn retire_generation(&mut self) -> Result<RestoreGenerationRetirement, RestorationError> {
        RestorationCoordinator.retire_generation(
            &text("receiver-1"),
            generation(1),
            monotonic(12),
            &self.transport,
            &mut self.store,
            &mut self.leases,
            &self.transactions,
            &mut self.events,
            &mut self.sink,
        )
    }

    fn restart_volatile(&mut self) {
        self.leases = LeaseManager::new(16, 32).expect("test lease bounds are valid");
        self.transactions =
            TransactionCoordinator::new(16).expect("test transaction bounds are valid");
        self.events = event_log();
        self.sink = FakeSink::default();
    }
}

#[test]
fn missing_policy_defaults_disabled_and_enable_disable_is_cas_versioned() {
    let coordinator = RestorationCoordinator;
    let receiver_id: ReceiverId = text("receiver-1");
    let trigger = RestoreTrigger {
        trigger_id: text("trigger-start"),
        kind: RestoreTriggerKind::ServiceStart,
        receiver_id: receiver_id.clone(),
        generation_id: generation(1),
        target_device_id: None,
    };
    let mut store = MemoryPersistenceStore::default();

    assert_eq!(
        coordinator.plan_restore(&trigger, &mut store),
        Ok(RestorePlanResult::Disabled)
    );

    let enabled = coordinator
        .set_restore_enabled(&receiver_id, true, &mut store)
        .expect("missing policy can be enabled");
    assert!(enabled.enabled);
    assert_eq!(enabled.revision.get(), 1);
    assert_eq!(store.policy_cas_calls[0].expected_revision, None);
    assert_eq!(
        coordinator.plan_restore(&trigger, &mut store),
        Ok(RestorePlanResult::NoStableIntents)
    );

    let disabled = coordinator
        .set_restore_enabled(&receiver_id, false, &mut store)
        .expect("existing policy can be disabled");
    assert!(!disabled.enabled);
    assert_eq!(disabled.revision.get(), 2);
    assert_eq!(
        store.policy_cas_calls[1].expected_revision,
        Some(enabled.revision)
    );
    assert_eq!(
        coordinator.plan_restore(&trigger, &mut store),
        Ok(RestorePlanResult::Disabled)
    );

    let durable_before_conflict = store.durable_snapshot();
    store.conflict_next_policy_cas = true;
    assert_eq!(
        coordinator.set_restore_enabled(&receiver_id, true, &mut store),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SavePolicy
        ))
    );
    assert_eq!(store.durable_snapshot(), durable_before_conflict);
    assert_eq!(
        store
            .policy_cas_calls
            .last()
            .expect("conflicting CAS is observed")
            .expected_revision,
        Some(disabled.revision)
    );
}

#[test]
fn definitive_success_captures_static_and_off_in_one_batch() {
    let coordinator = RestorationCoordinator;
    let mut request = stable_request("mixed", &["keyboard-1", "mouse-1"]);
    request.frames[1].colors.fill(zero_color());
    request.stable_intents[1].mode = hfx_domain::StableLightingMode::Off;
    let captures = vec![
        StableIntentCapture {
            device_id: text("mouse-1"),
            lighting: StableLighting::Off,
        },
        StableIntentCapture {
            device_id: text("keyboard-1"),
            lighting: StableLighting::Static(request.frames[0].colors.clone()),
        },
    ];
    let terminal = successful_terminal(&request);
    let captured_at = wall_time(1_700_000_000_000);
    let mut store = MemoryPersistenceStore::default();

    let intents = coordinator
        .commit_stable_transaction(&request, &terminal, &captures, captured_at, &mut store)
        .expect("definitive stable transaction is captured");

    assert_eq!(intents.len(), 2);
    assert_eq!(store.stable_cas_calls.len(), 1);
    assert_eq!(store.stable_cas_calls[0].len(), 2);
    assert!(
        store.stable_cas_calls[0]
            .iter()
            .all(|change| change.expected_revision.is_none())
    );
    assert_eq!(
        store.stable_cas_calls[0]
            .iter()
            .map(|change| change.entry.device_id().as_str())
            .collect::<Vec<_>>(),
        vec!["keyboard-1", "mouse-1"]
    );

    let entries = store.persisted_entries(&request.receiver_id);
    assert_eq!(entries.len(), 2);
    let PersistedStableEntry::Present(keyboard) = &entries[0] else {
        panic!("keyboard intent is present")
    };
    let PersistedStableEntry::Present(mouse) = &entries[1] else {
        panic!("mouse intent is present")
    };
    assert_eq!(keyboard.device_id.as_str(), "keyboard-1");
    assert_eq!(
        keyboard.lighting,
        StableLighting::Static(request.frames[0].colors.clone())
    );
    assert_eq!(mouse.device_id.as_str(), "mouse-1");
    assert_eq!(mouse.lighting, StableLighting::Off);
    for intent in [keyboard, mouse] {
        assert_eq!(intent.revision.get(), 1);
        assert_eq!(intent.source_transaction_id, request.transaction_id);
        assert_eq!(intent.source_request_digest, terminal.request_digest);
        assert_eq!(intent.captured_at, captured_at);
    }
}

#[test]
fn declared_capture_is_idempotent_and_never_advances_revision_on_replay() {
    let coordinator = RestorationCoordinator;
    let request = stable_request("declared-replay", &["keyboard-1", "mouse-1"]);
    let terminal = successful_terminal(&request);
    let mut store = MemoryPersistenceStore::default();

    let first = coordinator
        .commit_declared_stable_transaction(&request, &terminal, wall_time(10), &mut store)
        .expect("first declared capture commits");
    let StableCommitOutcome::Captured(first) = first else {
        panic!("definitive stable completion must capture")
    };
    assert_eq!(store.stable_cas_calls.len(), 1);

    let replay = coordinator
        .commit_declared_stable_transaction(&request, &terminal, wall_time(999), &mut store)
        .expect("exact replay is idempotent");
    let StableCommitOutcome::Captured(replay) = replay else {
        panic!("exact replay returns retained intents")
    };
    assert_eq!(replay, first);
    assert_eq!(store.stable_cas_calls.len(), 1);
    assert!(replay.iter().all(|intent| intent.revision.get() == 1));
    assert!(
        replay
            .iter()
            .all(|intent| intent.captured_at == wall_time(10))
    );
}

#[test]
fn declared_capture_ignores_effect_restore_and_nondefinitive_terminals() {
    let coordinator = RestorationCoordinator;
    let stable = stable_request("declared-ignore", &["mouse-1"]);
    let terminal = successful_terminal(&stable);
    let mut store = MemoryPersistenceStore::default();

    let mut effect = stable.clone();
    effect.transaction_class = TransactionClass::EffectFrame;
    effect.stable_intents.clear();
    let mut effect_terminal = terminal.clone();
    effect_terminal.request_digest = canonical_request_digest(&effect).expect("effect digest");
    assert_eq!(
        coordinator
            .commit_declared_stable_transaction(
                &effect,
                &effect_terminal,
                wall_time(1),
                &mut store,
            )
            .expect("effect completion is ignored"),
        StableCommitOutcome::NotApplicable
    );

    let mut restore = effect;
    restore.transaction_class = TransactionClass::Restore;
    let mut restore_terminal = effect_terminal;
    restore_terminal.request_digest = canonical_request_digest(&restore).expect("restore digest");
    assert_eq!(
        coordinator
            .commit_declared_stable_transaction(
                &restore,
                &restore_terminal,
                wall_time(2),
                &mut store,
            )
            .expect("restore completion is ignored"),
        StableCommitOutcome::NotApplicable
    );

    let mut failed = terminal;
    failed.state = TransactionState::Failed;
    assert_eq!(
        coordinator
            .commit_declared_stable_transaction(&stable, &failed, wall_time(3), &mut store,)
            .expect("failed stable completion is ignored"),
        StableCommitOutcome::NotApplicable
    );
    assert!(store.stable_cas_calls.is_empty());
}

#[test]
fn effect_frame_is_rejected_without_persistence() {
    let coordinator = RestorationCoordinator;
    let mut request = stable_request("effect", &["mouse-1"]);
    request.transaction_class = TransactionClass::EffectFrame;
    request.stable_intents.clear();
    let terminal = successful_terminal(&request);
    let mut store = MemoryPersistenceStore::default();
    let durable_before = store.durable_snapshot();

    assert_eq!(
        coordinator.commit_stable_transaction(
            &request,
            &terminal,
            &static_captures(&request),
            wall_time(10),
            &mut store,
        ),
        Err(RestorationError::InvalidStableTransaction)
    );
    assert_eq!(store.durable_snapshot(), durable_before);
    assert!(store.stable_cas_calls.is_empty());
}

#[test]
fn failed_partial_and_rejected_application_are_not_persisted() {
    let coordinator = RestorationCoordinator;
    let mut store = MemoryPersistenceStore::default();
    let seed = stable_request("seed", &["mouse-1"]);
    commit_static(coordinator, &seed, 1, &mut store);

    let request = stable_request("invalid-update", &["mouse-1"]);
    let definitive = successful_terminal(&request);
    let mut failed = definitive.clone();
    failed.state = TransactionState::Failed;
    let mut partial_delivery = definitive.clone();
    partial_delivery.delivered_frames =
        DeliveredFrameCount::try_from(0_u16).expect("zero delivered frames is valid");
    let mut partial_effect = definitive.clone();
    partial_effect.side_effect_certainty = SideEffectCertainty::Partial;
    let mut rejected = definitive;
    rejected.device_application = DeviceApplicationState::Rejected;

    let durable_before = store.durable_snapshot();
    store.stable_cas_calls.clear();
    for (case, terminal) in [
        ("failed", failed),
        ("partial delivery", partial_delivery),
        ("partial side effect", partial_effect),
        ("rejected application", rejected),
    ] {
        assert_eq!(
            coordinator.commit_stable_transaction(
                &request,
                &terminal,
                &static_captures(&request),
                wall_time(2),
                &mut store,
            ),
            Err(RestorationError::InvalidStableTransaction),
            "{case} must not replace stable intent"
        );
    }
    assert_eq!(store.durable_snapshot(), durable_before);
    assert!(store.stable_cas_calls.is_empty());
}

#[test]
fn multi_device_capture_is_atomic_when_one_revision_conflicts() {
    let coordinator = RestorationCoordinator;
    let mut store = MemoryPersistenceStore::default();
    let seed = stable_request("seed-both", &["keyboard-1", "mouse-1"]);
    commit_static(coordinator, &seed, 1, &mut store);
    let stale_entries = store.persisted_entries(&seed.receiver_id);

    let mouse_update = stable_request("mouse-update", &["mouse-1"]);
    commit_static(coordinator, &mouse_update, 2, &mut store);
    let durable_before = store.durable_snapshot();

    store.stable_read_override = Some(stale_entries);
    store.stable_cas_calls.clear();
    let both_update = stable_request("both-update", &["keyboard-1", "mouse-1"]);
    assert_eq!(
        coordinator.commit_stable_transaction(
            &both_update,
            &successful_terminal(&both_update),
            &static_captures(&both_update),
            wall_time(3),
            &mut store,
        ),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveIntent
        ))
    );

    assert_eq!(store.stable_cas_calls.len(), 1);
    assert_eq!(store.stable_cas_calls[0].len(), 2);
    assert_eq!(store.durable_snapshot(), durable_before);
    let keyboard_id = text("keyboard-1");
    let mouse_id = text("mouse-1");
    let PersistedStableEntry::Present(keyboard) = store
        .persisted_entry(&seed.receiver_id, &keyboard_id)
        .expect("keyboard intent remains")
    else {
        panic!("keyboard entry remains present")
    };
    let PersistedStableEntry::Present(mouse) = store
        .persisted_entry(&seed.receiver_id, &mouse_id)
        .expect("mouse intent remains")
    else {
        panic!("mouse entry remains present")
    };
    assert_eq!(keyboard.source_transaction_id, seed.transaction_id);
    assert_eq!(mouse.source_transaction_id, mouse_update.transaction_id);
}

#[test]
fn tombstone_prevents_a_stale_writer_from_resurrecting_intent() {
    let coordinator = RestorationCoordinator;
    let mut store = MemoryPersistenceStore::default();
    let initial = stable_request("initial", &["mouse-1"]);
    commit_static(coordinator, &initial, 1, &mut store);
    let stale_entries = store.persisted_entries(&initial.receiver_id);
    let device_id = text("mouse-1");

    let tombstones = coordinator
        .clear_stable_intents(
            &initial.receiver_id,
            std::slice::from_ref(&device_id),
            wall_time(2),
            &mut store,
        )
        .expect("active intent is tombstoned");
    assert_eq!(tombstones[0].revision.get(), 2);
    assert!(tombstones[0].previous_content_digest.is_some());
    let durable_with_tombstone = store.durable_snapshot();

    store.stable_read_override = Some(stale_entries);
    let stale_update = stable_request("stale-update", &["mouse-1"]);
    assert_eq!(
        coordinator.commit_stable_transaction(
            &stale_update,
            &successful_terminal(&stale_update),
            &static_captures(&stale_update),
            wall_time(3),
            &mut store,
        ),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveIntent
        ))
    );
    assert_eq!(store.durable_snapshot(), durable_with_tombstone);
    assert!(matches!(
        store.persisted_entry(&initial.receiver_id, &device_id),
        Some(PersistedStableEntry::Deleted(tombstone)) if tombstone == &tombstones[0]
    ));
}

#[test]
fn stable_intent_cas_conflict_causes_no_mutation() {
    let coordinator = RestorationCoordinator;
    let mut store = MemoryPersistenceStore::default();
    let initial = stable_request("initial-cas", &["mouse-1"]);
    commit_static(coordinator, &initial, 1, &mut store);
    let durable_before = store.durable_snapshot();

    store.conflict_next_stable_cas = true;
    let update = stable_request("conflicting-cas", &["mouse-1"]);
    assert_eq!(
        coordinator.commit_stable_transaction(
            &update,
            &successful_terminal(&update),
            &static_captures(&update),
            wall_time(2),
            &mut store,
        ),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveIntent
        ))
    );
    assert_eq!(store.durable_snapshot(), durable_before);
    assert_eq!(
        store
            .stable_cas_calls
            .last()
            .expect("conflicting CAS is observed")[0]
            .expected_revision,
        Some(IntentRevision::try_from(1_u64).expect("test revision is valid"))
    );
}

#[test]
fn queued_claim_dispatches_once_and_replays_terminal_state() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));

    assert!(matches!(
        runtime.advance(&claim_id),
        Ok(RestoreAdvanceResult::Queued(_))
    ));
    let terminal = runtime
        .dispatch(&claim_id)
        .expect("queued claim dispatches");
    assert!(matches!(terminal.status, RestoreRecordStatus::Succeeded(_)));
    assert_eq!(runtime.transport.dispatches.len(), 1);
    assert_eq!(runtime.transport.physical_writes, 1);

    assert!(matches!(
        runtime.advance(&claim_id),
        Ok(RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Succeeded(_),
            ..
        }))
    ));
    assert_eq!(runtime.transport.dispatches.len(), 1);
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn crash_after_prepared_cas_rebuilds_queue_without_duplicate_write() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    store.conflict_restore_cas_call = Some(store.restore_cas_calls + 2);
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));

    assert_eq!(
        runtime.advance(&claim_id),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore
        ))
    );
    assert!(matches!(
        runtime
            .store
            .restore_record(&claim_id)
            .expect("store read succeeds")
            .expect("claim remains")
            .status,
        RestoreRecordStatus::Prepared(_)
    ));
    assert!(runtime.transport.dispatches.is_empty());

    runtime.restart_volatile();
    runtime.store.conflict_restore_cas_call = None;
    assert!(matches!(
        runtime.advance(&claim_id),
        Ok(RestoreAdvanceResult::Queued(_))
    ));
    let terminal = runtime
        .dispatch(&claim_id)
        .expect("recovered claim dispatches");
    assert!(matches!(terminal.status, RestoreRecordStatus::Succeeded(_)));
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn queued_and_applying_records_rebuild_after_process_crash() {
    for checkpoint in ["queued", "applying"] {
        let mut store = MemoryPersistenceStore::default();
        let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
        let claim_id = claims[0].claim_id.clone();
        let mut runtime =
            RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
        assert!(matches!(
            runtime.advance(&claim_id),
            Ok(RestoreAdvanceResult::Queued(_))
        ));
        if checkpoint == "applying" {
            RestorationCoordinator
                .mark_applying(&claim_id, &mut runtime.store)
                .expect("applying checkpoint is persisted");
        }

        runtime.restart_volatile();
        assert!(matches!(
            runtime.advance(&claim_id),
            Ok(RestoreAdvanceResult::Queued(_))
        ));
        let terminal = runtime
            .dispatch(&claim_id)
            .expect("rebuilt queue dispatches");
        assert!(
            matches!(terminal.status, RestoreRecordStatus::Succeeded(_)),
            "checkpoint {checkpoint}"
        );
        assert_eq!(
            runtime.transport.dispatches.len(),
            1,
            "checkpoint {checkpoint}"
        );
        assert_eq!(
            runtime.transport.physical_writes, 1,
            "checkpoint {checkpoint}"
        );
    }
}

#[test]
fn retained_success_after_terminal_cas_crash_finishes_without_second_write() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&claim_id).expect("claim queues");
    runtime.store.conflict_restore_cas_call = Some(runtime.store.restore_cas_calls + 2);

    assert_eq!(
        runtime.dispatch(&claim_id),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore
        ))
    );
    assert_eq!(runtime.transport.physical_writes, 1);
    assert!(matches!(
        runtime
            .store
            .restore_record(&claim_id)
            .expect("store read succeeds")
            .expect("claim remains")
            .status,
        RestoreRecordStatus::Applying(_)
    ));

    runtime.restart_volatile();
    runtime.store.conflict_restore_cas_call = None;
    let recovered = runtime
        .advance(&claim_id)
        .expect("retained outcome completes the record");
    assert!(matches!(
        recovered,
        RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Succeeded(_),
            ..
        })
    ));
    assert_eq!(runtime.transport.dispatches.len(), 1);
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn ambiguous_transport_history_fails_closed_without_dispatch() {
    for (reconciliation, error_kind) in [
        (
            TransportReconciliation::Evicted,
            ProtocolErrorKind::OutcomeEvicted,
        ),
        (
            TransportReconciliation::Unavailable,
            ProtocolErrorKind::OutcomeUnknown,
        ),
        (
            TransportReconciliation::Conflict,
            ProtocolErrorKind::OutcomeUnknown,
        ),
    ] {
        let mut store = MemoryPersistenceStore::default();
        let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
        let claim_id = claims[0].claim_id.clone();
        let mut runtime =
            RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
        runtime.advance(&claim_id).expect("claim queues");
        runtime.restart_volatile();
        runtime.transport.forced_reconciliation = Some(reconciliation);

        let terminal = runtime
            .advance(&claim_id)
            .expect("ambiguous history becomes a terminal failure");
        let RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Failed(completion),
            ..
        }) = terminal
        else {
            panic!("ambiguous history must fail closed")
        };
        assert_eq!(completion.error_kind, Some(error_kind));
        assert!(completion.live_write_executed);
        assert_eq!(
            completion.side_effect_certainty,
            SideEffectCertainty::Possible
        );
        assert!(!completion.automatic_retry);
        assert!(runtime.transport.dispatches.is_empty());
        assert_eq!(runtime.transport.physical_writes, 0);
    }
}

#[test]
fn retained_safe_failure_defers_and_preserves_exact_facts() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&claim_id).expect("claim queues");
    runtime.restart_volatile();
    runtime.transport.forced_reconciliation = Some(TransportReconciliation::RetainedFailure(
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(0_u16)
                .expect("zero delivered count is valid"),
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: DeviceApplicationState::Unverified,
        },
    ));

    let deferred = runtime
        .advance(&claim_id)
        .expect("safe retained failure defers");
    let RestoreAdvanceResult::Deferred(RestoreRecord {
        status: RestoreRecordStatus::Deferred(detail),
        ..
    }) = deferred
    else {
        panic!("safe retained failure must be deferred")
    };
    assert_eq!(detail.reason, RestoreDeferReason::SafeTransactionFailure);
    let prior = detail.prior_outcome.expect("safe outcome is retained");
    assert!(prior.automatic_retry);
    assert!(!prior.live_write_executed);
    assert_eq!(prior.side_effect_certainty, SideEffectCertainty::None);
    assert!(runtime.transport.dispatches.is_empty());
}

#[test]
fn sleeping_sibling_defers_while_ready_device_restores_independently() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(
        RestorationCoordinator,
        &mut store,
        &["keyboard-1", "mouse-1"],
    );
    let keyboard = claims
        .iter()
        .find(|record| record.device_id.as_str() == "keyboard-1")
        .expect("keyboard claim exists")
        .claim_id
        .clone();
    let mouse = claims
        .iter()
        .find(|record| record.device_id.as_str() == "mouse-1")
        .expect("mouse claim exists")
        .claim_id
        .clone();
    let mut runtime = RestoreRuntime::new(
        store,
        devices(&[
            ("keyboard-1", DeviceWriteReadiness::Sleeping),
            ("mouse-1", DeviceWriteReadiness::Ready),
        ]),
    );

    assert!(matches!(
        runtime.advance(&keyboard),
        Ok(RestoreAdvanceResult::Deferred(RestoreRecord {
            status: RestoreRecordStatus::Deferred(ref detail),
            ..
        })) if detail.reason == RestoreDeferReason::DeviceSleeping
    ));
    runtime.advance(&mouse).expect("ready mouse queues");
    let mouse_terminal = runtime.dispatch(&mouse).expect("ready mouse restores");
    assert!(matches!(
        mouse_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(runtime.transport.physical_writes, 1);

    runtime.devices = devices(&[
        ("keyboard-1", DeviceWriteReadiness::Ready),
        ("mouse-1", DeviceWriteReadiness::Ready),
    ]);
    let returned = plan_claims(
        RestorationCoordinator,
        &mut runtime.store,
        "restore-trigger-keyboard-return",
        RestoreTriggerKind::DeviceReturn,
        Some("keyboard-1"),
    );
    assert_eq!(returned.len(), 1);
    assert_eq!(returned[0].device_id.as_str(), "keyboard-1");
    let returned_claim = returned[0].claim_id.clone();
    runtime
        .advance(&returned_claim)
        .expect("returned keyboard queues");
    let keyboard_terminal = runtime
        .dispatch(&returned_claim)
        .expect("returned keyboard restores");
    assert!(matches!(
        keyboard_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(runtime.transport.physical_writes, 2);
}

#[test]
fn profile_drift_invalidates_before_any_hardware_write() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.profiles.valid = false;

    let result = runtime
        .advance(&claim_id)
        .expect("profile drift is represented as invalidation");
    assert!(matches!(
        result,
        RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Invalidated(ref detail),
            ..
        }) if detail.reason == RestoreInvalidationReason::ProfileChanged
    ));
    assert!(runtime.transport.dispatches.is_empty());
    assert_eq!(runtime.transport.physical_writes, 0);
}

#[test]
fn durable_claim_never_crosses_into_a_distinct_opaque_generation() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.transport.generation_id = generation(7);

    let result = runtime
        .advance(&claim_id)
        .expect("old generation claim is terminally invalidated");

    assert!(matches!(
        result,
        RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Invalidated(ref detail),
            ..
        }) if detail.reason == RestoreInvalidationReason::StaleGeneration
    ));
    assert!(runtime.transport.dispatches.is_empty());
    assert_eq!(runtime.transport.physical_writes, 0);
}

#[test]
fn transport_failure_with_possible_side_effects_is_terminal_and_not_retried() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.transport.plan = PlannedTransport::Failure(TransportFailureFacts {
        delivered_frames: DeliveredFrameCount::try_from(0_u16)
            .expect("zero delivered count is valid"),
        side_effect_certainty: SideEffectCertainty::Possible,
        live_write_executed: true,
        automatic_retry_safe: false,
        device_application: DeviceApplicationState::Unverified,
    });

    runtime.advance(&claim_id).expect("claim queues");
    let terminal = runtime.dispatch(&claim_id).expect("failure is recorded");
    let RestoreRecordStatus::Failed(completion) = terminal.status else {
        panic!("possible side effects must be terminally failed")
    };
    assert_eq!(
        completion.error_kind,
        Some(ProtocolErrorKind::TransportFailure)
    );
    assert!(completion.live_write_executed);
    assert_eq!(
        completion.side_effect_certainty,
        SideEffectCertainty::Possible
    );
    assert!(!completion.automatic_retry);
    assert_eq!(runtime.transport.dispatches.len(), 1);
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn claim_cas_conflict_stops_before_ownership_or_hardware() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    store.conflict_restore_cas_call = Some(store.restore_cas_calls + 1);
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));

    assert_eq!(
        runtime.advance(&claim_id),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore
        ))
    );
    assert!(matches!(
        runtime
            .store
            .restore_record(&claim_id)
            .expect("store read succeeds")
            .expect("claim remains")
            .status,
        RestoreRecordStatus::Planned
    ));
    assert_eq!(runtime.transactions.queued_len(), 0);
    assert!(runtime.transport.dispatches.is_empty());
    assert_eq!(runtime.transport.physical_writes, 0);
}

#[test]
fn external_owner_defers_restore_without_stealing_the_resource() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    let resource = common::resource("receiver-1", 1, "mouse-1");
    let external_client: hfx_domain::ClientId = text("external-client");
    let external_lease: hfx_domain::LeaseId = text("external-lease");
    runtime
        .leases
        .acquire(
            common::lease_request(
                "external-lease-request",
                "external-client",
                vec![resource.clone()],
            ),
            external_lease.clone(),
            monotonic(0),
        )
        .expect("external owner acquires the resource");

    let result = runtime
        .advance(&claim_id)
        .expect("ownership conflict is a deferred state");
    assert!(matches!(
        result,
        RestoreAdvanceResult::Deferred(RestoreRecord {
            status: RestoreRecordStatus::Deferred(ref detail),
            ..
        }) if detail.reason == RestoreDeferReason::OwnershipConflict
    ));
    assert!(runtime.leases.owns(
        &external_client,
        &external_lease,
        std::slice::from_ref(&resource),
        monotonic(10)
    ));
    assert!(runtime.transport.dispatches.is_empty());
}

#[test]
fn dispatch_rechecks_sleep_and_profile_before_transport() {
    for gate in ["sleep", "profile"] {
        let mut store = MemoryPersistenceStore::default();
        let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
        let claim_id = claims[0].claim_id.clone();
        let mut runtime =
            RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
        runtime
            .advance(&claim_id)
            .expect("claim queues while ready");
        if gate == "sleep" {
            runtime.devices = devices(&[("mouse-1", DeviceWriteReadiness::Sleeping)]);
        } else {
            runtime.profiles.valid = false;
        }

        let record = runtime
            .dispatch(&claim_id)
            .expect("dispatch gate is durably represented");
        if gate == "sleep" {
            assert!(matches!(
                record.status,
                RestoreRecordStatus::Deferred(ref detail)
                    if detail.reason == RestoreDeferReason::DeviceSleeping
                        && detail.prior_outcome.is_some()
            ));
        } else {
            assert!(matches!(
                record.status,
                RestoreRecordStatus::Invalidated(ref detail)
                    if detail.reason == RestoreInvalidationReason::ProfileChanged
            ));
        }
        assert!(runtime.transport.dispatches.is_empty(), "gate {gate}");
        assert_eq!(runtime.transport.physical_writes, 0, "gate {gate}");
        assert_eq!(runtime.transactions.queued_len(), 0, "gate {gate}");
    }
}

#[test]
fn changed_runtime_authority_creates_new_attempt_only_after_not_observed() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let claim_id = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    let first = runtime.advance(&claim_id).expect("first authority queues");
    let RestoreAdvanceResult::Queued(first) = first else {
        panic!("first attempt is queued")
    };
    let RestoreRecordStatus::Queued(first_attempt) = first.status else {
        panic!("first queued record retains its attempt")
    };
    assert_eq!(first_attempt.attempt_number.get(), 1);

    runtime.restart_volatile();
    runtime.sessions = FakeSessions {
        session_id: text("restore-session-2"),
        epoch: AuthorizationEpoch::try_from(2_u64).expect("test epoch is valid"),
        live: true,
    };
    let second_authority = RestorationAuthority {
        client_id: text("restore-client-2"),
        submission: SubmissionBinding {
            session_id: text("restore-session-2"),
            authorization_epoch: AuthorizationEpoch::try_from(2_u64).expect("test epoch is valid"),
            dispatch_nonce: DispatchNonce::try_from(100_u64).expect("test nonce is valid"),
        },
        lease_duration_ms: LeaseDurationMs::try_from(20_000_u32)
            .expect("test lease duration is valid"),
        deadline_ms: monotonic(60_000),
    };
    let second = runtime
        .advance_with(&claim_id, &second_authority)
        .expect("not-observed old attempt permits a freshly bound attempt");
    let RestoreAdvanceResult::Queued(second) = second else {
        panic!("second attempt is queued")
    };
    let RestoreRecordStatus::Queued(second_attempt) = second.status else {
        panic!("second queued record retains its attempt")
    };
    assert_eq!(second_attempt.attempt_number.get(), 2);
    assert_eq!(second_attempt.submission.dispatch_nonce.get(), 100);
    assert_eq!(
        second_attempt.request.client_id.as_str(),
        "restore-client-2"
    );
    assert_eq!(second_attempt.lease_request.duration_ms.get(), 20_000);
    assert_eq!(runtime.transport.reconcile_calls.borrow().len(), 1);
    assert!(runtime.transport.dispatches.is_empty());

    let terminal = runtime
        .dispatch(&claim_id)
        .expect("new authority dispatches");
    assert!(matches!(terminal.status, RestoreRecordStatus::Succeeded(_)));
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn uncertain_prior_trigger_blocks_a_new_automatic_write() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"]);
    let old_claim = claims[0].claim_id.clone();
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&old_claim).expect("old claim queues");
    runtime.restart_volatile();
    runtime.transport.forced_reconciliation = Some(TransportReconciliation::Unavailable);
    assert!(matches!(
        runtime.advance(&old_claim),
        Ok(RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Failed(_),
            ..
        }))
    ));

    runtime.transport.forced_reconciliation = None;
    let newer = plan_claims(
        RestorationCoordinator,
        &mut runtime.store,
        "restore-trigger-resume",
        RestoreTriggerKind::SystemResume,
        None,
    );
    assert_eq!(newer.len(), 1);
    assert_eq!(
        runtime.advance(&newer[0].claim_id),
        Err(RestorationError::PriorOutcomeUncertain)
    );
    assert!(runtime.transport.dispatches.is_empty());
}

#[test]
fn trigger_replay_is_idempotent_and_device_return_is_scoped() {
    let coordinator = RestorationCoordinator;
    let mut store = MemoryPersistenceStore::default();
    let initial = seed_restore(coordinator, &mut store, &["keyboard-1", "mouse-1"]);
    let replayed = plan_claims(
        coordinator,
        &mut store,
        "restore-trigger-start",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    assert_eq!(replayed, initial);

    let returned = plan_claims(
        coordinator,
        &mut store,
        "restore-trigger-mouse-return",
        RestoreTriggerKind::DeviceReturn,
        Some("mouse-1"),
    );
    assert_eq!(returned.len(), 1);
    assert_eq!(returned[0].device_id.as_str(), "mouse-1");
    assert!(matches!(
        store
            .restore_record(
                &initial
                    .iter()
                    .find(|record| record.device_id.as_str() == "mouse-1")
                    .expect("old mouse claim exists")
                    .claim_id
            )
            .expect("store read succeeds")
            .expect("old mouse claim remains")
            .status,
        RestoreRecordStatus::Invalidated(ref detail)
            if detail.reason == RestoreInvalidationReason::SupersededTrigger
    ));
    assert!(matches!(
        store
            .restore_record(
                &initial
                    .iter()
                    .find(|record| record.device_id.as_str() == "keyboard-1")
                    .expect("old keyboard claim exists")
                    .claim_id
            )
            .expect("store read succeeds")
            .expect("old keyboard claim remains")
            .status,
        RestoreRecordStatus::Planned
    ));

    let invalid = RestoreTrigger {
        trigger_id: text("invalid-device-return"),
        kind: RestoreTriggerKind::DeviceReturn,
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        target_device_id: None,
    };
    assert_eq!(
        coordinator.plan_restore(&invalid, &mut store),
        Err(RestorationError::InvalidTrigger)
    );
}

#[test]
fn generation_retirement_atomically_invalidates_unattempted_sibling_claims() {
    let mut store = MemoryPersistenceStore::default();
    let claims = seed_restore(
        RestorationCoordinator,
        &mut store,
        &["keyboard-1", "mouse-1"],
    );
    let mouse = claims
        .iter()
        .find(|claim| claim.device_id.as_str() == "mouse-1")
        .expect("mouse claim exists")
        .claim_id
        .clone();
    let mut runtime = RestoreRuntime::new(
        store,
        devices(&[("mouse-1", DeviceWriteReadiness::Sleeping)]),
    );
    assert!(matches!(
        runtime.advance(&mouse),
        Ok(RestoreAdvanceResult::Deferred(_))
    ));
    runtime.transport.present = false;
    let cas_before = runtime.store.restore_cas_calls;

    let retired = runtime
        .retire_generation()
        .expect("retirement invalidates both claims atomically");
    assert_eq!(retired.updated.len(), 2);
    assert_eq!(retired.already_terminal, 0);
    assert_eq!(runtime.store.restore_cas_calls, cas_before + 1);
    assert!(retired.updated.iter().all(|record| matches!(
        record.status,
        RestoreRecordStatus::Invalidated(ref detail)
            if detail.reason == RestoreInvalidationReason::StaleGeneration
    )));
    assert_eq!(runtime.sink.events.len(), 2);

    let replay = runtime
        .retire_generation()
        .expect("retirement replay is idempotent");
    assert!(replay.updated.is_empty());
    assert_eq!(replay.already_terminal, 2);
    assert_eq!(runtime.store.restore_cas_calls, cas_before + 1);
    assert_eq!(runtime.sink.events.len(), 2);
}

#[test]
fn generation_retirement_consumes_revoked_queue_terminal_without_transport_guessing() {
    let mut store = MemoryPersistenceStore::default();
    let claim = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"])
        .remove(0)
        .claim_id;
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&claim).expect("restore claim queues");
    runtime.transport.present = false;
    runtime
        .transactions
        .invalidate_generation(
            &text("receiver-1"),
            generation(1),
            &mut runtime.events,
            &mut runtime.sink,
        )
        .expect("generation queue is revoked");
    let _ = runtime
        .leases
        .invalidate_generation(&text("receiver-1"), generation(1));

    let retired = runtime
        .retire_generation()
        .expect("revoked queued claim retires");
    assert!(matches!(
        retired.updated[0].status,
        RestoreRecordStatus::Invalidated(ref detail)
            if detail.reason == RestoreInvalidationReason::StaleGeneration
    ));
    assert!(runtime.transport.reconcile_calls.borrow().is_empty());
    assert!(runtime.transport.dispatches.is_empty());
}

#[test]
fn generation_retirement_preserves_confirmed_delivery_after_terminal_cas_failure() {
    let mut store = MemoryPersistenceStore::default();
    let claim = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"])
        .remove(0)
        .claim_id;
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&claim).expect("restore claim queues");
    runtime.store.conflict_restore_cas_call = Some(runtime.store.restore_cas_calls + 2);
    assert!(matches!(
        runtime.dispatch(&claim),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore
        ))
    ));
    assert_eq!(runtime.transport.physical_writes, 1);
    runtime.store.conflict_restore_cas_call = None;
    runtime.transport.present = false;

    let retired = runtime
        .retire_generation()
        .expect("confirmed delivery survives retirement");
    assert!(matches!(
        retired.updated[0].status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(runtime.transport.physical_writes, 1);
}

#[test]
fn generation_retirement_preserves_ambiguous_transport_history_as_failure() {
    let mut store = MemoryPersistenceStore::default();
    let claim = seed_restore(RestorationCoordinator, &mut store, &["mouse-1"])
        .remove(0)
        .claim_id;
    let mut runtime =
        RestoreRuntime::new(store, devices(&[("mouse-1", DeviceWriteReadiness::Ready)]));
    runtime.advance(&claim).expect("restore claim queues");
    runtime.restart_volatile();
    runtime.transport.present = false;
    runtime.transport.forced_reconciliation = Some(TransportReconciliation::Evicted);

    let retired = runtime
        .retire_generation()
        .expect("ambiguous history becomes terminal failure");
    let RestoreRecordStatus::Failed(completion) = &retired.updated[0].status else {
        panic!("evicted outcome must not be labeled safely stale")
    };
    assert_eq!(
        completion.error_kind,
        Some(ProtocolErrorKind::OutcomeEvicted)
    );
    assert_eq!(
        completion.side_effect_certainty,
        SideEffectCertainty::Possible
    );
    assert!(completion.live_write_executed);
    assert!(!completion.automatic_retry);
}

#[test]
fn generation_retirement_rejects_active_generation_and_batch_conflict_is_atomic() {
    let mut store = MemoryPersistenceStore::default();
    seed_restore(
        RestorationCoordinator,
        &mut store,
        &["keyboard-1", "mouse-1"],
    );
    let mut runtime = RestoreRuntime::new(store, FakeDevices::default());
    assert_eq!(
        runtime.retire_generation(),
        Err(RestorationError::GenerationStillActive {
            receiver_id: text("receiver-1"),
            generation_id: generation(1),
        })
    );

    runtime.transport.present = false;
    let durable_before = runtime.store.durable_snapshot();
    let events_before = runtime.sink.events.len();
    runtime.store.conflict_restore_cas_call = Some(runtime.store.restore_cas_calls + 1);
    assert_eq!(
        runtime.retire_generation(),
        Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore
        ))
    );
    assert_eq!(runtime.store.durable_snapshot(), durable_before);
    assert_eq!(runtime.sink.events.len(), events_before);
}
