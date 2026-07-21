// SPDX-License-Identifier: GPL-2.0-only

use crate::staged_events::StagedEvents;
use hfx_core::{
    BoundedEventLog, ChildIdentity, EndpointIdentity, EventLogError, EventSink, LifecycleError,
    ObservationStamp, ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverTransport,
};
use hfx_domain::{
    ActivityState, ApplyOutcome, BatteryPercent, ContactState, EndpointId, EventKind,
    FreshnessState, GenerationId, LogicalDeviceId, PairingState, PowerState, PresenceState,
    ReceiverId, ReceiverLifecycleState, RouteState, SleepState,
};
use std::fmt;

/// One typed passive fact emitted by the kernel transport boundary.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LifecycleObservationKind {
    ReceiverState(ReceiverLifecycleState),
    RegisterDevice(ChildIdentity),
    RegisterEndpoint {
        device_id: LogicalDeviceId,
        identity: EndpointIdentity,
    },
    Pairing {
        device_id: LogicalDeviceId,
        value: PairingState,
    },
    BatteryReported {
        device_id: LogicalDeviceId,
        percentage: BatteryPercent,
    },
    BatteryUnavailable {
        device_id: LogicalDeviceId,
    },
    BatteryStale {
        device_id: LogicalDeviceId,
    },
    Route {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: RouteState,
    },
    Power {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: PowerState,
    },
    Sleep {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: SleepState,
    },
    Activity {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: ActivityState,
    },
    Contact {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: ContactState,
    },
    Freshness {
        device_id: LogicalDeviceId,
        endpoint_id: EndpointId,
        value: FreshnessState,
    },
}

impl LifecycleObservationKind {
    fn device_id(&self) -> Option<&LogicalDeviceId> {
        match self {
            Self::ReceiverState(_) => None,
            Self::RegisterDevice(identity) => Some(identity.device_id()),
            Self::RegisterEndpoint { device_id, .. }
            | Self::Pairing { device_id, .. }
            | Self::BatteryReported { device_id, .. }
            | Self::BatteryUnavailable { device_id }
            | Self::BatteryStale { device_id }
            | Self::Route { device_id, .. }
            | Self::Power { device_id, .. }
            | Self::Sleep { device_id, .. }
            | Self::Activity { device_id, .. }
            | Self::Contact { device_id, .. }
            | Self::Freshness { device_id, .. } => Some(device_id),
        }
    }

    const fn updates_battery(&self) -> bool {
        matches!(
            self,
            Self::BatteryReported { .. }
                | Self::BatteryUnavailable { .. }
                | Self::BatteryStale { .. }
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LifecycleObservation {
    pub receiver_id: ReceiverId,
    pub stamp: ObservationStamp,
    pub kind: LifecycleObservationKind,
}

impl LifecycleObservation {
    #[must_use]
    pub fn device_id(&self) -> Option<&LogicalDeviceId> {
        self.kind.device_id()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AppliedLifecycleObservation {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub receiver_before: ReceiverLifecycleState,
    pub receiver_after: ReceiverLifecycleState,
    pub device_id: Option<LogicalDeviceId>,
    pub device_presence_before: Option<PresenceState>,
    pub device_presence_after: Option<PresenceState>,
    pub events: Vec<EventKind>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LifecycleObservationOutcome {
    Applied(AppliedLifecycleObservation),
    Ignored(ApplyOutcome),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LifecycleObservationError {
    TransportGenerationMismatch {
        receiver_id: ReceiverId,
        observed_generation: GenerationId,
        transport_generation: Option<GenerationId>,
    },
    RestrictedReceiverTransition(ReceiverLifecycleState),
    MissingReceiver(ReceiverId),
    Lifecycle(LifecycleError),
    Event(EventLogError),
}

impl fmt::Display for LifecycleObservationError {
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
            Self::RestrictedReceiverTransition(state) => write!(
                formatter,
                "receiver transition to {state} requires generation orchestration"
            ),
            Self::MissingReceiver(receiver_id) => {
                write!(formatter, "receiver state is missing: {receiver_id}")
            }
            Self::Lifecycle(error) => write!(formatter, "{error}"),
            Self::Event(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for LifecycleObservationError {}

impl From<LifecycleError> for LifecycleObservationError {
    fn from(error: LifecycleError) -> Self {
        Self::Lifecycle(error)
    }
}

impl From<EventLogError> for LifecycleObservationError {
    fn from(error: EventLogError) -> Self {
        Self::Event(error)
    }
}

/// Atomic ingress for passive generation-bound lifecycle evidence.
#[derive(Clone, Copy, Debug, Default)]
pub struct LifecycleObservationOrchestrator;

impl LifecycleObservationOrchestrator {
    /// Applies one transport-confirmed fact and publishes only the resulting
    /// canonical state transitions.
    ///
    /// Unsupported children remain visible without gaining write authority.
    /// Disconnect is deliberately excluded because it must revoke leases and
    /// queued transactions through [`crate::GenerationOrchestrator`].
    ///
    /// # Errors
    ///
    /// Returns a typed error without partial state or event mutation.
    pub fn apply<T, S>(
        observation: LifecycleObservation,
        transport: &T,
        receivers: &mut ReceiverLifecycleRegistry,
        events: &mut BoundedEventLog,
        sink: &mut S,
    ) -> Result<LifecycleObservationOutcome, LifecycleObservationError>
    where
        T: ReceiverTransport,
        S: EventSink,
    {
        let receiver_id = observation.receiver_id;
        let generation_id = observation.stamp.generation_id();
        let transport_generation = transport.current_generation(&receiver_id);
        if transport_generation != Some(generation_id) {
            return Err(LifecycleObservationError::TransportGenerationMismatch {
                receiver_id,
                observed_generation: generation_id,
                transport_generation,
            });
        }
        if let LifecycleObservationKind::ReceiverState(state) = &observation.kind
            && matches!(
                state,
                ReceiverLifecycleState::Disconnecting | ReceiverLifecycleState::Unknown
            )
        {
            return Err(LifecycleObservationError::RestrictedReceiverTransition(
                *state,
            ));
        }

        let machine = receivers
            .get(&receiver_id)
            .ok_or_else(|| LifecycleObservationError::MissingReceiver(receiver_id.clone()))?;
        let Some(current) = machine.current() else {
            return Ok(LifecycleObservationOutcome::Ignored(
                ApplyOutcome::RejectedReceiverAbsent,
            ));
        };
        let receiver_before = current.lifecycle().value();
        let device_id = observation.kind.device_id().cloned();
        let device_before = device_id
            .as_ref()
            .and_then(|device_id| current.device(device_id));
        let before_presence = device_before.map(hfx_core::DeviceLifecycle::presence);
        let updates_battery = observation.kind.updates_battery();
        let event_context = ObservationEventContext {
            receiver_id: receiver_id.clone(),
            generation_id,
            receiver_before,
            device_id,
            before_presence,
            updates_battery,
        };

        let mut next_receivers = receivers.clone();
        let mut next_events = events.clone();
        let mut emitted = StagedEvents::default();
        let machine = next_receivers
            .get_mut(&receiver_id)
            .ok_or_else(|| LifecycleObservationError::MissingReceiver(receiver_id.clone()))?;
        let outcome = apply_kind(machine, observation.kind, observation.stamp)?;
        if outcome != ApplyOutcome::Applied {
            return Ok(LifecycleObservationOutcome::Ignored(outcome));
        }

        let current = machine
            .current()
            .ok_or_else(|| LifecycleObservationError::MissingReceiver(receiver_id.clone()))?;
        let receiver_after = current.lifecycle().value();
        let device_id = event_context.device_id.clone();
        let device_presence_before = event_context.before_presence;
        let device_presence_after = device_id
            .as_ref()
            .and_then(|device_id| current.device(device_id))
            .map(hfx_core::DeviceLifecycle::presence);
        let event_kinds =
            append_observation_events(event_context, current, &mut next_events, &mut emitted)?;

        *receivers = next_receivers;
        *events = next_events;
        emitted.publish(sink);
        Ok(LifecycleObservationOutcome::Applied(
            AppliedLifecycleObservation {
                receiver_id,
                generation_id,
                receiver_before,
                receiver_after,
                device_id,
                device_presence_before,
                device_presence_after,
                events: event_kinds,
            },
        ))
    }
}

struct ObservationEventContext {
    receiver_id: ReceiverId,
    generation_id: GenerationId,
    receiver_before: ReceiverLifecycleState,
    device_id: Option<LogicalDeviceId>,
    before_presence: Option<PresenceState>,
    updates_battery: bool,
}

fn append_observation_events(
    context: ObservationEventContext,
    current: &hfx_core::ReceiverGenerationLifecycle,
    events: &mut BoundedEventLog,
    emitted: &mut StagedEvents,
) -> Result<Vec<EventKind>, LifecycleObservationError> {
    let mut event_kinds = Vec::with_capacity(2);
    let receiver_after = current.lifecycle().value();
    if context.receiver_before != receiver_after {
        let kind = receiver_event(receiver_after)?;
        emitted.append_lifecycle(
            events,
            kind,
            context.receiver_id.clone(),
            context.generation_id,
            None,
        )?;
        event_kinds.push(kind);
    }

    if let Some(device_id) = context.device_id {
        let after_presence = current
            .device(&device_id)
            .map(hfx_core::DeviceLifecycle::presence);
        if context.before_presence != after_presence
            && let Some(presence) = after_presence
        {
            let kind = presence_event(presence);
            emitted.append_lifecycle(
                events,
                kind,
                context.receiver_id.clone(),
                context.generation_id,
                Some(device_id.clone()),
            )?;
            event_kinds.push(kind);
        }
        if context.updates_battery {
            emitted.append_lifecycle(
                events,
                EventKind::BatteryUpdated,
                context.receiver_id,
                context.generation_id,
                Some(device_id),
            )?;
            event_kinds.push(EventKind::BatteryUpdated);
        }
    }
    Ok(event_kinds)
}

fn receiver_event(state: ReceiverLifecycleState) -> Result<EventKind, LifecycleObservationError> {
    match state {
        ReceiverLifecycleState::Active => Ok(EventKind::ReceiverAvailable),
        ReceiverLifecycleState::Suspended | ReceiverLifecycleState::PartiallySuspended => {
            Ok(EventKind::ReceiverSuspended)
        }
        ReceiverLifecycleState::Disconnecting | ReceiverLifecycleState::Unknown => Err(
            LifecycleObservationError::RestrictedReceiverTransition(state),
        ),
    }
}

fn apply_kind(
    machine: &mut ReceiverLifecycleMachine,
    kind: LifecycleObservationKind,
    stamp: ObservationStamp,
) -> Result<ApplyOutcome, LifecycleError> {
    match kind {
        LifecycleObservationKind::ReceiverState(value) => {
            Ok(machine.transition_receiver(value, stamp))
        }
        LifecycleObservationKind::RegisterDevice(identity) => {
            machine.register_device(identity, stamp)
        }
        LifecycleObservationKind::RegisterEndpoint {
            device_id,
            identity,
        } => machine.register_endpoint(&device_id, identity, stamp),
        LifecycleObservationKind::Pairing { device_id, value } => {
            Ok(machine.observe_pairing(&device_id, value, stamp))
        }
        LifecycleObservationKind::BatteryReported {
            device_id,
            percentage,
        } => Ok(machine.observe_battery_reported(&device_id, percentage, stamp)),
        LifecycleObservationKind::BatteryUnavailable { device_id } => {
            Ok(machine.observe_battery_unavailable(&device_id, stamp))
        }
        LifecycleObservationKind::BatteryStale { device_id } => {
            Ok(machine.mark_battery_stale(&device_id, stamp))
        }
        LifecycleObservationKind::Route {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_route(&device_id, &endpoint_id, value, stamp),
        LifecycleObservationKind::Power {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_power(&device_id, &endpoint_id, value, stamp),
        LifecycleObservationKind::Sleep {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_sleep(&device_id, &endpoint_id, value, stamp),
        LifecycleObservationKind::Activity {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_activity(&device_id, &endpoint_id, value, stamp),
        LifecycleObservationKind::Contact {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_contact(&device_id, &endpoint_id, value, stamp),
        LifecycleObservationKind::Freshness {
            device_id,
            endpoint_id,
            value,
        } => machine.observe_freshness(&device_id, &endpoint_id, value, stamp),
    }
}

const fn presence_event(presence: PresenceState) -> EventKind {
    match presence {
        PresenceState::Available => EventKind::DeviceAvailable,
        PresenceState::Sleeping => EventKind::DeviceSleeping,
        PresenceState::Unavailable => EventKind::DeviceUnavailable,
        PresenceState::Unknown => EventKind::DeviceUnknown,
    }
}
