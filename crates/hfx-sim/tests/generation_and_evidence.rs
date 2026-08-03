// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{available, delayed_event, event, mouse, scenario};
use hfx_domain::{ApplyOutcome, RouteState};
use hfx_sim::run_replay;
use serde_json::json;

#[test]
fn delayed_older_observation_cannot_overwrite_newer_evidence() {
    let scenario = scenario(
        json!([mouse()]),
        json!([
            delayed_event(
                10,
                100,
                1,
                json!({"kind": "route-observed", "device_id": "mouse-1", "state": "unavailable"})
            ),
            available("mouse-1", 20, 1)
        ]),
    );
    let result = run_replay(&scenario).expect("replay succeeds");
    let mouse = result
        .final_snapshot
        .devices
        .values()
        .next()
        .expect("mouse exists");
    assert_eq!(mouse.route.value, RouteState::Available);
    assert_eq!(
        result.trace[1].outcome,
        ApplyOutcome::IgnoredOlderObservation
    );
    assert_eq!(result.metrics.ignored_older_observations, 1);
}

#[test]
fn reconnect_requires_a_new_generation_and_old_events_fail_closed() {
    let scenario = scenario(
        json!([mouse()]),
        json!([
            event(10, 1, json!({"kind": "receiver-disconnected"})),
            event(20, 1, json!({"kind": "receiver-connected"})),
            event(30, 2, json!({"kind": "receiver-connected"})),
            available("mouse-1", 40, 1),
            event(
                50,
                2,
                json!({"kind": "device-pairing", "device_id": "mouse-1", "state": "paired"})
            ),
            available("mouse-1", 51, 2)
        ]),
    );
    let result = run_replay(&scenario).expect("replay succeeds");
    assert_eq!(
        result.trace[1].outcome,
        ApplyOutcome::RejectedInvalidTransition
    );
    assert_eq!(
        result.trace[3].outcome,
        ApplyOutcome::RejectedStaleGeneration
    );
    assert_eq!(result.final_snapshot.receiver_generation.get(), 2);
    assert_eq!(
        result
            .final_snapshot
            .devices
            .values()
            .next()
            .expect("mouse exists")
            .route
            .value,
        RouteState::Available
    );
}
