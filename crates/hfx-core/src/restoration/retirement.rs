// SPDX-License-Identifier: GPL-2.0-only

//! Generation retirement and atomic reconciliation of durable restore claims.

use super::completion::{
    completion_from_failure, completion_from_receipt, completion_from_terminal,
    release_attempt_lease, terminal_event_draft, unknown_completion,
};
use super::engine::dispatch_from_attempt;
use super::records::{active_attempt, load_records};
use super::{
    PersistenceOperation, RestorationCoordinator, RestorationError, RestoreGenerationRetirement,
    transition_record,
};
use crate::{
    BoundedEventLog, EventSink, LeaseManager, OutcomeLookup, PersistenceCasOutcome,
    PersistenceStore, ReceiverTransport, RestoreAttempt, RestoreCompletion, RestoreInvalidation,
    RestoreRecord, RestoreRecordChange, RestoreRecordStatus, TransactionCoordinator,
    TransportReconciliation,
};
use hfx_domain::{
    DeviceApplicationState, GenerationId, MonotonicMs, ProtocolErrorKind, ReceiverId,
    RestoreInvalidationReason, SideEffectCertainty, TransactionState,
};
use hfx_protocol::TransactionResult;

impl RestorationCoordinator {
    /// Reconciles every nonterminal claim bound to a retired generation.
    ///
    /// Claims with no observed dispatch are invalidated as stale. Claims whose
    /// exact transaction or transport outcome proves a prior attempt are
    /// completed with that evidence instead, preserving possible side effects.
    /// All sibling claim changes compare and commit as one persistence batch.
    ///
    /// # Errors
    ///
    /// Returns an error while the generation is still transport-active, when
    /// an unsent transaction has not yet been revoked, or when persistence,
    /// lease, record, or event invariants fail. No volatile state is committed
    /// when the durable batch cannot be committed.
    #[allow(clippy::too_many_arguments)]
    pub fn retire_generation<T, S, E>(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        now: MonotonicMs,
        transport: &T,
        store: &mut S,
        leases: &mut LeaseManager,
        transactions: &TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        S: PersistenceStore,
        E: EventSink,
    {
        if transport.current_generation(receiver_id) == Some(generation_id) {
            return Err(RestorationError::GenerationStillActive {
                receiver_id: receiver_id.clone(),
                generation_id,
            });
        }

        let records = load_records(receiver_id, store)?;
        let mut next_leases = leases.clone();
        let mut updated = Vec::new();
        let mut changes = Vec::new();
        let mut already_terminal = 0;
        for record in records
            .into_iter()
            .filter(|record| record.generation_id == generation_id)
        {
            if record.status.is_terminal() {
                already_terminal += 1;
                continue;
            }
            let expected_revision = record.revision;
            let retired = retire_record(record, now, transport, transactions, &mut next_leases)?;
            changes.push(RestoreRecordChange {
                expected_revision: Some(expected_revision),
                record: retired.clone(),
            });
            updated.push(retired);
        }
        let mut next_events = events.clone();
        let mut emitted = Vec::with_capacity(updated.len());
        for record in &updated {
            emitted.push(next_events.append(terminal_event_draft(record))?);
        }
        if !changes.is_empty() {
            match store
                .compare_and_set_restore_records(&changes)
                .map_err(|_| RestorationError::Persistence(PersistenceOperation::SaveRestore))?
            {
                PersistenceCasOutcome::Applied => {}
                PersistenceCasOutcome::Conflict => {
                    return Err(RestorationError::PersistenceConflict(
                        PersistenceOperation::SaveRestore,
                    ));
                }
            }
        }

        *leases = next_leases;
        *events = next_events;
        for event in emitted {
            let _ = sink.try_emit(&event);
        }
        Ok(RestoreGenerationRetirement {
            receiver_id: receiver_id.clone(),
            generation_id,
            updated,
            already_terminal,
        })
    }
}

fn retire_record<T: ReceiverTransport>(
    record: RestoreRecord,
    now: MonotonicMs,
    transport: &T,
    transactions: &TransactionCoordinator,
    leases: &mut LeaseManager,
) -> Result<RestoreRecord, RestorationError> {
    let Some(attempt) = active_attempt(&record.status).cloned() else {
        return transition_record(
            record,
            RestoreRecordStatus::Invalidated(RestoreInvalidation {
                reason: RestoreInvalidationReason::StaleGeneration,
            }),
        );
    };

    let completion =
        match transactions.outcome(&attempt.request.client_id, &attempt.request.transaction_id) {
            OutcomeLookup::Retained(TransactionResult::Terminal(terminal)) => {
                Some(completion_from_terminal(&attempt, terminal)?)
            }
            OutcomeLookup::Retained(TransactionResult::Unavailable(_)) => Some(unknown_completion(
                &attempt,
                ProtocolErrorKind::OutcomeUnknown,
            )?),
            OutcomeLookup::Retained(TransactionResult::Progress(_)) => {
                return Err(RestorationError::PriorClaimUnresolved);
            }
            OutcomeLookup::Evicted => Some(unknown_completion(
                &attempt,
                ProtocolErrorKind::OutcomeEvicted,
            )?),
            OutcomeLookup::Forbidden => return Err(RestorationError::RecordIdentityConflict),
            OutcomeLookup::Unknown => completion_from_reconciliation(
                &attempt,
                transport.reconcile(&dispatch_from_attempt(&attempt)),
            )?,
        };
    release_attempt_lease(&attempt, leases, now)?;
    match completion {
        Some(completion) => retire_with_completion(record, completion),
        None => transition_record(
            record,
            RestoreRecordStatus::Invalidated(RestoreInvalidation {
                reason: RestoreInvalidationReason::StaleGeneration,
            }),
        ),
    }
}

fn completion_from_reconciliation(
    attempt: &RestoreAttempt,
    reconciliation: TransportReconciliation,
) -> Result<Option<RestoreCompletion>, RestorationError> {
    match reconciliation {
        TransportReconciliation::NotObserved => Ok(None),
        TransportReconciliation::Retained(receipt) => {
            completion_from_receipt(attempt, receipt).map(Some)
        }
        TransportReconciliation::RetainedFailure(facts) => {
            completion_from_failure(attempt, facts).map(Some)
        }
        TransportReconciliation::Evicted => {
            unknown_completion(attempt, ProtocolErrorKind::OutcomeEvicted).map(Some)
        }
        TransportReconciliation::Unavailable | TransportReconciliation::Conflict => {
            unknown_completion(attempt, ProtocolErrorKind::OutcomeUnknown).map(Some)
        }
    }
}

fn retire_with_completion(
    record: RestoreRecord,
    mut completion: RestoreCompletion,
) -> Result<RestoreRecord, RestorationError> {
    let successful = completion.state == TransactionState::Succeeded
        && completion.delivered_frames.get() == 1
        && completion.side_effect_certainty == SideEffectCertainty::Committed
        && completion.live_write_executed
        && completion.device_application != DeviceApplicationState::Rejected
        && completion.error_kind.is_none();
    if successful {
        return transition_record(record, RestoreRecordStatus::Succeeded(completion));
    }
    let definitely_unwritten = !completion.live_write_executed
        && completion.delivered_frames.get() == 0
        && completion.side_effect_certainty == SideEffectCertainty::None;
    if definitely_unwritten
        || (completion.state == TransactionState::Revoked
            && completion.error_kind == Some(ProtocolErrorKind::StaleGeneration))
    {
        return transition_record(
            record,
            RestoreRecordStatus::Invalidated(RestoreInvalidation {
                reason: RestoreInvalidationReason::StaleGeneration,
            }),
        );
    }
    if completion.state == TransactionState::Succeeded {
        completion.state = TransactionState::Failed;
        completion.error_kind = Some(ProtocolErrorKind::InternalFailure);
        completion.automatic_retry = false;
    } else if completion.state != TransactionState::Failed {
        completion.state = TransactionState::Failed;
        completion
            .error_kind
            .get_or_insert(ProtocolErrorKind::InternalFailure);
    }
    transition_record(record, RestoreRecordStatus::Failed(completion))
}
