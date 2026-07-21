// SPDX-License-Identifier: GPL-2.0-only

//! Strict in-memory persistence used by deterministic restoration simulation.

use crate::transport::SimCrashSignal;
use hfx_core::{
    MAX_RESTORE_RECORDS_PER_RECEIVER, MAX_STABLE_ENTRIES_PER_RECEIVER, PersistedRestorePolicy,
    PersistedStableEntry, PersistenceCasOutcome, PersistenceStore, RestoreRecord,
    RestoreRecordChange, StableIntentChange,
};
use hfx_domain::{
    LogicalDeviceId, PersistenceRevision, ReceiverId, RestoreClaimId, RestoreRecordState,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::panic::panic_any;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SimPersistenceError {
    Capacity,
    DuplicateChange,
    IdentityConflict,
}

impl fmt::Display for SimPersistenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Capacity => "simulated persistence capacity is exhausted",
            Self::DuplicateChange => "simulated persistence batch contains a duplicate key",
            Self::IdentityConflict => {
                "simulated persistence key conflicts with the record identity"
            }
        })
    }
}

impl std::error::Error for SimPersistenceError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RestoreCasCrashPhase {
    Before,
    After,
}

/// Durable, compare-and-set persistence whose contents survive harness restarts.
#[derive(Clone, Debug)]
pub struct SimPersistenceStore {
    max_stable_entries: usize,
    max_restore_records: usize,
    policies: BTreeMap<ReceiverId, PersistedRestorePolicy>,
    stable_entries: BTreeMap<(ReceiverId, LogicalDeviceId), PersistedStableEntry>,
    restore_records: BTreeMap<RestoreClaimId, RestoreRecord>,
    restore_crash: Option<(RestoreCasCrashPhase, RestoreRecordState)>,
}

impl Default for SimPersistenceStore {
    fn default() -> Self {
        Self::new(
            MAX_STABLE_ENTRIES_PER_RECEIVER,
            MAX_RESTORE_RECORDS_PER_RECEIVER,
        )
    }
}

impl SimPersistenceStore {
    #[must_use]
    pub const fn new(max_stable_entries: usize, max_restore_records: usize) -> Self {
        Self {
            max_stable_entries,
            max_restore_records,
            policies: BTreeMap::new(),
            stable_entries: BTreeMap::new(),
            restore_records: BTreeMap::new(),
            restore_crash: None,
        }
    }

    #[must_use]
    pub fn policy(&self, receiver_id: &ReceiverId) -> Option<&PersistedRestorePolicy> {
        self.policies.get(receiver_id)
    }

    #[must_use]
    pub fn stable_entry(
        &self,
        receiver_id: &ReceiverId,
        device_id: &LogicalDeviceId,
    ) -> Option<&PersistedStableEntry> {
        self.stable_entries
            .get(&(receiver_id.clone(), device_id.clone()))
    }

    #[must_use]
    pub fn record(&self, claim_id: &RestoreClaimId) -> Option<&RestoreRecord> {
        self.restore_records.get(claim_id)
    }

    #[must_use]
    pub fn record_count(&self) -> usize {
        self.restore_records.len()
    }

    #[must_use]
    pub fn stable_entry_count(&self) -> usize {
        self.stable_entries.len()
    }

    pub(crate) fn arm_before_restore_record_cas(&mut self, state: RestoreRecordState) {
        self.restore_crash = Some((RestoreCasCrashPhase::Before, state));
    }

    pub(crate) fn arm_after_restore_record_cas(&mut self, state: RestoreRecordState) {
        self.restore_crash = Some((RestoreCasCrashPhase::After, state));
    }

    fn crash_if_armed(&mut self, phase: RestoreCasCrashPhase, state: RestoreRecordState) {
        if self.restore_crash == Some((phase, state)) {
            self.restore_crash = None;
            let signal = match phase {
                RestoreCasCrashPhase::Before => SimCrashSignal::BeforeRestoreRecordCas(state),
                RestoreCasCrashPhase::After => SimCrashSignal::AfterRestoreRecordCas(state),
            };
            panic_any(signal);
        }
    }
}

impl PersistenceStore for SimPersistenceStore {
    type Error = SimPersistenceError;

    fn restore_policy(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Option<PersistedRestorePolicy>, Self::Error> {
        Ok(self.policies.get(receiver_id).cloned())
    }

    fn compare_and_set_restore_policy(
        &mut self,
        expected_revision: Option<PersistenceRevision>,
        policy: &PersistedRestorePolicy,
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        let actual = self
            .policies
            .get(&policy.receiver_id)
            .map(|record| record.revision);
        if actual != expected_revision {
            return Ok(PersistenceCasOutcome::Conflict);
        }
        self.policies
            .insert(policy.receiver_id.clone(), policy.clone());
        Ok(PersistenceCasOutcome::Applied)
    }

    fn stable_entries(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<PersistedStableEntry>, Self::Error> {
        Ok(self
            .stable_entries
            .iter()
            .filter(|((stored_receiver_id, _), _)| stored_receiver_id == receiver_id)
            .map(|(_, entry)| entry.clone())
            .collect())
    }

    fn compare_and_set_stable_entries(
        &mut self,
        changes: &[StableIntentChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        let keys = changes
            .iter()
            .map(|change| {
                (
                    change.entry.receiver_id().clone(),
                    change.entry.device_id().clone(),
                )
            })
            .collect::<BTreeSet<_>>();
        if keys.len() != changes.len() {
            return Err(SimPersistenceError::DuplicateChange);
        }
        if self.stable_entries.len().saturating_add(
            keys.iter()
                .filter(|key| !self.stable_entries.contains_key(*key))
                .count(),
        ) > self.max_stable_entries
        {
            return Err(SimPersistenceError::Capacity);
        }
        let revisions_match = changes.iter().all(|change| {
            let key = (
                change.entry.receiver_id().clone(),
                change.entry.device_id().clone(),
            );
            self.stable_entries
                .get(&key)
                .map(PersistedStableEntry::revision)
                == change.expected_revision
        });
        if !revisions_match {
            return Ok(PersistenceCasOutcome::Conflict);
        }
        for change in changes {
            let key = (
                change.entry.receiver_id().clone(),
                change.entry.device_id().clone(),
            );
            self.stable_entries.insert(key, change.entry.clone());
        }
        Ok(PersistenceCasOutcome::Applied)
    }

    fn restore_records(&self, receiver_id: &ReceiverId) -> Result<Vec<RestoreRecord>, Self::Error> {
        Ok(self
            .restore_records
            .values()
            .filter(|record| &record.receiver_id == receiver_id)
            .cloned()
            .collect())
    }

    fn restore_record(
        &self,
        claim_id: &RestoreClaimId,
    ) -> Result<Option<RestoreRecord>, Self::Error> {
        Ok(self.restore_records.get(claim_id).cloned())
    }

    fn compare_and_set_restore_records(
        &mut self,
        changes: &[RestoreRecordChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        if changes.is_empty() {
            return Ok(PersistenceCasOutcome::Applied);
        }
        let claims = changes
            .iter()
            .map(|change| change.record.claim_id.clone())
            .collect::<BTreeSet<_>>();
        if claims.len() != changes.len() {
            return Err(SimPersistenceError::DuplicateChange);
        }
        for change in changes {
            if self
                .restore_records
                .get(&change.record.claim_id)
                .is_some_and(|current| {
                    current.receiver_id != change.record.receiver_id
                        || current.device_id != change.record.device_id
                        || current.trigger_id != change.record.trigger_id
                })
            {
                return Err(SimPersistenceError::IdentityConflict);
            }
            let actual = self
                .restore_records
                .get(&change.record.claim_id)
                .map(|current| current.revision);
            if actual != change.expected_revision {
                return Ok(PersistenceCasOutcome::Conflict);
            }
        }
        let additions = changes
            .iter()
            .filter(|change| !self.restore_records.contains_key(&change.record.claim_id))
            .count();
        if self.restore_records.len().saturating_add(additions) > self.max_restore_records {
            return Err(SimPersistenceError::Capacity);
        }
        for change in changes {
            self.crash_if_armed(RestoreCasCrashPhase::Before, change.record.status.state());
        }
        for change in changes {
            self.restore_records
                .insert(change.record.claim_id.clone(), change.record.clone());
        }
        for change in changes {
            self.crash_if_armed(RestoreCasCrashPhase::After, change.record.status.state());
        }
        Ok(PersistenceCasOutcome::Applied)
    }
}
