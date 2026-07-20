// SPDX-License-Identifier: GPL-2.0-only

#![allow(dead_code)]

use hfx_sim::Scenario;
use serde_json::{Value, json};

pub fn scenario(children: Value, events: Value) -> Scenario {
    let mut value = json!({
        "schema": "hyperflux-simulator-scenario-v1",
        "scenario_id": "test-scenario",
        "provenance": {
            "source": "deterministic-simulator",
            "test_fixture": true,
            "hardware_claim_authority": false,
            "private_identifiers_exported": false,
            "sanitization": "no-private-identifiers-v1"
        },
        "initial": {
            "receiver_profile_id": "receiver.razer.hyperflux-v2.1532-00cf",
            "receiver_generation": "1",
            "surface_profile_id": null,
            "children": []
        },
        "events": []
    });
    value["initial"]["children"] = children;
    value["events"] = events;
    serde_json::from_value(value).expect("test scenario is structurally valid")
}

pub fn mouse() -> Value {
    json!({
        "logical_device_id": "mouse-1",
        "device_kind": "mouse",
        "product_id": 205,
        "profile_id": "child.razer.basilisk-v3-pro-35k.00cd",
        "pairing": "paired"
    })
}

pub fn keyboard() -> Value {
    json!({
        "logical_device_id": "keyboard-1",
        "device_kind": "keyboard",
        "product_id": 662,
        "profile_id": "child.razer.deathstalker-v2-pro-tkl.0296",
        "pairing": "paired"
    })
}

pub fn unknown_mouse() -> Value {
    json!({
        "logical_device_id": "unknown-1",
        "device_kind": "mouse",
        "product_id": 65535,
        "profile_id": null,
        "pairing": "paired"
    })
}

pub fn event(time: u64, generation: u64, value: Value) -> Value {
    let mut event = json!({
        "observed_at_ms": time,
        "generation_id": generation.to_string(),
        "event": {}
    });
    event["event"] = value;
    event
}

pub fn delayed_event(time: u64, delay: u64, generation: u64, value: Value) -> Value {
    let mut event = json!({
        "observed_at_ms": time,
        "delay_ms": delay,
        "generation_id": generation.to_string(),
        "event": {}
    });
    event["event"] = value;
    event
}

pub fn available(device_id: &str, time: u64, generation: u64) -> Value {
    event(
        time,
        generation,
        json!({"kind": "route-observed", "device_id": device_id, "state": "available"}),
    )
}
