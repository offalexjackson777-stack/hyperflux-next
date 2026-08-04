// SPDX-License-Identifier: GPL-2.0-only

use crate::clock::VirtualClock;
use crate::event::{InitialState, SimulatorEvent};
use crate::state::{
    BatteryState, DeviceSnapshot, EvidenceCell, ReplayMetrics, RestoreSnapshot, SimulatorError,
    StateSnapshot,
};
use hfx_domain::{
    ActivityState, ApplyOutcome, ContactState, DeviceKind, FixtureSource, FreshnessState,
    GenerationId, LogicalDeviceId, PairingState, PowerState, PresenceState, ProfileKind,
    ReceiverLifecycleState, RouteState, SleepState, TransportOutcome,
};
use hfx_profiles::{ProfileRecord, profile_by_id};
use std::collections::{BTreeMap, BTreeSet};

pub struct Simulator {
    clock: VirtualClock,
    snapshot: StateSnapshot,
    metrics: ReplayMetrics,
}

impl Simulator {
    /// Creates a simulator from a profile-bound initial state.
    ///
    /// # Errors
    ///
    /// Returns [`SimulatorError::InvalidScenario`] when a receiver, surface, or
    /// child profile is missing, has the wrong kind, or conflicts with the
    /// declared product identity.
    pub fn new(source: FixtureSource, initial: &InitialState) -> Result<Self, SimulatorError> {
        let receiver = profile_by_id(initial.receiver_profile_id.as_str()).ok_or_else(|| {
            SimulatorError::InvalidScenario("receiver profile is not registered".to_owned())
        })?;
        if receiver.profile_kind != ProfileKind::Receiver {
            return Err(SimulatorError::InvalidScenario(
                "receiver profile id does not name a receiver".to_owned(),
            ));
        }
        if let Some(surface_id) = &initial.surface_profile_id {
            let surface = profile_by_id(surface_id.as_str()).ok_or_else(|| {
                SimulatorError::InvalidScenario("surface profile is not registered".to_owned())
            })?;
            if surface.profile_kind != ProfileKind::Surface {
                return Err(SimulatorError::InvalidScenario(
                    "surface profile id does not name surface metadata".to_owned(),
                ));
            }
        }
        let mut devices = BTreeMap::new();
        for child in &initial.children {
            if matches!(child.device_kind, DeviceKind::Receiver | DeviceKind::Mat) {
                return Err(SimulatorError::InvalidScenario(
                    "initial child kind must be mouse, keyboard, or unknown".to_owned(),
                ));
            }
            if let Some(profile_id) = &child.profile_id {
                let profile = profile_by_id(profile_id.as_str()).ok_or_else(|| {
                    SimulatorError::InvalidScenario(format!(
                        "child profile is not registered: {profile_id}"
                    ))
                })?;
                validate_child_profile(profile, child.device_kind, child.product_id.get())?;
            }
            let contact = if child.device_kind == DeviceKind::Mouse {
                ContactState::Unknown
            } else {
                ContactState::NotApplicable
            };
            let mut snapshot = DeviceSnapshot {
                logical_device_id: child.logical_device_id.clone(),
                device_kind: child.device_kind,
                product_id: child.product_id,
                profile_id: child.profile_id.clone(),
                pairing: EvidenceCell {
                    value: child.pairing,
                    observed_at_ms: Some(0),
                },
                route: EvidenceCell::unknown(RouteState::Unknown),
                power: EvidenceCell::unknown(PowerState::Unknown),
                sleep: EvidenceCell::unknown(SleepState::Unknown),
                contact: EvidenceCell::unknown(contact),
                activity: EvidenceCell::unknown(ActivityState::Unknown),
                freshness: EvidenceCell::unknown(FreshnessState::Unknown),
                battery: EvidenceCell::unknown(BatteryState::Unknown),
                presence: PresenceState::Unknown,
            };
            snapshot.derive_presence();
            if devices
                .insert(child.logical_device_id.clone(), snapshot)
                .is_some()
            {
                return Err(SimulatorError::InvalidScenario(format!(
                    "duplicate logical device id: {}",
                    child.logical_device_id
                )));
            }
        }
        Ok(Self {
            clock: VirtualClock::new(0),
            snapshot: StateSnapshot {
                source,
                test_fixture: true,
                hardware_claim_authority: false,
                receiver_profile_id: initial.receiver_profile_id.clone(),
                receiver_generation: initial.receiver_generation,
                receiver_connected: true,
                receiver_lifecycle: EvidenceCell {
                    value: ReceiverLifecycleState::Active,
                    observed_at_ms: Some(0),
                },
                surface_profile_id: initial.surface_profile_id.clone(),
                devices,
                pending_restore: None,
            },
            metrics: ReplayMetrics::default(),
        })
    }

    /// Advances virtual time without consulting the wall clock.
    ///
    /// # Errors
    ///
    /// Returns an error if `target_ms` would move virtual time backwards.
    pub fn advance_to(&mut self, target_ms: u64) -> Result<(), SimulatorError> {
        self.clock.advance_to(target_ms)?;
        Ok(())
    }

    #[must_use]
    pub const fn now_ms(&self) -> u64 {
        self.clock.now_ms()
    }

    #[must_use]
    pub fn snapshot(&self) -> &StateSnapshot {
        &self.snapshot
    }

    #[must_use]
    pub const fn metrics(&self) -> ReplayMetrics {
        self.metrics
    }

    /// Records the bounded replay queue's peak depth.
    ///
    /// # Errors
    ///
    /// Returns an error when the platform queue depth cannot be represented in
    /// the portable replay metric.
    pub fn set_peak_queue_depth(&mut self, depth: usize) -> Result<(), SimulatorError> {
        self.metrics.peak_queue_depth = u64::try_from(depth)
            .map_err(|_| SimulatorError::InvalidScenario("queue depth exceeds u64".to_owned()))?;
        Ok(())
    }

    pub fn apply(
        &mut self,
        event: &SimulatorEvent,
        generation_id: GenerationId,
        observed_at_ms: u64,
    ) -> ApplyOutcome {
        let outcome = self.apply_inner(event, generation_id, observed_at_ms);
        self.metrics.events = self.metrics.events.saturating_add(1);
        match outcome {
            ApplyOutcome::Applied => self.metrics.applied = self.metrics.applied.saturating_add(1),
            ApplyOutcome::IgnoredOlderObservation => {
                self.metrics.ignored_older_observations =
                    self.metrics.ignored_older_observations.saturating_add(1);
            }
            ApplyOutcome::RecordedMalformedObservation => {
                self.metrics.malformed_observations =
                    self.metrics.malformed_observations.saturating_add(1);
            }
            _ => self.metrics.rejected = self.metrics.rejected.saturating_add(1),
        }
        match (event, outcome) {
            (
                SimulatorEvent::LightingFrame {
                    outcome: TransportOutcome::Delivered,
                    ..
                },
                ApplyOutcome::Applied,
            ) => self.metrics.delivered_frames = self.metrics.delivered_frames.saturating_add(1),
            (
                SimulatorEvent::LightingFrame {
                    outcome: TransportOutcome::Failed,
                    ..
                },
                ApplyOutcome::RejectedTransportFailure,
            ) => self.metrics.failed_frames = self.metrics.failed_frames.saturating_add(1),
            _ => {}
        }
        outcome
    }

    fn apply_inner(
        &mut self,
        event: &SimulatorEvent,
        generation_id: GenerationId,
        observed_at_ms: u64,
    ) -> ApplyOutcome {
        if matches!(event, SimulatorEvent::ReceiverConnected) {
            return self.connect_receiver(generation_id, observed_at_ms);
        }
        if generation_id != self.snapshot.receiver_generation {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if !self.snapshot.receiver_connected {
            return ApplyOutcome::RejectedReceiverAbsent;
        }
        match event {
            SimulatorEvent::ReceiverDisconnected => self.disconnect_receiver(observed_at_ms),
            SimulatorEvent::ReceiverConnected => unreachable!("handled before generation check"),
            SimulatorEvent::ReceiverLifecycle { state } => {
                self.apply_receiver_lifecycle(*state, observed_at_ms)
            }
            SimulatorEvent::DevicePairing { .. }
            | SimulatorEvent::RouteObserved { .. }
            | SimulatorEvent::PowerObserved { .. }
            | SimulatorEvent::SleepObserved { .. }
            | SimulatorEvent::ContactObserved { .. }
            | SimulatorEvent::ActivityObserved { .. }
            | SimulatorEvent::FreshnessObserved { .. }
            | SimulatorEvent::BatteryReported { .. }
            | SimulatorEvent::BatteryUnavailable { .. }
            | SimulatorEvent::MalformedObservation { .. } => {
                self.apply_device_event(event, observed_at_ms)
            }
            SimulatorEvent::LightingFrame { .. }
            | SimulatorEvent::RestoreStarted { .. }
            | SimulatorEvent::RestoreTarget { .. } => self.apply_write_event(event, generation_id),
        }
    }

    fn disconnect_receiver(&mut self, observed_at_ms: u64) -> ApplyOutcome {
        self.snapshot.receiver_connected = false;
        self.snapshot.receiver_lifecycle = EvidenceCell {
            value: ReceiverLifecycleState::Disconnecting,
            observed_at_ms: Some(observed_at_ms),
        };
        if self.snapshot.pending_restore.take().is_some() {
            self.metrics.invalidated_restores = self.metrics.invalidated_restores.saturating_add(1);
        }
        for device in self.snapshot.devices.values_mut() {
            device.reset_generation_evidence();
        }
        ApplyOutcome::Applied
    }

    fn apply_receiver_lifecycle(
        &mut self,
        state: ReceiverLifecycleState,
        observed_at_ms: u64,
    ) -> ApplyOutcome {
        if state == ReceiverLifecycleState::Unknown {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        observation_outcome(
            self.snapshot
                .receiver_lifecycle
                .apply(state, observed_at_ms),
        )
    }

    fn apply_device_event(&mut self, event: &SimulatorEvent, observed_at_ms: u64) -> ApplyOutcome {
        match event {
            SimulatorEvent::DevicePairing { device_id, state } => {
                if *state == PairingState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.pairing.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::RouteObserved { device_id, state } => {
                if *state == RouteState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.route.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::PowerObserved { device_id, state } => {
                if *state == PowerState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.power.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::SleepObserved { device_id, state } => {
                if *state == SleepState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.sleep.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::ContactObserved { device_id, state } => {
                apply_contact_observation(&mut self.snapshot, device_id, *state, observed_at_ms)
            }
            SimulatorEvent::ActivityObserved { device_id, state } => {
                if *state == ActivityState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.activity.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::FreshnessObserved { device_id, state } => {
                if *state == FreshnessState::Unknown {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device.freshness.apply(*state, observed_at_ms)
                })
            }
            SimulatorEvent::BatteryReported {
                device_id,
                percentage,
            } => apply_device_value(&mut self.snapshot, device_id, |device| {
                device.battery.apply(
                    BatteryState::Reported {
                        percentage: *percentage,
                    },
                    observed_at_ms,
                )
            }),
            SimulatorEvent::BatteryUnavailable { device_id } => {
                apply_device_value(&mut self.snapshot, device_id, |device| {
                    device
                        .battery
                        .apply(BatteryState::Unavailable, observed_at_ms)
                })
            }
            SimulatorEvent::MalformedObservation { device_id, .. } => {
                if self.snapshot.devices.contains_key(device_id) {
                    ApplyOutcome::RecordedMalformedObservation
                } else {
                    ApplyOutcome::RejectedUnknownDevice
                }
            }
            _ => unreachable!("device dispatcher received a non-device event"),
        }
    }

    fn apply_write_event(
        &mut self,
        event: &SimulatorEvent,
        generation_id: GenerationId,
    ) -> ApplyOutcome {
        match event {
            SimulatorEvent::LightingFrame {
                targets, outcome, ..
            } => {
                if let Some(rejection) = validate_write_targets(&self.snapshot, targets) {
                    return rejection;
                }
                if *outcome == TransportOutcome::Failed {
                    return ApplyOutcome::RejectedTransportFailure;
                }
                ApplyOutcome::Applied
            }
            SimulatorEvent::RestoreStarted {
                restore_id,
                targets,
            } => {
                if self.snapshot.pending_restore.is_some() || targets.is_empty() {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                if let Some(rejection) = validate_write_targets(&self.snapshot, targets) {
                    return rejection;
                }
                let target_set = targets.iter().cloned().collect::<BTreeSet<_>>();
                if target_set.len() != targets.len() {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                self.snapshot.pending_restore = Some(RestoreSnapshot {
                    restore_id: restore_id.clone(),
                    generation_id,
                    targets: target_set,
                    delivered: BTreeSet::new(),
                });
                ApplyOutcome::Applied
            }
            SimulatorEvent::RestoreTarget {
                restore_id,
                device_id,
                outcome,
            } => {
                let Some(restore) = self.snapshot.pending_restore.as_mut() else {
                    return ApplyOutcome::RejectedInvalidTransition;
                };
                if restore.restore_id != *restore_id
                    || restore.generation_id != generation_id
                    || !restore.targets.contains(device_id)
                    || restore.delivered.contains(device_id)
                {
                    return ApplyOutcome::RejectedInvalidTransition;
                }
                if *outcome == TransportOutcome::Failed {
                    self.snapshot.pending_restore = None;
                    return ApplyOutcome::RejectedTransportFailure;
                }
                restore.delivered.insert(device_id.clone());
                if restore.delivered == restore.targets {
                    self.snapshot.pending_restore = None;
                    self.metrics.completed_restores =
                        self.metrics.completed_restores.saturating_add(1);
                }
                ApplyOutcome::Applied
            }
            _ => unreachable!("write dispatcher received a non-write event"),
        }
    }

    fn connect_receiver(
        &mut self,
        generation_id: GenerationId,
        observed_at_ms: u64,
    ) -> ApplyOutcome {
        if self.snapshot.receiver_connected || generation_id <= self.snapshot.receiver_generation {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        if self.snapshot.pending_restore.take().is_some() {
            self.metrics.invalidated_restores = self.metrics.invalidated_restores.saturating_add(1);
        }
        self.snapshot.receiver_generation = generation_id;
        self.snapshot.receiver_connected = true;
        self.snapshot.receiver_lifecycle = EvidenceCell {
            value: ReceiverLifecycleState::Active,
            observed_at_ms: Some(observed_at_ms),
        };
        for device in self.snapshot.devices.values_mut() {
            device.reset_generation_evidence();
        }
        ApplyOutcome::Applied
    }
}

fn validate_child_profile(
    profile: &ProfileRecord,
    device_kind: DeviceKind,
    product_id: u16,
) -> Result<(), SimulatorError> {
    if profile.profile_kind != ProfileKind::Child
        || profile.device_kind != device_kind
        || profile.product_id != Some(product_id)
    {
        return Err(SimulatorError::InvalidScenario(
            "child profile, kind, and product id do not match".to_owned(),
        ));
    }
    Ok(())
}

fn apply_device_value(
    snapshot: &mut StateSnapshot,
    device_id: &LogicalDeviceId,
    update: impl FnOnce(&mut DeviceSnapshot) -> bool,
) -> ApplyOutcome {
    let Some(device) = snapshot.devices.get_mut(device_id) else {
        return ApplyOutcome::RejectedUnknownDevice;
    };
    let changed = update(device);
    device.derive_presence();
    observation_outcome(changed)
}

fn apply_contact_observation(
    snapshot: &mut StateSnapshot,
    device_id: &LogicalDeviceId,
    state: ContactState,
    observed_at_ms: u64,
) -> ApplyOutcome {
    if matches!(state, ContactState::Unknown | ContactState::NotApplicable) {
        return ApplyOutcome::RejectedInvalidTransition;
    }
    let Some(device) = snapshot.devices.get_mut(device_id) else {
        return ApplyOutcome::RejectedUnknownDevice;
    };
    if device.device_kind != DeviceKind::Mouse {
        return ApplyOutcome::RejectedInvalidTransition;
    }
    let changed = device.contact.apply(state, observed_at_ms);
    device.derive_presence();
    observation_outcome(changed)
}

const fn observation_outcome(changed: bool) -> ApplyOutcome {
    if changed {
        ApplyOutcome::Applied
    } else {
        ApplyOutcome::IgnoredOlderObservation
    }
}

fn validate_write_targets(
    snapshot: &StateSnapshot,
    targets: &[LogicalDeviceId],
) -> Option<ApplyOutcome> {
    if targets.is_empty() || targets.len() > 32 {
        return Some(ApplyOutcome::RejectedInvalidTransition);
    }
    let mut unique = BTreeSet::new();
    for target in targets {
        if !unique.insert(target) {
            return Some(ApplyOutcome::RejectedInvalidTransition);
        }
        let Some(device) = snapshot.devices.get(target) else {
            return Some(ApplyOutcome::RejectedUnknownDevice);
        };
        let Some(profile_id) = &device.profile_id else {
            return Some(ApplyOutcome::RejectedUnqualifiedWrite);
        };
        let Some(profile) = profile_by_id(profile_id.as_str()) else {
            return Some(ApplyOutcome::RejectedUnqualifiedWrite);
        };
        if !profile
            .capabilities
            .iter()
            .any(|capability| capability.id == "lighting.direct-frame" && capability.writable)
        {
            return Some(ApplyOutcome::RejectedUnqualifiedWrite);
        }
        if device.pairing.value != PairingState::Paired
            || device.route.value != RouteState::Available
            || device.power.value == PowerState::Off
            || device.sleep.value == SleepState::Asleep
        {
            return Some(ApplyOutcome::RejectedUnavailableRoute);
        }
    }
    None
}
