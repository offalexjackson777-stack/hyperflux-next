// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{FixtureSource, GenerationId, PairingState, RouteState};
use hfx_sim::{MAX_REPLAY_BYTES, parse_replay, run_replay};

const FIXTURE: &[u8] = include_bytes!("../../../tests/fixtures/replay/qualified-lifecycle-v1.json");

#[test]
fn sanitized_replay_is_bitwise_deterministic_and_non_authoritative() {
    let scenario = parse_replay(FIXTURE).expect("sanitized fixture parses");
    let first = run_replay(&scenario).expect("first replay succeeds");
    let second = run_replay(&scenario).expect("second replay succeeds");

    assert_eq!(first, second);
    assert_eq!(first.content_sha256.len(), 64);
    assert_eq!(first.source, FixtureSource::SanitizedReplay);
    assert!(first.test_fixture);
    assert!(!first.hardware_claim_authority);
    assert_eq!(
        first.final_snapshot.receiver_generation,
        GenerationId::try_from(2_u64).expect("valid generation")
    );
    assert_eq!(first.metrics.completed_restores, 1);
    assert_eq!(first.metrics.rejected, 0);
    for device in first.final_snapshot.devices.values() {
        assert_eq!(device.pairing.value, PairingState::Paired);
        assert_eq!(device.route.value, RouteState::Available);
    }
}

#[test]
fn external_replay_rejects_unbounded_or_non_sanitized_input() {
    assert!(parse_replay(&[]).is_err());
    assert!(parse_replay(&vec![b' '; MAX_REPLAY_BYTES + 1]).is_err());

    let text = String::from_utf8(FIXTURE.to_vec()).expect("fixture is UTF-8");
    let deterministic = text.replace("sanitized-replay", "deterministic-simulator");
    assert!(parse_replay(deterministic.as_bytes()).is_err());
}

#[test]
fn replay_rejects_private_or_unknown_fields_instead_of_retaining_them() {
    let mut value: serde_json::Value = serde_json::from_slice(FIXTURE).expect("fixture is JSON");
    value["initial"]["children"][0]["hardware_serial"] = serde_json::json!("private-canary");
    let encoded = serde_json::to_vec(&value).expect("modified fixture serializes");
    let error = parse_replay(&encoded).expect_err("private field is rejected");
    assert!(error.to_string().contains("unknown field"));
}
