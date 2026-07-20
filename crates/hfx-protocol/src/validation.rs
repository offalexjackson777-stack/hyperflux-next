// SPDX-License-Identifier: GPL-2.0-only

use crate::{LeaseRequest, ResourceKey, TransactionRequest};
use hfx_domain::{ResourceKind, TransactionClass};
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
    NonCanonicalFrameOrder,
    EmptyColors,
    TooManyColors,
    TooManyAggregateColors,
    FrameWithoutLightingLease,
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
            Self::NonCanonicalFrameOrder => {
                "transaction frame indices are not contiguous and ordered"
            }
            Self::EmptyColors => "frame contains no colors",
            Self::TooManyColors => "frame exceeds the color bound",
            Self::TooManyAggregateColors => "transaction exceeds the aggregate color bound",
            Self::FrameWithoutLightingLease => "frame target lacks a lighting resource",
            Self::ResourceOutsideTransactionGeneration => {
                "transaction resource is outside the bound receiver generation"
            }
            Self::UnsupportedTransactionClass => "transaction class has no protocol-v1 payload",
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
    if !matches!(
        request.transaction_class,
        TransactionClass::EffectFrame | TransactionClass::StaticLighting
    ) {
        return Err(ProtocolValidationError::UnsupportedTransactionClass);
    }
    if request.resources.iter().any(|resource| {
        resource.receiver_id != request.receiver_id
            || resource.generation_id != request.generation_id
    }) {
        return Err(ProtocolValidationError::ResourceOutsideTransactionGeneration);
    }
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
    Ok(())
}
