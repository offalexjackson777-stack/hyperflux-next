// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{available, event, keyboard, mouse, scenario, unknown_mouse};
use hfx_domain::{ApplyOutcome, PresenceState};
use hfx_sim::{BatteryState, run_replay};
use serde_json::json;

#[test]
fn mouse_only_and_keyboard_only_topologies_need_no_sibling() {
    for (device, id) in [(mouse(), "mouse-1"), (keyboard(), "keyboard-1")] {
        let scenario = scenario(json!([device]), json!([available(id, 1, 1)]));
        let result = run_replay(&scenario).expect("independent child topology succeeds");
        assert_eq!(result.final_snapshot.devices.len(), 1);
        assert_eq!(
            result
                .final_snapshot
                .devices
                .values()
                .next()
                .expect("child exists")
                .presence,
            PresenceState::Available
        );
    }
}

#[test]
fn unknown_child_is_visible_but_receives_zero_write_authority() {
    let scenario = scenario(
        json!([unknown_mouse()]),
        json!([
            available("unknown-1", 1, 1),
            event(
                2,
                1,
                json!({
                    "kind": "lighting-frame",
                    "transaction_id": "unknown-write",
                    "frame_index": 0,
                    "targets": ["unknown-1"],
                    "outcome": "delivered"
                })
            )
        ]),
    );
    let result = run_replay(&scenario).expect("replay succeeds");
    assert_eq!(
        result.trace[1].outcome,
        ApplyOutcome::RejectedUnqualifiedWrite
    );
    assert_eq!(result.metrics.delivered_frames, 0);
}

#[test]
fn battery_unknown_unavailable_and_zero_are_distinct() {
    let initial = scenario(json!([keyboard()]), json!([]));
    let initial_result = run_replay(&initial).expect("initial replay succeeds");
    assert_eq!(
        initial_result
            .final_snapshot
            .devices
            .values()
            .next()
            .expect("keyboard exists")
            .battery
            .value,
        BatteryState::Unknown
    );

    let scenario = scenario(
        json!([keyboard()]),
        json!([
            event(
                1,
                1,
                json!({"kind": "battery-unavailable", "device_id": "keyboard-1"})
            ),
            event(
                2,
                1,
                json!({"kind": "battery-reported", "device_id": "keyboard-1", "percentage": 0})
            )
        ]),
    );
    let result = run_replay(&scenario).expect("battery replay succeeds");
    assert!(matches!(
        result
            .final_snapshot
            .devices
            .values()
            .next()
            .expect("keyboard exists")
            .battery
            .value,
        BatteryState::Reported { percentage } if percentage.get() == 0
    ));
}

#[test]
fn sleep_and_explicit_power_off_produce_different_presence_facts() {
    let scenario = scenario(
        json!([mouse()]),
        json!([
            available("mouse-1", 1, 1),
            event(
                2,
                1,
                json!({"kind": "sleep-observed", "device_id": "mouse-1", "state": "asleep"})
            ),
            event(
                3,
                1,
                json!({"kind": "sleep-observed", "device_id": "mouse-1", "state": "awake"})
            ),
            event(
                4,
                1,
                json!({"kind": "power-observed", "device_id": "mouse-1", "state": "off"})
            )
        ]),
    );
    let result = run_replay(&scenario).expect("state replay succeeds");
    assert_eq!(result.trace[1].outcome, ApplyOutcome::Applied);
    assert_eq!(
        result
            .final_snapshot
            .devices
            .values()
            .next()
            .expect("mouse exists")
            .presence,
        PresenceState::Unavailable
    );
}

#[test]
fn keyboard_contact_observation_is_rejected_atomically() {
    let scenario = scenario(
        json!([keyboard()]),
        json!([event(
            1,
            1,
            json!({"kind": "contact-observed", "device_id": "keyboard-1", "state": "on-mat"})
        )]),
    );
    let result = run_replay(&scenario).expect("replay succeeds");
    assert_eq!(
        result.trace[0].outcome,
        ApplyOutcome::RejectedInvalidTransition
    );
}
