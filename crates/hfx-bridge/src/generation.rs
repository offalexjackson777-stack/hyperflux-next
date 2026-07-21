// SPDX-License-Identifier: GPL-2.0-only

use crate::{ReceiverProfileBinding, RuntimeProfileAuthority, RuntimeProfileAuthorityError};
use hfx_core::{
    BoundedEventLog, EventDelivery, EventDraft, EventLogError, EventSink, LeaseManager,
    LifecycleError, LifecycleLimits, ObservationStamp, ReceiverLifecycleMachine,
    ReceiverLifecycleRegistry, ReceiverRegistryError, ReceiverTransport, TransactionCoordinator,
    TransactionCoordinatorError,
};
use hfx_domain::{
    ApplyOutcome, EventKind, GenerationId, LeaseId, ProductId, ReceiverId, TransactionId, VendorId,
};
use hfx_protocol::BridgeEvent;
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
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GenerationActivationOutcome {
    Applied(GenerationActivation),
    Ignored(ApplyOutcome),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GenerationOrchestrationError {
    TransportGenerationMismatch {
        receiver_id: ReceiverId,
        observed_generation: GenerationId,
        transport_generation: Option<GenerationId>,
    },
    Lifecycle(LifecycleError),
    Registry(ReceiverRegistryError),
    Profile(RuntimeProfileAuthorityError),
    MissingQualifiedBinding {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    },
    Transaction(TransactionCoordinatorError),
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
    pub fn activate<T, S>(
        observation: ReceiverGenerationObservation,
        limits: LifecycleLimits,
        transport: &T,
        receivers: &mut ReceiverLifecycleRegistry,
        profiles: &mut RuntimeProfileAuthority,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<GenerationActivationOutcome, GenerationOrchestrationError>
    where
        T: ReceiverTransport,
        S: EventSink,
    {
        let receiver_id = observation.receiver_id;
        let generation_id = observation.stamp.generation_id();
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
        let mut emitted = CollectedEvents::default();

        let previous_generation = next_receivers
            .get(&receiver_id)
            .and_then(ReceiverLifecycleMachine::current)
            .map(hfx_core::ReceiverGenerationLifecycle::generation_id);
        let lifecycle_outcome = if let Some(machine) = next_receivers.get_mut(&receiver_id) {
            if machine.current().is_some() {
                machine.replace_generation(observation.stamp)
            } else {
                machine.discover(observation.stamp)
            }
        } else {
            let mut machine = ReceiverLifecycleMachine::new(receiver_id.clone(), limits)?;
            let outcome = machine.discover(observation.stamp);
            if outcome == ApplyOutcome::Applied {
                next_receivers.register(machine)?;
            }
            outcome
        };
        if lifecycle_outcome != ApplyOutcome::Applied {
            return Ok(GenerationActivationOutcome::Ignored(lifecycle_outcome));
        }

        let mut revoked_leases = Vec::new();
        let mut revoked_transactions = Vec::new();
        if let Some(previous) = previous_generation {
            let _ = next_profiles.retire(&receiver_id, previous);
            revoked_transactions = next_transactions
                .invalidate_generation(&receiver_id, previous, &mut next_events, &mut emitted)?
                .into_iter()
                .map(|terminal| terminal.transaction_id)
                .collect();
            for grant in next_leases.invalidate_generation(&receiver_id, previous) {
                revoked_leases.push(grant.lease_id.clone());
                append_ownership_event(&mut next_events, &mut emitted, grant.lease_id)?;
            }
        }

        let qualification = match next_profiles.bind_receiver(
            receiver_id.clone(),
            generation_id,
            observation.vendor_id,
            observation.product_id,
        ) {
            Ok(_) => {
                let Some(binding) = next_profiles.binding(&receiver_id).cloned() else {
                    return Err(GenerationOrchestrationError::MissingQualifiedBinding {
                        receiver_id,
                        generation_id,
                    });
                };
                GenerationQualification::Qualified(binding)
            }
            Err(RuntimeProfileAuthorityError::UnsupportedReceiver(_, _)) => {
                GenerationQualification::Unqualified
            }
            Err(error) => return Err(GenerationOrchestrationError::Profile(error)),
        };

        let generation_event = next_events.append(EventDraft {
            kind: EventKind::GenerationReplaced,
            receiver_id: Some(receiver_id.clone()),
            generation_id: Some(generation_id),
            device_id: None,
            lease_id: None,
            transaction_id: None,
            finding_id: None,
        })?;
        let _ = emitted.try_emit(&generation_event);

        *receivers = next_receivers;
        *profiles = next_profiles;
        *leases = next_leases;
        *transactions = next_transactions;
        *events = next_events;
        for event in emitted.events {
            let _ = sink.try_emit(&event);
        }

        Ok(GenerationActivationOutcome::Applied(GenerationActivation {
            receiver_id,
            generation_id,
            previous_generation,
            qualification,
            revoked_leases,
            revoked_transactions,
        }))
    }
}

#[derive(Clone, Debug, Default)]
struct CollectedEvents {
    events: Vec<BridgeEvent>,
}

impl EventSink for CollectedEvents {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.events.push(event.clone());
        EventDelivery::Accepted
    }
}

fn append_ownership_event(
    events: &mut BoundedEventLog,
    emitted: &mut CollectedEvents,
    lease_id: LeaseId,
) -> Result<(), EventLogError> {
    let event = events.append(EventDraft {
        kind: EventKind::OwnershipChanged,
        receiver_id: None,
        generation_id: None,
        device_id: None,
        lease_id: Some(lease_id),
        transaction_id: None,
        finding_id: None,
    })?;
    let _ = emitted.try_emit(&event);
    Ok(())
}
