// SPDX-License-Identifier: GPL-2.0-only

use crate::{BatteryObservation, BridgeSnapshot, LogicalDeviceSnapshot, ReceiverSnapshot};
use hfx_domain::{PresenceState, RouteState, SleepState, TelemetryAvailability};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SnapshotValidationError {
    TooManyReceivers,
    ReceiversNotCanonical,
    TooManyDevices,
    DevicesNotCanonical,
    TooManyEndpoints,
    EndpointsNotCanonical,
    TooManyCapabilities,
    CapabilitiesNotCanonical,
    TooManyOwnershipRecords,
    OwnershipNotCanonical,
    OwnershipOutsideReceiverGeneration,
    OwnershipTargetsUnknownDevice,
    BatteryValueContradiction,
    ReceiverProfileBindingContradiction,
    DeviceProfileBindingContradiction,
    PresenceContradiction,
}

impl fmt::Display for SnapshotValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::TooManyReceivers => "snapshot exceeds the receiver bound",
            Self::ReceiversNotCanonical => "snapshot receivers are duplicated or unordered",
            Self::TooManyDevices => "receiver snapshot exceeds the device bound",
            Self::DevicesNotCanonical => "receiver devices are duplicated or unordered",
            Self::TooManyEndpoints => "device snapshot exceeds the endpoint bound",
            Self::EndpointsNotCanonical => "device endpoints are duplicated or unordered",
            Self::TooManyCapabilities => "device snapshot exceeds the capability bound",
            Self::CapabilitiesNotCanonical => "device capabilities are duplicated or unordered",
            Self::TooManyOwnershipRecords => "receiver snapshot exceeds the ownership bound",
            Self::OwnershipNotCanonical => "ownership records are duplicated or unordered",
            Self::OwnershipOutsideReceiverGeneration => {
                "ownership is outside the containing receiver generation"
            }
            Self::OwnershipTargetsUnknownDevice => "ownership targets an unknown logical device",
            Self::BatteryValueContradiction => {
                "battery availability contradicts the optional percentage"
            }
            Self::ReceiverProfileBindingContradiction => {
                "receiver profile identity and digest are incomplete"
            }
            Self::DeviceProfileBindingContradiction => {
                "device profile digest lacks a profile identity"
            }
            Self::PresenceContradiction => "device presence contradicts endpoint evidence",
        })
    }
}

impl std::error::Error for SnapshotValidationError {}

fn validate_battery(battery: &BatteryObservation) -> Result<(), SnapshotValidationError> {
    let has_value = battery.percentage.is_some();
    if matches!(battery.availability, TelemetryAvailability::Reported) != has_value {
        return Err(SnapshotValidationError::BatteryValueContradiction);
    }
    Ok(())
}

fn validate_device(device: &LogicalDeviceSnapshot) -> Result<(), SnapshotValidationError> {
    if device.profile_digest.is_some() && device.profile_id.is_none() {
        return Err(SnapshotValidationError::DeviceProfileBindingContradiction);
    }
    if device.endpoints.len() > 8 {
        return Err(SnapshotValidationError::TooManyEndpoints);
    }
    if device
        .endpoints
        .windows(2)
        .any(|pair| pair[0].endpoint_id >= pair[1].endpoint_id)
    {
        return Err(SnapshotValidationError::EndpointsNotCanonical);
    }
    if device.capabilities.len() > 128 {
        return Err(SnapshotValidationError::TooManyCapabilities);
    }
    if device
        .capabilities
        .windows(2)
        .any(|pair| pair[0] >= pair[1])
    {
        return Err(SnapshotValidationError::CapabilitiesNotCanonical);
    }
    validate_battery(&device.battery)?;

    let route_available = device
        .endpoints
        .iter()
        .any(|endpoint| endpoint.route_state == RouteState::Available);
    let sleep_observed = device
        .endpoints
        .iter()
        .any(|endpoint| endpoint.sleep_state == SleepState::Asleep);
    let contradiction = match device.presence {
        PresenceState::Available => !route_available,
        PresenceState::Sleeping => !sleep_observed,
        PresenceState::Unavailable => route_available,
        PresenceState::Unknown => false,
    };
    if contradiction {
        return Err(SnapshotValidationError::PresenceContradiction);
    }
    Ok(())
}

fn validate_receiver(receiver: &ReceiverSnapshot) -> Result<(), SnapshotValidationError> {
    if receiver.profile_id.is_some() != receiver.profile_digest.is_some() {
        return Err(SnapshotValidationError::ReceiverProfileBindingContradiction);
    }
    if receiver.devices.len() > 32 {
        return Err(SnapshotValidationError::TooManyDevices);
    }
    if receiver
        .devices
        .windows(2)
        .any(|pair| pair[0].device_id >= pair[1].device_id)
    {
        return Err(SnapshotValidationError::DevicesNotCanonical);
    }
    for device in &receiver.devices {
        validate_device(device)?;
    }

    if receiver.ownership.len() > 96 {
        return Err(SnapshotValidationError::TooManyOwnershipRecords);
    }
    if receiver
        .ownership
        .windows(2)
        .any(|pair| pair[0].resource >= pair[1].resource)
    {
        return Err(SnapshotValidationError::OwnershipNotCanonical);
    }
    for ownership in &receiver.ownership {
        if ownership.resource.receiver_id != receiver.receiver_id
            || ownership.resource.generation_id != receiver.generation_id
        {
            return Err(SnapshotValidationError::OwnershipOutsideReceiverGeneration);
        }
        if !receiver
            .devices
            .iter()
            .any(|device| device.device_id == ownership.resource.device_id)
        {
            return Err(SnapshotValidationError::OwnershipTargetsUnknownDevice);
        }
    }
    Ok(())
}

/// Validates cross-record invariants on one complete bridge projection.
///
/// # Errors
///
/// Returns an error for oversized, duplicated, unordered, generation-crossing,
/// or internally contradictory snapshot facts.
pub fn validate_bridge_snapshot(snapshot: &BridgeSnapshot) -> Result<(), SnapshotValidationError> {
    if snapshot.receivers.len() > 16 {
        return Err(SnapshotValidationError::TooManyReceivers);
    }
    if snapshot
        .receivers
        .windows(2)
        .any(|pair| pair[0].receiver_id >= pair[1].receiver_id)
    {
        return Err(SnapshotValidationError::ReceiversNotCanonical);
    }
    for receiver in &snapshot.receivers {
        validate_receiver(receiver)?;
    }
    Ok(())
}
