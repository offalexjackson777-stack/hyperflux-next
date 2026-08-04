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
mod tests;
