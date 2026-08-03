// SPDX-License-Identifier: GPL-2.0-only

use super::*;

fn test_path(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "hyperflux-next-qualification-state-{}-{label}/session.json",
        std::process::id()
    ))
}

#[test]
fn state_round_trips_privately_and_rejects_stale_authority() {
    let path = test_path("round-trip");
    let _ = fs::remove_dir_all(path.parent().expect("parent"));
    let mut state = QualificationSessionState {
        revision: 7,
        identity_fingerprint: Some([9_u8; 32]),
        run_id: Some("local-test".to_owned()),
        pending_transaction: Some(PendingLightingTransaction {
            stage_id: "mouse-00cd-lighting".to_owned(),
            transaction_id: hfx_domain::TransactionId::try_from("qualification-test")
                .expect("transaction id"),
            step: crate::qualification_runner::LightingStep::LightingOff,
            prior_progress: Some(StageProgress {
                status: crate::StageStatus::AwaitingObservation,
                result: None,
                checkpoint: Some(crate::qualification::StageCheckpoint {
                    checkpoint_id: "lighting-blue".to_owned(),
                    instructions: Vec::new(),
                    observations: Vec::new(),
                    evidence_refs: vec!["bridge-transaction:previous".to_owned()],
                }),
            }),
        }),
        ..QualificationSessionState::default()
    };
    save_state(&path, &state).expect("state saves");
    assert_eq!(
        fs::metadata(&path).expect("metadata").permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(load_state(&path).expect("state loads"), Some(state.clone()));
    state.coordinator_sha256 = "0".repeat(64);
    let payload = serde_json::to_vec_pretty(&state).expect("state serializes");
    fs::write(&path, payload).expect("test state changes");
    assert!(matches!(
        load_state(&path),
        Err(QualificationStateError::Stale)
    ));
    fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
}

#[test]
fn stale_state_is_archived_once_without_data_loss() {
    let path = test_path("archive-stale");
    let _ = fs::remove_dir_all(path.parent().expect("parent"));
    save_state(&path, &QualificationSessionState::default()).expect("state saves");
    let mut stale: serde_json::Value =
        serde_json::from_slice(&fs::read(&path).expect("state reads")).expect("state parses");
    stale["coordinator_sha256"] = serde_json::Value::String("0".repeat(64));
    let payload = serde_json::to_vec_pretty(&stale).expect("stale state serializes");
    fs::write(&path, &payload).expect("stale state writes");
    assert!(matches!(
        load_state(&path),
        Err(QualificationStateError::Stale)
    ));

    let first = archive_stale_state(&path).expect("stale state archives");
    let second = archive_stale_state(&path).expect("same state reuses archive");
    assert_eq!(first, second);
    assert_eq!(fs::read(first).expect("archive reads"), payload);
    fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
}

#[test]
fn only_a_profile_catalog_change_is_exposed_for_guarded_migration() {
    let path = test_path("profile-catalog-stale");
    let _ = fs::remove_dir_all(path.parent().expect("parent"));
    save_state(&path, &QualificationSessionState::default()).expect("state saves");
    let mut stale: serde_json::Value =
        serde_json::from_slice(&fs::read(&path).expect("state reads")).expect("state parses");
    stale
        .as_object_mut()
        .expect("state object")
        .remove("stage_authorities");
    stale["profile_catalog_sha256"] = serde_json::Value::String("0".repeat(64));
    fs::write(
        &path,
        serde_json::to_vec_pretty(&stale).expect("state serializes"),
    )
    .expect("state writes");

    let (candidate, compatibility) = load_state_with_compatibility(&path)
        .expect("candidate loads")
        .expect("candidate exists");
    assert_eq!(
        compatibility,
        QualificationStateCompatibility::ProfileCatalogStale
    );
    assert_eq!(candidate.profile_catalog_sha256, "0".repeat(64));
    assert!(candidate.stage_authorities.is_empty());
    assert!(matches!(
        load_state(&path),
        Err(QualificationStateError::Stale)
    ));

    stale["runtime_sha256"] = serde_json::Value::String("1".repeat(64));
    fs::write(
        &path,
        serde_json::to_vec_pretty(&stale).expect("state serializes"),
    )
    .expect("state writes");
    assert!(matches!(
        load_state_with_compatibility(&path),
        Err(QualificationStateError::Stale)
    ));
    fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
}

#[test]
fn exact_legacy_source_bound_coordinator_migrates_without_losing_results() {
    let path = test_path("legacy-coordinator");
    let _ = fs::remove_dir_all(path.parent().expect("parent"));
    let mut state = QualificationSessionState {
        revision: 19,
        run_id: Some("local-preserved".to_owned()),
        results: BTreeMap::from([(
            "keyboard-profile-identity".to_owned(),
            StageProgress {
                status: crate::StageStatus::Passed,
                result: None,
                checkpoint: None,
            },
        )]),
        ..QualificationSessionState::default()
    };
    state.coordinator_sha256 = LEGACY_SOURCE_BOUND_COORDINATOR_SHA256.to_owned();
    fs::create_dir_all(path.parent().expect("parent")).expect("parent creates");
    write_private_file(
        &path,
        &serde_json::to_vec_pretty(&state).expect("legacy state serializes"),
    )
    .expect("legacy state writes");

    let migrated = load_state(&path)
        .expect("legacy state loads")
        .expect("legacy state exists");
    assert_eq!(migrated.coordinator_sha256, coordinator_sha256());
    assert_eq!(migrated.revision, 19);
    assert_eq!(migrated.run_id.as_deref(), Some("local-preserved"));
    assert_eq!(migrated.results, state.results);
    fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
}

#[test]
fn state_rejects_world_readable_files() {
    let path = test_path("permissions");
    let _ = fs::remove_dir_all(path.parent().expect("parent"));
    save_state(&path, &QualificationSessionState::default()).expect("state saves");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("permissions change");
    assert!(matches!(
        load_state(&path),
        Err(QualificationStateError::Insecure)
    ));
    fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
}
