// SPDX-License-Identifier: GPL-2.0-only

//! Durable restoration composition for the production bridge.

use crate::{
    DisabledRestorationSource, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationSnapshotSource,
};
use hfx_core::{
    BoundedEventLog, CURRENT_PERSISTENCE_SCHEMA_VERSION, CompletedTransaction,
    DeviceStateAuthority, EventSink, LeaseManager, MAX_RESTORE_RECORDS_PER_RECEIVER,
    PersistenceOperation, PersistenceStore, ProfileRegistry, ReceiverTransport,
    RestorationAuthority, RestorationCoordinator, RestorationError, RestoreAdvanceResult,
    RestoreGenerationRetirement, RestorePlanResult, RestoreRecord, RestoreRecordStatus,
    RestoreTrigger, SessionAuthority, StableCommitOutcome, TransactionCoordinator,
};
use hfx_domain::{
    GenerationId, MonotonicMs, ReceiverId, RestoreClaimId, RestoreState, TransactionClass,
    WallClockUnixMs,
};
use hfx_protocol::TransactionRequest;

/// Owns durable stable-lighting capture, projection, and generation retirement.
pub trait RestorationRuntime: RestorationSnapshotSource {
    /// Synchronizes the persisted policy with the service configuration.
    ///
    /// # Errors
    ///
    /// Returns a durable policy validation or compare-and-set failure.
    fn set_enabled(
        &mut self,
        _receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        if enabled {
            Err(RestorationError::RuntimeDisabled)
        } else {
            Ok(())
        }
    }

    /// Loads all validated nonterminal claims for one receiver.
    ///
    /// # Errors
    ///
    /// Returns a persistence or record validation failure.
    fn pending_records(
        &self,
        _receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        Ok(Vec::new())
    }

    /// Plans claims for one exact lifecycle trigger.
    ///
    /// # Errors
    ///
    /// Returns a trigger, persistence, capacity, or prior-attempt barrier.
    fn plan_restore(
        &mut self,
        _trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        Ok(RestorePlanResult::Disabled)
    }

    /// Advances one durable claim without performing a hardware write.
    ///
    /// # Errors
    ///
    /// Returns the production coordinator's authority, persistence, lease, or
    /// transaction failure.
    #[allow(clippy::too_many_arguments)]
    fn advance_claim<A, D, P, T, E>(
        &mut self,
        _claim_id: &RestoreClaimId,
        _authority: &RestorationAuthority,
        _now: MonotonicMs,
        _sessions: &A,
        _devices: &D,
        _profiles: &P,
        _transport: &T,
        _leases: &mut LeaseManager,
        _transactions: &mut TransactionCoordinator,
        _events: &mut BoundedEventLog,
        _sink: &mut E,
    ) -> Result<RestoreAdvanceResult, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        Err(RestorationError::RuntimeDisabled)
    }

    /// Dispatches one claim already durably marked queued.
    ///
    /// # Errors
    ///
    /// Returns the production coordinator's revalidation, transport,
    /// persistence, lease, transaction, or event failure.
    #[allow(clippy::too_many_arguments)]
    fn dispatch_claim<A, D, P, T, E>(
        &mut self,
        _claim_id: &RestoreClaimId,
        _now: MonotonicMs,
        _sessions: &A,
        _devices: &D,
        _profiles: &P,
        _transport: &mut T,
        _leases: &mut LeaseManager,
        _transactions: &mut TransactionCoordinator,
        _events: &mut BoundedEventLog,
        _sink: &mut E,
    ) -> Result<RestoreRecord, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        Err(RestorationError::RuntimeDisabled)
    }

    /// Durably invalidates the prior stable state immediately before a
    /// dispatchable stable-lighting request reaches transport.
    ///
    /// Implementations that do not persist restoration state may keep the
    /// default no-op. Production durable storage uses this as the first phase
    /// of a tombstone/write/capture protocol.
    ///
    /// # Errors
    ///
    /// Returns a persistence or validation failure before any hardware write.
    fn prepare_stable_dispatch(
        &mut self,
        _request: &TransactionRequest,
        _prepared_at: WallClockUnixMs,
    ) -> Result<(), RestorationError> {
        Ok(())
    }

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
    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        let existing = self
            .store
            .restore_policy(receiver_id)
            .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadPolicy))?;
        if let Some(policy) = existing {
            if policy.schema_version.get() != CURRENT_PERSISTENCE_SCHEMA_VERSION {
                return Err(RestorationError::InvalidSchemaVersion);
            }
            if policy.receiver_id != *receiver_id {
                return Err(RestorationError::ReceiverMismatch);
            }
            if policy.enabled == enabled {
                return Ok(());
            }
        }
        RestorationCoordinator
            .set_restore_enabled(receiver_id, enabled, &mut self.store)
            .map(|_| ())
    }

    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        RestorationCoordinator.pending_restore_records(receiver_id, &self.store)
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        RestorationCoordinator.plan_restore(trigger, &mut self.store)
    }

    fn advance_claim<A, D, P, T, E>(
        &mut self,
        claim_id: &RestoreClaimId,
        authority: &RestorationAuthority,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &T,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreAdvanceResult, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        RestorationCoordinator.advance_claim(
            claim_id,
            authority,
            now,
            sessions,
            devices,
            profiles,
            transport,
            &mut self.store,
            leases,
            transactions,
            events,
            sink,
        )
    }

    fn dispatch_claim<A, D, P, T, E>(
        &mut self,
        claim_id: &RestoreClaimId,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &mut T,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreRecord, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        RestorationCoordinator.dispatch_claim(
            claim_id,
            now,
            sessions,
            devices,
            profiles,
            transport,
            &mut self.store,
            leases,
            transactions,
            events,
            sink,
        )
    }

    fn prepare_stable_dispatch(
        &mut self,
        request: &TransactionRequest,
        prepared_at: WallClockUnixMs,
    ) -> Result<(), RestorationError> {
        if request.transaction_class != TransactionClass::StaticLighting
            || request.stable_intents.is_empty()
        {
            return Ok(());
        }
        let device_ids = request
            .stable_intents
            .iter()
            .map(|intent| intent.device_id.clone())
            .collect::<Vec<_>>();
        RestorationCoordinator
            .clear_stable_intents(
                &request.receiver_id,
                &device_ids,
                prepared_at,
                &mut self.store,
            )
            .map(|_| ())
    }

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
