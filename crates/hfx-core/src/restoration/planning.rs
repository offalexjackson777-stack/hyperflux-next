// SPDX-License-Identifier: GPL-2.0-only

//! Restore-claim planning and compatible stable-intent migration.

use super::intent::{intent_digest, load_entries};
use super::records::{
    enforce_prior_outcomes, lighting_resource, load_records, new_restore_record, persist_record,
    supersede_prior_claims, validate_same_claim,
};
use super::{
    PersistenceOperation, RestorationCoordinator, RestorationError, RestorePlanResult,
    next_intent_revision, validate_schema,
};
use crate::{
    PersistedStableEntry, PersistedStableIntent, PersistenceCasOutcome, PersistenceStore,
    ProfileRegistry, RestoreRecord, RestoreTrigger, StableIntentChange,
};
use hfx_domain::{GenerationId, ReceiverId};
use hfx_protocol::ResourceKey;

impl RestorationCoordinator {
    /// Rebinds saved stable-lighting intents across an explicitly compatible
    /// profile upgrade without creating restore claims.
    ///
    /// This is separated from claim planning so a process restart can update
    /// durable intent authority before recovering older deferred claims.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid triggers, persistence failures, uncertain
    /// prior outcomes, or compare-and-set conflicts.
    pub fn rebind_compatible_stable_intents<P: ProfileRegistry, S: PersistenceStore>(
        &self,
        trigger: &RestoreTrigger,
        profiles: &P,
        store: &mut S,
    ) -> Result<usize, RestorationError> {
        validate_trigger(trigger)?;
        let policy = store
            .restore_policy(&trigger.receiver_id)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadPolicy))?;
        let Some(policy) = policy else {
            return Ok(0);
        };
        validate_schema(policy.schema_version)?;
        if policy.receiver_id != trigger.receiver_id {
            return Err(RestorationError::ReceiverMismatch);
        }
        if !policy.enabled {
            return Ok(0);
        }
        let records = load_records(&trigger.receiver_id, store)?;
        validate_trigger_history(trigger, &records)?;
        migrate_compatible_stable_intents(trigger, profiles, &records, store)
    }

    /// Creates one durable claim per profile-compatible stable intent for a
    /// lifecycle trigger.
    ///
    /// Repeating the same trigger returns the same records. A newer trigger
    /// invalidates safely supersedable claims, while attempted claims remain a
    /// barrier until their exact transport outcome is reconciled. Known profile
    /// incompatibility creates no claim, while temporarily unavailable profile
    /// authority is reported separately so the caller may retain bounded retries.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid persisted records, capacity violations,
    /// storage failures, or compare-and-set conflicts.
    pub fn plan_restore<P: ProfileRegistry, S: PersistenceStore>(
        &self,
        trigger: &RestoreTrigger,
        profiles: &P,
        store: &mut S,
    ) -> Result<RestorePlanResult, RestorationError> {
        validate_trigger(trigger)?;
        let policy = store
            .restore_policy(&trigger.receiver_id)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadPolicy))?;
        let Some(policy) = policy else {
            return Ok(RestorePlanResult::Disabled);
        };
        validate_schema(policy.schema_version)?;
        if policy.receiver_id != trigger.receiver_id {
            return Err(RestorationError::ReceiverMismatch);
        }
        if !policy.enabled {
            return Ok(RestorePlanResult::Disabled);
        }
        let mut records = load_records(&trigger.receiver_id, store)?;
        validate_trigger_history(trigger, &records)?;
        migrate_compatible_stable_intents(trigger, profiles, &records, store)?;
        let entries = load_entries(&trigger.receiver_id, store)?;
        let target_intents = entries
            .into_iter()
            .filter_map(|entry| match entry {
                PersistedStableEntry::Present(intent) => Some(intent),
                PersistedStableEntry::Deleted(_) => None,
            })
            .filter(|intent| {
                trigger
                    .target_device_id
                    .as_ref()
                    .is_none_or(|target| target == &intent.device_id)
            })
            .collect::<Vec<_>>();
        if target_intents.is_empty() {
            return Ok(RestorePlanResult::NoStableIntents);
        }
        let mut authority_unavailable = false;
        let mut intents = Vec::with_capacity(target_intents.len());
        for intent in target_intents {
            let resource = lighting_resource(
                &trigger.receiver_id,
                trigger.generation_id,
                &intent.device_id,
            );
            match intent_profile_compatibility(
                &trigger.receiver_id,
                trigger.generation_id,
                &intent,
                &resource,
                profiles,
            ) {
                IntentProfileCompatibility::Compatible => intents.push(intent),
                IntentProfileCompatibility::Unavailable => authority_unavailable = true,
                IntentProfileCompatibility::Incompatible => {}
            }
        }
        if intents.is_empty() {
            return Ok(if authority_unavailable {
                RestorePlanResult::ProfileAuthorityUnavailable
            } else {
                RestorePlanResult::NoCompatibleStableIntents
            });
        }
        let mut planned = Vec::with_capacity(intents.len());
        for intent in intents {
            let candidate = new_restore_record(trigger, &intent)?;
            supersede_prior_claims(&candidate, &mut records, store)?;
            if let Some(existing) = records
                .iter()
                .find(|record| record.claim_id == candidate.claim_id)
                .cloned()
            {
                validate_same_claim(&existing, &candidate)?;
                planned.push(existing);
                continue;
            }
            persist_record(None, &candidate, store)?;
            records.push(candidate.clone());
            planned.push(candidate);
        }
        Ok(RestorePlanResult::Planned(planned))
    }

    /// Loads every validated nonterminal claim for one receiver.
    ///
    /// Production uses this before planning a new lifecycle trigger so a
    /// process restart reconciles an already prepared or applying attempt
    /// before it can supersede that durable authority.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed, over-capacity, foreign, duplicate, or
    /// unavailable persisted restore records.
    pub fn pending_restore_records<S: PersistenceStore>(
        &self,
        receiver_id: &ReceiverId,
        store: &S,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        load_records(receiver_id, store).map(|records| {
            records
                .into_iter()
                .filter(|record| !record.status.is_terminal())
                .collect()
        })
    }
}

fn migrate_compatible_stable_intents<P: ProfileRegistry, S: PersistenceStore>(
    trigger: &RestoreTrigger,
    profiles: &P,
    records: &[RestoreRecord],
    store: &mut S,
) -> Result<usize, RestorationError> {
    let Some(receiver_profile) =
        profiles.receiver_profile(&trigger.receiver_id, trigger.generation_id)
    else {
        return Ok(0);
    };
    let entries = load_entries(&trigger.receiver_id, store)?;
    let mut changes = Vec::new();

    for entry in entries {
        let PersistedStableEntry::Present(intent) = entry else {
            continue;
        };
        if trigger
            .target_device_id
            .as_ref()
            .is_some_and(|target| target != &intent.device_id)
            || intent.receiver_profile_id != receiver_profile.profile_id
            || intent.receiver_profile_digest != receiver_profile.profile_digest
        {
            continue;
        }

        let resource = lighting_resource(
            &trigger.receiver_id,
            trigger.generation_id,
            &intent.device_id,
        );
        let Some(current) = profiles.device_profile(&resource) else {
            continue;
        };
        if intent.profile_digest == current.profile_digest
            || intent.profile_id != current.profile_id
            || intent.application_slot_count != current.application_slot_count
            || !profiles.permits_stable_lighting_migration(&resource, &intent.profile_digest)
        {
            continue;
        }
        enforce_prior_outcomes(records.iter().filter(|record| {
            record.receiver_id == intent.receiver_id
                && record.device_id == intent.device_id
                && record.intent_revision == intent.revision
                && record.intent_digest == intent.content_digest
        }))?;

        let expected_revision = intent.revision;
        let mut migrated = intent;
        migrated.profile_digest = current.profile_digest;
        migrated.revision = next_intent_revision(Some(expected_revision))?;
        migrated.content_digest = intent_digest(
            &migrated.receiver_id,
            &migrated.device_id,
            &migrated.receiver_profile_id,
            &migrated.receiver_profile_digest,
            &migrated.profile_id,
            &migrated.profile_digest,
            migrated.application_slot_count.get(),
            &migrated.lighting,
        )?;
        changes.push(StableIntentChange {
            expected_revision: Some(expected_revision),
            entry: PersistedStableEntry::Present(migrated),
        });
    }

    if changes.is_empty() {
        return Ok(0);
    }
    let migrated_count = changes.len();
    match store
        .compare_and_set_stable_entries(&changes)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::SaveIntent))?
    {
        PersistenceCasOutcome::Applied => Ok(migrated_count),
        PersistenceCasOutcome::Conflict => Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveIntent,
        )),
    }
}

fn validate_trigger(trigger: &RestoreTrigger) -> Result<(), RestorationError> {
    let scoped_correctly = match trigger.kind {
        hfx_domain::RestoreTriggerKind::DeviceReturn => trigger.target_device_id.is_some(),
        hfx_domain::RestoreTriggerKind::ServiceStart
        | hfx_domain::RestoreTriggerKind::ReceiverGeneration
        | hfx_domain::RestoreTriggerKind::SystemResume => trigger.target_device_id.is_none(),
    };
    if scoped_correctly {
        Ok(())
    } else {
        Err(RestorationError::InvalidTrigger)
    }
}

fn validate_trigger_history(
    trigger: &RestoreTrigger,
    records: &[RestoreRecord],
) -> Result<(), RestorationError> {
    let same_trigger = records
        .iter()
        .filter(|record| record.trigger_id == trigger.trigger_id)
        .collect::<Vec<_>>();
    let identity_matches = same_trigger.iter().all(|record| {
        record.trigger_kind == trigger.kind
            && record.receiver_id == trigger.receiver_id
            && record.generation_id == trigger.generation_id
    });
    let target_matches = trigger.target_device_id.as_ref().is_none_or(|target| {
        same_trigger
            .iter()
            .all(|record| &record.device_id == target)
    });
    if identity_matches && target_matches {
        Ok(())
    } else {
        Err(RestorationError::RecordIdentityConflict)
    }
}

pub(super) fn profile_matches<P: ProfileRegistry>(
    record: &RestoreRecord,
    intent: &PersistedStableIntent,
    resource: &ResourceKey,
    profiles: &P,
) -> bool {
    intent_profile_compatibility(
        &record.receiver_id,
        record.generation_id,
        intent,
        resource,
        profiles,
    ) == IntentProfileCompatibility::Compatible
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum IntentProfileCompatibility {
    Compatible,
    Incompatible,
    Unavailable,
}

fn intent_profile_compatibility<P: ProfileRegistry>(
    receiver_id: &ReceiverId,
    generation_id: GenerationId,
    intent: &PersistedStableIntent,
    resource: &ResourceKey,
    profiles: &P,
) -> IntentProfileCompatibility {
    let Some(receiver_profile) = profiles.receiver_profile(receiver_id, generation_id) else {
        return IntentProfileCompatibility::Unavailable;
    };
    if receiver_profile.profile_id != intent.receiver_profile_id
        || receiver_profile.profile_digest != intent.receiver_profile_digest
    {
        return IntentProfileCompatibility::Incompatible;
    }
    let Some(device_profile) = profiles.device_profile(resource) else {
        return IntentProfileCompatibility::Unavailable;
    };
    if !profiles.supports(resource)
        || device_profile.profile_id != intent.profile_id
        || device_profile.profile_digest != intent.profile_digest
        || device_profile.application_slot_count != intent.application_slot_count
    {
        return IntentProfileCompatibility::Incompatible;
    }
    IntentProfileCompatibility::Compatible
}
