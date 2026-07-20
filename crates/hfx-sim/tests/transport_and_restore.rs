// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{available, event, keyboard, mouse, scenario};
use hfx_domain::ApplyOutcome;
use hfx_sim::run_replay;
use serde_json::json;

#[test]
fn bounded_high_rate_stream_delivers_each_qualified_frame_once() {
    let mut events = vec![available("mouse-1", 0, 1)];
    for frame in 0..240_u32 {
        events.push(event(
            u64::from(frame) + 1,
            1,
            json!({
                "kind": "lighting-frame",
                "transaction_id": format!("frame-{frame}"),
                "frame_index": frame,
                "targets": ["mouse-1"],
                "outcome": "delivered"
            }),
        ));
    }
    let scenario = scenario(json!([mouse()]), serde_json::Value::Array(events));
    let result = run_replay(&scenario).expect("frame stream succeeds");
    assert_eq!(result.metrics.delivered_frames, 240);
    assert_eq!(result.metrics.rejected, 0);
    assert_eq!(result.metrics.peak_queue_depth, 241);
}

#[test]
fn transport_failure_is_terminal_and_never_counted_as_delivery() {
    let scenario = scenario(
        json!([mouse()]),
        json!([
            available("mouse-1", 0, 1),
            event(
                1,
                1,
                json!({
                    "kind": "lighting-frame",
                    "transaction_id": "failed-frame",
                    "frame_index": 0,
                    "targets": ["mouse-1"],
                    "outcome": "failed"
                })
            )
        ]),
    );
    let result = run_replay(&scenario).expect("failure is represented in result");
    assert_eq!(
        result.trace[1].outcome,
        ApplyOutcome::RejectedTransportFailure
    );
    assert_eq!(result.metrics.failed_frames, 1);
    assert_eq!(result.metrics.delivered_frames, 0);
}

#[test]
fn restore_completes_once_and_disconnect_invalidates_partial_work() {
    let scenario = scenario(
        json!([keyboard(), mouse()]),
        json!([
            available("keyboard-1", 0, 1),
            available("mouse-1", 1, 1),
            event(
                2,
                1,
                json!({"kind": "restore-started", "restore_id": "restore-1", "targets": ["keyboard-1", "mouse-1"]})
            ),
            event(
                3,
                1,
                json!({"kind": "restore-target", "restore_id": "restore-1", "device_id": "keyboard-1", "outcome": "delivered"})
            ),
            event(4, 1, json!({"kind": "receiver-disconnected"})),
            event(
                5,
                1,
                json!({"kind": "restore-target", "restore_id": "restore-1", "device_id": "mouse-1", "outcome": "delivered"})
            ),
            event(6, 2, json!({"kind": "receiver-connected"})),
            event(
                7,
                1,
                json!({"kind": "restore-target", "restore_id": "restore-1", "device_id": "mouse-1", "outcome": "delivered"})
            ),
            event(
                8,
                2,
                json!({"kind": "device-pairing", "device_id": "keyboard-1", "state": "paired"})
            ),
            available("keyboard-1", 9, 2),
            event(
                10,
                2,
                json!({"kind": "device-pairing", "device_id": "mouse-1", "state": "paired"})
            ),
            available("mouse-1", 11, 2),
            event(
                12,
                2,
                json!({"kind": "restore-started", "restore_id": "restore-2", "targets": ["keyboard-1", "mouse-1"]})
            ),
            event(
                13,
                2,
                json!({"kind": "restore-target", "restore_id": "restore-2", "device_id": "keyboard-1", "outcome": "delivered"})
            ),
            event(
                14,
                2,
                json!({"kind": "restore-target", "restore_id": "restore-2", "device_id": "mouse-1", "outcome": "delivered"})
            )
        ]),
    );
    let result = run_replay(&scenario).expect("restore scenario succeeds");
    assert_eq!(
        result.trace[5].outcome,
        ApplyOutcome::RejectedReceiverAbsent
    );
    assert_eq!(
        result.trace[7].outcome,
        ApplyOutcome::RejectedStaleGeneration
    );
    assert_eq!(result.metrics.invalidated_restores, 1);
    assert_eq!(result.metrics.completed_restores, 1);
    assert!(result.final_snapshot.pending_restore.is_none());
}

#[test]
fn malformed_observation_records_a_failure_without_changing_state() {
    let scenario = scenario(
        json!([mouse()]),
        json!([
            event(
                1,
                1,
                json!({"kind": "malformed-observation", "device_id": "mouse-1", "dimension": "battery", "reason": "invalid-value"})
            ),
            event(
                2,
                1,
                json!({"kind": "malformed-observation", "device_id": "mouse-1", "dimension": "contact", "reason": "truncated"})
            )
        ]),
    );
    let result = run_replay(&scenario).expect("malformed reports are modeled");
    assert_eq!(
        result.trace[0].snapshot_sha256,
        result.trace[1].snapshot_sha256
    );
    assert_eq!(result.metrics.malformed_observations, 2);
    assert_eq!(result.metrics.applied, 0);
}
