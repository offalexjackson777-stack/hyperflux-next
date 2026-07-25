// SPDX-License-Identifier: GPL-2.0-only

use crate::native_invalidation::NativeInvalidationJournal;
use crate::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, GenerationActivationOutcome,
    GenerationOrchestrationError, GenerationOrchestrator, LifecycleObservation,
    LifecycleObservationError, LifecycleObservationOrchestrator, LifecycleObservationOutcome,
    NativeExchangeJournal, NativeExchangeJournalError, NativeExchangeRejection,
    NativeInvalidationJournalError, NativeRouteJournal, NativeRouteJournalError,
    NativeRouteProjectionError, ReceiverDisconnectCompletionOutcome, ReceiverDisconnectObservation,
    ReceiverDisconnectOutcome, ReceiverGenerationObservation, RestorationRuntime, RpcFailure,
    RuntimeIdentityError, RuntimeIdentityIssuer, RuntimeProfileAuthority, SessionIdentitySource,
    SessionRegistry, SnapshotProjectionError, SnapshotProjector, SubscriptionRegistry,
    SubscriptionRegistryError, project_native_routes,
};
use hfx_core::{
    BoundedEventLog, Clock, DesiredEffectError, DiagnosticRegistry, DiagnosticRegistryError,
    DispatchResult, EventDraft, EventLogError, EventSink, LeaseManager, LeaseManagerError,
    LifecycleLimits, NATIVE_REPORT_BYTES, NativeEndpointLane, NativeTransportDispatch,
    NativeTransportOutcome, NativeTransportResult, OutcomeJournalError, OutcomeLookup,
    PersistedStableIntent, ProfileRegistry, ReceiverLifecycleRegistry, ReceiverTransport,
    RestorationAuthority, RestorationError, RestoreAdvanceResult, RestorePlanResult, RestoreRecord,
    RestoreTrigger, SessionAuthority, StableCommitOutcome, SubmissionResult,
    TransactionCoordinator, TransactionCoordinatorError, TransactionQueueError,
    VolatileEffectRegistry, WallClock,
};
use hfx_domain::{
    DeviceKind, EventKind, FindingId, LeaseDurationMs, MonotonicMs, NativeReportHex,
    NativeRouteAccess, NativeRouteToken, ProjectionRevision, ProtocolErrorKind, QueueCapacity,
    ReceiverId, ResourceKind, RestoreClaimId, RouteState, StreamEpoch, StreamId,
};
use hfx_errors::ErrorCode;
use hfx_integration_model::project_integration_view;
use hfx_profiles::{NativeExchangeRecord, native_response_matches, validate_native_request};
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EventBatch, IntegrationView, LeaseRequest, LeaseResult,
    NativeExchangeMatchedResponse, NativeExchangeNotSent, NativeExchangeOutcome,
    NativeExchangeRequest, NativeExchangeResult, NativeExchangeUncertain, NativeRoute,
    NativeRouteEventBatch, NativeRouteInventory, ReleaseLeaseRequest, RenewLeaseRequest,
    SubscriptionRequest, TransactionLookup, TransactionRequest, TransactionResult,
    TransactionUnavailable, validate_lease_request,
};
use sha2::{Digest, Sha256};
use std::fmt;
use std::ops::Deref;

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
pub enum StableCaptureStatus {
    NotApplicable,
    Captured(Vec<PersistedStableIntent>),
    Failed(RestorationError),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BridgeDispatchResult {
    pub dispatch: DispatchResult,
    pub stable_capture: StableCaptureStatus,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesiredEffectReplayStatus {
    NotAvailable,
    Queued,
    Deferred,
    Invalidated,
}

impl Deref for BridgeDispatchResult {
    type Target = DispatchResult;

    fn deref(&self) -> &Self::Target {
        &self.dispatch
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CoreBridgeBackendError {
    Identity(RuntimeIdentityError),
    Lease(LeaseManagerError),
    Transaction(TransactionCoordinatorError),
    Event(EventLogError),
    Diagnostic(DiagnosticRegistryError),
    Subscription(SubscriptionRegistryError),
    NativeRoute(NativeRouteJournalError),
    NativeExchange(NativeExchangeJournalError),
    NativeInvalidation(NativeInvalidationJournalError),
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
            Self::NativeRoute(_) => "native route journal initialization failed",
            Self::NativeExchange(_) => "native exchange journal initialization failed",
            Self::NativeInvalidation(_) => "native invalidation journal initialization failed",
        })
    }
}

impl std::error::Error for CoreBridgeBackendError {}

/// Application-neutral composition of the production bridge policy core.
#[derive(Debug)]
pub struct CoreBridgeBackend<C, W, T, R, S> {
    config: CoreBridgeConfig,
    clock: C,
    wall_clock: W,
    transport: T,
    restoration: R,
    identities: RuntimeIdentityIssuer,
    receivers: ReceiverLifecycleRegistry,
    profiles: RuntimeProfileAuthority,
    leases: LeaseManager,
    transactions: TransactionCoordinator,
    desired_effects: VolatileEffectRegistry,
    events: BoundedEventLog,
    diagnostics: DiagnosticRegistry,
    subscriptions: SubscriptionRegistry,
    native_routes: NativeRouteJournal,
    native_exchanges: NativeExchangeJournal,
    native_invalidations: NativeInvalidationJournal,
    native_subscriptions: SubscriptionRegistry,
    event_sink: S,
}

impl<C, W, T, R, S> CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
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
        wall_clock: W,
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
        let desired_effects = VolatileEffectRegistry::new(usize::from(
            config.transaction_capacity.get(),
        ))
        .map_err(|_| {
            CoreBridgeBackendError::Transaction(TransactionCoordinatorError::InvalidCapacity)
        })?;
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
        let native_routes = NativeRouteJournal::new(
            &config.stream_id,
            config.stream_epoch,
            config.projection_revision,
            usize::from(config.event_capacity.get()),
        )
        .map_err(CoreBridgeBackendError::NativeRoute)?;
        let native_exchanges =
            NativeExchangeJournal::new(usize::from(config.transaction_capacity.get()))
                .map_err(CoreBridgeBackendError::NativeExchange)?;
        let native_invalidations =
            NativeInvalidationJournal::new(usize::from(config.transaction_capacity.get()))
                .map_err(CoreBridgeBackendError::NativeInvalidation)?;
        let native_subscriptions =
            SubscriptionRegistry::new(usize::from(config.subscription_capacity.get()))
                .map_err(CoreBridgeBackendError::Subscription)?;
        Ok(Self {
            config,
            clock,
            wall_clock,
            transport,
            restoration,
            identities,
            receivers,
            profiles,
            leases,
            transactions,
            desired_effects,
            events,
            diagnostics,
            subscriptions,
            native_routes,
            native_exchanges,
            native_invalidations,
            native_subscriptions,
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
    ) -> Result<BridgeDispatchResult, RpcFailure> {
        self.tick()?;
        let now = self.clock.now();
        let profiles = self.profiles.view(&self.receivers);
        let prepared_request = self
            .transactions
            .next_dispatchable_request(
                now,
                sessions,
                &self.leases,
                &profiles,
                &profiles,
                &self.transport,
            )
            .cloned();
        if let Some(request) = prepared_request.as_ref() {
            self.restoration
                .prepare_stable_dispatch(request, self.wall_clock.now_unix_ms())
                .map_err(persistence_write_failure)?;
        }
        let dispatch = self
            .transactions
            .dispatch_next(
                now,
                sessions,
                &self.leases,
                &profiles,
                &profiles,
                &mut self.transport,
                &mut self.events,
                &mut self.event_sink,
            )
            .map_err(transaction_failure)?;
        let stable_capture =
            dispatch
                .completed
                .as_ref()
                .map_or(StableCaptureStatus::NotApplicable, |completed| {
                    match self
                        .restoration
                        .capture_completed(completed, self.wall_clock.now_unix_ms())
                    {
                        Ok(StableCommitOutcome::NotApplicable) => {
                            StableCaptureStatus::NotApplicable
                        }
                        Ok(StableCommitOutcome::Captured(intents)) => {
                            StableCaptureStatus::Captured(intents)
                        }
                        Err(error) => StableCaptureStatus::Failed(error),
                    }
                });
        if let Some(completed) = dispatch.completed.as_ref() {
            self.desired_effects
                .observe_completed(&completed.request, &completed.terminal);
        }
        Ok(BridgeDispatchResult {
            dispatch,
            stable_capture,
        })
    }

    /// Advances time-driven policy and reconciles native route authority.
    ///
    /// # Errors
    ///
    /// Returns a canonical lease, profile, or route failure without granting
    /// authority from a partially reconciled native inventory.
    pub fn tick(&mut self) -> Result<(), RpcFailure> {
        self.expire_leases()?;
        self.synchronize_native_routes()
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
        let outcome = GenerationOrchestrator::activate(
            observation,
            self.config.lifecycle_limits,
            &self.transport,
            &mut self.restoration,
            &mut self.receivers,
            &mut self.profiles,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.event_sink,
        )?;
        if let GenerationActivationOutcome::Applied(activation) = &outcome
            && let Some(previous_generation) = activation.previous_generation
        {
            self.desired_effects
                .remove_generation(&activation.receiver_id, previous_generation);
        }
        Ok(outcome)
    }

    /// Begins a transport-confirmed receiver disconnect and revokes all live
    /// authority belonging to its generation.
    ///
    /// # Errors
    ///
    /// Returns a typed orchestration error without partial state mutation.
    pub fn begin_receiver_disconnect(
        &mut self,
        observation: ReceiverDisconnectObservation,
    ) -> Result<ReceiverDisconnectOutcome, GenerationOrchestrationError> {
        let outcome = GenerationOrchestrator::begin_disconnect(
            observation,
            &self.transport,
            &mut self.restoration,
            &mut self.receivers,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.event_sink,
        )?;
        if let ReceiverDisconnectOutcome::Applied(disconnect) = &outcome {
            self.desired_effects
                .remove_generation(&disconnect.receiver_id, disconnect.generation_id);
        }
        Ok(outcome)
    }

    /// Completes a previously begun receiver disconnect and retires its
    /// generation profile binding.
    ///
    /// # Errors
    ///
    /// Returns a typed orchestration error without partial state mutation.
    pub fn complete_receiver_disconnect(
        &mut self,
        observation: ReceiverDisconnectObservation,
    ) -> Result<ReceiverDisconnectCompletionOutcome, GenerationOrchestrationError> {
        GenerationOrchestrator::complete_disconnect(
            observation,
            &self.transport,
            &mut self.receivers,
            &mut self.profiles,
        )
    }

    /// Applies one passive, transport-confirmed receiver or child observation.
    ///
    /// # Errors
    ///
    /// Returns a typed observation error without partial state or event
    /// mutation.
    pub fn observe_lifecycle(
        &mut self,
        observation: LifecycleObservation,
    ) -> Result<LifecycleObservationOutcome, LifecycleObservationError> {
        LifecycleObservationOrchestrator::apply(
            observation,
            &self.transport,
            &mut self.receivers,
            &mut self.events,
            &mut self.event_sink,
        )
    }

    /// Synchronizes one receiver's durable restoration policy with the
    /// validated service configuration.
    ///
    /// # Errors
    ///
    /// Returns a durable validation or compare-and-set failure.
    pub fn set_restoration_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        self.restoration.set_enabled(receiver_id, enabled)
    }

    /// Loads all validated nonterminal restoration claims for one receiver.
    ///
    /// # Errors
    ///
    /// Returns a persistence or durable-record validation failure.
    pub fn pending_restoration_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        self.restoration.pending_records(receiver_id)
    }

    /// Rebinds stable intents whose current profile explicitly preserves both
    /// lighting semantics and the application map.
    ///
    /// # Errors
    ///
    /// Returns a persistence, compatibility, or prior-outcome barrier.
    pub fn rebind_compatible_stable_intents(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<usize, RestorationError> {
        let profiles = self.profiles.view(&self.receivers);
        self.restoration
            .rebind_compatible_stable_intents(trigger, &profiles)
    }

    /// Plans one exact lifecycle restoration trigger.
    ///
    /// # Errors
    ///
    /// Returns a trigger, persistence, capacity, or prior-attempt barrier.
    pub fn plan_restoration(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        let profiles = self.profiles.view(&self.receivers);
        self.restoration.plan_restore(trigger, &profiles)
    }

    /// Advances one restoration claim through the same session, ownership,
    /// profile, generation, transaction, and event authorities as SDK writes.
    /// This phase never dispatches hardware.
    ///
    /// # Errors
    ///
    /// Returns a durable restoration or runtime-identity failure.
    pub fn advance_restoration(
        &mut self,
        claim_id: &RestoreClaimId,
        session: &AuthorizedSession,
        sessions: &SessionRegistry,
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        let now = self.clock.now();
        let deadline = now
            .get()
            .checked_add(authority_window_ms)
            .ok_or(RestorationError::AuthorityDeadlineOverflow)?;
        let authority = RestorationAuthority {
            client_id: session.client_id.clone(),
            submission: session.submission_binding(
                self.identities
                    .dispatch_nonce()
                    .map_err(|_| RestorationError::Identifier)?,
            ),
            lease_duration_ms,
            deadline_ms: MonotonicMs::try_from(deadline)
                .map_err(|_| RestorationError::AuthorityDeadlineOverflow)?,
        };
        let profiles = self.profiles.view(&self.receivers);
        self.restoration.advance_claim(
            claim_id,
            &authority,
            now,
            sessions,
            &profiles,
            &profiles,
            &self.transport,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.event_sink,
        )
    }

    /// Revalidates and dispatches one durably queued restoration claim.
    ///
    /// # Errors
    ///
    /// Returns a generation, authority, profile, transport, transaction,
    /// persistence, lease, or event failure without authorizing an implicit
    /// retry after an uncertain side effect.
    pub fn dispatch_restoration(
        &mut self,
        claim_id: &RestoreClaimId,
        sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError> {
        let now = self.clock.now();
        let profiles = self.profiles.view(&self.receivers);
        self.restoration.dispatch_claim(
            claim_id,
            now,
            sessions,
            &profiles,
            &profiles,
            &mut self.transport,
            &mut self.leases,
            &mut self.transactions,
            &mut self.events,
            &mut self.event_sink,
        )
    }

    /// Queues the latest owner-bound software-effect frame for one returning
    /// endpoint through the ordinary transaction authority.
    ///
    /// Expected stale ownership and temporary readiness outcomes are returned
    /// as lifecycle statuses. Structural, identity, event, and outcome failures
    /// remain typed RPC failures.
    ///
    /// # Errors
    ///
    /// Returns a typed RPC failure when fresh replay identity cannot be issued,
    /// retained effect state is invalid, or ordinary transaction admission
    /// encounters a non-recoverable internal failure.
    #[allow(clippy::too_many_arguments)]
    pub fn queue_desired_effect_replay(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: hfx_domain::GenerationId,
        device_id: &hfx_domain::LogicalDeviceId,
        sessions: &SessionRegistry,
        authority_window_ms: u64,
    ) -> Result<DesiredEffectReplayStatus, RpcFailure> {
        let deadline = self
            .clock
            .now()
            .get()
            .checked_add(authority_window_ms)
            .and_then(|value| hfx_domain::MonotonicMs::try_from(value).ok())
            .ok_or_else(RpcFailure::internal)?;
        let request_id = self.identities.request_id().map_err(identity_failure)?;
        let transaction_id = self.identities.transaction_id().map_err(identity_failure)?;
        let dispatch_nonce = self.identities.dispatch_nonce().map_err(identity_failure)?;
        let Some(replay) = self
            .desired_effects
            .plan_replay(
                receiver_id,
                generation_id,
                device_id,
                request_id,
                transaction_id,
                dispatch_nonce,
                deadline,
            )
            .map_err(desired_effect_failure)?
        else {
            return Ok(DesiredEffectReplayStatus::NotAvailable);
        };
        let profiles = self.profiles.view(&self.receivers);
        match self.transactions.submit(
            replay.request,
            replay.submission,
            self.clock.now(),
            sessions,
            &self.leases,
            &profiles,
            &profiles,
            &self.transport,
            &mut self.events,
            &mut self.event_sink,
        ) {
            Ok(SubmissionResult::Queued(_)) => Ok(DesiredEffectReplayStatus::Queued),
            Ok(SubmissionResult::Replay(_)) => Err(RpcFailure::internal()),
            Err(
                TransactionCoordinatorError::DeviceNotReady(_)
                | TransactionCoordinatorError::Queue(
                    TransactionQueueError::Full | TransactionQueueError::DeadlineElapsed,
                ),
            ) => Ok(DesiredEffectReplayStatus::Deferred),
            Err(
                TransactionCoordinatorError::SessionRevoked
                | TransactionCoordinatorError::OwnershipDenied
                | TransactionCoordinatorError::StaleGeneration
                | TransactionCoordinatorError::UnsupportedResource
                | TransactionCoordinatorError::ProfileBindingChanged,
            ) => {
                self.desired_effects
                    .remove_target(receiver_id, generation_id, device_id);
                Ok(DesiredEffectReplayStatus::Invalidated)
            }
            Err(error) => Err(transaction_failure(error)),
        }
    }

    #[must_use]
    pub fn now(&self) -> MonotonicMs {
        self.clock.now()
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

    #[must_use]
    pub const fn restoration(&self) -> &R {
        &self.restoration
    }

    pub const fn restoration_mut(&mut self) -> &mut R {
        &mut self.restoration
    }

    pub const fn clock_mut(&mut self) -> &mut C {
        &mut self.clock
    }

    pub const fn wall_clock_mut(&mut self) -> &mut W {
        &mut self.wall_clock
    }

    #[must_use]
    pub fn queued_transactions(&self) -> usize {
        self.transactions.queued_len()
    }

    #[must_use]
    pub fn desired_effect_frames(&self) -> usize {
        self.desired_effects.len()
    }

    #[must_use]
    pub fn active_subscriptions(&self) -> usize {
        self.subscriptions
            .len()
            .saturating_add(self.native_subscriptions.len())
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

    fn synchronize_native_routes(&mut self) -> Result<(), RpcFailure> {
        let previous = self.native_routes.inventory().routes;
        let routes = project_native_routes(&self.receivers, &self.profiles)
            .map_err(native_projection_failure)?;
        self.native_routes
            .synchronize(routes, &mut self.identities)
            .map_err(native_route_failure)?;
        let current = self.native_routes.inventory().routes;

        for route in &current {
            if let Some(route_token) = &route.route_token {
                self.native_invalidations.retire(route_token);
            }
        }
        for route in previous {
            let Some(route_token) = route.route_token.as_ref() else {
                continue;
            };
            if current
                .iter()
                .any(|candidate| candidate.route_token.as_ref() == Some(route_token))
            {
                continue;
            }
            let rejection = native_route_invalidation(&route, &current);
            let finding_id = native_finding(rejection.error_kind(), false);
            self.native_exchanges.invalidate_route(
                route_token,
                rejection.error_kind(),
                &finding_id,
            );
            self.native_invalidations
                .invalidate(route_token.clone(), rejection);
        }
        Ok(())
    }

    fn qualify_resources(&mut self, request: &LeaseRequest) -> Result<(), RpcFailure> {
        validate_lease_request(request).map_err(|_| request_failure())?;
        if request
            .resources
            .iter()
            .any(|resource| resource.kind == ResourceKind::NativeControl)
        {
            self.synchronize_native_routes()?;
        }
        let profiles = self.profiles.view(&self.receivers);
        for resource in &request.resources {
            if self.transport.current_generation(&resource.receiver_id)
                != Some(resource.generation_id)
            {
                return Err(generation_failure());
            }
            match resource.kind {
                ResourceKind::Lighting => {
                    if !self
                        .transport
                        .write_available(&resource.receiver_id, resource.generation_id)
                    {
                        return Err(transport_unavailable_failure());
                    }
                    if !profiles.supports(resource) {
                        return Err(profile_failure());
                    }
                }
                ResourceKind::NativeControl => {
                    let route = self
                        .native_routes
                        .route_for_resource(resource)
                        .ok_or_else(profile_failure)?;
                    if route.access != NativeRouteAccess::Qualified
                        || route.route_state != RouteState::Available
                    {
                        return Err(profile_failure());
                    }
                    if !self
                        .transport
                        .native_exchange_available(&resource.receiver_id, resource.generation_id)
                    {
                        return Err(transport_unavailable_failure());
                    }
                }
                ResourceKind::Settings | ResourceKind::Pairing => {
                    return Err(profile_failure());
                }
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
        for grant in &expired {
            emitted.push(
                next_events
                    .append(EventDraft {
                        kind: EventKind::OwnershipChanged,
                        receiver_id: None,
                        generation_id: None,
                        device_id: None,
                        lease_id: Some(grant.lease_id.clone()),
                        transaction_id: None,
                        finding_id: None,
                    })
                    .map_err(|error| event_failure(&error))?,
            );
        }

        self.leases = next_leases;
        self.events = next_events;
        for grant in expired {
            self.desired_effects.remove_lease(&grant.lease_id);
        }
        for event in emitted {
            let _ = self.event_sink.try_emit(&event);
        }
        Ok(())
    }

    fn complete_native_exchange(
        &mut self,
        request: NativeExchangeRequest,
        result: NativeExchangeResult,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        if let Some(outcome) = self
            .native_exchanges
            .retained(&request)
            .map_err(native_exchange_failure)?
        {
            return Ok(outcome);
        }
        let outcome = NativeExchangeOutcome {
            request_id: request.request_id.clone(),
            route_token: request.route_token.clone(),
            result,
        };
        self.native_exchanges
            .remember(request, outcome.clone())
            .map_err(native_exchange_failure)?;
        Ok(outcome)
    }

    fn reject_native_before_set(
        &mut self,
        request: NativeExchangeRequest,
        error_kind: ProtocolErrorKind,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        self.complete_native_exchange(
            request,
            NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind,
                finding_id: native_finding(error_kind, false),
            }),
        )
    }
}

impl<C, W, T, R, S> BridgeRpcBackend for CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
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

    fn integration_view(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        let snapshot = self.snapshot(context)?;
        project_integration_view(
            &snapshot,
            self.profiles.catalog(),
            Some(&context.session().client_id),
        )
        .map_err(|_| RpcFailure::internal())
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
        let released_lease_id = request.lease_id.clone();
        let decision = self
            .leases
            .release_request_decision(request, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        if decision.changed {
            self.desired_effects.remove_lease(&released_lease_id);
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
        self.desired_effects
            .validate_admission(&request)
            .map_err(desired_effect_failure)?;
        let nonce = self.identities.dispatch_nonce().map_err(identity_failure)?;
        let submission = context.session().submission_binding(nonce);
        let desired_request = request.clone();
        let profiles = self.profiles.view(&self.receivers);
        let result = self
            .transactions
            .submit(
                request,
                submission.clone(),
                self.clock.now(),
                context.sessions(),
                &self.leases,
                &profiles,
                &profiles,
                &self.transport,
                &mut self.events,
                &mut self.event_sink,
            )
            .map_err(transaction_failure)?;
        if matches!(result, SubmissionResult::Queued(_)) {
            self.desired_effects
                .capture_admitted(&desired_request, &submission)
                .map_err(desired_effect_failure)?;
        }
        Ok(match result {
            SubmissionResult::Queued(progress) => TransactionResult::Progress(progress),
            SubmissionResult::Replay(result) => result,
        })
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

    fn native_enumerate(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<NativeRouteInventory, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        self.synchronize_native_routes()?;
        Ok(self.native_routes.inventory())
    }

    fn native_transact(
        &mut self,
        context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        if let Some(rejection) = self.native_invalidations.rejection(&request.route_token) {
            return self.reject_native_before_set(request, rejection.error_kind());
        }
        if let Some(outcome) = self
            .native_exchanges
            .retained(&request)
            .map_err(native_exchange_failure)?
        {
            return Ok(outcome);
        }

        self.synchronize_native_routes()?;
        let Some(route) = self
            .native_routes
            .route_by_token(&request.route_token)
            .cloned()
        else {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        };
        if route.access != NativeRouteAccess::Qualified {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        }
        if route.route_state != RouteState::Available {
            return self.reject_native_before_set(request, ProtocolErrorKind::TransportFailure);
        }
        let Some(resource) = route.resource.clone() else {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        };
        if !self.leases.owns(
            &request.client_id,
            &request.lease_id,
            std::slice::from_ref(&resource),
            self.clock.now(),
        ) {
            return self.reject_native_before_set(request, ProtocolErrorKind::OwnershipConflict);
        }
        if self.transport.current_generation(&route.receiver_id) != Some(route.generation_id) {
            return self.reject_native_before_set(request, ProtocolErrorKind::StaleGeneration);
        }
        if !self
            .transport
            .native_exchange_available(&route.receiver_id, route.generation_id)
        {
            return self.reject_native_before_set(request, ProtocolErrorKind::TransportFailure);
        }
        if usize::from(route.report_bytes.get()) != NATIVE_REPORT_BYTES {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        }
        let native_profile = match native_profile_for_route(&self.profiles, &self.receivers, &route)
        {
            Ok(profile) => profile,
            Err(error_kind) => return self.reject_native_before_set(request, error_kind),
        };

        let endpoint = match route.device_kind {
            DeviceKind::Mouse => NativeEndpointLane::Pointer,
            DeviceKind::Keyboard => NativeEndpointLane::Keyboard,
            _ => {
                return self
                    .reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
            }
        };
        let Ok(report) = decode_native_report(&request.report) else {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        };
        if validate_native_request(&native_profile, &report).is_err() {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        }
        let route_token_digest = native_route_token_digest(&request.route_token);
        let dispatch = NativeTransportDispatch {
            session_id: context.session().session_id.clone(),
            authorization_epoch: context.session().authorization_epoch,
            dispatch_nonce: self.identities.dispatch_nonce().map_err(identity_failure)?,
            receiver_id: route.receiver_id,
            generation_id: route.generation_id,
            endpoint,
            route_token_digest,
            report,
            timeout_ms: request.timeout_ms,
        };
        let transport_outcome = self.transport.native_exchange(&dispatch);
        let result = validated_native_transport_result(
            &native_profile,
            &report,
            &dispatch,
            &transport_outcome,
        )?;
        self.complete_native_exchange(request, result)
    }

    fn reject_native_exchange(
        &mut self,
        context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
        rejection: NativeExchangeRejection,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.reject_native_before_set(request, rejection.error_kind())
    }

    fn take_native_route_invalidations(
        &mut self,
    ) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
        self.native_invalidations.take_pending()
    }

    fn native_watch(
        &mut self,
        context: BackendRequestContext<'_>,
        request: SubscriptionRequest,
    ) -> Result<NativeRouteEventBatch, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.synchronize_native_routes()?;
        let subscription_id = self
            .native_subscriptions
            .resolve(context.session(), &request, &mut self.identities)
            .map_err(subscription_failure)?;
        self.native_routes
            .read(subscription_id, &request)
            .map_err(native_route_failure)
    }

    fn disconnect(&mut self, session: &AuthorizedSession) -> Result<(), RpcFailure> {
        self.tick()?;
        self.desired_effects.remove_session(&session.session_id);
        let _ = self.subscriptions.revoke_session(session);
        let _ = self.native_subscriptions.revoke_session(session);
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

fn decode_native_report(value: &NativeReportHex) -> Result<[u8; NATIVE_REPORT_BYTES], RpcFailure> {
    let bytes = value.as_str().as_bytes();
    if bytes.len() != NATIVE_REPORT_BYTES * 2 {
        return Err(request_failure());
    }
    let mut report = [0_u8; NATIVE_REPORT_BYTES];
    for (index, pair) in bytes.chunks_exact(2).enumerate() {
        let high = decode_lower_hex(pair[0]).ok_or_else(request_failure)?;
        let low = decode_lower_hex(pair[1]).ok_or_else(request_failure)?;
        report[index] = (high << 4) | low;
    }
    Ok(report)
}

const fn decode_lower_hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
}

fn encode_native_report(report: &[u8; NATIVE_REPORT_BYTES]) -> Result<NativeReportHex, RpcFailure> {
    const LOWER_HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(NATIVE_REPORT_BYTES * 2);
    for byte in report {
        encoded.push(char::from(LOWER_HEX[usize::from(byte >> 4)]));
        encoded.push(char::from(LOWER_HEX[usize::from(byte & 0x0f)]));
    }
    NativeReportHex::try_from(encoded).map_err(|_| RpcFailure::internal())
}

fn native_route_token_digest(token: &hfx_domain::NativeRouteToken) -> [u8; 32] {
    Sha256::digest(token.as_str().as_bytes()).into()
}

fn native_profile_for_route(
    profiles: &RuntimeProfileAuthority,
    receivers: &ReceiverLifecycleRegistry,
    route: &NativeRoute,
) -> Result<NativeExchangeRecord, ProtocolErrorKind> {
    let profile = profiles
        .view(receivers)
        .native_exchange(&route.receiver_id, route.generation_id)
        .ok_or(ProtocolErrorKind::UnsupportedFeature)?;
    if profile.protocol_family != route.protocol_family.as_str()
        || profile.report_bytes != route.report_bytes.get()
    {
        return Err(ProtocolErrorKind::InvalidRequest);
    }
    Ok(profile)
}

fn validated_native_transport_result(
    profile: &NativeExchangeRecord,
    request: &[u8; NATIVE_REPORT_BYTES],
    dispatch: &NativeTransportDispatch,
    outcome: &NativeTransportOutcome,
) -> Result<NativeExchangeResult, RpcFailure> {
    if outcome.dispatch_nonce != dispatch.dispatch_nonce
        || outcome.route_token_digest != dispatch.route_token_digest
    {
        return Ok(native_uncertain_result());
    }
    if let NativeTransportResult::MatchedResponse(response) = &outcome.result
        && native_response_matches(profile, request, response) != Ok(true)
    {
        return Ok(native_uncertain_result());
    }
    protocol_native_result(&outcome.result)
}

fn native_uncertain_result() -> NativeExchangeResult {
    NativeExchangeResult::MayHaveBeenSent(NativeExchangeUncertain {
        error_kind: ProtocolErrorKind::OutcomeUnknown,
        finding_id: finding(ErrorCode::HfxTransport002),
    })
}

fn protocol_native_result(
    result: &NativeTransportResult,
) -> Result<NativeExchangeResult, RpcFailure> {
    Ok(match result {
        NativeTransportResult::NotSent { error_kind } => {
            NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: *error_kind,
                finding_id: native_finding(*error_kind, false),
            })
        }
        NativeTransportResult::MayHaveBeenSent { error_kind } => {
            NativeExchangeResult::MayHaveBeenSent(NativeExchangeUncertain {
                error_kind: *error_kind,
                finding_id: native_finding(*error_kind, true),
            })
        }
        NativeTransportResult::MatchedResponse(response) => {
            NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: encode_native_report(response)?,
            })
        }
    })
}

fn native_finding(error_kind: ProtocolErrorKind, uncertain: bool) -> FindingId {
    if uncertain {
        return finding(ErrorCode::HfxTransport002);
    }
    finding(match error_kind {
        ProtocolErrorKind::IncompatibleVersion => ErrorCode::HfxProtocol001,
        ProtocolErrorKind::UnsupportedFeature => ErrorCode::HfxProfile001,
        ProtocolErrorKind::InvalidRequest => ErrorCode::HfxRequest001,
        ProtocolErrorKind::OwnershipConflict => ErrorCode::HfxOwnership002,
        ProtocolErrorKind::StaleGeneration => ErrorCode::HfxGeneration001,
        ProtocolErrorKind::DeadlineExceeded => ErrorCode::HfxDeadline001,
        ProtocolErrorKind::QueueFull => ErrorCode::HfxQueue001,
        ProtocolErrorKind::TransportFailure => ErrorCode::HfxTransport001,
        ProtocolErrorKind::OutcomeUnknown => ErrorCode::HfxOutcome001,
        ProtocolErrorKind::OutcomeEvicted => ErrorCode::HfxOutcome002,
        ProtocolErrorKind::InternalFailure => ErrorCode::HfxInternal001,
    })
}

fn native_route_invalidation(
    previous: &NativeRoute,
    current: &[NativeRoute],
) -> NativeExchangeRejection {
    let replacement = current.iter().find(|candidate| {
        candidate.receiver_id == previous.receiver_id
            && candidate.device_id == previous.device_id
            && candidate.endpoint_id == previous.endpoint_id
    });
    match replacement {
        Some(route) if route.generation_id != previous.generation_id => {
            NativeExchangeRejection::StaleGeneration
        }
        Some(route) if route.access != NativeRouteAccess::Qualified => {
            NativeExchangeRejection::RouteWithdrawn
        }
        Some(route) if route.route_state != RouteState::Available => {
            NativeExchangeRejection::RouteUnavailable
        }
        Some(_) | None => NativeExchangeRejection::RouteUnavailable,
    }
}

fn desired_effect_failure(_error: DesiredEffectError) -> RpcFailure {
    RpcFailure::internal()
}

fn native_projection_failure(_error: NativeRouteProjectionError) -> RpcFailure {
    RpcFailure::internal()
}

fn native_route_failure(error: NativeRouteJournalError) -> RpcFailure {
    match error {
        NativeRouteJournalError::SubscriptionMismatch => request_failure(),
        NativeRouteJournalError::RouteCapacity => queue_failure(),
        NativeRouteJournalError::InvalidCapacity
        | NativeRouteJournalError::Identity(_)
        | NativeRouteJournalError::InvalidStreamIdentity
        | NativeRouteJournalError::SequenceExhausted
        | NativeRouteJournalError::DropCounterExhausted => RpcFailure::internal(),
    }
}

fn native_exchange_failure(error: NativeExchangeJournalError) -> RpcFailure {
    match error {
        NativeExchangeJournalError::RequestIdentityReused => request_failure(),
        NativeExchangeJournalError::InvalidCapacity => RpcFailure::internal(),
    }
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

const fn transport_unavailable_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxTransport001,
        ProtocolErrorKind::TransportFailure,
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

fn persistence_write_failure(_error: RestorationError) -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxPersistence002,
        ProtocolErrorKind::InternalFailure,
    )
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

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        AuthorizationEpoch, DispatchNonce, EndpointId, GenerationId, LogicalDeviceId,
        NativeExchangeTimeoutMs, NativeProtocolFamily, NativeReportByteCount, ProductId, SessionId,
        VendorId,
    };

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is canonical")
    }

    fn route(generation: u64, access: NativeRouteAccess, route_state: RouteState) -> NativeRoute {
        NativeRoute {
            receiver_id: text::<ReceiverId>("receiver-1"),
            generation_id: GenerationId::try_from(generation).expect("generation is canonical"),
            device_id: text::<LogicalDeviceId>("mouse-1"),
            endpoint_id: text::<EndpointId>("pointer"),
            device_kind: DeviceKind::Mouse,
            vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is canonical"),
            product_id: ProductId::try_from(0x00cd_u16).expect("product is canonical"),
            route_state,
            access,
            protocol_family: text::<NativeProtocolFamily>("razer-feature-report-v1"),
            report_bytes: NativeReportByteCount::try_from(90_u16)
                .expect("report size is canonical"),
            route_token: None,
            resource: None,
        }
    }

    fn native_report(status: u8) -> [u8; NATIVE_REPORT_BYTES] {
        let mut report = [0_u8; NATIVE_REPORT_BYTES];
        report[0] = status;
        report[5] = 2;
        report[6] = 0x1f;
        report[7] = 0x03;
        report[8] = 0xaa;
        report[9] = 0x55;
        report[88] = report[2..88].iter().fold(0_u8, |value, byte| value ^ byte);
        report
    }

    fn native_dispatch() -> NativeTransportDispatch {
        NativeTransportDispatch {
            session_id: text::<SessionId>("session-1"),
            authorization_epoch: AuthorizationEpoch::try_from(1_u64)
                .expect("authorization epoch is canonical"),
            dispatch_nonce: DispatchNonce::try_from(1_u64).expect("nonce is canonical"),
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is canonical"),
            endpoint: NativeEndpointLane::Pointer,
            route_token_digest: [0x5a; 32],
            report: native_report(0),
            timeout_ms: NativeExchangeTimeoutMs::try_from(100_u16).expect("timeout is canonical"),
        }
    }

    #[test]
    fn route_invalidation_preserves_generation_availability_and_authority_causes() {
        let previous = route(1, NativeRouteAccess::Qualified, RouteState::Available);
        assert_eq!(
            native_route_invalidation(
                &previous,
                &[route(
                    2,
                    NativeRouteAccess::Qualified,
                    RouteState::Available,
                )],
            ),
            NativeExchangeRejection::StaleGeneration
        );
        assert_eq!(
            native_route_invalidation(
                &previous,
                &[route(
                    1,
                    NativeRouteAccess::Qualified,
                    RouteState::Unavailable,
                )],
            ),
            NativeExchangeRejection::RouteUnavailable
        );
        assert_eq!(
            native_route_invalidation(
                &previous,
                &[route(
                    1,
                    NativeRouteAccess::Candidate,
                    RouteState::Available,
                )],
            ),
            NativeExchangeRejection::RouteWithdrawn
        );
        assert_eq!(
            native_route_invalidation(&previous, &[]),
            NativeExchangeRejection::RouteUnavailable
        );
    }

    #[test]
    fn bridge_accepts_only_structurally_valid_matching_native_responses() {
        let catalog = hfx_profiles::RuntimeProfileCatalog::load().expect("profiles are valid");
        let profile = catalog
            .receiver(
                VendorId::try_from(0x1532_u16).expect("vendor is canonical"),
                ProductId::try_from(0x00cf_u16).expect("product is canonical"),
            )
            .and_then(|receiver| receiver.native_exchange)
            .expect("receiver native profile exists");
        let dispatch = native_dispatch();
        let matched = NativeTransportOutcome {
            dispatch_nonce: dispatch.dispatch_nonce,
            route_token_digest: dispatch.route_token_digest,
            result: NativeTransportResult::MatchedResponse(native_report(2)),
        };
        assert!(matches!(
            validated_native_transport_result(&profile, &dispatch.report, &dispatch, &matched),
            Ok(NativeExchangeResult::MatchedResponse(_))
        ));

        let mut malformed = native_report(2);
        malformed[88] ^= 1;
        let malformed = NativeTransportOutcome {
            result: NativeTransportResult::MatchedResponse(malformed),
            ..matched.clone()
        };
        assert_eq!(
            validated_native_transport_result(&profile, &dispatch.report, &dispatch, &malformed),
            Ok(native_uncertain_result())
        );

        let mut unrelated = native_report(2);
        unrelated[7] ^= 1;
        unrelated[88] = unrelated[2..88]
            .iter()
            .fold(0_u8, |value, byte| value ^ byte);
        let unrelated = NativeTransportOutcome {
            result: NativeTransportResult::MatchedResponse(unrelated),
            ..matched
        };
        assert_eq!(
            validated_native_transport_result(&profile, &dispatch.report, &dispatch, &unrelated),
            Ok(native_uncertain_result())
        );
    }
}
