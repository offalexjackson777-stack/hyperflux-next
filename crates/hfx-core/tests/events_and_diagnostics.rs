// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::text;
use hfx_core::{
    BoundedDiagnosticSink, BoundedEventLog, DiagnosticRegistry, DiagnosticRegistryError,
    EventDraft, EventLogError,
};
use hfx_domain::{
    ErrorSeverity, EventBatchLimit, EventKind, PrivacyClass, ProjectionRevision, QueueCapacity,
    SequenceNumber, StreamEpoch,
};
use hfx_protocol::{DiagnosticFinding, EventCursor, SubscriptionRequest};

fn draft(kind: EventKind) -> EventDraft {
    EventDraft {
        kind,
        receiver_id: Some(text("receiver-1")),
        generation_id: Some(common::generation(1)),
        device_id: None,
        lease_id: None,
        transaction_id: None,
        finding_id: None,
    }
}

fn subscription(after: u64) -> SubscriptionRequest {
    SubscriptionRequest {
        client_id: text("client-1"),
        subscription_id: None,
        expected_cursor: Some(EventCursor {
            stream_id: text("stream-1"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("revision is valid"),
            sequence: SequenceNumber::try_from(after).expect("sequence is valid"),
        }),
        max_events: EventBatchLimit::try_from(256_u16).expect("batch bound is valid"),
    }
}

#[test]
fn bounded_event_log_reports_eviction_and_resume_gaps() {
    let mut log = BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("revision is valid"),
        2,
    )
    .expect("event bound is valid");
    log.append(draft(EventKind::DeviceAvailable))
        .expect("first event appends");
    log.append(draft(EventKind::BatteryUpdated))
        .expect("second event appends");
    log.append(draft(EventKind::DeviceSleeping))
        .expect("third event evicts oldest");

    let lost = log
        .read(text("subscription-1"), &subscription(0))
        .expect("batch is valid");
    assert!(lost.cursor_gap);
    assert!(lost.events.is_empty());
    assert_eq!(lost.dropped_events.get(), 1);
    assert_eq!(lost.oldest_available.get(), 2);
    assert_eq!(lost.latest_available.get(), 3);

    let resumable = log
        .read(
            text("subscription-2"),
            &SubscriptionRequest {
                max_events: EventBatchLimit::try_from(1_u16).expect("batch bound is valid"),
                ..subscription(1)
            },
        )
        .expect("oldest predecessor cursor remains resumable");
    assert!(!resumable.cursor_gap);
    assert_eq!(resumable.events.len(), 1);
    assert_eq!(resumable.events[0].sequence.get(), 2);

    let mut wrong_stream = subscription(3);
    wrong_stream
        .expected_cursor
        .as_mut()
        .expect("test cursor exists")
        .stream_id = text("older-stream");
    let restarted = log
        .read(text("subscription-3"), &wrong_stream)
        .expect("stream mismatch is represented, not parsed as an error");
    assert!(restarted.cursor_gap);
    assert!(restarted.events.is_empty());
}

#[test]
fn event_subscription_identity_must_match() {
    let log = BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("revision is valid"),
        8,
    )
    .expect("event bound is valid");
    let mut request = subscription(0);
    request.subscription_id = Some(text("subscription-other"));
    assert_eq!(
        log.read(text("subscription-1"), &request),
        Err(EventLogError::SubscriptionMismatch)
    );
}

#[test]
fn initial_subscription_and_pagination_have_truthful_cursors() {
    let mut log = BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("revision is valid"),
        8,
    )
    .expect("event bound is valid");
    log.append(draft(EventKind::DeviceAvailable))
        .expect("first event appends");
    log.append(draft(EventKind::BatteryUpdated))
        .expect("second event appends");

    let projection_cursor = log.cursor();
    assert_eq!(projection_cursor.stream_id.as_str(), "stream-1");
    assert_eq!(projection_cursor.stream_epoch.get(), 1);
    assert_eq!(projection_cursor.projection_revision.get(), 1);
    assert_eq!(projection_cursor.sequence.get(), 2);

    let first = log
        .read(
            text("subscription-1"),
            &SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: None,
                expected_cursor: None,
                max_events: EventBatchLimit::try_from(1_u16).expect("batch bound is valid"),
            },
        )
        .expect("initial subscription is valid");
    assert!(!first.cursor_gap);
    assert!(first.has_more);
    assert_eq!(first.events.len(), 1);
    assert_eq!(first.next_cursor.sequence.get(), 1);

    let second = log
        .read(
            text("subscription-1"),
            &SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: Some(text("subscription-1")),
                expected_cursor: Some(first.next_cursor),
                max_events: EventBatchLimit::try_from(1_u16).expect("batch bound is valid"),
            },
        )
        .expect("cursor resumes the same subscription");
    assert!(!second.cursor_gap);
    assert!(!second.has_more);
    assert_eq!(second.events.len(), 1);
    assert_eq!(second.next_cursor.sequence.get(), 2);
}

fn finding(id: &str, explanation: &str) -> DiagnosticFinding {
    DiagnosticFinding {
        finding_id: text(id),
        severity: ErrorSeverity::Warning,
        cause: text("bounded technical cause"),
        explanation: text(explanation),
        safe_action: text("run the documented verification"),
        privacy: PrivacyClass::Public,
        documentation: text("docs/troubleshooting/finding.md"),
    }
}

#[test]
fn diagnostic_registry_is_bounded_and_updates_in_place() {
    let mut registry = DiagnosticRegistry::new(1).expect("capacity is valid");
    registry
        .raise(finding("HF-TEST-001", "first explanation"))
        .expect("first finding fits");
    registry
        .raise(finding("HF-TEST-001", "updated explanation"))
        .expect("same finding updates in place");
    assert_eq!(
        registry.raise(finding("HF-TEST-002", "second finding")),
        Err(DiagnosticRegistryError::CapacityExhausted)
    );
    let snapshot = registry.snapshot(
        SequenceNumber::try_from(4_u64).expect("sequence is valid"),
        QueueCapacity::try_from(256_u16).expect("capacity is valid"),
        QueueCapacity::try_from(32_u16).expect("capacity is valid"),
    );
    assert_eq!(snapshot.findings.len(), 1);
    assert_eq!(
        snapshot.findings[0].explanation.as_str(),
        "updated explanation"
    );
    assert!(registry.clear(&text("HF-TEST-001")));
}

#[test]
fn diagnostic_output_never_blocks_on_full_capacity() {
    let mut sink = BoundedDiagnosticSink::new(2).expect("capacity is valid");
    sink.try_push("one");
    sink.try_push("two");
    sink.try_push("three");
    assert_eq!(
        sink.iter().copied().collect::<Vec<_>>(),
        vec!["two", "three"]
    );
    assert_eq!(sink.dropped(), 1);
}
