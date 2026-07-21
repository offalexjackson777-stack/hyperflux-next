// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{
    device_profile_digest, device_profile_id, generation, lease_request, receiver_profile_digest,
    receiver_profile_id, text, time, transaction_request,
};
use hfx_core::{
    BoundedEventLog, DeviceStateAuthority, EventDelivery, EventSink, LeaseManager, OutcomeLookup,
    ProfileRegistry, QualifiedDeviceProfile, QualifiedReceiverProfile, ReceiverTransport,
    SessionAuthority, SubmissionBinding, SubmissionResult, TransactionCoordinator,
    TransactionCoordinatorError, TransactionQueueError, TransportDispatch, TransportFailure,
    TransportFailureFacts, TransportReceipt, TransportReconciliation, TransportTerminal,
};
use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DeviceWriteReadiness,
    DispatchNonce, LedCount, ProjectionRevision, ProtocolErrorKind, QueueAdmission, ResourceKind,
    SideEffectCertainty, StreamEpoch, TransactionClass, TransactionState,
};
use hfx_protocol::{BridgeEvent, TransactionRequest, TransactionResult};

#[derive(Clone, Debug)]
struct FakeTransportError(TransportFailureFacts);

impl TransportFailure for FakeTransportError {
    fn facts(&self) -> TransportFailureFacts {
        self.0
    }
}

#[derive(Clone, Debug)]
enum PlannedDispatch {
    Receipt(TransportReceipt),
    Failure(FakeTransportError),
}

#[derive(Clone, Debug)]
struct FakeTransport {
    receiver_id: hfx_domain::ReceiverId,
    generation_id: hfx_domain::GenerationId,
    plan: PlannedDispatch,
    reconciliation: TransportReconciliation,
    dispatches: Vec<TransportDispatch>,
}

impl ReceiverTransport for FakeTransport {
    type Error = FakeTransportError;

    fn current_generation(
        &self,
        receiver_id: &hfx_domain::ReceiverId,
    ) -> Option<hfx_domain::GenerationId> {
        (receiver_id == &self.receiver_id).then_some(self.generation_id)
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        self.reconciliation
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.dispatches.push(dispatch.clone());
        match &self.plan {
            PlannedDispatch::Receipt(receipt) => Ok(*receipt),
            PlannedDispatch::Failure(error) => Err(error.clone()),
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

#[derive(Clone, Copy, Debug)]
struct FakeProfiles {
    supported: bool,
    current: bool,
    readiness: DeviceWriteReadiness,
}

impl ProfileRegistry for FakeProfiles {
    fn supports(&self, _resource: &hfx_protocol::ResourceKey) -> bool {
        self.supported
    }

    fn receiver_profile(
        &self,
        receiver_id: &hfx_domain::ReceiverId,
        generation_id: hfx_domain::GenerationId,
    ) -> Option<QualifiedReceiverProfile> {
        (self.supported && receiver_id.as_str() == "receiver-1" && generation_id == generation(1))
            .then(|| QualifiedReceiverProfile {
                profile_id: receiver_profile_id(),
                profile_digest: if self.current {
                    receiver_profile_digest()
                } else {
                    text(&"c".repeat(64))
                },
            })
    }

    fn device_profile(
        &self,
        resource: &hfx_protocol::ResourceKey,
    ) -> Option<QualifiedDeviceProfile> {
        (self.supported
            && resource.receiver_id.as_str() == "receiver-1"
            && resource.generation_id == generation(1)
            && resource.kind == ResourceKind::Lighting)
            .then(|| QualifiedDeviceProfile {
                profile_id: device_profile_id(resource.device_id.as_str()),
                profile_digest: if self.current {
                    device_profile_digest()
                } else {
                    text(&"d".repeat(64))
                },
                application_slot_count: LedCount::try_from(1_u16).expect("test LED count is valid"),
            })
    }
}

impl DeviceStateAuthority for FakeProfiles {
    fn write_readiness(&self, _resource: &hfx_protocol::ResourceKey) -> DeviceWriteReadiness {
        self.readiness
    }
}

fn fake_profiles(supported: bool) -> FakeProfiles {
    FakeProfiles {
        supported,
        current: true,
        readiness: DeviceWriteReadiness::Ready,
    }
}

#[derive(Debug)]
struct FakeSink {
    delivery: EventDelivery,
    attempted: Vec<BridgeEvent>,
}

impl EventSink for FakeSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.attempted.push(event.clone());
        self.delivery
    }
}

fn request(
    id: &str,
    class: TransactionClass,
    deadline: u64,
    device_count: u32,
) -> TransactionRequest {
    let devices = (0..device_count)
        .map(|index| format!("device-{index}"))
        .collect::<Vec<_>>();
    let device_refs = devices.iter().map(String::as_str).collect::<Vec<_>>();
    transaction_request(id, class, deadline, &device_refs)
}

fn binding(nonce: u64) -> SubmissionBinding {
    SubmissionBinding {
        session_id: text("session-1"),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("epoch is valid"),
        dispatch_nonce: DispatchNonce::try_from(nonce).expect("nonce is valid"),
    }
}

fn sessions() -> FakeSessions {
    FakeSessions {
        session_id: text("session-1"),
        epoch: AuthorizationEpoch::try_from(1_u64).expect("epoch is valid"),
        live: true,
    }
}

fn leases(resources: Vec<hfx_protocol::ResourceKey>) -> LeaseManager {
    let mut leases = LeaseManager::new(8, 16).expect("lease bounds are valid");
    leases
        .acquire(
            lease_request("lease-request-1", "client-1", resources),
            text("lease-1"),
            time(0),
        )
        .expect("lease request is accepted");
    leases
}

fn transport(plan: PlannedDispatch) -> FakeTransport {
    FakeTransport {
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        plan,
        reconciliation: TransportReconciliation::NotObserved,
        dispatches: Vec::new(),
    }
}

fn successful_receipt(frames: u16) -> TransportReceipt {
    TransportReceipt {
        terminal: TransportTerminal::Delivered,
        delivered_frames: DeliveredFrameCount::try_from(frames).expect("frame count is valid"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry_safe: false,
        device_application: DeviceApplicationState::Unverified,
    }
}

fn events() -> BoundedEventLog {
    BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("stream epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("revision is valid"),
        32,
    )
    .expect("event log bounds are valid")
}

fn sink() -> FakeSink {
    FakeSink {
        delivery: EventDelivery::Accepted,
        attempted: Vec::new(),
    }
}

#[test]
fn exact_replay_never_queues_or_dispatches_twice() {
    let request = request("one", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();

    let first = coordinator
        .submit(
            request.clone(),
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("request is admitted");
    assert!(matches!(
        first,
        SubmissionResult::Queued(ref progress)
            if progress.admission == QueueAdmission::Enqueued
    ));
    let queued_replay = coordinator
        .submit(
            request.clone(),
            binding(2),
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("exact retry replays progress");
    assert!(matches!(
        queued_replay,
        SubmissionResult::Replay(TransactionResult::Progress(_))
    ));
    assert_eq!(coordinator.queued_len(), 1);

    let dispatched = coordinator
        .dispatch_next(
            time(3),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("dispatch completes");
    let terminal = dispatched.completed.expect("one transaction completes");
    assert_eq!(terminal.state, TransactionState::Succeeded);
    assert_eq!(terminal.terminal_sequence.get(), 1);
    assert_eq!(transport.dispatches.len(), 1);
    let hardware_dispatch = &transport.dispatches[0];
    assert_eq!(hardware_dispatch.receiver_profile_id, receiver_profile_id());
    assert_eq!(
        hardware_dispatch.receiver_profile_digest,
        receiver_profile_digest()
    );
    assert_eq!(hardware_dispatch.device_profiles.len(), 1);
    assert_eq!(hardware_dispatch.request_digest.as_str().len(), 64);

    let terminal_replay = coordinator
        .submit(
            request,
            binding(3),
            time(4),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("exact retry replays terminal outcome");
    assert!(matches!(
        terminal_replay,
        SubmissionResult::Replay(TransactionResult::Terminal(ref replayed))
            if replayed == &terminal.terminal
    ));
    assert_eq!(transport.dispatches.len(), 1);
    assert_eq!(sink.attempted.len(), 1);
}

#[test]
fn admission_rejects_missing_authority_before_any_transport() {
    let base = request("base", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(base.resources.clone());
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut events = events();
    let mut sink = sink();

    let mut denied_sessions = sessions();
    denied_sessions.live = false;
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    assert_eq!(
        coordinator.submit(
            request("session", TransactionClass::StaticLighting, 100, 1),
            binding(1),
            time(1),
            &denied_sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::SessionRevoked)
    );

    let live_sessions = sessions();
    let mut unowned = request("owner", TransactionClass::StaticLighting, 100, 1);
    unowned.lease_id = text("another-lease");
    assert_eq!(
        coordinator.submit(
            unowned,
            binding(2),
            time(1),
            &live_sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::OwnershipDenied)
    );

    transport.generation_id = generation(2);
    assert_eq!(
        coordinator.submit(
            request("stale", TransactionClass::StaticLighting, 100, 1),
            binding(3),
            time(1),
            &live_sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::StaleGeneration)
    );
    transport.generation_id = generation(1);
    assert_eq!(
        coordinator.submit(
            request("profile", TransactionClass::StaticLighting, 100, 1),
            binding(4),
            time(1),
            &live_sessions,
            &leases,
            &fake_profiles(false),
            &fake_profiles(false),
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::UnsupportedResource)
    );
    assert_eq!(coordinator.queued_len(), 0);
    assert!(transport.dispatches.is_empty());
    assert!(sink.attempted.is_empty());
}

#[test]
fn device_readiness_blocks_admission_and_is_rechecked_before_dispatch() {
    let request = request("readiness", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let ready = fake_profiles(true);
    let sleeping = FakeProfiles {
        readiness: DeviceWriteReadiness::Sleeping,
        ..ready
    };
    let unavailable = FakeProfiles {
        readiness: DeviceWriteReadiness::Unavailable,
        ..ready
    };
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut events = events();
    let mut sink = sink();

    let mut rejected = TransactionCoordinator::new(4).expect("capacity is valid");
    assert_eq!(
        rejected.submit(
            request.clone(),
            binding(1),
            time(1),
            &sessions,
            &leases,
            &ready,
            &sleeping,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::DeviceNotReady(
            DeviceWriteReadiness::Sleeping
        ))
    );
    assert_eq!(rejected.queued_len(), 0);

    let mut queued = TransactionCoordinator::new(4).expect("capacity is valid");
    queued
        .submit(
            request,
            binding(2),
            time(1),
            &sessions,
            &leases,
            &ready,
            &ready,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("ready device is queued");
    let terminal = queued
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &ready,
            &unavailable,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("readiness loss becomes a terminal outcome")
        .completed
        .expect("one queued transaction is completed");
    assert_eq!(terminal.state, TransactionState::Revoked);
    assert_eq!(
        terminal.error_kind,
        Some(ProtocolErrorKind::TransportFailure)
    );
    assert!(!terminal.live_write_executed);
    assert_eq!(terminal.side_effect_certainty, SideEffectCertainty::None);
    assert!(transport.dispatches.is_empty());
}

#[test]
fn effect_coalescing_is_explicit_but_stable_queue_order_is_strict() {
    let first = request("old", TransactionClass::EffectFrame, 100, 1);
    let leases = leases(first.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut coordinator = TransactionCoordinator::new(2).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();
    coordinator
        .submit(
            first,
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("first effect is queued");
    let replacement = coordinator
        .submit(
            request("new", TransactionClass::EffectFrame, 100, 1),
            binding(2),
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("new effect coalesces the unsent frame");
    assert!(matches!(
        replacement,
        SubmissionResult::Queued(ref progress)
            if progress.admission == QueueAdmission::Coalesced
    ));
    assert!(matches!(
        coordinator.outcome(&text("client-1"), &text("transaction-old")),
        OutcomeLookup::Retained(TransactionResult::Terminal(terminal))
            if terminal.state == TransactionState::Superseded
                && terminal.superseded_by.as_ref().is_some_and(
                    |id| id.as_str() == "transaction-new"
                )
                && !terminal.automatic_retry
    ));

    let mut strict = TransactionCoordinator::new(1).expect("capacity is valid");
    strict
        .submit(
            request("stable-1", TransactionClass::StaticLighting, 100, 1),
            binding(3),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("first stable request is queued");
    assert_eq!(
        strict.submit(
            request("stable-2", TransactionClass::StaticLighting, 100, 1),
            binding(4),
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::Queue(
            TransactionQueueError::Full
        ))
    );
}

#[test]
fn dispatch_rechecks_session_and_generation_before_hardware() {
    let first_request = request("session", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(first_request.resources.clone());
    let mut sessions = sessions();
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();
    coordinator
        .submit(
            first_request,
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("request is queued");
    sessions.live = false;
    let revoked = coordinator
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("revocation is terminally recorded")
        .completed
        .expect("one transaction is revoked");
    assert_eq!(revoked.state, TransactionState::Revoked);
    assert_eq!(
        revoked.error_kind,
        Some(ProtocolErrorKind::OwnershipConflict)
    );
    assert!(revoked.automatic_retry);
    assert!(transport.dispatches.is_empty());

    let second = request("generation", TransactionClass::StaticLighting, 100, 1);
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    sessions.live = true;
    coordinator
        .submit(
            second,
            binding(2),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("second request is queued");
    transport.generation_id = generation(2);
    let stale = coordinator
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("stale generation is terminally recorded")
        .completed
        .expect("one transaction is revoked");
    assert_eq!(stale.state, TransactionState::Revoked);
    assert_eq!(stale.error_kind, Some(ProtocolErrorKind::StaleGeneration));
    assert!(transport.dispatches.is_empty());
}

#[test]
fn profile_bindings_are_checked_at_admission_and_immediately_before_dispatch() {
    let request = request("profile", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut events = events();
    let mut sink = sink();
    let stale_profiles = FakeProfiles {
        supported: true,
        current: false,
        readiness: DeviceWriteReadiness::Ready,
    };
    let mut rejected = TransactionCoordinator::new(4).expect("capacity is valid");
    assert_eq!(
        rejected.submit(
            request.clone(),
            binding(1),
            time(1),
            &sessions,
            &leases,
            &stale_profiles,
            &stale_profiles,
            &transport,
            &mut events,
            &mut sink,
        ),
        Err(TransactionCoordinatorError::ProfileBindingChanged)
    );

    let mut queued = TransactionCoordinator::new(4).expect("capacity is valid");
    queued
        .submit(
            request,
            binding(2),
            time(1),
            &sessions,
            &leases,
            &fake_profiles(true),
            &fake_profiles(true),
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("current binding is queued");
    let mut transport = transport;
    let terminal = queued
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &stale_profiles,
            &stale_profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("profile drift is terminally recorded")
        .completed
        .expect("queued transaction is revoked");
    assert_eq!(terminal.state, TransactionState::Revoked);
    assert_eq!(
        terminal.error_kind,
        Some(ProtocolErrorKind::StaleGeneration)
    );
    assert!(transport.dispatches.is_empty());
}

#[test]
fn retained_transport_outcome_reconciles_without_a_second_write() {
    let request = request("retained", TransactionClass::Restore, 100, 1);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    transport.reconciliation = TransportReconciliation::Retained(successful_receipt(1));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();
    coordinator
        .submit(
            request,
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("restore is queued");
    let terminal = coordinator
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("retained outcome reconciles")
        .completed
        .expect("transaction reaches terminal state");
    assert_eq!(terminal.state, TransactionState::Succeeded);
    assert!(transport.dispatches.is_empty());
}

#[test]
fn ambiguous_transport_history_fails_closed_without_replay() {
    for (suffix, reconciliation, expected_error) in [
        (
            "evicted",
            TransportReconciliation::Evicted,
            ProtocolErrorKind::OutcomeEvicted,
        ),
        (
            "unavailable",
            TransportReconciliation::Unavailable,
            ProtocolErrorKind::OutcomeUnknown,
        ),
        (
            "conflict",
            TransportReconciliation::Conflict,
            ProtocolErrorKind::OutcomeUnknown,
        ),
    ] {
        let request = request(suffix, TransactionClass::Restore, 100, 1);
        let leases = leases(request.resources.clone());
        let sessions = sessions();
        let profiles = fake_profiles(true);
        let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
        transport.reconciliation = reconciliation;
        let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
        let mut events = events();
        let mut sink = sink();
        coordinator
            .submit(
                request,
                binding(1),
                time(1),
                &sessions,
                &leases,
                &profiles,
                &profiles,
                &transport,
                &mut events,
                &mut sink,
            )
            .expect("restore is queued");
        let terminal = coordinator
            .dispatch_next(
                time(2),
                &sessions,
                &leases,
                &profiles,
                &profiles,
                &mut transport,
                &mut events,
                &mut sink,
            )
            .expect("uncertain history becomes terminal")
            .completed
            .expect("transaction reaches terminal state");
        assert_eq!(terminal.state, TransactionState::Failed);
        assert_eq!(terminal.error_kind, Some(expected_error));
        assert_eq!(
            terminal.side_effect_certainty,
            SideEffectCertainty::Possible
        );
        assert!(terminal.live_write_executed);
        assert!(!terminal.automatic_retry);
        assert!(transport.dispatches.is_empty());
    }
}

#[test]
fn partial_transport_failure_is_conservative_and_never_auto_retried() {
    let request = request("partial", TransactionClass::StaticLighting, 100, 2);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Failure(FakeTransportError(
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("frame count is valid"),
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: DeviceApplicationState::Unverified,
        },
    )));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = FakeSink {
        delivery: EventDelivery::Full,
        attempted: Vec::new(),
    };
    coordinator
        .submit(
            request,
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("request is queued");
    let terminal = coordinator
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("failure is terminally recorded")
        .completed
        .expect("one transaction completes");
    assert_eq!(terminal.state, TransactionState::Failed);
    assert_eq!(terminal.delivered_frames.get(), 1);
    assert_eq!(terminal.side_effect_certainty, SideEffectCertainty::Partial);
    assert!(terminal.live_write_executed);
    assert!(!terminal.automatic_retry);
    assert_eq!(
        terminal.error_kind,
        Some(ProtocolErrorKind::TransportFailure)
    );
    assert_eq!(sink.attempted.len(), 1);
}

#[test]
fn impossible_transport_counts_fail_internally_instead_of_becoming_success() {
    let request = request("invalid-report", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(request.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let mut transport = transport(PlannedDispatch::Receipt(successful_receipt(2)));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();
    coordinator
        .submit(
            request,
            binding(1),
            time(1),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &transport,
            &mut events,
            &mut sink,
        )
        .expect("request is queued");
    let terminal = coordinator
        .dispatch_next(
            time(2),
            &sessions,
            &leases,
            &profiles,
            &profiles,
            &mut transport,
            &mut events,
            &mut sink,
        )
        .expect("invalid report becomes a terminal failure")
        .completed
        .expect("one transaction completes");
    assert_eq!(terminal.state, TransactionState::Failed);
    assert_eq!(
        terminal.error_kind,
        Some(ProtocolErrorKind::InternalFailure)
    );
    assert_eq!(terminal.delivered_frames.get(), 1);
    assert!(!terminal.automatic_retry);
}

#[test]
fn explicit_session_invalidation_terminally_accounts_for_all_removed_work() {
    let first = request("one", TransactionClass::StaticLighting, 100, 1);
    let leases = leases(first.resources.clone());
    let sessions = sessions();
    let profiles = fake_profiles(true);
    let transport = transport(PlannedDispatch::Receipt(successful_receipt(1)));
    let mut coordinator = TransactionCoordinator::new(4).expect("capacity is valid");
    let mut events = events();
    let mut sink = sink();
    for (id, nonce) in [("one", 1), ("two", 2)] {
        coordinator
            .submit(
                request(id, TransactionClass::StaticLighting, 100, 1),
                binding(nonce),
                time(1),
                &sessions,
                &leases,
                &profiles,
                &profiles,
                &transport,
                &mut events,
                &mut sink,
            )
            .expect("request is queued");
    }
    let revoked = coordinator
        .invalidate_session(&text("session-1"), &mut events, &mut sink)
        .expect("session invalidation is recorded");
    assert_eq!(revoked.len(), 2);
    assert!(revoked.iter().all(|terminal| {
        terminal.state == TransactionState::Revoked
            && terminal.error_kind == Some(ProtocolErrorKind::OwnershipConflict)
            && !terminal.live_write_executed
    }));
    assert_eq!(coordinator.queued_len(), 0);
    assert_eq!(sink.attempted.len(), 2);
}
