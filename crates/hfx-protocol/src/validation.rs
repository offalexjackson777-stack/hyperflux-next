// SPDX-License-Identifier: GPL-2.0-only

use crate::{LeaseRequest, ResourceKey, TransactionRequest};
use hfx_domain::{LogicalDeviceId, ResourceKind, StableLightingMode, TransactionClass};
use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProtocolValidationError {
    EmptyResources,
    TooManyResources,
    DuplicateResource,
    ResourcesNotCanonical,
    EmptyFrames,
    TooManyFrames,
    DuplicateFrameIndex,
    DuplicateFrameTarget,
    NonCanonicalFrameOrder,
    EmptyProfileBindings,
    TooManyProfileBindings,
    DuplicateProfileBinding,
    ProfileBindingsNotCanonical,
    ZeroApplicationSlots,
    FrameWithoutProfileBinding,
    ProfileBindingWithoutFrame,
    FrameColorCountMismatch,
    TooManyStableIntents,
    DuplicateStableIntent,
    StableIntentsNotCanonical,
    StableIntentWithoutFrame,
    FrameWithoutStableIntent,
    StableIntentOnNonStableTransaction,
    OffIntentHasLitColor,
    EmptyColors,
    TooManyColors,
    TooManyAggregateColors,
    FrameWithoutLightingLease,
    ResourceWithoutFrame,
    ResourceOutsideTransactionGeneration,
    UnsupportedTransactionClass,
}

impl fmt::Display for ProtocolValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::EmptyResources => "request has no resources",
            Self::TooManyResources => "request exceeds the resource bound",
            Self::DuplicateResource => "request contains a duplicate resource",
            Self::ResourcesNotCanonical => "request resources are not in canonical order",
            Self::EmptyFrames => "transaction has no frames",
            Self::TooManyFrames => "transaction exceeds the frame bound",
            Self::DuplicateFrameIndex => "transaction contains a duplicate frame index",
            Self::DuplicateFrameTarget => "transaction contains more than one frame for a device",
            Self::NonCanonicalFrameOrder => {
                "transaction frame indices are not contiguous and ordered"
            }
            Self::EmptyProfileBindings => "transaction has no device profile bindings",
            Self::TooManyProfileBindings => "transaction exceeds the profile-binding bound",
            Self::DuplicateProfileBinding => {
                "transaction contains a duplicate device profile binding"
            }
            Self::ProfileBindingsNotCanonical => {
                "transaction profile bindings are not in canonical device order"
            }
            Self::ZeroApplicationSlots => "device profile binding has no application slots",
            Self::FrameWithoutProfileBinding => "frame target lacks a device profile binding",
            Self::ProfileBindingWithoutFrame => "device profile binding has no matching frame",
            Self::FrameColorCountMismatch => {
                "frame color count does not match its bound device profile"
            }
            Self::TooManyStableIntents => "transaction exceeds the stable-intent bound",
            Self::DuplicateStableIntent => {
                "transaction contains a duplicate stable-lighting intent"
            }
            Self::StableIntentsNotCanonical => {
                "transaction stable-lighting intents are not in canonical device order"
            }
            Self::StableIntentWithoutFrame => "stable-lighting intent has no matching frame target",
            Self::FrameWithoutStableIntent => {
                "static-lighting frame lacks an explicit semantic intent"
            }
            Self::StableIntentOnNonStableTransaction => {
                "non-static transaction carries stable-lighting intent"
            }
            Self::OffIntentHasLitColor => "Off intent contains a non-black frame color",
            Self::EmptyColors => "frame contains no colors",
            Self::TooManyColors => "frame exceeds the color bound",
            Self::TooManyAggregateColors => "transaction exceeds the aggregate color bound",
            Self::FrameWithoutLightingLease => "frame target lacks a lighting resource",
            Self::ResourceWithoutFrame => "transaction resource has no matching lighting frame",
            Self::ResourceOutsideTransactionGeneration => {
                "transaction resource is outside the bound receiver generation"
            }
            Self::UnsupportedTransactionClass => {
                "transaction class has no current protocol payload"
            }
        })
    }
}

impl std::error::Error for ProtocolValidationError {}

fn validate_resources(resources: &[ResourceKey]) -> Result<(), ProtocolValidationError> {
    if resources.is_empty() {
        return Err(ProtocolValidationError::EmptyResources);
    }
    if resources.len() > 32 {
        return Err(ProtocolValidationError::TooManyResources);
    }
    let unique = resources.iter().collect::<BTreeSet<_>>();
    if unique.len() != resources.len() {
        return Err(ProtocolValidationError::DuplicateResource);
    }
    if resources.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err(ProtocolValidationError::ResourcesNotCanonical);
    }
    Ok(())
}

/// Validates bounded atomic lease-request structure.
///
/// # Errors
///
/// Returns an error when the resource set is empty, oversized, or duplicated.
pub fn validate_lease_request(request: &LeaseRequest) -> Result<(), ProtocolValidationError> {
    validate_resources(&request.resources)
}

/// Validates bounded transaction structure before ownership or generation checks.
///
/// # Errors
///
/// Returns an error for invalid resource sets, invalid frame bounds, duplicate
/// frame indices, or frame targets without a declared lighting resource.
pub fn validate_transaction(request: &TransactionRequest) -> Result<(), ProtocolValidationError> {
    validate_resources(&request.resources)?;
    validate_transaction_scope(request)?;
    let frame_devices = validate_frame_topology(request)?;
    validate_profile_bindings(request, &frame_devices)?;
    validate_frame_payloads(request)?;
    validate_stable_intents(request, &frame_devices)?;
    validate_resource_coverage(request, &frame_devices)
}

fn validate_transaction_scope(request: &TransactionRequest) -> Result<(), ProtocolValidationError> {
    if !matches!(
        request.transaction_class,
        TransactionClass::EffectFrame
            | TransactionClass::StaticLighting
            | TransactionClass::Restore
    ) {
        return Err(ProtocolValidationError::UnsupportedTransactionClass);
    }
    if request.resources.iter().any(|resource| {
        resource.receiver_id != request.receiver_id
            || resource.generation_id != request.generation_id
    }) {
        return Err(ProtocolValidationError::ResourceOutsideTransactionGeneration);
    }
    Ok(())
}

fn validate_frame_topology(
    request: &TransactionRequest,
) -> Result<BTreeSet<&LogicalDeviceId>, ProtocolValidationError> {
    if request.frames.is_empty() {
        return Err(ProtocolValidationError::EmptyFrames);
    }
    if request.frames.len() > 32 {
        return Err(ProtocolValidationError::TooManyFrames);
    }
    let indices = request
        .frames
        .iter()
        .map(|frame| frame.frame_index)
        .collect::<BTreeSet<_>>();
    if indices.len() != request.frames.len() {
        return Err(ProtocolValidationError::DuplicateFrameIndex);
    }
    if request.frames.iter().enumerate().any(|(index, frame)| {
        u32::try_from(index).map_or(true, |value| frame.frame_index.get() != value)
    }) {
        return Err(ProtocolValidationError::NonCanonicalFrameOrder);
    }
    let frame_devices = request
        .frames
        .iter()
        .map(|frame| &frame.device_id)
        .collect::<BTreeSet<_>>();
    if frame_devices.len() != request.frames.len() {
        return Err(ProtocolValidationError::DuplicateFrameTarget);
    }
    Ok(frame_devices)
}

fn validate_profile_bindings(
    request: &TransactionRequest,
    frame_devices: &BTreeSet<&LogicalDeviceId>,
) -> Result<(), ProtocolValidationError> {
    if request.device_profiles.is_empty() {
        return Err(ProtocolValidationError::EmptyProfileBindings);
    }
    if request.device_profiles.len() > 32 {
        return Err(ProtocolValidationError::TooManyProfileBindings);
    }
    let profile_devices = request
        .device_profiles
        .iter()
        .map(|binding| &binding.device_id)
        .collect::<BTreeSet<_>>();
    if profile_devices.len() != request.device_profiles.len() {
        return Err(ProtocolValidationError::DuplicateProfileBinding);
    }
    if request
        .device_profiles
        .windows(2)
        .any(|pair| pair[0].device_id >= pair[1].device_id)
    {
        return Err(ProtocolValidationError::ProfileBindingsNotCanonical);
    }
    if request
        .device_profiles
        .iter()
        .any(|binding| binding.application_slot_count.get() == 0)
    {
        return Err(ProtocolValidationError::ZeroApplicationSlots);
    }
    if frame_devices
        .iter()
        .any(|device_id| !profile_devices.contains(device_id))
    {
        return Err(ProtocolValidationError::FrameWithoutProfileBinding);
    }
    if profile_devices
        .iter()
        .any(|device_id| !frame_devices.contains(device_id))
    {
        return Err(ProtocolValidationError::ProfileBindingWithoutFrame);
    }
    Ok(())
}

fn validate_frame_payloads(request: &TransactionRequest) -> Result<(), ProtocolValidationError> {
    let mut aggregate_colors = 0_usize;
    for frame in &request.frames {
        if frame.colors.is_empty() {
            return Err(ProtocolValidationError::EmptyColors);
        }
        if frame.colors.len() > 4096 {
            return Err(ProtocolValidationError::TooManyColors);
        }
        aggregate_colors = aggregate_colors
            .checked_add(frame.colors.len())
            .ok_or(ProtocolValidationError::TooManyAggregateColors)?;
        if aggregate_colors > 16_384 {
            return Err(ProtocolValidationError::TooManyAggregateColors);
        }
        let Some(binding) = request
            .device_profiles
            .iter()
            .find(|binding| binding.device_id == frame.device_id)
        else {
            return Err(ProtocolValidationError::FrameWithoutProfileBinding);
        };
        if usize::from(binding.application_slot_count.get()) != frame.colors.len() {
            return Err(ProtocolValidationError::FrameColorCountMismatch);
        }
    }
    Ok(())
}

fn validate_stable_intents(
    request: &TransactionRequest,
    frame_devices: &BTreeSet<&LogicalDeviceId>,
) -> Result<(), ProtocolValidationError> {
    if request.transaction_class != TransactionClass::StaticLighting {
        return if request.stable_intents.is_empty() {
            Ok(())
        } else {
            Err(ProtocolValidationError::StableIntentOnNonStableTransaction)
        };
    }
    if request.stable_intents.len() > 32 {
        return Err(ProtocolValidationError::TooManyStableIntents);
    }
    let intent_devices = request
        .stable_intents
        .iter()
        .map(|intent| &intent.device_id)
        .collect::<BTreeSet<_>>();
    if intent_devices.len() != request.stable_intents.len() {
        return Err(ProtocolValidationError::DuplicateStableIntent);
    }
    if request
        .stable_intents
        .windows(2)
        .any(|pair| pair[0].device_id >= pair[1].device_id)
    {
        return Err(ProtocolValidationError::StableIntentsNotCanonical);
    }
    if intent_devices
        .iter()
        .any(|device_id| !frame_devices.contains(device_id))
    {
        return Err(ProtocolValidationError::StableIntentWithoutFrame);
    }
    if frame_devices
        .iter()
        .any(|device_id| !intent_devices.contains(device_id))
    {
        return Err(ProtocolValidationError::FrameWithoutStableIntent);
    }
    for intent in &request.stable_intents {
        if intent.mode != StableLightingMode::Off {
            continue;
        }
        let frame = request
            .frames
            .iter()
            .find(|frame| frame.device_id == intent.device_id)
            .ok_or(ProtocolValidationError::StableIntentWithoutFrame)?;
        if frame
            .colors
            .iter()
            .any(|color| color.red.get() != 0 || color.green.get() != 0 || color.blue.get() != 0)
        {
            return Err(ProtocolValidationError::OffIntentHasLitColor);
        }
    }
    Ok(())
}

fn validate_resource_coverage(
    request: &TransactionRequest,
    frame_devices: &BTreeSet<&LogicalDeviceId>,
) -> Result<(), ProtocolValidationError> {
    for frame in &request.frames {
        let resource = ResourceKey {
            receiver_id: request.receiver_id.clone(),
            generation_id: request.generation_id,
            device_id: frame.device_id.clone(),
            kind: ResourceKind::Lighting,
        };
        if !request.resources.contains(&resource) {
            return Err(ProtocolValidationError::FrameWithoutLightingLease);
        }
    }
    if request.resources.iter().any(|resource| {
        resource.kind != ResourceKind::Lighting || !frame_devices.contains(&resource.device_id)
    }) {
        return Err(ProtocolValidationError::ResourceWithoutFrame);
    }
    Ok(())
}
