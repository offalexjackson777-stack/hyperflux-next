// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    BoundedEventLog, BoundedOutcomeJournal, BoundedTransactionQueue, DeviceStateAuthority,
    EventDraft, EventLogError, EventSink, LeaseManager, OutcomeJournalError, OutcomeLookup,
    ProfileRegistry, QueuedTransaction, ReceiverTransport, RequestDigestError, RequestReplay,
    SessionAuthority, SubmissionBinding, TransactionMachine, TransactionQueueError,
    TransactionTransitionError, TransportDispatch, TransportFailure, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal, canonical_request_digest,
};
use hfx_domain::{
    DeliveredFrameCount, DeviceApplicationState, DeviceWriteReadiness, EventKind, FrameCount,
    GenerationId, MonotonicMs, ProtocolErrorKind, QueueAdmission, ReceiverId, ResourceKind,
    SessionId, SideEffectCertainty, TransactionId, TransactionState,
};
use hfx_protocol::{
    TransactionProgress, TransactionRequest, TransactionResult, TransactionTerminal,
};
use std::fmt;
use std::ops::Deref;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SubmissionResult {
    Queued(TransactionProgress),
    Replay(TransactionResult),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompletedTransaction {
    pub request: TransactionRequest,
    pub terminal: TransactionTerminal,
}

impl Deref for CompletedTransaction {
    type Target = TransactionTerminal;

    fn deref(&self) -> &Self::Target {
        &self.terminal
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DispatchResult {
    pub expired: Vec<TransactionTerminal>,
    pub completed: Option<CompletedTransaction>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TransactionCoordinatorError {
    InvalidCapacity,
    Digest(RequestDigestError),
    RequestIdReused,
    TransactionIdReused,
    OutcomeEvicted(TransactionId),
    SessionRevoked,
    OwnershipDenied,
    StaleGeneration,
    UnsupportedResource,
    ProfileBindingChanged,
    DeviceNotReady(DeviceWriteReadiness),
    Queue(TransactionQueueError),
    Outcome(OutcomeJournalError),
    Event(EventLogError),
    Transition(TransactionTransitionError),
    FrameCount,
    TransactionNotQueued(TransactionId),
}

impl fmt::Display for TransactionCoordinatorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "transaction coordinator capacity is invalid",
            Self::Digest(_) => "transaction request cannot be given a canonical identity",
            Self::RequestIdReused => "request identity was reused with different content",
            Self::TransactionIdReused => "transaction identity is already known",
            Self::OutcomeEvicted(_) => "the exact prior outcome was evicted from bounded history",
            Self::SessionRevoked => "writer session no longer authorizes this transaction",
            Self::OwnershipDenied => "the client does not own every requested resource",
            Self::StaleGeneration => "the transaction targets a stale receiver generation",
            Self::UnsupportedResource => "the active profile does not support every resource",
            Self::ProfileBindingChanged => {
                "the qualified receiver or device profile binding changed"
            }
            Self::DeviceNotReady(_) => "one or more requested devices are not ready for writes",
            Self::Queue(_) => "the bounded transaction queue rejected the request",
            Self::Outcome(_) => "the bounded outcome journal rejected the update",
            Self::Event(_) => "the canonical event stream rejected the terminal event",
            Self::Transition(_) => "the transaction attempted an invalid state transition",
            Self::FrameCount => "the transaction frame count cannot be represented",
            Self::TransactionNotQueued(_) => "the exact transaction is not queued",
        })
    }
}

impl std::error::Error for TransactionCoordinatorError {}

impl From<RequestDigestError> for TransactionCoordinatorError {
    fn from(value: RequestDigestError) -> Self {
        Self::Digest(value)
    }
}

impl From<TransactionQueueError> for TransactionCoordinatorError {
    fn from(value: TransactionQueueError) -> Self {
        Self::Queue(value)
    }
}

impl From<OutcomeJournalError> for TransactionCoordinatorError {
    fn from(value: OutcomeJournalError) -> Self {
        Self::Outcome(value)
    }
}

impl From<EventLogError> for TransactionCoordinatorError {
    fn from(value: EventLogError) -> Self {
        Self::Event(value)
    }
}

impl From<TransactionTransitionError> for TransactionCoordinatorError {
    fn from(value: TransactionTransitionError) -> Self {
        Self::Transition(value)
    }
}

#[derive(Clone, Debug)]
pub struct TransactionCoordinator {
    queue: BoundedTransactionQueue,
    outcomes: BoundedOutcomeJournal,
}

impl TransactionCoordinator {
    /// Creates one coordinator with equal pending-work and retained-outcome bounds.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is zero or exceeds the shared protocol bound.
    pub fn new(capacity: usize) -> Result<Self, TransactionCoordinatorError> {
        if !(1..=4096).contains(&capacity) {
            return Err(TransactionCoordinatorError::InvalidCapacity);
        }
        Ok(Self {
            queue: BoundedTransactionQueue::new(capacity)?,
            outcomes: BoundedOutcomeJournal::new(capacity)?,
        })
    }

    /// Validates and atomically admits one generation- and ownership-bound request.
    ///
    /// # Errors
    ///
    /// Returns a typed rejection without beginning transport when request identity,
    /// session authority, ownership, generation, capability, deadline, or bounds fail.
    #[allow(clippy::too_many_arguments)]
    pub fn submit<A, D, P, T, S>(
        &mut self,
        request: TransactionRequest,
        binding: SubmissionBinding,
        now: MonotonicMs,
        sessions: &A,
        leases: &LeaseManager,
        profiles: &P,
        devices: &D,
        transport: &T,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<SubmissionResult, TransactionCoordinatorError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        S: EventSink,
    {
        if !sessions.authorizes(&binding.session_id, binding.authorization_epoch) {
            return Err(TransactionCoordinatorError::SessionRevoked);
        }
        let digest = canonical_request_digest(&request)?;
        match self
            .outcomes
            .replay(&request.client_id, &request.request_id, &digest)
        {
            RequestReplay::Retained(result) => {
                return Ok(SubmissionResult::Replay(result.clone()));
            }
            RequestReplay::Evicted(transaction_id) => {
                return Err(TransactionCoordinatorError::OutcomeEvicted(
                    transaction_id.clone(),
                ));
            }
            RequestReplay::Conflict => {
                return Err(TransactionCoordinatorError::RequestIdReused);
            }
            RequestReplay::Unknown => {}
        }
        if !matches!(
            self.outcomes
                .lookup(&request.client_id, &request.transaction_id),
            OutcomeLookup::Unknown
        ) {
            return Err(TransactionCoordinatorError::TransactionIdReused);
        }
        let mut machine = TransactionMachine::default();
        machine.advance(TransactionState::Validated)?;
        if !leases.owns(
            &request.client_id,
            &request.lease_id,
            &request.resources,
            now,
        ) {
            return Err(TransactionCoordinatorError::OwnershipDenied);
        }
        machine.advance(TransactionState::OwnershipBound)?;
        if transport.current_generation(&request.receiver_id) != Some(request.generation_id) {
            return Err(TransactionCoordinatorError::StaleGeneration);
        }
        machine.advance(TransactionState::GenerationBound)?;
        if !request
            .resources
            .iter()
            .all(|resource| profiles.supports(resource))
        {
            return Err(TransactionCoordinatorError::UnsupportedResource);
        }
        if !profiles_match(&request, profiles) {
            return Err(TransactionCoordinatorError::ProfileBindingChanged);
        }
        if let Some(readiness) = first_unready(&request, devices) {
            return Err(TransactionCoordinatorError::DeviceNotReady(readiness));
        }

        let client_id = request.client_id.clone();
        let transaction_id = request.transaction_id.clone();
        let mut queued = QueuedTransaction {
            request,
            request_digest: digest,
            session_id: binding.session_id,
            authorization_epoch: binding.authorization_epoch,
            dispatch_nonce: binding.dispatch_nonce,
            admission: QueueAdmission::Enqueued,
        };
        let decision = self.queue.admit(queued.clone(), now)?;
        queued.admission = decision.admission;
        machine.advance(TransactionState::Queued)?;

        if let Some(superseded) = decision.superseded {
            self.finish_unsent(
                superseded,
                TransactionState::Superseded,
                None,
                Some(transaction_id.clone()),
                events,
                sink,
            )?;
        }
        let progress = progress(&queued, TransactionState::Queued)?;
        if let Err(error) = self
            .outcomes
            .record(client_id, TransactionResult::Progress(progress.clone()))
        {
            let _ = self.queue.remove_transaction(&transaction_id);
            return Err(error.into());
        }
        Ok(SubmissionResult::Queued(progress))
    }

    /// Dispatches at most one queued transaction after rechecking every authority.
    ///
    /// # Errors
    ///
    /// Returns an error only for internal bounded-state or event-journal failures;
    /// expected transport failures become immutable terminal transaction outcomes.
    #[allow(clippy::too_many_arguments)]
    pub fn dispatch_next<A, D, P, T, S>(
        &mut self,
        now: MonotonicMs,
        sessions: &A,
        leases: &LeaseManager,
        profiles: &P,
        devices: &D,
        transport: &mut T,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<DispatchResult, TransactionCoordinatorError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        S: EventSink,
    {
        let decision = self.queue.take_next(now);
        self.dispatch_decision(
            decision, now, sessions, leases, profiles, devices, transport, events, sink,
        )
    }

    /// Dispatches one exact queued transaction after rechecking every authority.
    ///
    /// Other live queue entries remain queued. This is used by durable workflows
    /// that must persist an applying checkpoint before one named hardware write.
    ///
    /// # Errors
    ///
    /// Returns `TransactionNotQueued` when the target is neither live nor among
    /// the elapsed entries completed during this call. Other failures match
    /// [`Self::dispatch_next`].
    #[allow(clippy::too_many_arguments)]
    pub fn dispatch_transaction<A, D, P, T, S>(
        &mut self,
        transaction_id: &TransactionId,
        now: MonotonicMs,
        sessions: &A,
        leases: &LeaseManager,
        profiles: &P,
        devices: &D,
        transport: &mut T,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<DispatchResult, TransactionCoordinatorError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        S: EventSink,
    {
        let decision = self.queue.take_transaction(transaction_id, now);
        let target_expired = decision
            .expired
            .iter()
            .any(|queued| &queued.request.transaction_id == transaction_id);
        if decision.next.is_none() && !target_expired {
            return Err(TransactionCoordinatorError::TransactionNotQueued(
                transaction_id.clone(),
            ));
        }
        self.dispatch_decision(
            decision, now, sessions, leases, profiles, devices, transport, events, sink,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn dispatch_decision<A, D, P, T, S>(
        &mut self,
        decision: crate::DequeueDecision,
        now: MonotonicMs,
        sessions: &A,
        leases: &LeaseManager,
        profiles: &P,
        devices: &D,
        transport: &mut T,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<DispatchResult, TransactionCoordinatorError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        S: EventSink,
    {
        let mut expired = Vec::with_capacity(decision.expired.len());
        for queued in decision.expired {
            expired.push(self.finish_unsent(
                queued,
                TransactionState::Revoked,
                Some(ProtocolErrorKind::DeadlineExceeded),
                None,
                events,
                sink,
            )?);
        }
        let Some(queued) = decision.next else {
            return Ok(DispatchResult {
                expired,
                completed: None,
            });
        };

        let preflight_error =
            dispatch_preflight(&queued, now, sessions, leases, profiles, devices, transport);
        if let Some(error_kind) = preflight_error {
            let completed = self.finish_unsent_completion(
                queued,
                TransactionState::Revoked,
                Some(error_kind),
                None,
                events,
                sink,
            )?;
            return Ok(DispatchResult {
                expired,
                completed: Some(completed),
            });
        }

        let mut machine = queued_machine()?;
        machine.advance(TransactionState::Sent)?;
        let sent = progress(&queued, TransactionState::Sent)?;
        self.outcomes.record(
            queued.request.client_id.clone(),
            TransactionResult::Progress(sent),
        )?;
        let dispatch = TransportDispatch {
            session_id: queued.session_id.clone(),
            authorization_epoch: queued.authorization_epoch,
            dispatch_nonce: queued.dispatch_nonce,
            receiver_id: queued.request.receiver_id.clone(),
            generation_id: queued.request.generation_id,
            transaction_id: queued.request.transaction_id.clone(),
            request_digest: queued.request_digest.clone(),
            receiver_profile_id: queued.request.receiver_profile_id.clone(),
            receiver_profile_digest: queued.request.receiver_profile_digest.clone(),
            device_profiles: queued.request.device_profiles.clone(),
            frames: queued.request.frames.clone(),
        };
        let declared = declared_frames(&queued)?;
        let (state, error_kind, facts) = reconcile_or_dispatch(transport, &dispatch, declared)?;
        machine.advance(state)?;
        let completed = self.finish(queued, state, error_kind, None, facts, events, sink)?;
        Ok(DispatchResult {
            expired,
            completed: Some(completed),
        })
    }

    /// Revokes all unsent work bound to a retired receiver generation.
    ///
    /// # Errors
    ///
    /// Returns an error if terminal outcome or event recording fails.
    pub fn invalidate_generation<S: EventSink>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<Vec<TransactionTerminal>, TransactionCoordinatorError> {
        let removed = self.queue.invalidate_generation(receiver_id, generation_id);
        self.finish_removed(removed, ProtocolErrorKind::StaleGeneration, events, sink)
    }

    /// Revokes all unsent work belonging to one closed writer session.
    ///
    /// # Errors
    ///
    /// Returns an error if terminal outcome or event recording fails.
    pub fn invalidate_session<S: EventSink>(
        &mut self,
        session_id: &SessionId,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<Vec<TransactionTerminal>, TransactionCoordinatorError> {
        let removed = self.queue.invalidate_session(session_id);
        self.finish_removed(removed, ProtocolErrorKind::OwnershipConflict, events, sink)
    }

    /// Revokes one exact unsent transaction with a retry-safe terminal outcome.
    ///
    /// This never invokes transport and leaves unrelated queue entries intact.
    ///
    /// # Errors
    ///
    /// Returns `TransactionNotQueued` when the target is absent, or a typed
    /// outcome/event failure while recording the immutable cancellation.
    pub fn cancel_transaction<S: EventSink>(
        &mut self,
        transaction_id: &TransactionId,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<TransactionTerminal, TransactionCoordinatorError> {
        let queued = self
            .queue
            .remove_transaction(transaction_id)
            .ok_or_else(|| {
                TransactionCoordinatorError::TransactionNotQueued(transaction_id.clone())
            })?;
        self.finish_unsent(queued, TransactionState::Revoked, None, None, events, sink)
    }

    #[must_use]
    pub fn outcome(
        &self,
        client_id: &hfx_domain::ClientId,
        transaction_id: &TransactionId,
    ) -> OutcomeLookup<'_> {
        self.outcomes.lookup(client_id, transaction_id)
    }

    #[must_use]
    pub fn queued_len(&self) -> usize {
        self.queue.len()
    }

    fn finish_removed<S: EventSink>(
        &mut self,
        removed: Vec<QueuedTransaction>,
        error_kind: ProtocolErrorKind,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<Vec<TransactionTerminal>, TransactionCoordinatorError> {
        removed
            .into_iter()
            .map(|queued| {
                self.finish_unsent(
                    queued,
                    TransactionState::Revoked,
                    Some(error_kind),
                    None,
                    events,
                    sink,
                )
            })
            .collect()
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_unsent<S: EventSink>(
        &mut self,
        queued: QueuedTransaction,
        state: TransactionState,
        error_kind: Option<ProtocolErrorKind>,
        superseded_by: Option<TransactionId>,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<TransactionTerminal, TransactionCoordinatorError> {
        self.finish_unsent_completion(queued, state, error_kind, superseded_by, events, sink)
            .map(|completed| completed.terminal)
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_unsent_completion<S: EventSink>(
        &mut self,
        queued: QueuedTransaction,
        state: TransactionState,
        error_kind: Option<ProtocolErrorKind>,
        superseded_by: Option<TransactionId>,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<CompletedTransaction, TransactionCoordinatorError> {
        let mut machine = queued_machine()?;
        machine.advance(state)?;
        self.finish(
            queued,
            state,
            error_kind,
            superseded_by,
            TransportFailureFacts {
                delivered_frames: zero_delivered()?,
                side_effect_certainty: SideEffectCertainty::None,
                live_write_executed: false,
                automatic_retry_safe: true,
                device_application: DeviceApplicationState::Unverified,
            },
            events,
            sink,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn finish<S: EventSink>(
        &mut self,
        queued: QueuedTransaction,
        state: TransactionState,
        error_kind: Option<ProtocolErrorKind>,
        superseded_by: Option<TransactionId>,
        facts: TransportFailureFacts,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<CompletedTransaction, TransactionCoordinatorError> {
        let declared_frames = declared_frames(&queued)?;
        let request = queued.request;
        let event = events.append(EventDraft {
            kind: EventKind::TransactionCompleted,
            receiver_id: Some(request.receiver_id.clone()),
            generation_id: Some(request.generation_id),
            device_id: None,
            lease_id: Some(request.lease_id.clone()),
            transaction_id: Some(request.transaction_id.clone()),
            finding_id: None,
        })?;
        let _ = sink.try_emit(&event);
        let client_id = request.client_id.clone();
        let terminal = TransactionTerminal {
            request_id: request.request_id.clone(),
            request_digest: queued.request_digest.clone(),
            transaction_id: request.transaction_id.clone(),
            receiver_id: request.receiver_id.clone(),
            generation_id: request.generation_id,
            state,
            declared_frames,
            delivered_frames: facts.delivered_frames,
            side_effect_certainty: facts.side_effect_certainty,
            live_write_executed: facts.live_write_executed,
            automatic_retry: state != TransactionState::Superseded && retry_is_safe(facts),
            device_application: facts.device_application,
            terminal_sequence: event.sequence,
            error_kind,
            superseded_by,
        };
        self.outcomes
            .record(client_id, TransactionResult::Terminal(terminal.clone()))?;
        Ok(CompletedTransaction { request, terminal })
    }
}

fn progress(
    queued: &QueuedTransaction,
    state: TransactionState,
) -> Result<TransactionProgress, TransactionCoordinatorError> {
    Ok(TransactionProgress {
        request_id: queued.request.request_id.clone(),
        request_digest: queued.request_digest.clone(),
        transaction_id: queued.request.transaction_id.clone(),
        receiver_id: queued.request.receiver_id.clone(),
        generation_id: queued.request.generation_id,
        state,
        admission: queued.admission,
        declared_frames: declared_frames(queued)?,
        delivered_frames: zero_delivered()?,
        side_effect_certainty: SideEffectCertainty::None,
        live_write_executed: false,
    })
}

fn queued_machine() -> Result<TransactionMachine, TransactionCoordinatorError> {
    let mut machine = TransactionMachine::default();
    for state in [
        TransactionState::Validated,
        TransactionState::OwnershipBound,
        TransactionState::GenerationBound,
        TransactionState::Queued,
    ] {
        machine.advance(state)?;
    }
    Ok(machine)
}

fn profiles_match<P: ProfileRegistry>(request: &TransactionRequest, profiles: &P) -> bool {
    let Some(receiver) = profiles.receiver_profile(&request.receiver_id, request.generation_id)
    else {
        return false;
    };
    if receiver.profile_id != request.receiver_profile_id
        || receiver.profile_digest != request.receiver_profile_digest
    {
        return false;
    }
    request.device_profiles.iter().all(|declared| {
        let resource = hfx_protocol::ResourceKey {
            receiver_id: request.receiver_id.clone(),
            generation_id: request.generation_id,
            device_id: declared.device_id.clone(),
            kind: ResourceKind::Lighting,
        };
        profiles.device_profile(&resource).is_some_and(|current| {
            current.profile_id == declared.profile_id
                && current.profile_digest == declared.profile_digest
                && current.application_slot_count == declared.application_slot_count
        })
    })
}

fn dispatch_preflight<A, D, P, T>(
    queued: &QueuedTransaction,
    now: MonotonicMs,
    sessions: &A,
    leases: &LeaseManager,
    profiles: &P,
    devices: &D,
    transport: &T,
) -> Option<ProtocolErrorKind>
where
    A: SessionAuthority,
    D: DeviceStateAuthority,
    P: ProfileRegistry,
    T: ReceiverTransport,
{
    if !sessions.authorizes(&queued.session_id, queued.authorization_epoch)
        || !leases.owns(
            &queued.request.client_id,
            &queued.request.lease_id,
            &queued.request.resources,
            now,
        )
    {
        Some(ProtocolErrorKind::OwnershipConflict)
    } else if transport.current_generation(&queued.request.receiver_id)
        != Some(queued.request.generation_id)
    {
        Some(ProtocolErrorKind::StaleGeneration)
    } else if !queued
        .request
        .resources
        .iter()
        .all(|resource| profiles.supports(resource))
    {
        Some(ProtocolErrorKind::UnsupportedFeature)
    } else if !profiles_match(&queued.request, profiles) {
        Some(ProtocolErrorKind::StaleGeneration)
    } else if first_unready(&queued.request, devices).is_some() {
        Some(ProtocolErrorKind::TransportFailure)
    } else {
        None
    }
}

fn first_unready<D: DeviceStateAuthority>(
    request: &TransactionRequest,
    devices: &D,
) -> Option<DeviceWriteReadiness> {
    request.resources.iter().find_map(|resource| {
        let readiness = devices.write_readiness(resource);
        (readiness != DeviceWriteReadiness::Ready).then_some(readiness)
    })
}

fn reconcile_or_dispatch<T: ReceiverTransport>(
    transport: &mut T,
    dispatch: &TransportDispatch,
    declared: FrameCount,
) -> Result<ClassifiedTransport, TransactionCoordinatorError> {
    Ok(match transport.reconcile(dispatch) {
        TransportReconciliation::NotObserved => match transport.dispatch(dispatch) {
            Ok(receipt) => classify_receipt(receipt, declared),
            Err(error) => classify_failure(error.facts(), declared),
        },
        TransportReconciliation::Retained(receipt) => classify_receipt(receipt, declared),
        TransportReconciliation::RetainedFailure(facts) => classify_failure(facts, declared),
        TransportReconciliation::Evicted => (
            TransactionState::Failed,
            Some(ProtocolErrorKind::OutcomeEvicted),
            unknown_side_effect_facts()?,
        ),
        TransportReconciliation::Unavailable | TransportReconciliation::Conflict => (
            TransactionState::Failed,
            Some(ProtocolErrorKind::OutcomeUnknown),
            unknown_side_effect_facts()?,
        ),
    })
}

fn declared_frames(queued: &QueuedTransaction) -> Result<FrameCount, TransactionCoordinatorError> {
    let count = u16::try_from(queued.request.frames.len())
        .map_err(|_| TransactionCoordinatorError::FrameCount)?;
    FrameCount::try_from(count).map_err(|_| TransactionCoordinatorError::FrameCount)
}

fn zero_delivered() -> Result<DeliveredFrameCount, TransactionCoordinatorError> {
    DeliveredFrameCount::try_from(0_u16).map_err(|_| TransactionCoordinatorError::FrameCount)
}

fn unknown_side_effect_facts() -> Result<TransportFailureFacts, TransactionCoordinatorError> {
    Ok(TransportFailureFacts {
        delivered_frames: zero_delivered()?,
        side_effect_certainty: SideEffectCertainty::Possible,
        live_write_executed: true,
        automatic_retry_safe: false,
        device_application: DeviceApplicationState::Unverified,
    })
}

type ClassifiedTransport = (
    TransactionState,
    Option<ProtocolErrorKind>,
    TransportFailureFacts,
);

fn classify_receipt(receipt: TransportReceipt, declared: FrameCount) -> ClassifiedTransport {
    let reported_too_many = receipt.delivered_frames.get() > declared.get();
    let facts = normalize_facts(
        TransportFailureFacts {
            delivered_frames: receipt.delivered_frames,
            side_effect_certainty: receipt.side_effect_certainty,
            live_write_executed: receipt.live_write_executed,
            automatic_retry_safe: receipt.automatic_retry_safe,
            device_application: receipt.device_application,
        },
        declared,
    );
    if reported_too_many {
        return (
            TransactionState::Failed,
            Some(ProtocolErrorKind::InternalFailure),
            facts,
        );
    }
    match receipt.terminal {
        TransportTerminal::Delivered
            if facts.delivered_frames.get() == declared.get()
                && facts.side_effect_certainty == SideEffectCertainty::Committed
                && facts.live_write_executed =>
        {
            (TransactionState::Succeeded, None, facts)
        }
        TransportTerminal::Revoked => (
            TransactionState::Revoked,
            Some(ProtocolErrorKind::StaleGeneration),
            facts,
        ),
        TransportTerminal::Delivered | TransportTerminal::Failed => (
            TransactionState::Failed,
            Some(ProtocolErrorKind::TransportFailure),
            facts,
        ),
    }
}

fn classify_failure(facts: TransportFailureFacts, declared: FrameCount) -> ClassifiedTransport {
    let error_kind = if facts.delivered_frames.get() > declared.get() {
        ProtocolErrorKind::InternalFailure
    } else {
        ProtocolErrorKind::TransportFailure
    };
    (
        TransactionState::Failed,
        Some(error_kind),
        normalize_facts(facts, declared),
    )
}

fn normalize_facts(
    mut facts: TransportFailureFacts,
    declared: FrameCount,
) -> TransportFailureFacts {
    let delivered = facts.delivered_frames.get().min(declared.get());
    facts.delivered_frames =
        DeliveredFrameCount::try_from(delivered).unwrap_or(facts.delivered_frames);
    if delivered > 0 && delivered < declared.get() {
        facts.side_effect_certainty = SideEffectCertainty::Partial;
    }
    facts.live_write_executed = facts.live_write_executed
        || delivered > 0
        || facts.side_effect_certainty != SideEffectCertainty::None;
    if facts.live_write_executed && facts.side_effect_certainty == SideEffectCertainty::None {
        facts.side_effect_certainty = SideEffectCertainty::Possible;
    }
    facts.automatic_retry_safe = retry_is_safe(facts);
    facts
}

const fn retry_is_safe(facts: TransportFailureFacts) -> bool {
    facts.automatic_retry_safe
        && !facts.live_write_executed
        && facts.delivered_frames.get() == 0
        && matches!(facts.side_effect_certainty, SideEffectCertainty::None)
}
