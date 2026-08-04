// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use crate::StageResult;
use crate::qualification_lifecycle::capture_lifecycle_run;
use std::fs;

fn fixture(name: &str) -> QualificationView {
    serde_json::from_slice(
        &fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../apps/device-qualification/tests/fixtures")
                .join(name),
        )
        .expect("fixture reads"),
    )
    .expect("fixture parses")
}

use std::path::PathBuf;

fn passed(summary: &str) -> StageProgress {
    StageProgress {
        status: StageStatus::Passed,
        result: Some(StageResult {
            summary: summary.to_owned(),
            completed_at: "2026-07-23T10:00:00Z".to_owned(),
            evidence_refs: vec!["physical:test".to_owned()],
        }),
        checkpoint: None,
    }
}

fn captured_run(view: &QualificationView) -> LifecycleRunState {
    let mut captured = view.clone();
    for group in captured.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
        if group.group_id != "lifecycle" {
            for stage in &mut group.stages {
                stage.status = StageStatus::Passed;
            }
        }
    }
    let lifecycle_stage = captured.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-lifecycle"))
        .expect("lifecycle stage")
        .clone();
    capture_lifecycle_run(&captured, &lifecycle_stage).expect("run captures")
}

fn replace_profile_digest(run: &mut LifecycleRunState, device_id: &str, digest: &str) {
    run.plans
        .iter_mut()
        .find(|plan| plan.device_id == device_id)
        .and_then(|plan| plan.profile_binding.as_mut())
        .expect("plan profile binding")
        .digest = digest.to_owned();
    run.receiver
        .devices
        .iter_mut()
        .find(|device| device.device_id == device_id)
        .and_then(|device| device.profile.as_mut())
        .expect("device profile binding")
        .digest = digest.to_owned();
}

fn replace_stage_ids(run: &mut LifecycleRunState) -> BTreeMap<String, String> {
    let mut replacements = BTreeMap::new();
    for plan in &mut run.plans {
        for stage in plan.groups.iter_mut().flat_map(|group| &mut group.stages) {
            let role = StageRole::from_stage_id(&stage.stage_id)
                .expect("generated stage role")
                .as_str();
            let current_id = stage.stage_id.clone();
            let predecessor_id = format!("predecessor-{}-{role}", plan.device_id);
            stage.stage_id.clone_from(&predecessor_id);
            stage.action_id = Some(format!("run:{predecessor_id}"));
            replacements.insert(current_id, predecessor_id);
        }
    }
    replacements
}

#[test]
fn migration_preserves_only_allowlisted_identical_passed_stage_contracts() {
    let catalog = RuntimeProfileCatalog::load().expect("catalog loads");
    let current = fixture("rust-qualified-pair.json");
    let mut old_run = captured_run(&current);

    replace_profile_digest(
        &mut old_run,
        "mouse-test",
        "f72d38a70e9f4f7fb56848937abafa4773902de64b9d07b22e11c12df6dbfe33",
    );
    replace_profile_digest(
        &mut old_run,
        "keyboard-test",
        "fcdd53fe104e649cc7d2c38c939bbd41095dc3eb53488ebb30a8615a76bbfba6",
    );
    let predecessor_stage_ids = replace_stage_ids(&mut old_run);

    let all_stages = current
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .map(|stage| stage.stage_id.clone())
        .collect::<Vec<_>>();
    let mut stale = QualificationSessionState {
        profile_catalog_sha256: "0".repeat(64),
        identity_fingerprint: Some([1; 32]),
        run_id: Some("local-preserved".to_owned()),
        results: all_stages
            .iter()
            .map(|stage_id| {
                let predecessor_id = predecessor_stage_ids
                    .get(stage_id)
                    .expect("predecessor stage id");
                (predecessor_id.clone(), passed(predecessor_id))
            })
            .collect(),
        lifecycle_run: Some(old_run),
        pending_transaction: None,
        hardware_write_executed: true,
        ..QualificationSessionState::default()
    };
    stale.profile_catalog_sha256 = "0".repeat(64);

    let migrated =
        migrate_profile_catalog_state(stale, &current, &catalog, [2; 32]).expect("state migrates");
    assert_eq!(migrated.run_id.as_deref(), Some("local-preserved"));
    assert!(migrated.hardware_write_executed);
    assert_eq!(migrated.identity_fingerprint, Some([2; 32]));
    assert!(migrated.lifecycle_run.is_none());
    assert!(migrated.pending_transaction.is_none());
    assert_eq!(migrated.results.len(), 5);
    let roles = migrated
        .results
        .keys()
        .map(|stage_id| {
            let device = if stage_id.starts_with("mouse-") {
                "mouse"
            } else {
                "keyboard"
            };
            let role = StageRole::from_stage_id(stage_id)
                .expect("generated stage role")
                .as_str();
            format!("{device}:{role}")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        roles,
        [
            "keyboard:lighting",
            "keyboard:map",
            "mouse:lighting",
            "mouse:map",
            "mouse:telemetry",
        ]
    );
    assert!(migrated.results.values().all(|progress| {
        progress
            .result
            .as_ref()
            .is_some_and(|result| result.evidence_refs.len() == 2)
    }));
}

#[test]
fn migration_rejects_unknown_predecessor_and_changed_contract() {
    let catalog = RuntimeProfileCatalog::load().expect("catalog loads");
    let current = fixture("rust-qualified-mouse.json");
    let mut old_run = captured_run(&current);
    old_run.plans[0]
        .profile_binding
        .as_mut()
        .expect("binding")
        .digest = "0".repeat(64);
    old_run.receiver.devices[0]
        .profile
        .as_mut()
        .expect("profile")
        .digest = "0".repeat(64);
    let stage_id = current.plans[0].groups[1].stages[0].stage_id.clone();
    let stale = QualificationSessionState {
        profile_catalog_sha256: "0".repeat(64),
        results: BTreeMap::from([(stage_id, passed("old"))]),
        lifecycle_run: Some(old_run),
        ..QualificationSessionState::default()
    };
    assert!(migrate_profile_catalog_state(stale, &current, &catalog, [2; 32]).is_none());

    let mut old_run = captured_run(&current);
    old_run.plans[0].groups[1].stages[0]
        .description
        .push_str(" changed");
    let stage_id = current.plans[0].groups[1].stages[0].stage_id.clone();
    let stale = QualificationSessionState {
        profile_catalog_sha256: "0".repeat(64),
        results: BTreeMap::from([(stage_id, passed("old"))]),
        lifecycle_run: Some(old_run),
        ..QualificationSessionState::default()
    };
    assert!(migrate_profile_catalog_state(stale, &current, &catalog, [2; 32]).is_none());
}

#[test]
fn persisted_stage_authority_supports_migration_before_lifecycle_starts() {
    let catalog = RuntimeProfileCatalog::load().expect("catalog loads");
    let current = fixture("rust-qualified-mouse.json");
    let mut old_run = captured_run(&current);
    replace_profile_digest(
        &mut old_run,
        "mouse-test",
        "f72d38a70e9f4f7fb56848937abafa4773902de64b9d07b22e11c12df6dbfe33",
    );
    let stage_id = current.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-telemetry"))
        .expect("telemetry stage")
        .stage_id
        .clone();
    let stale = QualificationSessionState {
        profile_catalog_sha256: "0".repeat(64),
        results: BTreeMap::from([(stage_id.clone(), passed("telemetry"))]),
        stage_authorities: stage_authorities_from_lifecycle(&old_run),
        lifecycle_run: None,
        ..QualificationSessionState::default()
    };

    let migrated = migrate_profile_catalog_state(stale, &current, &catalog, [3; 32])
        .expect("state migrates from persisted authority");
    assert_eq!(migrated.results.len(), 1);
    assert!(migrated.results.contains_key(&stage_id));
    assert!(migrated.lifecycle_run.is_none());
    assert_eq!(
        migrated.stage_authorities,
        qualification_stage_authorities(&current)
    );
}
