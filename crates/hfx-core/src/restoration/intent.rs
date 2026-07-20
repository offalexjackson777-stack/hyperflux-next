// SPDX-License-Identifier: GPL-2.0-only

use super::{
    MAX_STABLE_ENTRIES_PER_RECEIVER, PersistenceOperation, RestorationCoordinator,
    RestorationError, StableIntentCapture, current_schema_version, next_intent_revision,
    next_persistence_revision, sha256_hex, validate_schema,
};
use crate::{
    PersistedRestorePolicy, PersistedStableEntry, PersistedStableIntent, PersistenceCasOutcome,
    PersistenceStore, StableIntentChange, StableIntentTombstone, StableLighting,
    canonical_request_digest,
};
use hfx_domain::{
    DeviceApplicationState, IntentDigest, LogicalDeviceId, ProfileDigest, ProfileId, ReceiverId,
    SideEffectCertainty, TransactionClass, TransactionState, WallClockUnixMs,
};
use hfx_protocol::{RgbColor, TransactionRequest, TransactionTerminal};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

impl RestorationCoordinator {
    /// Atomically enables or disables stable-lighting restoration.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid stored data, revision conflicts, or storage failures.
    pub fn set_restore_enabled<S: PersistenceStore>(
        &self,
        receiver_id: &ReceiverId,
        enabled: bool,
        store: &mut S,
    ) -> Result<PersistedRestorePolicy, RestorationError> {
        let current = store
            .restore_policy(receiver_id)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadPolicy))?;
        if let Some(policy) = &current {
            validate_schema(policy.schema_version)?;
            if &policy.receiver_id != receiver_id {
                return Err(RestorationError::ReceiverMismatch);
            }
        }
        let expected = current.as_ref().map(|policy| policy.revision);
        let policy = PersistedRestorePolicy {
            schema_version: current_schema_version()?,
            receiver_id: receiver_id.clone(),
            enabled,
            revision: next_persistence_revision(expected)?,
        };
        match store
            .compare_and_set_restore_policy(expected, &policy)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::SavePolicy))?
        {
            PersistenceCasOutcome::Applied => Ok(policy),
            PersistenceCasOutcome::Conflict => Err(RestorationError::PersistenceConflict(
                PersistenceOperation::SavePolicy,
            )),
        }
    }

    /// Atomically captures stable `Static` or `Off` intent after one definitive
    /// successful transaction.
    ///
    /// # Errors
    ///
    /// Returns an error before storage for effect frames, incomplete transport
    /// outcomes, mismatched captures, invalid persisted records, or CAS failure.
    pub fn commit_stable_transaction<S: PersistenceStore>(
        &self,
        request: &TransactionRequest,
        terminal: &TransactionTerminal,
        captures: &[StableIntentCapture],
        captured_at: WallClockUnixMs,
        store: &mut S,
    ) -> Result<Vec<PersistedStableIntent>, RestorationError> {
        validate_stable_terminal(request, terminal)?;
        let captures = capture_map(request, captures)?;
        let entries = load_entries(&request.receiver_id, store)?;
        let current = entries
            .iter()
            .map(|entry| (entry.device_id().clone(), entry))
            .collect::<BTreeMap<_, _>>();
        let mut intents = Vec::with_capacity(request.frames.len());
        let mut changes = Vec::with_capacity(request.frames.len());
        for frame in &request.frames {
            let lighting = captures
                .get(&frame.device_id)
                .ok_or(RestorationError::CaptureMismatch)?;
            validate_capture(lighting, &frame.colors)?;
            let binding = request
                .device_profiles
                .iter()
                .find(|binding| binding.device_id == frame.device_id)
                .ok_or(RestorationError::CaptureMismatch)?;
            let expected_revision = current.get(&frame.device_id).map(|entry| entry.revision());
            let revision = next_intent_revision(expected_revision)?;
            let content_digest = intent_digest(
                &request.receiver_id,
                &frame.device_id,
                &request.receiver_profile_id,
                &request.receiver_profile_digest,
                &binding.profile_id,
                &binding.profile_digest,
                binding.application_slot_count.get(),
                lighting,
            )?;
            let intent = PersistedStableIntent {
                schema_version: current_schema_version()?,
                receiver_id: request.receiver_id.clone(),
                device_id: frame.device_id.clone(),
                receiver_profile_id: request.receiver_profile_id.clone(),
                receiver_profile_digest: request.receiver_profile_digest.clone(),
                profile_id: binding.profile_id.clone(),
                profile_digest: binding.profile_digest.clone(),
                application_slot_count: binding.application_slot_count,
                revision,
                content_digest,
                source_transaction_id: request.transaction_id.clone(),
                source_request_digest: terminal.request_digest.clone(),
                lighting: (*lighting).clone(),
                captured_at,
            };
            changes.push(StableIntentChange {
                expected_revision,
                entry: PersistedStableEntry::Present(intent.clone()),
            });
            intents.push(intent);
        }
        changes.sort_unstable_by(|left, right| left.entry.device_id().cmp(right.entry.device_id()));
        match store
            .compare_and_set_stable_entries(&changes)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::SaveIntent))?
        {
            PersistenceCasOutcome::Applied => Ok(intents),
            PersistenceCasOutcome::Conflict => Err(RestorationError::PersistenceConflict(
                PersistenceOperation::SaveIntent,
            )),
        }
    }

    /// Atomically replaces selected stable intents with versioned tombstones.
    ///
    /// # Errors
    ///
    /// Returns an error for duplicate targets, invalid stored records, revision
    /// overflow, storage failure, or compare-and-set conflict.
    pub fn clear_stable_intents<S: PersistenceStore>(
        &self,
        receiver_id: &ReceiverId,
        device_ids: &[LogicalDeviceId],
        deleted_at: WallClockUnixMs,
        store: &mut S,
    ) -> Result<Vec<StableIntentTombstone>, RestorationError> {
        let unique = device_ids.iter().collect::<BTreeSet<_>>();
        if unique.len() != device_ids.len() {
            return Err(RestorationError::DuplicateDevice);
        }
        let entries = load_entries(receiver_id, store)?;
        let current = entries
            .iter()
            .map(|entry| (entry.device_id().clone(), entry))
            .collect::<BTreeMap<_, _>>();
        let mut tombstones = Vec::with_capacity(device_ids.len());
        let mut changes = Vec::with_capacity(device_ids.len());
        for device_id in unique {
            let prior = current.get(device_id);
            let expected_revision = prior.map(|entry| entry.revision());
            let tombstone = StableIntentTombstone {
                schema_version: current_schema_version()?,
                receiver_id: receiver_id.clone(),
                device_id: device_id.clone(),
                revision: next_intent_revision(expected_revision)?,
                previous_content_digest: prior.and_then(|entry| match entry {
                    PersistedStableEntry::Present(intent) => Some(intent.content_digest.clone()),
                    PersistedStableEntry::Deleted(tombstone) => {
                        tombstone.previous_content_digest.clone()
                    }
                }),
                deleted_at,
            };
            changes.push(StableIntentChange {
                expected_revision,
                entry: PersistedStableEntry::Deleted(tombstone.clone()),
            });
            tombstones.push(tombstone);
        }
        match store
            .compare_and_set_stable_entries(&changes)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::SaveIntent))?
        {
            PersistenceCasOutcome::Applied => Ok(tombstones),
            PersistenceCasOutcome::Conflict => Err(RestorationError::PersistenceConflict(
                PersistenceOperation::SaveIntent,
            )),
        }
    }
}

pub(super) fn load_entries<S: PersistenceStore>(
    receiver_id: &ReceiverId,
    store: &S,
) -> Result<Vec<PersistedStableEntry>, RestorationError> {
    let mut entries = store
        .stable_entries(receiver_id)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadIntent))?;
    if entries.len() > MAX_STABLE_ENTRIES_PER_RECEIVER {
        return Err(RestorationError::StableEntryCapacity);
    }
    entries.sort_unstable_by(|left, right| left.device_id().cmp(right.device_id()));
    let mut devices = BTreeSet::new();
    for entry in &entries {
        validate_schema(match entry {
            PersistedStableEntry::Present(intent) => intent.schema_version,
            PersistedStableEntry::Deleted(tombstone) => tombstone.schema_version,
        })?;
        if entry.receiver_id() != receiver_id {
            return Err(RestorationError::ReceiverMismatch);
        }
        if !devices.insert(entry.device_id().clone()) {
            return Err(RestorationError::DuplicateDevice);
        }
        if let PersistedStableEntry::Present(intent) = entry {
            validate_intent_digest(intent)?;
        }
    }
    Ok(entries)
}

fn validate_stable_terminal(
    request: &TransactionRequest,
    terminal: &TransactionTerminal,
) -> Result<(), RestorationError> {
    let digest = canonical_request_digest(request)
        .map_err(|_| RestorationError::InvalidStableTransaction)?;
    let frame_count = u16::try_from(request.frames.len())
        .map_err(|_| RestorationError::InvalidStableTransaction)?;
    let definitive = request.transaction_class == TransactionClass::StaticLighting
        && terminal.request_id == request.request_id
        && terminal.request_digest == digest
        && terminal.transaction_id == request.transaction_id
        && terminal.receiver_id == request.receiver_id
        && terminal.generation_id == request.generation_id
        && terminal.state == TransactionState::Succeeded
        && terminal.declared_frames.get() == frame_count
        && terminal.delivered_frames.get() == frame_count
        && terminal.side_effect_certainty == SideEffectCertainty::Committed
        && terminal.live_write_executed
        && !terminal.automatic_retry
        && terminal.device_application != DeviceApplicationState::Rejected
        && terminal.error_kind.is_none();
    if definitive {
        Ok(())
    } else {
        Err(RestorationError::InvalidStableTransaction)
    }
}

fn capture_map<'a>(
    request: &TransactionRequest,
    captures: &'a [StableIntentCapture],
) -> Result<BTreeMap<LogicalDeviceId, &'a StableLighting>, RestorationError> {
    if captures.len() != request.frames.len() {
        return Err(RestorationError::CaptureMismatch);
    }
    let mut result = BTreeMap::new();
    for capture in captures {
        if result
            .insert(capture.device_id.clone(), &capture.lighting)
            .is_some()
        {
            return Err(RestorationError::DuplicateDevice);
        }
    }
    if request
        .frames
        .iter()
        .any(|frame| !result.contains_key(&frame.device_id))
    {
        return Err(RestorationError::CaptureMismatch);
    }
    Ok(result)
}

fn validate_capture(lighting: &StableLighting, frame: &[RgbColor]) -> Result<(), RestorationError> {
    match lighting {
        StableLighting::Off
            if frame.iter().all(|color| {
                color.red.get() == 0 && color.green.get() == 0 && color.blue.get() == 0
            }) =>
        {
            Ok(())
        }
        StableLighting::Static(colors) if colors == frame => Ok(()),
        StableLighting::Off | StableLighting::Static(_) => Err(RestorationError::CaptureMismatch),
    }
}

#[derive(Serialize)]
struct IntentDigestInput<'a> {
    schema_version: u16,
    receiver_id: &'a ReceiverId,
    device_id: &'a LogicalDeviceId,
    receiver_profile_id: &'a ProfileId,
    receiver_profile_digest: &'a ProfileDigest,
    profile_id: &'a ProfileId,
    profile_digest: &'a ProfileDigest,
    application_slot_count: u16,
    lighting: &'a StableLighting,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn intent_digest(
    receiver_id: &ReceiverId,
    device_id: &LogicalDeviceId,
    receiver_profile_id: &ProfileId,
    receiver_profile_digest: &ProfileDigest,
    profile_id: &ProfileId,
    profile_digest: &ProfileDigest,
    application_slot_count: u16,
    lighting: &StableLighting,
) -> Result<IntentDigest, RestorationError> {
    let input = IntentDigestInput {
        schema_version: super::CURRENT_PERSISTENCE_SCHEMA_VERSION,
        receiver_id,
        device_id,
        receiver_profile_id,
        receiver_profile_digest,
        profile_id,
        profile_digest,
        application_slot_count,
        lighting,
    };
    IntentDigest::try_from(sha256_hex(&input)?).map_err(|_| RestorationError::Identifier)
}

fn validate_intent_digest(intent: &PersistedStableIntent) -> Result<(), RestorationError> {
    if intent.application_slot_count.get() == 0 {
        return Err(RestorationError::IntentDigestMismatch);
    }
    let expected = intent_digest(
        &intent.receiver_id,
        &intent.device_id,
        &intent.receiver_profile_id,
        &intent.receiver_profile_digest,
        &intent.profile_id,
        &intent.profile_digest,
        intent.application_slot_count.get(),
        &intent.lighting,
    )?;
    if expected == intent.content_digest && validate_lighting_dimensions(intent) {
        Ok(())
    } else {
        Err(RestorationError::IntentDigestMismatch)
    }
}

fn validate_lighting_dimensions(intent: &PersistedStableIntent) -> bool {
    match &intent.lighting {
        StableLighting::Off => true,
        StableLighting::Static(colors) => {
            colors.len() == usize::from(intent.application_slot_count.get())
        }
    }
}
