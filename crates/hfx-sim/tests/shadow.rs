// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::PresenceState;
use hfx_sim::{ShadowDomain, parse_shadow_fixture, run_shadow_comparison};
use std::fs;
use std::path::PathBuf;

fn fixture_bytes() -> Vec<u8> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    fs::read(root.join("tests/fixtures/shadow/qualified-lifecycle-v1.json"))
        .expect("shadow fixture is readable")
}

#[test]
fn committed_shadow_fixture_matches_all_five_domains_without_authority() {
    let fixture = parse_shadow_fixture(&fixture_bytes()).expect("shadow fixture is valid");
    let first = run_shadow_comparison(&fixture).expect("shadow comparison runs");
    let second = run_shadow_comparison(&fixture).expect("shadow comparison is repeatable");

    assert_eq!(first, second);
    assert_eq!(first.status, "matched");
    assert_eq!(first.domains.len(), 5);
    assert!(first.domains.iter().all(|domain| domain.matched));
    assert!(first.differences.is_empty());
    assert!(first.boundary.test_fixture);
    assert!(first.boundary.read_only);
    assert!(!first.authority.hardware_claim_authority);
    assert!(!first.authority.publication_authorized);
    assert!(!first.side_effects.private_identifiers_exported);
    assert!(!first.side_effects.hardware_queried);
    assert!(!first.side_effects.hardware_writes_executed);
}

#[test]
fn semantic_drift_is_typed_as_a_divergence_instead_of_becoming_authority() {
    let mut fixture = parse_shadow_fixture(&fixture_bytes()).expect("shadow fixture is valid");
    let checkpoint = fixture
        .legacy_decisions
        .iter_mut()
        .find(|decision| decision.sequence == 4)
        .expect("sleep checkpoint exists");
    checkpoint
        .presence_states
        .as_mut()
        .expect("presence expectation exists")
        .insert("mouse-1".to_owned(), PresenceState::Available);

    let result = run_shadow_comparison(&fixture).expect("divergence remains a valid result");
    assert_eq!(result.status, "diverged");
    assert_eq!(result.differences.len(), 1);
    assert_eq!(result.differences[0].sequence, 4);
    assert_eq!(result.differences[0].domain, ShadowDomain::PresenceState);
    assert!(!result.side_effects.hardware_queried);
    assert!(!result.side_effects.hardware_writes_executed);
    assert!(!result.authority.publication_authorized);
}

#[test]
fn unsafe_shadow_provenance_is_rejected_before_replay() {
    let mut value: serde_json::Value =
        serde_json::from_slice(&fixture_bytes()).expect("fixture JSON parses");
    value["provenance"]["side_effects"]["hardware_queried"] = serde_json::Value::Bool(true);
    let bytes = serde_json::to_vec(&value).expect("fixture mutation serializes");

    let error = parse_shadow_fixture(&bytes).expect_err("hardware access must fail closed");
    assert!(
        error
            .to_string()
            .contains("read-only sanitized comparison boundary")
    );
}
