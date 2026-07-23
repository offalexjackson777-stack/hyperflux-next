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

pub(crate) fn device_return_restore(
    batch: &EventBatch,
    evidence_refs: &[String],
    receiver_id: &str,
    generation_id: u64,
    device_id: &str,
) -> Result<String, EventEvidenceError> {
    let anchor = latest_anchor_sequence(batch, evidence_refs)?;
    let mut departed = false;
    let mut returned = false;

    for event in events_after(batch, anchor) {
        if !matches_identity(event, receiver_id, generation_id, Some(device_id)) {
            continue;
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
mod tests {
    use super::*;
    use hfx_domain::{
        DroppedEventCount, GenerationId, LogicalDeviceId, ProjectionRevision, ReceiverId,
        StreamEpoch, StreamId, SubscriptionId, TransactionId,
    };
    use hfx_protocol::BridgeEvent;

    fn cursor(sequence: u64) -> EventCursor {
        EventCursor {
            stream_id: StreamId::try_from("qualification-event-test").expect("stream id"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("projection revision"),
            sequence: SequenceNumber::try_from(sequence).expect("sequence"),
        }
    }

    fn event(
        sequence: u64,
        kind: EventKind,
        generation: u64,
        device_id: Option<&str>,
        terminal_transaction: bool,
    ) -> BridgeEvent {
        BridgeEvent {
            sequence: SequenceNumber::try_from(sequence).expect("sequence"),
            kind,
            receiver_id: Some(ReceiverId::try_from("receiver-test").expect("receiver id")),
            generation_id: Some(GenerationId::try_from(generation).expect("generation")),
            device_id: device_id.map(|value| LogicalDeviceId::try_from(value).expect("device id")),
            lease_id: None,
            transaction_id: terminal_transaction.then(|| {
                TransactionId::try_from(format!("restore-transaction-{sequence}"))
                    .expect("transaction id")
            }),
            finding_id: None,
        }
    }

    fn batch(events: Vec<BridgeEvent>) -> EventBatch {
        let latest = events.last().map_or(1_u64, |event| event.sequence.get());
        EventBatch {
            subscription_id: SubscriptionId::try_from("qualification-event-subscription")
                .expect("subscription id"),
            next_cursor: cursor(latest),
            events,
            oldest_available: SequenceNumber::try_from(1_u64).expect("oldest sequence"),
            latest_available: SequenceNumber::try_from(latest).expect("latest sequence"),
            dropped_events: DroppedEventCount::try_from(0_u64).expect("dropped count"),
            cursor_gap: false,
            has_more: false,
        }
    }

    #[test]
    fn exact_device_return_chain_is_accepted() {
        let events = batch(vec![
            event(2, EventKind::DeviceUnavailable, 7, Some("keyboard"), false),
            event(3, EventKind::DeviceAvailable, 7, Some("keyboard"), false),
            event(4, EventKind::RestoreCompleted, 7, Some("keyboard"), true),
        ]);
        assert_eq!(
            device_return_restore(
                &events,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                7,
                "keyboard",
            ),
            Ok("bridge-restore-event:4:keyboard".to_owned())
        );
    }

    #[test]
    fn device_return_rejects_missing_anchor_and_wrong_identity() {
        let events = batch(vec![
            event(2, EventKind::DeviceUnavailable, 7, Some("keyboard"), false),
            event(3, EventKind::DeviceAvailable, 7, Some("keyboard"), false),
            event(4, EventKind::RestoreCompleted, 7, Some("mouse"), true),
        ]);
        assert_eq!(
            device_return_restore(&events, &[], "receiver-test", 7, "keyboard"),
            Err(EventEvidenceError::AnchorUnavailable)
        );
        assert_eq!(
            device_return_restore(
                &events,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                7,
                "keyboard",
            ),
            Err(EventEvidenceError::RestoreUnavailable)
        );
    }

    #[test]
    fn incomplete_history_is_never_accepted() {
        let mut events = batch(vec![event(
            2,
            EventKind::RestoreCompleted,
            7,
            Some("keyboard"),
            true,
        )]);
        events.cursor_gap = true;
        assert_eq!(
            resumed_generation_restores(
                &events,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                7,
                &["keyboard".to_owned()],
            ),
            Err(EventEvidenceError::HistoryIncomplete)
        );
    }

    #[test]
    fn system_resume_requires_a_receiver_boundary_before_exact_restores() {
        let devices = ["keyboard".to_owned(), "mouse".to_owned()];
        let without_boundary = batch(vec![
            event(2, EventKind::RestoreCompleted, 8, Some("keyboard"), true),
            event(3, EventKind::RestoreCompleted, 8, Some("mouse"), true),
        ]);
        assert_eq!(
            resumed_generation_restores(
                &without_boundary,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                8,
                &devices,
            ),
            Err(EventEvidenceError::RestoreUnavailable)
        );

        let with_boundary = batch(vec![
            event(2, EventKind::ReceiverSuspended, 7, None, false),
            event(3, EventKind::GenerationReplaced, 8, None, false),
            event(4, EventKind::ReceiverAvailable, 8, None, false),
            event(5, EventKind::RestoreCompleted, 8, Some("keyboard"), true),
            event(6, EventKind::RestoreCompleted, 8, Some("mouse"), true),
        ]);
        assert_eq!(
            resumed_generation_restores(
                &with_boundary,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                8,
                &devices,
            ),
            Ok(vec![
                "bridge-restore-event:5:keyboard".to_owned(),
                "bridge-restore-event:6:mouse".to_owned(),
            ])
        );
    }

    #[test]
    fn renewed_generation_requires_every_device_restore() {
        let mut events = batch(vec![
            event(2, EventKind::GenerationReplaced, 8, None, false),
            event(3, EventKind::RestoreCompleted, 8, Some("keyboard"), true),
            event(4, EventKind::RestoreCompleted, 8, Some("mouse"), true),
        ]);
        let devices = ["keyboard".to_owned(), "mouse".to_owned()];
        assert_eq!(
            renewed_generation_restores(
                &events,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                8,
                &devices,
            ),
            Ok(vec![
                "bridge-restore-event:3:keyboard".to_owned(),
                "bridge-restore-event:4:mouse".to_owned(),
            ])
        );
        events.events[2].generation_id = Some(GenerationId::try_from(9_u64).expect("generation"));
        assert_eq!(
            renewed_generation_restores(
                &events,
                &[cursor_evidence(&cursor(1))],
                "receiver-test",
                8,
                &devices,
            ),
            Err(EventEvidenceError::RestoreUnavailable)
        );
    }
}
