// SPDX-License-Identifier: GPL-2.0-only

mod failures;
mod native;
mod rpc_impl;
mod state;

use self::failures::{
    desired_effect_failure, identity_failure, persistence_write_failure, transaction_failure,
};
use crate::native_invalidation::NativeInvalidationJournal;
use crate::{
    AuthorizedSession, GenerationActivationOutcome, GenerationOrchestrationError,
    GenerationOrchestrator, LifecycleObservation, LifecycleObservationError,
    LifecycleObservationOrchestrator, LifecycleObservationOutcome, NativeExchangeJournal,
    NativeExchangeJournalError, NativeInvalidationJournalError, NativeRouteJournal,
    NativeRouteJournalError, ReceiverDisconnectCompletionOutcome, ReceiverDisconnectObservation,
    ReceiverDisconnectOutcome, ReceiverGenerationObservation, RestorationRuntime, RpcFailure,
    RuntimeIdentityError, RuntimeIdentityIssuer, RuntimeProfileAuthority, SessionIdentitySource,
    SessionRegistry, SubscriptionRegistry, SubscriptionRegistryError,
};
use hfx_core::{
    BoundedEventLog, Clock, DiagnosticRegistry, DiagnosticRegistryError, DispatchResult,
    EventLogError, EventSink, LeaseManager, LeaseManagerError, LifecycleLimits,
    PersistedStableIntent, ReceiverLifecycleRegistry, ReceiverTransport, RestorationAuthority,
    RestorationError, RestoreAdvanceResult, RestorePlanResult, RestoreRecord, RestoreTrigger,
    StableCommitOutcome, SubmissionResult, TransactionCoordinator, TransactionCoordinatorError,
    TransactionQueueError, VolatileEffectRegistry, WallClock,
};
use hfx_domain::{
    LeaseDurationMs, MonotonicMs, ProjectionRevision, QueueCapacity, ReceiverId, RestoreClaimId,
    StreamEpoch, StreamId,
};
use hfx_integration_model::SourceValidatedDeviceCatalog;
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
    NativeCommandPolicy,
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
            Self::NativeCommandPolicy => "native command policy initialization failed",
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
    source_validated_devices: SourceValidatedDeviceCatalog,
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
        let source_validated_devices = SourceValidatedDeviceCatalog::load()
            .map_err(|_| CoreBridgeBackendError::NativeCommandPolicy)?;
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
            source_validated_devices,
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
}

#[cfg(test)]
mod tests;
