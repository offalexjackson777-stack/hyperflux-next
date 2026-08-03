// SPDX-License-Identifier: GPL-2.0-only

//! Explicit receiver lifecycle state machine.

use super::{
    BatteryValue, ChildIdentity, DeviceLifecycle, EndpointIdentity, EndpointLifecycle,
    LifecycleError, LifecycleFact, LifecycleLimits, MAX_RETIRED_GENERATIONS, ObservationStamp,
    ReceiverGenerationLifecycle,
};
use hfx_domain::{
    ActivityState, ApplyOutcome, BatteryPercent, ContactState, EndpointId, FreshnessState,
    GenerationId, LogicalDeviceId, PairingState, PowerState, ReceiverId, ReceiverLifecycleState,
    RouteState, SleepState,
};
use std::collections::VecDeque;

/// Explicit receiver lifecycle with irreversible generation replacement.
///
/// The machine exposes passive state only. A current generation, qualified
/// child, or available route does not confer permission to write hardware.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverLifecycleMachine {
    receiver_id: ReceiverId,
    limits: LifecycleLimits,
    latest_generation: Option<GenerationId>,
    retired_generations: VecDeque<GenerationId>,
    last_receiver_observation: Option<ObservationStamp>,
    current: Option<ReceiverGenerationLifecycle>,
}

impl ReceiverLifecycleMachine {
    /// Creates an absent receiver lifecycle.
    ///
    /// # Errors
    ///
    /// This constructor is fallible to keep future limit validation at the
    /// boundary; currently all [`LifecycleLimits`] values are prevalidated.
    pub fn new(receiver_id: ReceiverId, limits: LifecycleLimits) -> Result<Self, LifecycleError> {
        if limits.max_devices == 0 || limits.max_endpoints_per_device == 0 {
            return Err(LifecycleError::InvalidLimits);
        }
        Ok(Self {
            receiver_id,
            limits,
            latest_generation: None,
            retired_generations: VecDeque::new(),
            last_receiver_observation: None,
            current: None,
        })
    }

    #[must_use]
    pub const fn receiver_id(&self) -> &ReceiverId {
        &self.receiver_id
    }

    #[must_use]
    pub const fn latest_generation(&self) -> Option<GenerationId> {
        self.latest_generation
    }

    #[must_use]
    pub const fn current(&self) -> Option<&ReceiverGenerationLifecycle> {
        self.current.as_ref()
    }

    /// Discovers a generation only while the receiver is absent.
    pub fn discover(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        if self.latest_generation == Some(stamp.generation_id())
            || self.retired_generations.contains(&stamp.generation_id())
        {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if self.current.is_some() {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if self
            .last_receiver_observation
            .as_ref()
            .is_some_and(|last| !stamp.lifetime_at_or_after(last))
        {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        self.latest_generation = Some(stamp.generation_id());
        self.last_receiver_observation = Some(stamp.clone());
        self.current = Some(ReceiverGenerationLifecycle::new(stamp));
        ApplyOutcome::Applied
    }

    /// Atomically retires the current generation and starts a newer empty one.
    pub fn replace_generation(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        let Some(current) = self.current.as_ref() else {
            return if self.latest_generation == Some(stamp.generation_id()) {
                ApplyOutcome::RejectedStaleGeneration
            } else {
                ApplyOutcome::RejectedReceiverAbsent
            };
        };
        if stamp.generation_id() == current.generation_id
            || self.retired_generations.contains(&stamp.generation_id())
        {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if !current.accepts_replacement(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        let retired = current.generation_id;
        self.remember_retired_generation(retired);
        self.latest_generation = Some(stamp.generation_id());
        self.last_receiver_observation = Some(stamp.clone());
        self.current = Some(ReceiverGenerationLifecycle::new(stamp));
        ApplyOutcome::Applied
    }

    /// Applies one declared receiver transition for the current generation.
    pub fn transition_receiver(
        &mut self,
        target: ReceiverLifecycleState,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.generation_rejection(stamp.generation_id()) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        if !current.accepts_receiver_observation(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        let from = current.lifecycle.value();
        if !valid_receiver_transition(from, target) {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        let changed = current.lifecycle.apply(target, stamp.clone());
        debug_assert!(changed, "receiver ordering was checked before mutation");
        self.last_receiver_observation = Some(stamp);
        ApplyOutcome::Applied
    }

    /// Completes an already-declared disconnect and retires its generation.
    pub fn complete_disconnect(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        if let Some(outcome) = self.generation_rejection(stamp.generation_id()) {
            return outcome;
        }
        let Some(current) = self.current.as_ref() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        if !current.accepts_receiver_observation(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if current.lifecycle.value() != ReceiverLifecycleState::Disconnecting {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        let retired = current.generation_id;
        self.last_receiver_observation = Some(stamp);
        self.current = None;
        self.remember_retired_generation(retired);
        ApplyOutcome::Applied
    }

    /// Registers or refreshes one independent logical child.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid child identity, immutable identity changes,
    /// or exhausted bounded capacity.
    pub fn register_device(
        &mut self,
        identity: ChildIdentity,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        if !stamp.at_or_after(&current.activated_at) {
            return Ok(ApplyOutcome::IgnoredOlderObservation);
        }
        if let Some(device) = current.devices.get_mut(identity.device_id()) {
            if !device.accepts(&stamp) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            if device.identity != identity {
                return Err(LifecycleError::DeviceIdentityConflict(
                    identity.device_id().clone(),
                ));
            }
            if !stamp.strictly_after(&device.registered_at) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            device.note(&stamp);
            device.registered_at = stamp;
            return Ok(ApplyOutcome::Applied);
        }
        if current.devices.len() >= self.limits.max_devices {
            return Err(LifecycleError::DeviceCapacity);
        }
        current.devices.insert(
            identity.device_id().clone(),
            DeviceLifecycle::new(identity, stamp),
        );
        Ok(ApplyOutcome::Applied)
    }

    /// Applies receiver-inventory pairing evidence without changing routes.
    pub fn observe_pairing(
        &mut self,
        device_id: &LogicalDeviceId,
        value: PairingState,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if value == PairingState::Unknown {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        self.observe_device_fact(device_id, value, stamp, |device| &mut device.pairing)
    }

    /// Applies a reported battery percentage and marks it fresh atomically.
    pub fn observe_battery_reported(
        &mut self,
        device_id: &LogicalDeviceId,
        percentage: BatteryPercent,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        self.observe_battery_value(device_id, BatteryValue::Reported(percentage), stamp)
    }

    /// Records that the receiver cannot currently provide a battery value.
    pub fn observe_battery_unavailable(
        &mut self,
        device_id: &LogicalDeviceId,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        self.observe_battery_value(device_id, BatteryValue::Unavailable, stamp)
    }

    /// Marks an existing battery observation stale without discarding its value.
    pub fn mark_battery_stale(
        &mut self,
        device_id: &LogicalDeviceId,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if !device.battery.value.is_observed() {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        if !device.battery.freshness.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        if device.battery.mark_stale(stamp) {
            ApplyOutcome::Applied
        } else {
            ApplyOutcome::IgnoredOlderObservation
        }
    }

    /// Registers or refreshes one route without modifying its independent facts.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown child, immutable identity change, or
    /// exhausted endpoint capacity.
    pub fn register_endpoint(
        &mut self,
        device_id: &LogicalDeviceId,
        identity: EndpointIdentity,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        if !device.accepts(&stamp) {
            return Ok(ApplyOutcome::IgnoredOlderObservation);
        }
        if let Some(endpoint) = device.endpoints.get(identity.endpoint_id()) {
            if endpoint.identity != identity {
                return Err(LifecycleError::EndpointIdentityConflict(
                    identity.endpoint_id().clone(),
                ));
            }
            if !stamp.strictly_after(&endpoint.registered_at) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            device.note(&stamp);
            let Some(endpoint) = device.endpoints.get_mut(identity.endpoint_id()) else {
                return Err(LifecycleError::UnknownEndpoint(
                    identity.endpoint_id().clone(),
                ));
            };
            endpoint.registered_at = stamp;
            return Ok(ApplyOutcome::Applied);
        }
        if device.endpoints.len() >= self.limits.max_endpoints_per_device {
            return Err(LifecycleError::EndpointCapacity(device_id.clone()));
        }
        device.note(&stamp);
        device.endpoints.insert(
            identity.endpoint_id().clone(),
            EndpointLifecycle::new(identity, stamp, device.identity.device_kind()),
        );
        Ok(ApplyOutcome::Applied)
    }

    /// Applies passive route availability evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_route(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: RouteState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == RouteState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.route
        })
    }

    /// Applies passive endpoint power evidence without deriving other facts.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_power(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: PowerState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == PowerState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.power
        })
    }

    /// Applies passive endpoint sleep evidence without changing pairing.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_sleep(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: SleepState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == SleepState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.sleep
        })
    }

    /// Applies passive endpoint activity evidence.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_activity(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: ActivityState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == ActivityState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.activity
        })
    }

    /// Applies mouse-only mat-contact evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_contact(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: ContactState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if matches!(value, ContactState::Unknown | ContactState::NotApplicable) {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        if device.identity.device_kind() != hfx_domain::DeviceKind::Mouse {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        apply_endpoint_fact(device, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.contact
        })
    }

    /// Applies passive freshness evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_freshness(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: FreshnessState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == FreshnessState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.freshness
        })
    }

    fn generation_rejection(&self, generation_id: GenerationId) -> Option<ApplyOutcome> {
        match self.current.as_ref() {
            Some(current) if generation_id == current.generation_id => None,
            Some(_) => Some(ApplyOutcome::RejectedStaleGeneration),
            None if self.latest_generation == Some(generation_id)
                || self.retired_generations.contains(&generation_id) =>
            {
                Some(ApplyOutcome::RejectedStaleGeneration)
            }
            None => Some(ApplyOutcome::RejectedReceiverAbsent),
        }
    }

    fn remember_retired_generation(&mut self, generation_id: GenerationId) {
        if self.retired_generations.contains(&generation_id) {
            return;
        }
        if self.retired_generations.len() == MAX_RETIRED_GENERATIONS {
            self.retired_generations.pop_front();
        }
        self.retired_generations.push_back(generation_id);
    }

    fn device_observation_rejection(&self, stamp: &ObservationStamp) -> Option<ApplyOutcome> {
        let rejection = self.generation_rejection(stamp.generation_id());
        if rejection.is_some() {
            return rejection;
        }
        self.current.as_ref().and_then(|current| {
            (current.lifecycle.value() == ReceiverLifecycleState::Disconnecting)
                .then_some(ApplyOutcome::RejectedReceiverAbsent)
        })
    }

    fn observe_device_fact<T>(
        &mut self,
        device_id: &LogicalDeviceId,
        value: T,
        stamp: ObservationStamp,
        select: fn(&mut DeviceLifecycle) -> &mut LifecycleFact<T>,
    ) -> ApplyOutcome
    where
        T: Copy,
    {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        let applied = select(device).apply(value, stamp);
        if !applied {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        ApplyOutcome::Applied
    }

    fn observe_battery_value(
        &mut self,
        device_id: &LogicalDeviceId,
        value: BatteryValue,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if !device.battery.value.accepts(&stamp) || !device.battery.freshness.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        if device.battery.apply_value(value, stamp) {
            ApplyOutcome::Applied
        } else {
            ApplyOutcome::IgnoredOlderObservation
        }
    }

    fn observe_endpoint_fact<T>(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: T,
        stamp: ObservationStamp,
        select: fn(&mut EndpointLifecycle) -> &mut LifecycleFact<T>,
    ) -> Result<ApplyOutcome, LifecycleError>
    where
        T: Copy,
    {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        apply_endpoint_fact(device, endpoint_id, value, stamp, select)
    }
}

fn apply_endpoint_fact<T>(
    device: &mut DeviceLifecycle,
    endpoint_id: &EndpointId,
    value: T,
    stamp: ObservationStamp,
    select: fn(&mut EndpointLifecycle) -> &mut LifecycleFact<T>,
) -> Result<ApplyOutcome, LifecycleError>
where
    T: Copy,
{
    if !device.accepts(&stamp) {
        return Ok(ApplyOutcome::IgnoredOlderObservation);
    }
    if !device.endpoints.contains_key(endpoint_id) {
        return Err(LifecycleError::UnknownEndpoint(endpoint_id.clone()));
    }
    device.note(&stamp);
    let Some(endpoint) = device.endpoints.get_mut(endpoint_id) else {
        return Err(LifecycleError::UnknownEndpoint(endpoint_id.clone()));
    };
    let applied = select(endpoint).apply(value, stamp);
    if !applied {
        return Ok(ApplyOutcome::IgnoredOlderObservation);
    }
    Ok(ApplyOutcome::Applied)
}

const fn valid_receiver_transition(
    from: ReceiverLifecycleState,
    to: ReceiverLifecycleState,
) -> bool {
    if matches!(from, ReceiverLifecycleState::Unknown)
        || matches!(to, ReceiverLifecycleState::Unknown)
    {
        return false;
    }
    !matches!(from, ReceiverLifecycleState::Disconnecting)
        || matches!(to, ReceiverLifecycleState::Disconnecting)
}
