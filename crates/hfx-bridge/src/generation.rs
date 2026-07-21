// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    ReceiverProfileBinding, RuntimeProfileAuthority, RuntimeProfileAuthorityError,
    restoration_runtime::GenerationRestorationRuntime, staged_events::StagedEvents,
};
use hfx_core::{
    BoundedEventLog, EventLogError, EventSink, LeaseManager, LifecycleError, LifecycleLimits,
    ObservationStamp, ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverRegistryError,
    ReceiverTransport, RestorationError, RestoreGenerationRetirement, TransactionCoordinator,
    TransactionCoordinatorError,
};
use hfx_domain::{
    ApplyOutcome, EventKind, GenerationId, LeaseId, ProductId, ReceiverId, ReceiverLifecycleState,
    TransactionId, VendorId,
};
use std::fmt;

/// Identity evidence for one transport-confirmed receiver generation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverGenerationObservation {
    pub receiver_id: ReceiverId,
    pub vendor_id: VendorId,
    pub product_id: ProductId,
    pub stamp: ObservationStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GenerationQualification {
    Qualified(ReceiverProfileBinding),
    Unqualified,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GenerationActivation {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub previous_generation: Option<GenerationId>,
    pub qualification: GenerationQualification,
    pub revoked_leases: Vec<LeaseId>,
    pub revoked_transactions: Vec<TransactionId>,
    pub restoration_retirement: Option<Box<RestoreGenerationRetirement>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GenerationActivationOutcome {
    Applied(GenerationActivation),
    Ignored(ApplyOutcome),
}

/// Transport-confirmed removal evidence for one active generation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverDisconnectObservation {
    pub receiver_id: ReceiverId,
    pub stamp: ObservationStamp,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverDisconnectBegan {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub revoked_leases: Vec<LeaseId>,
    pub revoked_transactions: Vec<TransactionId>,
    pub restoration_retirement: Box<RestoreGenerationRetirement>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ReceiverDisconnectOutcome {
    Applied(ReceiverDisconnectBegan),
    Ignored(ApplyOutcome),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverDisconnectCompleted {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub profile_retired: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ReceiverDisconnectCompletionOutcome {
    Applied(ReceiverDisconnectCompleted),
    Ignored(ApplyOutcome),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GenerationOrchestrationError {
    TransportGenerationMismatch {
        receiver_id: ReceiverId,
        observed_generation: GenerationId,
        transport_generation: Option<GenerationId>,
    },
    TransportStillPresent {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    },
    MissingReceiver(ReceiverId),
    Lifecycle(LifecycleError),
    Registry(ReceiverRegistryError),
    Profile(RuntimeProfileAuthorityError),
    MissingQualifiedBinding {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    },
    Transaction(TransactionCoordinatorError),
    Restoration(RestorationError),
    Event(EventLogError),
}

impl fmt::Display for GenerationOrchestrationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TransportGenerationMismatch {
                receiver_id,
                observed_generation,
                transport_generation,
            } => write!(
                formatter,
                "transport generation mismatch for {receiver_id}: observed {observed_generation}, transport {transport_generation:?}"
            ),
            Self::TransportStillPresent {
                receiver_id,
                generation_id,
            } => write!(
                formatter,
                "receiver {receiver_id} generation {generation_id} is still present in transport"
            ),
            Self::MissingReceiver(receiver_id) => {
                write!(
                    formatter,
                    "receiver state disappeared during staging: {receiver_id}"
                )
            }
            Self::Lifecycle(error) => write!(formatter, "{error}"),
            Self::Registry(error) => write!(formatter, "{error}"),
            Self::Profile(error) => write!(formatter, "{error}"),
            Self::MissingQualifiedBinding {
                receiver_id,
                generation_id,
            } => write!(
                formatter,
                "qualified profile binding is missing for {receiver_id} generation {generation_id}"
            ),
            Self::Transaction(error) => write!(formatter, "{error}"),
            Self::Restoration(error) => write!(formatter, "{error}"),
            Self::Event(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for GenerationOrchestrationError {}

impl From<LifecycleError> for GenerationOrchestrationError {
    fn from(error: LifecycleError) -> Self {
        Self::Lifecycle(error)
    }
}

impl From<ReceiverRegistryError> for GenerationOrchestrationError {
    fn from(error: ReceiverRegistryError) -> Self {
        Self::Registry(error)
    }
}

impl From<TransactionCoordinatorError> for GenerationOrchestrationError {
    fn from(error: TransactionCoordinatorError) -> Self {
        Self::Transaction(error)
    }
}

impl From<RestorationError> for GenerationOrchestrationError {
    fn from(error: RestorationError) -> Self {
        Self::Restoration(error)
    }
}

impl From<EventLogError> for GenerationOrchestrationError {
    fn from(error: EventLogError) -> Self {
        Self::Event(error)
    }
}

/// Atomically changes all generation-bound bridge authority.
#[derive(Clone, Copy, Debug, Default)]
pub struct GenerationOrchestrator;

impl GenerationOrchestrator {
    /// Activates one transport-confirmed generation and revokes all authority
    /// retained from the previous active generation.
    ///
    /// Unsupported receiver identities remain visible without receiving a
    /// profile binding. No staged mutation or event is committed on failure.
    ///
    /// # Errors
    ///
    /// Returns a typed error when transport and observation disagree or any
    /// bounded lifecycle, profile, transaction, or event invariant fails.
    #[allow(clippy::too_many_arguments)]
    pub fn activate<T, R, S>(
        observation: ReceiverGenerationObservation,
        limits: LifecycleLimits,
        transport: &T,
        restoration: &mut R,
        receivers: &mut ReceiverLifecycleRegistry,
        profiles: &mut RuntimeProfileAuthority,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<GenerationActivationOutcome, GenerationOrchestrationError>
    where
        T: ReceiverTransport,
        R: GenerationRestorationRuntime,
        S: EventSink,
    {
        let ReceiverGenerationObservation {
            receiver_id,
            vendor_id,
            product_id,
            stamp,
        } = observation;
        let generation_id = stamp.generation_id();
        let now = stamp.observed_at_ms();
        let transport_generation = transport.current_generation(&receiver_id);
        if transport_generation != Some(generation_id) {
            return Err(GenerationOrchestrationError::TransportGenerationMismatch {
                receiver_id,
                observed_generation: generation_id,
                transport_generation,
            });
        }

        let mut next_receivers = receivers.clone();
        let mut next_profiles = profiles.clone();
        let mut next_leases = leases.clone();
        let mut next_transactions = transactions.clone();
        let mut next_events = events.clone();
        let mut emitted = StagedEvents::default();

        let (lifecycle_outcome, previous_generation, was_absent) =
            stage_activation_lifecycle(&mut next_receivers, &receiver_id, stamp, limits)?;
        if lifecycle_outcome != ApplyOutcome::Applied {
            return Ok(GenerationActivationOutcome::Ignored(lifecycle_outcome));
        }

        let (revoked_leases, revoked_transactions) = stage_previous_authority(
            &receiver_id,
            previous_generation,
            &mut next_profiles,
            &mut next_leases,
            &mut next_transactions,
            &mut next_events,
            &mut emitted,
        )?;
        let qualification = stage_qualification(
            &receiver_id,
            generation_id,
            vendor_id,
            product_id,
            &mut next_profiles,
        )?;

        if previous_generation.is_some() {
            emitted.append_lifecycle(
                &mut next_events,
                EventKind::GenerationReplaced,
                receiver_id.clone(),
                generation_id,
                None,
            )?;
        }
        if was_absent {
            emitted.append_lifecycle(
                &mut next_events,
                EventKind::ReceiverAvailable,
                receiver_id.clone(),
                generation_id,
                None,
            )?;
        }

        let restoration_retirement = if let Some(previous) = previous_generation {
            Some(Box::new(restoration.retire_generation(
                &receiver_id,
                previous,
                now,
                transport,
                &mut next_leases,
                &next_transactions,
                &mut next_events,
                &mut emitted,
            )?))
        } else {
            None
        };

        *receivers = next_receivers;
        *profiles = next_profiles;
        *leases = next_leases;
        *transactions = next_transactions;
        *events = next_events;
        emitted.publish(sink);

        Ok(GenerationActivationOutcome::Applied(GenerationActivation {
            receiver_id,
            generation_id,
            previous_generation,
            qualification,
            revoked_leases,
            revoked_transactions,
            restoration_retirement,
        }))
    }

    /// Begins an observed physical disconnect and immediately revokes every
    /// lease and unsent transaction bound to that generation.
    ///
    /// # Errors
    ///
    /// Returns a typed error without partial mutation when transport still
    /// reports the receiver or any bounded state/event operation fails.
    #[allow(clippy::too_many_arguments)]
    pub fn begin_disconnect<T, R, S>(
        observation: ReceiverDisconnectObservation,
        transport: &T,
        restoration: &mut R,
        receivers: &mut ReceiverLifecycleRegistry,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<ReceiverDisconnectOutcome, GenerationOrchestrationError>
    where
        T: ReceiverTransport,
        R: GenerationRestorationRuntime,
        S: EventSink,
    {
        let receiver_id = observation.receiver_id;
        let generation_id = observation.stamp.generation_id();
        let now = observation.stamp.observed_at_ms();
        if let Some(transport_generation) = transport.current_generation(&receiver_id) {
            return Err(GenerationOrchestrationError::TransportStillPresent {
                receiver_id,
                generation_id: transport_generation,
            });
        }

        let Some(machine) = receivers.get(&receiver_id) else {
            return Ok(ReceiverDisconnectOutcome::Ignored(
                ApplyOutcome::RejectedReceiverAbsent,
            ));
        };
        let Some(current) = machine.current() else {
            return Ok(ReceiverDisconnectOutcome::Ignored(
                if machine
                    .highest_generation()
                    .is_some_and(|highest| generation_id <= highest)
                {
                    ApplyOutcome::RejectedStaleGeneration
                } else {
                    ApplyOutcome::RejectedReceiverAbsent
                },
            ));
        };
        if current.lifecycle().value() == ReceiverLifecycleState::Disconnecting {
            return Ok(ReceiverDisconnectOutcome::Ignored(
                ApplyOutcome::RejectedInvalidTransition,
            ));
        }
        let mut next_receivers = receivers.clone();
        let mut next_leases = leases.clone();
        let mut next_transactions = transactions.clone();
        let mut next_events = events.clone();
        let mut emitted = StagedEvents::default();
        let lifecycle_outcome = next_receivers
            .get_mut(&receiver_id)
            .ok_or_else(|| GenerationOrchestrationError::MissingReceiver(receiver_id.clone()))?
            .transition_receiver(ReceiverLifecycleState::Disconnecting, observation.stamp);
        if lifecycle_outcome != ApplyOutcome::Applied {
            return Ok(ReceiverDisconnectOutcome::Ignored(lifecycle_outcome));
        }

        emitted.append_lifecycle(
            &mut next_events,
            EventKind::ReceiverUnavailable,
            receiver_id.clone(),
            generation_id,
            None,
        )?;
        let revoked_transactions = next_transactions
            .invalidate_generation(&receiver_id, generation_id, &mut next_events, &mut emitted)?
            .into_iter()
            .map(|terminal| terminal.transaction_id)
            .collect();
        let mut revoked_leases = Vec::new();
        for grant in next_leases.invalidate_generation(&receiver_id, generation_id) {
            revoked_leases.push(grant.lease_id.clone());
            emitted.append_ownership(&mut next_events, grant.lease_id)?;
        }
        let restoration_retirement = Box::new(restoration.retire_generation(
            &receiver_id,
            generation_id,
            now,
            transport,
            &mut next_leases,
            &next_transactions,
            &mut next_events,
            &mut emitted,
        )?);

        *receivers = next_receivers;
        *leases = next_leases;
        *transactions = next_transactions;
        *events = next_events;
        emitted.publish(sink);

        Ok(ReceiverDisconnectOutcome::Applied(
            ReceiverDisconnectBegan {
                receiver_id,
                generation_id,
                revoked_leases,
                revoked_transactions,
                restoration_retirement,
            },
        ))
    }

    /// Retires a generation only after a later disconnect-completion
    /// observation. The earlier begin step has already revoked live authority.
    ///
    /// # Errors
    ///
    /// Returns a typed error without partial mutation when transport again
    /// reports the receiver or lifecycle/profile state cannot be committed.
    pub fn complete_disconnect<T>(
        observation: ReceiverDisconnectObservation,
        transport: &T,
        receivers: &mut ReceiverLifecycleRegistry,
        profiles: &mut RuntimeProfileAuthority,
    ) -> Result<ReceiverDisconnectCompletionOutcome, GenerationOrchestrationError>
    where
        T: ReceiverTransport,
    {
        let receiver_id = observation.receiver_id;
        let generation_id = observation.stamp.generation_id();
        if let Some(transport_generation) = transport.current_generation(&receiver_id) {
            return Err(GenerationOrchestrationError::TransportStillPresent {
                receiver_id,
                generation_id: transport_generation,
            });
        }

        let mut next_receivers = receivers.clone();
        let mut next_profiles = profiles.clone();
        let Some(machine) = next_receivers.get_mut(&receiver_id) else {
            return Ok(ReceiverDisconnectCompletionOutcome::Ignored(
                ApplyOutcome::RejectedReceiverAbsent,
            ));
        };
        let outcome = machine.complete_disconnect(observation.stamp);
        if outcome != ApplyOutcome::Applied {
            return Ok(ReceiverDisconnectCompletionOutcome::Ignored(outcome));
        }
        let profile_retired = next_profiles.retire(&receiver_id, generation_id);
        *receivers = next_receivers;
        *profiles = next_profiles;

        Ok(ReceiverDisconnectCompletionOutcome::Applied(
            ReceiverDisconnectCompleted {
                receiver_id,
                generation_id,
                profile_retired,
            },
        ))
    }
}

fn stage_previous_authority(
    receiver_id: &ReceiverId,
    previous_generation: Option<GenerationId>,
    profiles: &mut RuntimeProfileAuthority,
    leases: &mut LeaseManager,
    transactions: &mut TransactionCoordinator,
    events: &mut BoundedEventLog,
    emitted: &mut StagedEvents,
) -> Result<(Vec<LeaseId>, Vec<TransactionId>), GenerationOrchestrationError> {
    let Some(previous) = previous_generation else {
        return Ok((Vec::new(), Vec::new()));
    };
    let _ = profiles.retire(receiver_id, previous);
    let revoked_transactions = transactions
        .invalidate_generation(receiver_id, previous, events, emitted)?
        .into_iter()
        .map(|terminal| terminal.transaction_id)
        .collect();
    let mut revoked_leases = Vec::new();
    for grant in leases.invalidate_generation(receiver_id, previous) {
        revoked_leases.push(grant.lease_id.clone());
        emitted.append_ownership(events, grant.lease_id)?;
    }
    Ok((revoked_leases, revoked_transactions))
}

fn stage_qualification(
    receiver_id: &ReceiverId,
    generation_id: GenerationId,
    vendor_id: VendorId,
    product_id: ProductId,
    profiles: &mut RuntimeProfileAuthority,
) -> Result<GenerationQualification, GenerationOrchestrationError> {
    match profiles.bind_receiver(receiver_id.clone(), generation_id, vendor_id, product_id) {
        Ok(_) => profiles.binding(receiver_id).cloned().map_or_else(
            || {
                Err(GenerationOrchestrationError::MissingQualifiedBinding {
                    receiver_id: receiver_id.clone(),
                    generation_id,
                })
            },
            |binding| Ok(GenerationQualification::Qualified(binding)),
        ),
        Err(RuntimeProfileAuthorityError::UnsupportedReceiver(_, _)) => {
            Ok(GenerationQualification::Unqualified)
        }
        Err(error) => Err(GenerationOrchestrationError::Profile(error)),
    }
}

fn stage_activation_lifecycle(
    receivers: &mut ReceiverLifecycleRegistry,
    receiver_id: &ReceiverId,
    stamp: ObservationStamp,
    limits: LifecycleLimits,
) -> Result<(ApplyOutcome, Option<GenerationId>, bool), GenerationOrchestrationError> {
    let was_absent = receivers
        .get(receiver_id)
        .is_none_or(|machine| machine.current().is_none());
    let previous_generation = receivers
        .get(receiver_id)
        .and_then(ReceiverLifecycleMachine::highest_generation);
    let outcome = if let Some(machine) = receivers.get_mut(receiver_id) {
        if machine.current().is_some() {
            machine.replace_generation(stamp)
        } else {
            machine.discover(stamp)
        }
    } else {
        let mut machine = ReceiverLifecycleMachine::new(receiver_id.clone(), limits)?;
        let outcome = machine.discover(stamp);
        if outcome == ApplyOutcome::Applied {
            receivers.register(machine)?;
        }
        outcome
    };
    Ok((outcome, previous_generation, was_absent))
}
