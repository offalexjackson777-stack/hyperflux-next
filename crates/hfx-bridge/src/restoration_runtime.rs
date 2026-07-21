// SPDX-License-Identifier: GPL-2.0-only

//! Durable restoration composition for the production bridge.

use crate::{
    DisabledRestorationSource, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationSnapshotSource,
};
use hfx_core::{
    BoundedEventLog, CURRENT_PERSISTENCE_SCHEMA_VERSION, CompletedTransaction, EventSink,
    LeaseManager, MAX_RESTORE_RECORDS_PER_RECEIVER, PersistenceStore, ReceiverTransport,
    RestorationCoordinator, RestorationError, RestoreGenerationRetirement, RestoreRecordStatus,
    StableCommitOutcome, TransactionCoordinator,
};
use hfx_domain::{GenerationId, MonotonicMs, ReceiverId, RestoreState, WallClockUnixMs};

/// Owns durable stable-lighting capture, projection, and generation retirement.
pub trait RestorationRuntime: RestorationSnapshotSource {
    /// Observes one immutable completion after transport and outcome journaling.
    ///
    /// # Errors
    ///
    /// Returns a persistence error without changing the hardware terminal or
    /// authorizing an automatic transport retry.
    fn capture_completed(
        &mut self,
        completed: &CompletedTransaction,
        captured_at: WallClockUnixMs,
    ) -> Result<StableCommitOutcome, RestorationError>;

    /// Reconciles or invalidates every nonterminal claim for one retired generation.
    ///
    /// # Errors
    ///
    /// Returns a typed restoration failure before the caller commits its staged
    /// volatile generation transition.
    #[allow(clippy::too_many_arguments)]
    fn retire_generation<T, E>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        now: MonotonicMs,
        transport: &T,
        leases: &mut LeaseManager,
        transactions: &TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink;
}

impl RestorationRuntime for DisabledRestorationSource {
    fn capture_completed(
        &mut self,
        _completed: &CompletedTransaction,
        _captured_at: WallClockUnixMs,
    ) -> Result<StableCommitOutcome, RestorationError> {
        Ok(StableCommitOutcome::NotApplicable)
    }

    fn retire_generation<T, E>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        _now: MonotonicMs,
        _transport: &T,
        _leases: &mut LeaseManager,
        _transactions: &TransactionCoordinator,
        _events: &mut BoundedEventLog,
        _sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink,
    {
        Ok(RestoreGenerationRetirement {
            receiver_id: receiver_id.clone(),
            generation_id,
            updated: Vec::new(),
            already_terminal: 0,
        })
    }
}

/// One-owner durable restoration runtime backed by a persistence store.
#[derive(Debug)]
pub struct DurableRestorationRuntime<S> {
    store: S,
}

impl<S> DurableRestorationRuntime<S> {
    #[must_use]
    pub const fn new(store: S) -> Self {
        Self { store }
    }

    #[must_use]
    pub const fn store(&self) -> &S {
        &self.store
    }

    pub const fn store_mut(&mut self) -> &mut S {
        &mut self.store
    }

    #[must_use]
    pub fn into_store(self) -> S {
        self.store
    }
}

impl<S: PersistenceStore> RestorationSnapshotSource for DurableRestorationRuntime<S> {
    fn restoration(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        let policy = self
            .store
            .restore_policy(receiver_id)
            .map_err(|_| RestorationProjectionError::Unavailable)?;
        let Some(policy) = policy else {
            return Ok(disabled_snapshot());
        };
        if policy.receiver_id != *receiver_id
            || policy.schema_version.get() != CURRENT_PERSISTENCE_SCHEMA_VERSION
        {
            return Err(RestorationProjectionError::Unavailable);
        }
        if !policy.enabled {
            return Ok(disabled_snapshot());
        }

        let records = self
            .store
            .restore_records(receiver_id)
            .map_err(|_| RestorationProjectionError::Unavailable)?;
        if records.len() > MAX_RESTORE_RECORDS_PER_RECEIVER
            || records.iter().any(|record| {
                record.receiver_id != *receiver_id
                    || record.schema_version.get() != CURRENT_PERSISTENCE_SCHEMA_VERSION
            })
        {
            return Err(RestorationProjectionError::Unavailable);
        }
        Ok(ReceiverRestorationSnapshot {
            stable_restore_enabled: true,
            restore_state: aggregate_restore_state(
                records
                    .iter()
                    .filter(|record| record.generation_id == generation_id)
                    .map(|record| &record.status),
            ),
        })
    }
}

impl<S: PersistenceStore> RestorationRuntime for DurableRestorationRuntime<S> {
    fn capture_completed(
        &mut self,
        completed: &CompletedTransaction,
        captured_at: WallClockUnixMs,
    ) -> Result<StableCommitOutcome, RestorationError> {
        RestorationCoordinator.commit_declared_stable_transaction(
            &completed.request,
            &completed.terminal,
            captured_at,
            &mut self.store,
        )
    }

    fn retire_generation<T, E>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        now: MonotonicMs,
        transport: &T,
        leases: &mut LeaseManager,
        transactions: &TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink,
    {
        RestorationCoordinator.retire_generation(
            receiver_id,
            generation_id,
            now,
            transport,
            &mut self.store,
            leases,
            transactions,
            events,
            sink,
        )
    }
}

const fn disabled_snapshot() -> ReceiverRestorationSnapshot {
    ReceiverRestorationSnapshot {
        stable_restore_enabled: false,
        restore_state: RestoreState::Idle,
    }
}

fn aggregate_restore_state<'a>(
    statuses: impl Iterator<Item = &'a RestoreRecordStatus>,
) -> RestoreState {
    let mut saw_any = false;
    let mut saw_succeeded = false;
    let mut saw_invalidated = false;
    let mut saw_failed = false;
    let mut saw_planned = false;
    let mut saw_prepared = false;
    let mut saw_queued = false;
    let mut saw_applying = false;
    for status in statuses {
        saw_any = true;
        match status {
            RestoreRecordStatus::Planned | RestoreRecordStatus::Deferred(_) => {
                saw_planned = true;
            }
            RestoreRecordStatus::Prepared(_) => saw_prepared = true,
            RestoreRecordStatus::Queued(_) => saw_queued = true,
            RestoreRecordStatus::Applying(_) => saw_applying = true,
            RestoreRecordStatus::Succeeded(_) => saw_succeeded = true,
            RestoreRecordStatus::Failed(_) => saw_failed = true,
            RestoreRecordStatus::Invalidated(_) => saw_invalidated = true,
        }
    }
    if saw_applying {
        RestoreState::Applying
    } else if saw_queued {
        RestoreState::Queued
    } else if saw_prepared {
        RestoreState::GenerationBound
    } else if saw_planned {
        RestoreState::Planned
    } else if saw_failed {
        RestoreState::Failed
    } else if saw_invalidated {
        RestoreState::Invalidated
    } else if saw_succeeded {
        RestoreState::Succeeded
    } else if saw_any {
        RestoreState::Invalidated
    } else {
        RestoreState::Idle
    }
}

#[cfg(test)]
mod tests {
    use super::aggregate_restore_state;
    use hfx_core::{RestoreInvalidation, RestoreRecordStatus};
    use hfx_domain::{RestoreInvalidationReason, RestoreState};

    #[test]
    fn aggregate_prioritizes_active_work_then_conservative_terminal_truth() {
        assert_eq!(
            aggregate_restore_state([&RestoreRecordStatus::Planned].into_iter()),
            RestoreState::Planned
        );
        assert_eq!(
            aggregate_restore_state(
                [
                    &RestoreRecordStatus::Planned,
                    &RestoreRecordStatus::Invalidated(RestoreInvalidation {
                        reason: RestoreInvalidationReason::StaleGeneration,
                    }),
                ]
                .into_iter(),
            ),
            RestoreState::Planned
        );
        assert_eq!(
            aggregate_restore_state(
                [&RestoreRecordStatus::Invalidated(RestoreInvalidation {
                    reason: RestoreInvalidationReason::StaleGeneration,
                })]
                .into_iter(),
            ),
            RestoreState::Invalidated
        );
        assert_eq!(
            aggregate_restore_state(std::iter::empty()),
            RestoreState::Idle
        );
    }
}
