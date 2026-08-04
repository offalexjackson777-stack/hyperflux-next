// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{EventKind, SequenceNumber};
use hfx_protocol::{EventBatch, EventCursor};
use sha2::{Digest, Sha256};
use std::fmt;

const CURSOR_EVIDENCE_PREFIX: &str = "bridge-cursor-sha256:";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum EventEvidenceError {
    HistoryIncomplete,
    AnchorUnavailable,
    RestoreUnavailable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DeviceReturnEvidenceMode {
    ReceiverTransition,
    WatchedRetainedRoute,
}

impl fmt::Display for EventEvidenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::HistoryIncomplete => {
                "The bridge event history is incomplete for this lifecycle checkpoint."
            }
            Self::AnchorUnavailable => {
                "The retained lifecycle cursor is no longer available in the bridge event history."
            }
            Self::RestoreUnavailable => {
                "The exact profile-bound restore event is not visible after this lifecycle checkpoint."
            }
        })
    }
}

pub(crate) fn cursor_evidence(cursor: &EventCursor) -> String {
    let encoded = serde_json::to_vec(cursor).unwrap_or_default();
    format!("{CURSOR_EVIDENCE_PREFIX}{}", digest_hex(&encoded))
}

pub(crate) fn cursor_anchor_unavailable(batch: &EventBatch, evidence_refs: &[String]) -> bool {
    evidence_refs
        .iter()
        .any(|value| value.starts_with(CURSOR_EVIDENCE_PREFIX))
        && matches!(
            latest_anchor_sequence(batch, evidence_refs),
            Err(EventEvidenceError::AnchorUnavailable)
        )
}

pub(crate) fn device_return_restore(
    batch: &EventBatch,
    evidence_refs: &[String],
    receiver_id: &str,
    generation_id: u64,
    device_id: &str,
    mode: DeviceReturnEvidenceMode,
) -> Result<String, EventEvidenceError> {
    let anchor = latest_anchor_sequence(batch, evidence_refs)?;
    let mut departed = false;
    let mut returned = false;

    for event in events_after(batch, anchor) {
        if !matches_identity(event, receiver_id, generation_id, Some(device_id)) {
            continue;
        }
        if mode == DeviceReturnEvidenceMode::WatchedRetainedRoute
            && event.kind == EventKind::RestoreCompleted
            && event.transaction_id.as_ref().is_some()
        {
            return Ok(restore_evidence(event.sequence.get(), device_id));
        }
        match event.kind {
            EventKind::DeviceSleeping | EventKind::DeviceUnavailable => {
                departed = true;
                returned = false;
            }
            EventKind::DeviceAvailable if departed => returned = true,
            EventKind::RestoreCompleted if returned && event.transaction_id.as_ref().is_some() => {
                return Ok(restore_evidence(event.sequence.get(), device_id));
            }
            _ => {}
        }
    }
    Err(EventEvidenceError::RestoreUnavailable)
}

pub(crate) fn renewed_generation_restores(
    batch: &EventBatch,
    evidence_refs: &[String],
    receiver_id: &str,
    generation_id: u64,
    device_ids: &[String],
) -> Result<Vec<String>, EventEvidenceError> {
    let anchor = latest_anchor_sequence(batch, evidence_refs)?;
    let generation_start = events_after(batch, anchor)
        .find(|event| {
            matches_identity(event, receiver_id, generation_id, None)
                && matches!(
                    event.kind,
                    EventKind::GenerationReplaced | EventKind::ReceiverAvailable
                )
        })
        .map(|event| event.sequence.get())
        .ok_or(EventEvidenceError::RestoreUnavailable)?;
    all_device_restores_after(
        batch,
        generation_start,
        receiver_id,
        generation_id,
        device_ids,
    )
}

pub(crate) fn resumed_generation_restores(
    batch: &EventBatch,
    evidence_refs: &[String],
    receiver_id: &str,
    generation_id: u64,
    device_ids: &[String],
) -> Result<Vec<String>, EventEvidenceError> {
    let anchor = latest_anchor_sequence(batch, evidence_refs)?;
    let suspended = events_after(batch, anchor)
        .find(|event| {
            event.kind == EventKind::ReceiverSuspended && matches_receiver(event, receiver_id)
        })
        .map(|event| event.sequence.get())
        .ok_or(EventEvidenceError::RestoreUnavailable)?;
    let resumed = events_after(batch, suspended)
        .find(|event| {
            matches_identity(event, receiver_id, generation_id, None)
                && matches!(
                    event.kind,
                    EventKind::GenerationReplaced | EventKind::ReceiverAvailable
                )
        })
        .map(|event| event.sequence.get())
        .ok_or(EventEvidenceError::RestoreUnavailable)?;
    all_device_restores_after(batch, resumed, receiver_id, generation_id, device_ids)
}

fn all_device_restores_after(
    batch: &EventBatch,
    anchor: u64,
    receiver_id: &str,
    generation_id: u64,
    device_ids: &[String],
) -> Result<Vec<String>, EventEvidenceError> {
    let mut evidence = Vec::with_capacity(device_ids.len());
    for device_id in device_ids {
        let event = events_after(batch, anchor)
            .find(|event| {
                event.kind == EventKind::RestoreCompleted
                    && event.transaction_id.as_ref().is_some()
                    && matches_identity(event, receiver_id, generation_id, Some(device_id.as_str()))
            })
            .ok_or(EventEvidenceError::RestoreUnavailable)?;
        evidence.push(restore_evidence(event.sequence.get(), device_id));
    }
    Ok(evidence)
}

fn latest_anchor_sequence(
    batch: &EventBatch,
    evidence_refs: &[String],
) -> Result<u64, EventEvidenceError> {
    if batch.cursor_gap || batch.has_more {
        return Err(EventEvidenceError::HistoryIncomplete);
    }
    let lower = batch.oldest_available.get().saturating_sub(1);
    let upper = batch.latest_available.get();
    for evidence in evidence_refs
        .iter()
        .rev()
        .filter(|value| value.starts_with(CURSOR_EVIDENCE_PREFIX))
    {
        for sequence in lower..=upper {
            let Some(candidate) = cursor_at(&batch.next_cursor, sequence) else {
                continue;
            };
            if cursor_evidence(&candidate) == *evidence {
                return Ok(sequence);
            }
        }
    }
    Err(EventEvidenceError::AnchorUnavailable)
}

fn cursor_at(template: &EventCursor, sequence: u64) -> Option<EventCursor> {
    Some(EventCursor {
        stream_id: template.stream_id.clone(),
        stream_epoch: template.stream_epoch,
        projection_revision: template.projection_revision,
        sequence: SequenceNumber::try_from(sequence).ok()?,
    })
}

fn events_after(
    batch: &EventBatch,
    anchor: u64,
) -> impl Iterator<Item = &hfx_protocol::BridgeEvent> {
    batch
        .events
        .iter()
        .filter(move |event| event.sequence.get() > anchor)
}

fn matches_identity(
    event: &hfx_protocol::BridgeEvent,
    receiver_id: &str,
    generation_id: u64,
    device_id: Option<&str>,
) -> bool {
    matches_receiver(event, receiver_id)
        && event.generation_id.map(hfx_domain::GenerationId::get) == Some(generation_id)
        && device_id.is_none_or(|expected| {
            event
                .device_id
                .as_ref()
                .map(hfx_domain::LogicalDeviceId::as_str)
                == Some(expected)
        })
}

fn matches_receiver(event: &hfx_protocol::BridgeEvent, receiver_id: &str) -> bool {
    event
        .receiver_id
        .as_ref()
        .map(hfx_domain::ReceiverId::as_str)
        == Some(receiver_id)
}

fn restore_evidence(sequence: u64, device_id: &str) -> String {
    format!("bridge-restore-event:{sequence}:{device_id}")
}

fn digest_hex(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

#[cfg(test)]
mod tests;
