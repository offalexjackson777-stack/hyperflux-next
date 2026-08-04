// SPDX-License-Identifier: GPL-2.0-only

//! Durable restore-record identity, validation, and persistence helpers.

use super::{
    MAX_RESTORE_RECORDS_PER_RECEIVER, PersistenceOperation, RestorationError,
    current_schema_version, next_persistence_revision, sha256_hex, transition_record,
    validate_schema,
};
use crate::{
    PersistedStableIntent, PersistenceCasOutcome, PersistenceStore, RestoreAttempt,
    RestoreCompletion, RestoreInvalidation, RestoreRecord, RestoreRecordStatus, RestoreTrigger,
    canonical_request_digest,
};
use hfx_domain::{
    DeviceApplicationState, GenerationId, IntentRevision, LeaseId, LogicalDeviceId,
    PersistenceRevision, ReceiverId, RequestId, ResourceKind, RestoreAttemptNumber, RestoreClaimId,
    RestoreInvalidationReason, SideEffectCertainty, TransactionClass, TransactionId,
    TransactionState,
};
use hfx_protocol::ResourceKey;
use serde::Serialize;
use std::collections::BTreeSet;

pub(super) fn active_attempt(status: &RestoreRecordStatus) -> Option<&RestoreAttempt> {
    match status {
        RestoreRecordStatus::Prepared(attempt)
        | RestoreRecordStatus::Queued(attempt)
        | RestoreRecordStatus::Applying(attempt) => Some(attempt),
        RestoreRecordStatus::Planned
        | RestoreRecordStatus::Deferred(_)
        | RestoreRecordStatus::Succeeded(_)
        | RestoreRecordStatus::Failed(_)
        | RestoreRecordStatus::Invalidated(_) => None,
    }
}

pub(super) fn load_record<S: PersistenceStore>(
    claim_id: &RestoreClaimId,
    store: &S,
) -> Result<RestoreRecord, RestorationError> {
    let record = store
        .restore_record(claim_id)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadRestore))?
        .ok_or(RestorationError::UnknownClaim)?;
    validate_record(&record)?;
    Ok(record)
}

pub(super) fn load_records<S: PersistenceStore>(
    receiver_id: &ReceiverId,
    store: &S,
) -> Result<Vec<RestoreRecord>, RestorationError> {
    let records = store
        .restore_records(receiver_id)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadRestore))?;
    if records.len() > MAX_RESTORE_RECORDS_PER_RECEIVER {
        return Err(RestorationError::RestoreRecordCapacity);
    }
    let mut claims = BTreeSet::new();
    for record in &records {
        validate_record(record)?;
        if &record.receiver_id != receiver_id {
            return Err(RestorationError::ReceiverMismatch);
        }
        if !claims.insert(record.claim_id.clone()) {
            return Err(RestorationError::RecordIdentityConflict);
        }
    }
    Ok(records)
}

fn validate_record(record: &RestoreRecord) -> Result<(), RestorationError> {
    validate_schema(record.schema_version)?;
    if claim_id_for_record(record)? != record.claim_id {
        return Err(RestorationError::RecordIdentityConflict);
    }
    match &record.status {
        RestoreRecordStatus::Prepared(attempt)
        | RestoreRecordStatus::Queued(attempt)
        | RestoreRecordStatus::Applying(attempt) => validate_attempt(record, attempt)?,
        RestoreRecordStatus::Succeeded(completion) => {
            validate_completion(record, completion, true)?;
        }
        RestoreRecordStatus::Failed(completion) => {
            validate_completion(record, completion, false)?;
        }
        RestoreRecordStatus::Deferred(deferred) => {
            if let Some(completion) = &deferred.prior_outcome {
                validate_completion(record, completion, false)?;
            }
        }
        RestoreRecordStatus::Planned | RestoreRecordStatus::Invalidated(_) => {}
    }
    Ok(())
}

fn validate_attempt(
    record: &RestoreRecord,
    attempt: &RestoreAttempt,
) -> Result<(), RestorationError> {
    let suffix = attempt_suffix(&record.claim_id, attempt.attempt_number);
    let expected_resource =
        lighting_resource(&record.receiver_id, record.generation_id, &record.device_id);
    let binding = attempt
        .request
        .device_profiles
        .first()
        .ok_or(RestorationError::RecordIdentityConflict)?;
    let frame = attempt
        .request
        .frames
        .first()
        .ok_or(RestorationError::RecordIdentityConflict)?;
    let identity_matches = Some(attempt.attempt_number) == record.last_attempt
        && attempt.request.request_id == typed_id::<RequestId>("restore-request", &suffix)?
        && attempt.request.transaction_id == typed_id::<TransactionId>("restore-tx", &suffix)?
        && attempt.request.lease_id == typed_id::<LeaseId>("restore-lease", &suffix)?
        && attempt.lease_request.request_id
            == typed_id::<RequestId>("restore-lease-request", &suffix)?
        && attempt.request_digest
            == canonical_request_digest(&attempt.request)
                .map_err(|_| RestorationError::RecordIdentityConflict)?
        && attempt.request.transaction_class == TransactionClass::Restore
        && attempt.request.receiver_id == record.receiver_id
        && attempt.request.generation_id == record.generation_id
        && attempt.request.client_id == attempt.lease_request.client_id
        && attempt.request.resources == [expected_resource.clone()]
        && attempt.lease_request.resources == [expected_resource]
        && attempt.request.device_profiles.len() == 1
        && binding.device_id == record.device_id
        && attempt.request.frames.len() == 1
        && frame.device_id == record.device_id
        && frame.frame_index.get() == 0
        && frame.colors.len() == usize::from(binding.application_slot_count.get());
    if identity_matches {
        Ok(())
    } else {
        Err(RestorationError::RecordIdentityConflict)
    }
}

fn validate_completion(
    record: &RestoreRecord,
    completion: &RestoreCompletion,
    successful: bool,
) -> Result<(), RestorationError> {
    let Some(last_attempt) = record.last_attempt else {
        return Err(RestorationError::RecordIdentityConflict);
    };
    let suffix = attempt_suffix(&record.claim_id, completion.attempt_number);
    let safe_retry = completion.automatic_retry
        && !completion.live_write_executed
        && completion.delivered_frames.get() == 0
        && completion.side_effect_certainty == SideEffectCertainty::None;
    let success_facts = completion.state == TransactionState::Succeeded
        && completion.delivered_frames.get() == 1
        && completion.side_effect_certainty == SideEffectCertainty::Committed
        && completion.live_write_executed
        && !completion.automatic_retry
        && completion.device_application != DeviceApplicationState::Rejected
        && completion.error_kind.is_none();
    let failure_facts = matches!(
        completion.state,
        TransactionState::Failed | TransactionState::Revoked
    ) && completion.delivered_frames.get() <= 1
        && (!completion.automatic_retry || safe_retry)
        && (completion.delivered_frames.get() == 0 || completion.live_write_executed)
        && (completion.side_effect_certainty == SideEffectCertainty::None
            || completion.live_write_executed)
        && (completion.error_kind.is_some() || safe_retry);
    if completion.attempt_number == last_attempt
        && completion.transaction_id == typed_id::<TransactionId>("restore-tx", &suffix)?
        && if successful {
            success_facts
        } else {
            failure_facts
        }
    {
        Ok(())
    } else {
        Err(RestorationError::RecordIdentityConflict)
    }
}

pub(super) fn persist_record<S: PersistenceStore>(
    expected_revision: Option<PersistenceRevision>,
    record: &RestoreRecord,
    store: &mut S,
) -> Result<(), RestorationError> {
    match store
        .compare_and_set_restore_record(expected_revision, record)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::SaveRestore))?
    {
        PersistenceCasOutcome::Applied => Ok(()),
        PersistenceCasOutcome::Conflict => Err(RestorationError::PersistenceConflict(
            PersistenceOperation::SaveRestore,
        )),
    }
}

pub(super) fn supersede_prior_claims<S: PersistenceStore>(
    candidate: &RestoreRecord,
    records: &mut [RestoreRecord],
    store: &mut S,
) -> Result<(), RestorationError> {
    for record in records.iter_mut().filter(|record| {
        record.device_id == candidate.device_id
            && record.claim_id != candidate.claim_id
            && matches!(
                record.status,
                RestoreRecordStatus::Planned | RestoreRecordStatus::Deferred(_)
            )
    }) {
        let reason = if record.generation_id == candidate.generation_id {
            RestoreInvalidationReason::SupersededTrigger
        } else {
            RestoreInvalidationReason::StaleGeneration
        };
        let updated = transition_record(
            record.clone(),
            RestoreRecordStatus::Invalidated(RestoreInvalidation { reason }),
        )?;
        persist_record(Some(record.revision), &updated, store)?;
        *record = updated;
    }
    Ok(())
}

pub(super) fn enforce_prior_outcome_barrier<S: PersistenceStore>(
    candidate: &RestoreRecord,
    store: &S,
) -> Result<(), RestorationError> {
    let records = load_records(&candidate.receiver_id, store)?;
    enforce_prior_outcomes(records.iter().filter(|prior| {
        prior.claim_id != candidate.claim_id
            && prior.device_id == candidate.device_id
            && prior.intent_revision == candidate.intent_revision
            && prior.intent_digest == candidate.intent_digest
    }))
}

pub(super) fn enforce_prior_outcomes<'a>(
    records: impl IntoIterator<Item = &'a RestoreRecord>,
) -> Result<(), RestorationError> {
    for prior in records {
        if active_attempt(&prior.status).is_some() {
            return Err(RestorationError::PriorClaimUnresolved);
        }
        if let RestoreRecordStatus::Failed(completion) = &prior.status
            && (completion.live_write_executed
                || completion.delivered_frames.get() > 0
                || completion.side_effect_certainty != SideEffectCertainty::None)
        {
            return Err(RestorationError::PriorOutcomeUncertain);
        }
    }
    Ok(())
}

#[derive(Serialize)]
struct ClaimIdentity<'a> {
    schema_version: u16,
    trigger_id: &'a hfx_domain::RestoreTriggerId,
    trigger_kind: hfx_domain::RestoreTriggerKind,
    receiver_id: &'a ReceiverId,
    generation_id: GenerationId,
    device_id: &'a LogicalDeviceId,
    intent_revision: IntentRevision,
    intent_digest: &'a hfx_domain::IntentDigest,
}

pub(super) fn new_restore_record(
    trigger: &RestoreTrigger,
    intent: &PersistedStableIntent,
) -> Result<RestoreRecord, RestorationError> {
    let identity = ClaimIdentity {
        schema_version: super::CURRENT_PERSISTENCE_SCHEMA_VERSION,
        trigger_id: &trigger.trigger_id,
        trigger_kind: trigger.kind,
        receiver_id: &trigger.receiver_id,
        generation_id: trigger.generation_id,
        device_id: &intent.device_id,
        intent_revision: intent.revision,
        intent_digest: &intent.content_digest,
    };
    let claim_id = claim_id_from_identity(&identity)?;
    Ok(RestoreRecord {
        schema_version: current_schema_version()?,
        claim_id,
        trigger_id: trigger.trigger_id.clone(),
        trigger_kind: trigger.kind,
        receiver_id: trigger.receiver_id.clone(),
        generation_id: trigger.generation_id,
        device_id: intent.device_id.clone(),
        intent_revision: intent.revision,
        intent_digest: intent.content_digest.clone(),
        revision: next_persistence_revision(None)?,
        last_attempt: None,
        status: RestoreRecordStatus::Planned,
    })
}

fn claim_id_for_record(record: &RestoreRecord) -> Result<RestoreClaimId, RestorationError> {
    claim_id_from_identity(&ClaimIdentity {
        schema_version: super::CURRENT_PERSISTENCE_SCHEMA_VERSION,
        trigger_id: &record.trigger_id,
        trigger_kind: record.trigger_kind,
        receiver_id: &record.receiver_id,
        generation_id: record.generation_id,
        device_id: &record.device_id,
        intent_revision: record.intent_revision,
        intent_digest: &record.intent_digest,
    })
}

fn claim_id_from_identity(
    identity: &ClaimIdentity<'_>,
) -> Result<RestoreClaimId, RestorationError> {
    RestoreClaimId::try_from(format!("restore-{}", sha256_hex(identity)?))
        .map_err(|_| RestorationError::Identifier)
}

pub(super) fn validate_same_claim(
    existing: &RestoreRecord,
    candidate: &RestoreRecord,
) -> Result<(), RestorationError> {
    if existing.claim_id == candidate.claim_id
        && existing.trigger_id == candidate.trigger_id
        && existing.trigger_kind == candidate.trigger_kind
        && existing.receiver_id == candidate.receiver_id
        && existing.generation_id == candidate.generation_id
        && existing.device_id == candidate.device_id
        && existing.intent_revision == candidate.intent_revision
        && existing.intent_digest == candidate.intent_digest
    {
        Ok(())
    } else {
        Err(RestorationError::RecordIdentityConflict)
    }
}

pub(super) fn lighting_resource(
    receiver_id: &ReceiverId,
    generation_id: GenerationId,
    device_id: &LogicalDeviceId,
) -> ResourceKey {
    ResourceKey {
        receiver_id: receiver_id.clone(),
        generation_id,
        device_id: device_id.clone(),
        kind: ResourceKind::Lighting,
    }
}

pub(super) fn next_attempt_number(
    current: Option<RestoreAttemptNumber>,
) -> Result<RestoreAttemptNumber, RestorationError> {
    let value = current.map_or(Ok(1), |attempt| {
        attempt
            .get()
            .checked_add(1)
            .ok_or(RestorationError::RevisionOverflow)
    })?;
    RestoreAttemptNumber::try_from(value).map_err(|_| RestorationError::RevisionOverflow)
}

pub(super) fn attempt_suffix(claim_id: &RestoreClaimId, attempt: RestoreAttemptNumber) -> String {
    format!("{}-{}", claim_id.as_str(), attempt.get())
}

pub(super) fn typed_id<T>(prefix: &str, suffix: &str) -> Result<T, RestorationError>
where
    T: TryFrom<String>,
{
    T::try_from(format!("{prefix}-{suffix}")).map_err(|_| RestorationError::Identifier)
}
