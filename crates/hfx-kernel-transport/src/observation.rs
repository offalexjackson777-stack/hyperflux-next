// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    HFX_UAPI_ABI_VERSION, HFX_UAPI_MAX_OBSERVATIONS, HFX_UAPI_OBSERVATION_BATCH_FLAG_CURSOR_GAP,
    HfxUapiObservation, HfxUapiReadObservations, KernelTransportError, KernelTransportErrorKind,
};
use hfx_domain::GenerationId;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RawKernelObservation {
    pub sequence: u64,
    pub observed_boottime_ns: u64,
    pub kind: u32,
    pub endpoint_slot: u32,
    pub source: u32,
    pub confidence: u32,
    pub value: u32,
    pub auxiliary: u32,
}

impl From<HfxUapiObservation> for RawKernelObservation {
    fn from(value: HfxUapiObservation) -> Self {
        Self {
            sequence: value.sequence.0,
            observed_boottime_ns: value.observed_boottime_ns.0,
            kind: value.kind,
            endpoint_slot: value.endpoint_slot,
            source: value.source,
            confidence: value.confidence,
            value: value.value,
            auxiliary: value.auxiliary,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KernelObservationBatch {
    pub generation_id: GenerationId,
    pub oldest_sequence: u64,
    pub latest_sequence: u64,
    pub cursor_gap: bool,
    pub observations: Vec<RawKernelObservation>,
}

pub(crate) fn decode_observations(
    expected_generation: GenerationId,
    after_sequence: u64,
    value: &HfxUapiReadObservations,
) -> Result<KernelObservationBatch, KernelTransportError> {
    if value.version != HFX_UAPI_ABI_VERSION
        || usize::try_from(value.size).ok() != Some(size_of::<HfxUapiReadObservations>())
    {
        return Err(KernelTransportError::safe(
            KernelTransportErrorKind::AbiMismatch,
        ));
    }
    if value.receiver_generation.0 != expected_generation.get() {
        return Err(KernelTransportError::safe(
            KernelTransportErrorKind::GenerationMismatch,
        ));
    }
    let count = usize::try_from(value.count).map_err(|_| malformed_batch())?;
    if count > HFX_UAPI_MAX_OBSERVATIONS
        || value.flags & !HFX_UAPI_OBSERVATION_BATCH_FLAG_CURSOR_GAP != 0
    {
        return Err(malformed_batch());
    }
    let observations = value.observations[..count]
        .iter()
        .copied()
        .map(RawKernelObservation::from)
        .collect::<Vec<_>>();
    if observations
        .iter()
        .any(|item| item.sequence <= after_sequence)
        || observations
            .windows(2)
            .any(|pair| pair[0].sequence >= pair[1].sequence)
        || observations.iter().any(|item| {
            item.sequence < value.oldest_sequence.0 || item.sequence > value.latest_sequence.0
        })
        || (observations.is_empty() && value.oldest_sequence.0 > value.latest_sequence.0)
    {
        return Err(malformed_batch());
    }
    Ok(KernelObservationBatch {
        generation_id: expected_generation,
        oldest_sequence: value.oldest_sequence.0,
        latest_sequence: value.latest_sequence.0,
        cursor_gap: value.flags & HFX_UAPI_OBSERVATION_BATCH_FLAG_CURSOR_GAP != 0,
        observations,
    })
}

fn malformed_batch() -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::AbiMismatch)
}
