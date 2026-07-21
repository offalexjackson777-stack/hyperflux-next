// SPDX-License-Identifier: GPL-2.0-only

use crate::RuntimeProfileAuthority;
use hfx_core::{
    BatteryValue, BoundedEventLog, DeviceLifecycle, EndpointLifecycle, LeaseManager,
    ObservationStamp, ReceiverLifecycleRegistry,
};
use hfx_domain::{
    EvidenceConfidence, GenerationId, MonotonicMs, ReceiverId, RestoreState, SupportLevel,
    TelemetryAvailability,
};
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, EndpointSnapshot, LogicalDeviceSnapshot, ReceiverSnapshot,
    SnapshotValidationError, validate_bridge_snapshot,
};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReceiverRestorationSnapshot {
    pub stable_restore_enabled: bool,
    pub restore_state: RestoreState,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RestorationProjectionError {
    Unavailable,
}

impl fmt::Display for RestorationProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("restoration state is unavailable")
    }
}

impl std::error::Error for RestorationProjectionError {}

/// Supplies already-reconciled restoration state without exposing persistence.
pub trait RestorationSnapshotSource {
    /// # Errors
    ///
    /// Returns an error when authoritative restoration state cannot be read.
    fn restoration(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError>;
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DisabledRestorationSource;

impl RestorationSnapshotSource for DisabledRestorationSource {
    fn restoration(
        &self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        Ok(ReceiverRestorationSnapshot {
            stable_restore_enabled: false,
            restore_state: RestoreState::Idle,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SnapshotProjectionError {
    Restoration(RestorationProjectionError),
    InvalidSnapshot(SnapshotValidationError),
}

impl fmt::Display for SnapshotProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Restoration(error) => write!(formatter, "{error}"),
            Self::InvalidSnapshot(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for SnapshotProjectionError {}

/// Canonical application-neutral bridge snapshot projection.
#[derive(Clone, Copy, Debug)]
pub struct SnapshotProjector<'a> {
    profiles: &'a RuntimeProfileAuthority,
}

impl<'a> SnapshotProjector<'a> {
    #[must_use]
    pub const fn new(profiles: &'a RuntimeProfileAuthority) -> Self {
        Self { profiles }
    }

    /// Projects one complete cursor-bound snapshot and validates every
    /// cross-record invariant before returning it.
    ///
    /// # Errors
    ///
    /// Returns an error when restoration truth is unavailable or the composed
    /// state violates the bounded public protocol contract.
    pub fn project<R: RestorationSnapshotSource>(
        &self,
        receivers: &ReceiverLifecycleRegistry,
        leases: &mut LeaseManager,
        events: &BoundedEventLog,
        restoration: &R,
        now: MonotonicMs,
    ) -> Result<BridgeSnapshot, SnapshotProjectionError> {
        let mut projected_receivers = Vec::new();
        for machine in receivers.iter() {
            let Some(generation) = machine.current() else {
                continue;
            };
            let restoration = restoration
                .restoration(machine.receiver_id(), generation.generation_id())
                .map_err(SnapshotProjectionError::Restoration)?;
            let profile_view = self.profiles.view(receivers);
            let receiver_profile = self
                .profiles
                .binding(machine.receiver_id())
                .filter(|binding| binding.generation_id == generation.generation_id());
            let devices = generation
                .devices()
                .map(|device| {
                    Self::project_device(
                        profile_view.profile_for_device(
                            machine.receiver_id(),
                            generation.generation_id(),
                            device,
                        ),
                        device,
                    )
                })
                .collect();
            projected_receivers.push(ReceiverSnapshot {
                receiver_id: machine.receiver_id().clone(),
                generation_id: generation.generation_id(),
                profile_id: receiver_profile.map(|binding| binding.profile_id.clone()),
                profile_digest: receiver_profile.map(|binding| binding.profile_digest.clone()),
                lifecycle: generation.lifecycle().value(),
                devices,
                ownership: leases.ownership_snapshot(
                    machine.receiver_id(),
                    generation.generation_id(),
                    now,
                ),
                stable_restore_enabled: restoration.stable_restore_enabled,
                restore_state: restoration.restore_state,
            });
        }
        let snapshot = BridgeSnapshot {
            cursor: events.cursor(),
            receivers: projected_receivers,
        };
        validate_bridge_snapshot(&snapshot).map_err(SnapshotProjectionError::InvalidSnapshot)?;
        Ok(snapshot)
    }

    fn project_device(
        profile: Option<&hfx_profiles::RuntimeProfile>,
        device: &DeviceLifecycle,
    ) -> LogicalDeviceSnapshot {
        let mut capabilities = profile.map_or_else(Vec::new, |profile| {
            profile
                .capabilities
                .iter()
                .map(|capability| capability.id.clone())
                .collect()
        });
        capabilities.sort_unstable();
        LogicalDeviceSnapshot {
            device_id: device.identity().device_id().clone(),
            device_kind: device.identity().device_kind(),
            product_id: device.identity().product_id(),
            profile_id: profile.map(|profile| profile.profile_id.clone()),
            profile_digest: profile.map(|profile| profile.runtime_digest.clone()),
            pairing: device.pairing().value(),
            presence: device.presence(),
            support_level: profile.map_or(
                SupportLevel::ReadOnly,
                hfx_profiles::RuntimeProfile::support_level,
            ),
            endpoints: device.endpoints().map(project_endpoint).collect(),
            battery: project_battery(device),
            capabilities,
        }
    }
}

fn project_endpoint(endpoint: &EndpointLifecycle) -> EndpointSnapshot {
    let latest = latest_endpoint_stamp(endpoint);
    EndpointSnapshot {
        endpoint_id: endpoint.identity().endpoint_id().clone(),
        route_kind: endpoint.identity().route_kind(),
        route_state: endpoint.route().value(),
        connection_mode: endpoint.identity().connection_mode(),
        power_state: endpoint.power().value(),
        sleep_state: endpoint.sleep().value(),
        activity_state: endpoint.activity().value(),
        contact_state: endpoint.contact().value(),
        freshness: endpoint.freshness().value(),
        confidence: latest.confidence(),
        evidence_claim_id: Some(latest.evidence_claim_id().clone()),
        observed_at_ms: Some(latest.observed_at_ms()),
    }
}

fn latest_endpoint_stamp(endpoint: &EndpointLifecycle) -> &ObservationStamp {
    let mut latest = endpoint.registered_at();
    for stamp in [
        endpoint.route().stamp(),
        endpoint.power().stamp(),
        endpoint.sleep().stamp(),
        endpoint.activity().stamp(),
        endpoint.contact().stamp(),
        endpoint.freshness().stamp(),
    ]
    .into_iter()
    .flatten()
    {
        if (stamp.sequence(), stamp.observed_at_ms()) > (latest.sequence(), latest.observed_at_ms())
        {
            latest = stamp;
        }
    }
    latest
}

fn project_battery(device: &DeviceLifecycle) -> BatteryObservation {
    let battery = device.battery();
    let (availability, percentage) = match battery.value().value() {
        BatteryValue::Unknown => (TelemetryAvailability::Unknown, None),
        BatteryValue::Unavailable => (TelemetryAvailability::Unavailable, None),
        BatteryValue::Reported(percentage) => (TelemetryAvailability::Reported, Some(percentage)),
    };
    let value_stamp = battery.value().stamp();
    BatteryObservation {
        availability,
        percentage,
        freshness: battery.freshness().value(),
        confidence: value_stamp.map_or(EvidenceConfidence::Unknown, ObservationStamp::confidence),
        observed_at_ms: value_stamp.map(ObservationStamp::observed_at_ms),
    }
}
