// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_domain::{
    DroppedEventCount, GenerationId, LogicalDeviceId, ProjectionRevision, ReceiverId, StreamEpoch,
    StreamId, SubscriptionId, TransactionId,
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
            DeviceReturnEvidenceMode::ReceiverTransition,
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
        device_return_restore(
            &events,
            &[],
            "receiver-test",
            7,
            "keyboard",
            DeviceReturnEvidenceMode::ReceiverTransition,
        ),
        Err(EventEvidenceError::AnchorUnavailable)
    );
    assert_eq!(
        device_return_restore(
            &events,
            &[cursor_evidence(&cursor(1))],
            "receiver-test",
            7,
            "keyboard",
            DeviceReturnEvidenceMode::ReceiverTransition,
        ),
        Err(EventEvidenceError::RestoreUnavailable)
    );
}

#[test]
fn watched_retained_route_requires_a_newer_terminal_restore() {
    let events = batch(vec![event(
        2,
        EventKind::RestoreCompleted,
        7,
        Some("mouse"),
        true,
    )]);
    assert_eq!(
        device_return_restore(
            &events,
            &[cursor_evidence(&cursor(1))],
            "receiver-test",
            7,
            "mouse",
            DeviceReturnEvidenceMode::WatchedRetainedRoute,
        ),
        Ok("bridge-restore-event:2:mouse".to_owned())
    );
    assert_eq!(
        device_return_restore(
            &events,
            &[cursor_evidence(&cursor(2))],
            "receiver-test",
            7,
            "mouse",
            DeviceReturnEvidenceMode::WatchedRetainedRoute,
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
