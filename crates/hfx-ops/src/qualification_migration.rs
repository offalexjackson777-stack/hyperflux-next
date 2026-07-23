// SPDX-License-Identifier: GPL-2.0-only

#[cfg(test)]
use crate::qualification::StageProgress;
use crate::qualification::StageStatus;
use crate::qualification_lifecycle::LifecycleRunState;
use crate::qualification_state::{QualificationSessionState, QualificationStageAuthority};
use crate::{
    QualificationGroup, QualificationPlan, QualificationStage, QualificationView, ReceiverView,
};
use hfx_profiles::{PROFILE_SOURCE_SHA256, RuntimeProfile, RuntimeProfileCatalog};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum StageRole {
    Identity,
    Telemetry,
    Map,
    Lighting,
    Lifecycle,
}

impl StageRole {
    fn from_stage_id(stage_id: &str) -> Option<Self> {
        [
            ("-identity", Self::Identity),
            ("-telemetry", Self::Telemetry),
            ("-map", Self::Map),
            ("-lighting", Self::Lighting),
            ("-lifecycle", Self::Lifecycle),
        ]
        .into_iter()
        .find_map(|(suffix, role)| stage_id.ends_with(suffix).then_some(role))
    }

    const fn as_str(self) -> &'static str {
        match self {
            Self::Identity => "identity",
            Self::Telemetry => "telemetry",
            Self::Map => "map",
            Self::Lighting => "lighting",
            Self::Lifecycle => "lifecycle",
        }
    }
}

/// Carries forward only profile stages whose exact predecessor, hardware
/// generation, and immutable stage contract are explicitly authorized.
pub(crate) fn migrate_profile_catalog_state(
    mut stale: QualificationSessionState,
    current_view: &QualificationView,
    catalog: &RuntimeProfileCatalog,
    current_fingerprint: [u8; 32],
) -> Option<QualificationSessionState> {
    let current_authorities = qualification_stage_authorities(current_view);
    if current_authorities.is_empty() {
        return None;
    }
    let old_authorities = if stale.stage_authorities.is_empty() {
        stale
            .lifecycle_run
            .as_ref()
            .map(stage_authorities_from_lifecycle)
            .unwrap_or_default()
    } else {
        stale.stage_authorities.clone()
    };
    if old_authorities.is_empty() {
        return None;
    }
    let mut preserved = BTreeMap::new();

    for (stage_id, current_authority) in &current_authorities {
        let Some((old_stage_id, old_authority)) =
            matching_old_authority(stage_id, current_authority, &old_authorities)
        else {
            continue;
        };
        let Some(profile) = catalog
            .iter()
            .find(|profile| profile.profile_id.as_str() == current_authority.profile_id)
        else {
            continue;
        };
        if profile.runtime_digest.as_str() != current_authority.profile_digest {
            continue;
        }
        let Some(role) = StageRole::from_stage_id(stage_id) else {
            continue;
        };
        if !allowed_roles(
            profile,
            &old_authority.profile_digest,
            &current_authority.profile_digest,
        )
        .contains(&role)
        {
            continue;
        }
        let Some(progress) = stale.results.get(old_stage_id) else {
            continue;
        };
        if progress.status != StageStatus::Passed || progress.result.is_none() {
            continue;
        }
        let mut progress = progress.clone();
        if old_authority.profile_digest != current_authority.profile_digest {
            progress
                .result
                .as_mut()
                .expect("passed result was checked")
                .evidence_refs
                .push(format!(
                    "profile-migration:{}:{}:{}",
                    old_authority.profile_digest,
                    current_authority.profile_digest,
                    role.as_str()
                ));
        }
        preserved.insert(stage_id.clone(), progress);
    }

    if preserved.is_empty() {
        return None;
    }
    PROFILE_SOURCE_SHA256.clone_into(&mut stale.profile_catalog_sha256);
    stale.revision = stale.revision.saturating_add(1);
    stale.identity_fingerprint = Some(current_fingerprint);
    stale.results = preserved;
    stale.stage_authorities = current_authorities;
    stale.pending_transaction = None;
    stale.lifecycle_run = None;
    Some(stale)
}

pub(crate) fn qualification_stage_authorities(
    view: &QualificationView,
) -> BTreeMap<String, QualificationStageAuthority> {
    stage_authorities(&view.receivers, &view.plans)
}

fn stage_authorities_from_lifecycle(
    run: &LifecycleRunState,
) -> BTreeMap<String, QualificationStageAuthority> {
    stage_authorities(std::slice::from_ref(&run.receiver), &run.plans)
}

fn stage_authorities(
    receivers: &[ReceiverView],
    plans: &[QualificationPlan],
) -> BTreeMap<String, QualificationStageAuthority> {
    let mut authorities = BTreeMap::new();
    for plan in plans {
        let Some(receiver) = receivers.iter().find(|receiver| {
            receiver.receiver_id == plan.receiver_id && receiver.generation_id == plan.generation_id
        }) else {
            continue;
        };
        let Some(receiver_profile) = &receiver.profile else {
            continue;
        };
        let Some(device) = receiver
            .devices
            .iter()
            .find(|device| device.device_id == plan.device_id)
        else {
            continue;
        };
        let Some(profile) = &device.profile else {
            continue;
        };
        if plan.profile_binding.as_ref() != Some(profile) {
            continue;
        }
        for (group, stage) in plan_stages(plan) {
            let authority = QualificationStageAuthority {
                receiver_id: receiver.receiver_id.clone(),
                generation_id: receiver.generation_id,
                receiver_profile_id: receiver_profile.id.clone(),
                receiver_profile_digest: receiver_profile.digest.clone(),
                device_id: device.device_id.clone(),
                product_id: device.product_id,
                profile_id: profile.id.clone(),
                profile_digest: profile.digest.clone(),
                stage_contract_sha256: stage_contract_sha256(group, stage),
            };
            if authorities
                .insert(stage.stage_id.clone(), authority)
                .is_some()
            {
                return BTreeMap::new();
            }
        }
    }
    authorities
}

fn same_stage_authority(
    old: &QualificationStageAuthority,
    current: &QualificationStageAuthority,
) -> bool {
    old.receiver_id == current.receiver_id
        && old.generation_id == current.generation_id
        && old.receiver_profile_id == current.receiver_profile_id
        && old.receiver_profile_digest == current.receiver_profile_digest
        && old.device_id == current.device_id
        && old.product_id == current.product_id
        && old.profile_id == current.profile_id
        && old.stage_contract_sha256 == current.stage_contract_sha256
}

fn matching_old_authority<'a>(
    current_stage_id: &str,
    current: &QualificationStageAuthority,
    old_authorities: &'a BTreeMap<String, QualificationStageAuthority>,
) -> Option<(&'a str, &'a QualificationStageAuthority)> {
    let role = StageRole::from_stage_id(current_stage_id)?;
    let mut matches = old_authorities.iter().filter(|(stage_id, authority)| {
        StageRole::from_stage_id(stage_id) == Some(role) && same_stage_authority(authority, current)
    });
    let (stage_id, authority) = matches.next()?;
    if matches.next().is_some() {
        return None;
    }
    Some((stage_id.as_str(), authority))
}

fn allowed_roles(
    profile: &RuntimeProfile,
    old_digest: &str,
    current_digest: &str,
) -> Vec<StageRole> {
    if old_digest == current_digest {
        return vec![
            StageRole::Identity,
            StageRole::Telemetry,
            StageRole::Map,
            StageRole::Lighting,
            StageRole::Lifecycle,
        ];
    }
    profile
        .qualification_migrations
        .iter()
        .find(|migration| migration.from_runtime_digest.as_str() == old_digest)
        .map(|migration| {
            migration
                .preserve_stage_roles
                .iter()
                .filter_map(|role| match *role {
                    "telemetry" => Some(StageRole::Telemetry),
                    "map" => Some(StageRole::Map),
                    "lighting" => Some(StageRole::Lighting),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn plan_stages(
    plan: &QualificationPlan,
) -> impl Iterator<Item = (&QualificationGroup, &QualificationStage)> {
    plan.groups
        .iter()
        .flat_map(|group| group.stages.iter().map(move |stage| (group, stage)))
}

fn stage_contract_sha256(group: &QualificationGroup, stage: &QualificationStage) -> String {
    let role = match StageRole::from_stage_id(&stage.stage_id) {
        Some(role) => role.as_str(),
        None => stage.stage_id.as_str(),
    };
    let encoded = serde_json::to_vec(&(
        &group.group_id,
        &group.title,
        &group.description,
        role,
        &stage.title,
        &stage.description,
        stage.kind,
        stage.risk,
        &stage.capabilities,
        &stage.instructions,
        &stage.observations,
    ))
    .expect("qualification stage contracts contain no fallible values");
    let digest = Sha256::digest(encoded);
    let mut value = String::with_capacity(64);
    for byte in digest {
        write!(&mut value, "{byte:02x}").expect("writing to a string cannot fail");
    }
    value
}

#[cfg(test)]
mod tests {
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

        let migrated = migrate_profile_catalog_state(stale, &current, &catalog, [2; 32])
            .expect("state migrates");
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
}
