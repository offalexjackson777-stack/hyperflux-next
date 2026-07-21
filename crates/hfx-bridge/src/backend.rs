// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, GenerationActivationOutcome,
    GenerationOrchestrationError, GenerationOrchestrator, ReceiverGenerationObservation,
    RestorationSnapshotSource, RpcFailure, RuntimeIdentityError, RuntimeIdentityIssuer,
    RuntimeProfileAuthority, SessionIdentitySource, SessionRegistry, SnapshotProjectionError,
    SnapshotProjector, SubscriptionRegistry, SubscriptionRegistryError,
};
use hfx_core::{
    BoundedEventLog, Clock, DiagnosticRegistry, DiagnosticRegistryError, DispatchResult,
    EventDraft, EventLogError, EventSink, LeaseManager, LeaseManagerError, LifecycleLimits,
    OutcomeJournalError, OutcomeLookup, ProfileRegistry, ReceiverLifecycleRegistry,
    ReceiverTransport, SessionAuthority, SubmissionResult, TransactionCoordinator,
    TransactionCoordinatorError, TransactionQueueError,
};
use hfx_domain::{
    EventKind, FindingId, ProjectionRevision, ProtocolErrorKind, QueueCapacity, StreamEpoch,
    StreamId,
};
use hfx_errors::ErrorCode;
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EventBatch, LeaseRequest, LeaseResult, ReleaseLeaseRequest,
    RenewLeaseRequest, SubscriptionRequest, TransactionLookup, TransactionRequest,
    TransactionResult, TransactionUnavailable, validate_lease_request,
};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreBridgeConfig {
    pub lifecycle_limits: LifecycleLimits,
    pub lease_capacity: QueueCapacity,
    pub lease_history_capacity: QueueCapacity,
    pub transaction_capacity: QueueCapacity,
    pub event_capacity: QueueCapacity,
    pub diagnostic_capacity: QueueCapacity,
    pub subscription_capacity: QueueCapacity,
    pub stream_id: StreamId,
    pub stream_epoch: StreamEpoch,
    pub projection_revision: ProjectionRevision,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CoreBridgeBackendError {
    Identity(RuntimeIdentityError),
    Lease(LeaseManagerError),
    Transaction(TransactionCoordinatorError),
    Event(EventLogError),
    Diagnostic(DiagnosticRegistryError),
    Subscription(SubscriptionRegistryError),
}

impl fmt::Display for CoreBridgeBackendError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Identity(_) => "runtime identity initialization failed",
            Self::Lease(_) => "lease authority initialization failed",
            Self::Transaction(_) => "transaction authority initialization failed",
            Self::Event(_) => "event stream initialization failed",
            Self::Diagnostic(_) => "diagnostic registry initialization failed",
            Self::Subscription(_) => "subscription registry initialization failed",
        })
    }
}

impl std::error::Error for CoreBridgeBackendError {}

/// Application-neutral composition of the production bridge policy core.
#[derive(Debug)]
pub struct CoreBridgeBackend<C, T, R, S> {
    config: CoreBridgeConfig,
    clock: C,
    transport: T,
    restoration: R,
    identities: RuntimeIdentityIssuer,
    receivers: ReceiverLifecycleRegistry,
    profiles: RuntimeProfileAuthority,
    leases: LeaseManager,
    transactions: TransactionCoordinator,
    events: BoundedEventLog,
    diagnostics: DiagnosticRegistry,
    subscriptions: SubscriptionRegistry,
    event_sink: S,
}

impl<C, T, R, S> CoreBridgeBackend<C, T, R, S>
where
    C: Clock,
    T: ReceiverTransport,
    R: RestorationSnapshotSource,
    S: EventSink,
{
    /// Composes validated state owners and bounded runtime services.
    ///
    /// # Errors
    ///
    /// Returns a typed construction failure before a partially initialized
    /// backend becomes reachable.
    #[allow(clippy::too_many_arguments)]
    pub fn new<I: SessionIdentitySource>(
        config: CoreBridgeConfig,
        clock: C,
        transport: T,
        restoration: R,
        identity_source: &mut I,
        receivers: ReceiverLifecycleRegistry,
        profiles: RuntimeProfileAuthority,
        event_sink: S,
    ) -> Result<Self, CoreBridgeBackendError> {
        let identities = RuntimeIdentityIssuer::new(identity_source)
            .map_err(CoreBridgeBackendError::Identity)?;
        let leases = LeaseManager::new(
            usize::from(config.lease_capacity.get()),
            usize::from(config.lease_history_capacity.get()),
        )
        .map_err(CoreBridgeBackendError::Lease)?;
        let transactions =
            TransactionCoordinator::new(usize::from(config.transaction_capacity.get()))
                .map_err(CoreBridgeBackendError::Transaction)?;
        let events = BoundedEventLog::new(
            config.stream_id.clone(),
            config.stream_epoch,
            config.projection_revision,
            usize::from(config.event_capacity.get()),
        )
        .map_err(CoreBridgeBackendError::Event)?;
        let diagnostics = DiagnosticRegistry::new(usize::from(config.diagnostic_capacity.get()))
            .map_err(CoreBridgeBackendError::Diagnostic)?;
        let subscriptions =
            SubscriptionRegistry::new(usize::from(config.subscription_capacity.get()))
                .map_err(CoreBridgeBackendError::Subscription)?;
        Ok(Self {
            config,
            clock,
            transport,
            restoration,
            identities,
            receivers,
            profiles,
            leases,
            transactions,
            events,
            diagnostics,
            subscriptions,
            event_sink,
        })
    }

    /// Dispatches at most one queued transaction after rechecking all authority.
    ///
    /// # Errors
    ///
    /// Returns a catalog-backed internal failure; expected transport failures
    /// remain immutable transaction outcomes inside the returned result.
    pub fn dispatch_next(
        &mut self,
        sessions: &SessionRegistry,
    ) -> Result<DispatchResult, RpcFailure> {
        self.tick()?;
        let profiles = self.profiles.view(&self.receivers);
        self.transactions
            .dispatch_next(
                self.clock.now(),
                sessions,
                &self.leases,
                &profiles,
                &profiles,
                &mut self.transport,
                &mut self.events,
                &mut self.event_sink,
            )
            .map_err(transaction_failure)
    }

    /// Advances time-driven bridge policy without requiring an RPC request.
    ///
    /// # Errors
    ///
    /// Returns a canonical event failure without partially committing an
    /// ownership transition.
    pub fn tick(&mut self) -> Result<(), RpcFailure> {
        self.expire_leases()
    }

    /// Atomically activates one transport-confirmed receiver generation.
    ///
    /// # Errors
    ///
    /// Returns a typed orchestration error without partially changing
    /// generation-bound lifecycle, profiles, ownership, transactions, or events.
    pub fn activate_generation(
        &mut self,
        observation: ReceiverGenerationObservation,
    ) -> Result<GenerationActivationOutcome, GenerationOrchestrationError> {
        GenerationOrchestrator::activate(
            observation,
            self.config.lifecycle_limits,
            &self.transport,
            &mut self.receivers,
            &mut self.profiles,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.event_sink,
        )
    }

    #[must_use]
    pub const fn receivers(&self) -> &ReceiverLifecycleRegistry {
        &self.receivers
    }

    #[must_use]
    pub const fn profiles(&self) -> &RuntimeProfileAuthority {
        &self.profiles
    }

    #[must_use]
    pub const fn transport(&self) -> &T {
        &self.transport
    }

    pub const fn transport_mut(&mut self) -> &mut T {
        &mut self.transport
    }

    pub const fn clock_mut(&mut self) -> &mut C {
        &mut self.clock
    }

    #[must_use]
    pub fn queued_transactions(&self) -> usize {
        self.transactions.queued_len()
    }

    #[must_use]
    pub fn active_subscriptions(&self) -> usize {
        self.subscriptions.len()
    }

    fn authorize(context: BackendRequestContext<'_>) -> Result<(), RpcFailure> {
        let session = context.session();
        if context
            .sessions()
            .authorizes(&session.session_id, session.authorization_epoch)
        {
            Ok(())
        } else {
            Err(ownership_failure())
        }
    }

    fn authorize_client(
        context: BackendRequestContext<'_>,
        client_id: &hfx_domain::ClientId,
    ) -> Result<(), RpcFailure> {
        Self::authorize(context)?;
        if &context.session().client_id == client_id {
            Ok(())
        } else {
            Err(request_failure())
        }
    }

    fn qualify_resources(&self, request: &LeaseRequest) -> Result<(), RpcFailure> {
        validate_lease_request(request).map_err(|_| request_failure())?;
        let profiles = self.profiles.view(&self.receivers);
        for resource in &request.resources {
            if self.transport.current_generation(&resource.receiver_id)
                != Some(resource.generation_id)
            {
                return Err(generation_failure());
            }
            if !profiles.supports(resource) {
                return Err(profile_failure());
            }
        }
        Ok(())
    }

    fn record_ownership_change(&mut self, lease_id: hfx_domain::LeaseId) -> Result<(), RpcFailure> {
        let event = self
            .events
            .append(EventDraft {
                kind: EventKind::OwnershipChanged,
                receiver_id: None,
                generation_id: None,
                device_id: None,
                lease_id: Some(lease_id),
                transaction_id: None,
                finding_id: None,
            })
            .map_err(|error| event_failure(&error))?;
        let _ = self.event_sink.try_emit(&event);
        Ok(())
    }

    fn expire_leases(&mut self) -> Result<(), RpcFailure> {
        let mut next_leases = self.leases.clone();
        let expired = next_leases.expire(self.clock.now());
        if expired.is_empty() {
            return Ok(());
        }

        let mut next_events = self.events.clone();
        let mut emitted = Vec::with_capacity(expired.len());
        for grant in expired {
            emitted.push(
                next_events
                    .append(EventDraft {
                        kind: EventKind::OwnershipChanged,
                        receiver_id: None,
                        generation_id: None,
                        device_id: None,
                        lease_id: Some(grant.lease_id),
                        transaction_id: None,
                        finding_id: None,
                    })
                    .map_err(|error| event_failure(&error))?,
            );
        }

        self.leases = next_leases;
        self.events = next_events;
        for event in emitted {
            let _ = self.event_sink.try_emit(&event);
        }
        Ok(())
    }
}

impl<C, T, R, S> BridgeRpcBackend for CoreBridgeBackend<C, T, R, S>
where
    C: Clock,
    T: ReceiverTransport,
    R: RestorationSnapshotSource,
    S: EventSink,
{
    fn snapshot(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        SnapshotProjector::new(&self.profiles)
            .project(
                &self.receivers,
                &mut self.leases,
                &self.events,
                &self.restoration,
                self.clock.now(),
            )
            .map_err(|error| snapshot_failure(&error))
    }

    fn acquire_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.qualify_resources(&request)?;
        let lease_id = self.identities.lease_id().map_err(identity_failure)?;
        let decision = self
            .leases
            .acquire_decision(request, lease_id, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        Ok(decision.result)
    }

    fn renew_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let decision = self
            .leases
            .renew_request_decision(request, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        Ok(decision.result)
    }

    fn release_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let decision = self
            .leases
            .release_request_decision(request, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        Ok(decision.result)
    }

    fn submit_transaction(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let nonce = self.identities.dispatch_nonce().map_err(identity_failure)?;
        let profiles = self.profiles.view(&self.receivers);
        self.transactions
            .submit(
                request,
                context.session().submission_binding(nonce),
                self.clock.now(),
                context.sessions(),
                &self.leases,
                &profiles,
                &profiles,
                &self.transport,
                &mut self.events,
                &mut self.event_sink,
            )
            .map(|result| match result {
                SubmissionResult::Queued(progress) => TransactionResult::Progress(progress),
                SubmissionResult::Replay(result) => result,
            })
            .map_err(transaction_failure)
    }

    fn transaction_outcome(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        Ok(
            match self
                .transactions
                .outcome(&request.client_id, &request.transaction_id)
            {
                OutcomeLookup::Retained(result) => result.clone(),
                OutcomeLookup::Evicted => TransactionResult::Unavailable(TransactionUnavailable {
                    transaction_id: request.transaction_id,
                    error_kind: ProtocolErrorKind::OutcomeEvicted,
                    finding_id: finding(ErrorCode::HfxOutcome002),
                }),
                OutcomeLookup::Unknown => TransactionResult::Unavailable(TransactionUnavailable {
                    transaction_id: request.transaction_id,
                    error_kind: ProtocolErrorKind::OutcomeUnknown,
                    finding_id: finding(ErrorCode::HfxOutcome001),
                }),
                OutcomeLookup::Forbidden => return Err(ownership_failure()),
            },
        )
    }

    fn subscribe(
        &mut self,
        context: BackendRequestContext<'_>,
        request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let subscription_id = self
            .subscriptions
            .resolve(context.session(), &request, &mut self.identities)
            .map_err(subscription_failure)?;
        self.events
            .read(subscription_id, &request)
            .map_err(|error| event_failure(&error))
    }

    fn diagnostics(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        Ok(self.diagnostics.snapshot(
            self.events.latest_sequence(),
            self.config.event_capacity,
            self.config.transaction_capacity,
        ))
    }

    fn disconnect(&mut self, session: &AuthorizedSession) -> Result<(), RpcFailure> {
        self.tick()?;
        let _ = self.subscriptions.revoke_session(session);
        let released = self.leases.release_client(&session.client_id);
        let mut first_failure = None;
        for grant in released {
            if let Err(failure) = self.record_ownership_change(grant.lease_id) {
                first_failure.get_or_insert(failure);
            }
        }
        if let Err(error) = self.transactions.invalidate_session(
            &session.session_id,
            &mut self.events,
            &mut self.event_sink,
        ) {
            first_failure.get_or_insert_with(|| transaction_failure(error));
        }
        first_failure.map_or(Ok(()), Err)
    }
}

fn finding(code: ErrorCode) -> FindingId {
    FindingId::try_from(code.as_str())
        .expect("generated error codes satisfy the finding identity bound")
}

const fn request_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxRequest001, ProtocolErrorKind::InvalidRequest)
}

const fn ownership_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxOwnership002,
        ProtocolErrorKind::OwnershipConflict,
    )
}

const fn generation_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxGeneration001,
        ProtocolErrorKind::StaleGeneration,
    )
}

const fn profile_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxProfile001,
        ProtocolErrorKind::UnsupportedFeature,
    )
}

const fn queue_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxQueue001, ProtocolErrorKind::QueueFull)
}

const fn deadline_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxDeadline001,
        ProtocolErrorKind::DeadlineExceeded,
    )
}

const fn outcome_unknown_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxOutcome001, ProtocolErrorKind::OutcomeUnknown)
}

const fn outcome_evicted_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxOutcome002, ProtocolErrorKind::OutcomeEvicted)
}

fn identity_failure(_error: RuntimeIdentityError) -> RpcFailure {
    RpcFailure::internal()
}

fn snapshot_failure(error: &SnapshotProjectionError) -> RpcFailure {
    match error {
        SnapshotProjectionError::Restoration(_) => RpcFailure::new(
            ErrorCode::HfxPersistence001,
            ProtocolErrorKind::InternalFailure,
        ),
        SnapshotProjectionError::InvalidSnapshot(_) => RpcFailure::internal(),
    }
}

fn lease_failure(error: &LeaseManagerError) -> RpcFailure {
    match error {
        LeaseManagerError::InvalidRequest(_) | LeaseManagerError::RequestIdReused => {
            request_failure()
        }
        LeaseManagerError::LeaseCapacity | LeaseManagerError::HistoryCapacity => queue_failure(),
        LeaseManagerError::UnknownLease | LeaseManagerError::NotOwner => ownership_failure(),
        LeaseManagerError::InvalidCapacity | LeaseManagerError::ClockOverflow => {
            RpcFailure::internal()
        }
    }
}

fn transaction_failure(error: TransactionCoordinatorError) -> RpcFailure {
    match error {
        TransactionCoordinatorError::Digest(_)
        | TransactionCoordinatorError::RequestIdReused
        | TransactionCoordinatorError::TransactionIdReused => request_failure(),
        TransactionCoordinatorError::OutcomeEvicted(_) => outcome_evicted_failure(),
        TransactionCoordinatorError::SessionRevoked
        | TransactionCoordinatorError::OwnershipDenied => ownership_failure(),
        TransactionCoordinatorError::StaleGeneration => generation_failure(),
        TransactionCoordinatorError::UnsupportedResource => profile_failure(),
        TransactionCoordinatorError::ProfileBindingChanged => {
            RpcFailure::new(ErrorCode::HfxProfile002, ProtocolErrorKind::StaleGeneration)
        }
        TransactionCoordinatorError::DeviceNotReady(_) => RpcFailure::new(
            ErrorCode::HfxTransport001,
            ProtocolErrorKind::TransportFailure,
        ),
        TransactionCoordinatorError::Queue(error) => match error {
            TransactionQueueError::InvalidRequest(_) => request_failure(),
            TransactionQueueError::DeadlineElapsed => deadline_failure(),
            TransactionQueueError::Full => queue_failure(),
            TransactionQueueError::InvalidCapacity => RpcFailure::internal(),
        },
        TransactionCoordinatorError::Outcome(error) => match error {
            OutcomeJournalError::CapacityExhausted => queue_failure(),
            OutcomeJournalError::RequestIdReused => request_failure(),
            OutcomeJournalError::InvalidCapacity
            | OutcomeJournalError::UnavailableOutcome
            | OutcomeJournalError::IdentityChanged
            | OutcomeJournalError::InvalidProgression
            | OutcomeJournalError::TerminalOutcomeChanged => RpcFailure::internal(),
        },
        TransactionCoordinatorError::TransactionNotQueued(_) => outcome_unknown_failure(),
        TransactionCoordinatorError::InvalidCapacity
        | TransactionCoordinatorError::Event(_)
        | TransactionCoordinatorError::Transition(_)
        | TransactionCoordinatorError::FrameCount => RpcFailure::internal(),
    }
}

fn event_failure(error: &EventLogError) -> RpcFailure {
    match error {
        EventLogError::SubscriptionMismatch => request_failure(),
        EventLogError::InvalidCapacity
        | EventLogError::SequenceExhausted
        | EventLogError::DropCounterExhausted => RpcFailure::internal(),
    }
}

fn subscription_failure(error: SubscriptionRegistryError) -> RpcFailure {
    match error {
        SubscriptionRegistryError::CapacityExhausted => queue_failure(),
        SubscriptionRegistryError::UnknownSubscription
        | SubscriptionRegistryError::SubscriptionMismatch => request_failure(),
        SubscriptionRegistryError::InvalidCapacity | SubscriptionRegistryError::Identity(_) => {
            RpcFailure::internal()
        }
    }
}
