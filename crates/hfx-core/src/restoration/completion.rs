// SPDX-License-Identifier: GPL-2.0-only

//! Terminal restore accounting, durable completion, and event projection.

use super::records::{active_attempt, persist_record};
use super::{RestorationError, RestoreAdvanceResult, transition_record};
use crate::{
    BoundedEventLog, EventDraft, EventSink, LeaseManager, LeaseManagerError, PersistenceStore,
    RestoreAttempt, RestoreCompletion, RestoreDeferred, RestoreInvalidation, RestoreRecord,
    RestoreRecordStatus, TransactionCoordinatorError, TransportFailureFacts, TransportReceipt,
    TransportReconciliation, TransportTerminal,
};
use hfx_domain::{
    DeliveredFrameCount, DeviceApplicationState, DeviceWriteReadiness, EventKind, MonotonicMs,
    ProtocolErrorKind, RestoreDeferReason, RestoreInvalidationReason, SideEffectCertainty,
    TransactionState,
};
use hfx_protocol::TransactionTerminal;

pub(super) const fn readiness_defer_reason(
    readiness: DeviceWriteReadiness,
) -> Option<RestoreDeferReason> {
    match readiness {
        DeviceWriteReadiness::Ready => None,
        DeviceWriteReadiness::Sleeping => Some(RestoreDeferReason::DeviceSleeping),
        DeviceWriteReadiness::Unavailable => Some(RestoreDeferReason::DeviceUnavailable),
        DeviceWriteReadiness::Unknown => Some(RestoreDeferReason::DeviceUnknown),
    }
}

pub(super) fn reconcile_terminal<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    attempt: &RestoreAttempt,
    reconciliation: TransportReconciliation,
    store: &mut S,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreAdvanceResult, RestorationError> {
    let completion = match reconciliation {
        TransportReconciliation::Retained(receipt) => completion_from_receipt(attempt, receipt)?,
        TransportReconciliation::RetainedFailure(facts) => completion_from_failure(attempt, facts)?,
        TransportReconciliation::Evicted => {
            unknown_completion(attempt, ProtocolErrorKind::OutcomeEvicted)?
        }
        TransportReconciliation::Unavailable | TransportReconciliation::Conflict => {
            unknown_completion(attempt, ProtocolErrorKind::OutcomeUnknown)?
        }
        TransportReconciliation::NotObserved => {
            return Err(RestorationError::RecordIdentityConflict);
        }
    };
    finish_record(record, completion, store, events, sink)
}

pub(super) fn completion_from_receipt(
    attempt: &RestoreAttempt,
    receipt: TransportReceipt,
) -> Result<RestoreCompletion, RestorationError> {
    let reported_too_many = receipt.delivered_frames.get() > 1;
    let facts = normalize_facts(TransportFailureFacts {
        delivered_frames: receipt.delivered_frames,
        side_effect_certainty: receipt.side_effect_certainty,
        live_write_executed: receipt.live_write_executed,
        automatic_retry_safe: receipt.automatic_retry_safe,
        device_application: receipt.device_application,
    })?;
    let successful = !reported_too_many
        && receipt.terminal == TransportTerminal::Delivered
        && facts.delivered_frames.get() == 1
        && facts.side_effect_certainty == SideEffectCertainty::Committed
        && facts.live_write_executed
        && facts.device_application != DeviceApplicationState::Rejected;
    let (state, error_kind) = if successful {
        (TransactionState::Succeeded, None)
    } else if receipt.terminal == TransportTerminal::Revoked {
        (
            TransactionState::Revoked,
            Some(ProtocolErrorKind::StaleGeneration),
        )
    } else if reported_too_many {
        (
            TransactionState::Failed,
            Some(ProtocolErrorKind::InternalFailure),
        )
    } else {
        (
            TransactionState::Failed,
            Some(ProtocolErrorKind::TransportFailure),
        )
    };
    Ok(RestoreCompletion {
        attempt_number: attempt.attempt_number,
        transaction_id: attempt.request.transaction_id.clone(),
        request_digest: attempt.request_digest.clone(),
        state,
        delivered_frames: facts.delivered_frames,
        side_effect_certainty: facts.side_effect_certainty,
        live_write_executed: facts.live_write_executed,
        automatic_retry: facts.automatic_retry_safe && !successful,
        device_application: facts.device_application,
        error_kind,
    })
}

pub(super) fn completion_from_failure(
    attempt: &RestoreAttempt,
    facts: TransportFailureFacts,
) -> Result<RestoreCompletion, RestorationError> {
    let reported_too_many = facts.delivered_frames.get() > 1;
    let facts = normalize_facts(facts)?;
    Ok(RestoreCompletion {
        attempt_number: attempt.attempt_number,
        transaction_id: attempt.request.transaction_id.clone(),
        request_digest: attempt.request_digest.clone(),
        state: TransactionState::Failed,
        delivered_frames: facts.delivered_frames,
        side_effect_certainty: facts.side_effect_certainty,
        live_write_executed: facts.live_write_executed,
        automatic_retry: facts.automatic_retry_safe,
        device_application: facts.device_application,
        error_kind: Some(if reported_too_many {
            ProtocolErrorKind::InternalFailure
        } else {
            ProtocolErrorKind::TransportFailure
        }),
    })
}

fn normalize_facts(
    mut facts: TransportFailureFacts,
) -> Result<TransportFailureFacts, RestorationError> {
    let delivered = facts.delivered_frames.get().min(1);
    facts.delivered_frames = DeliveredFrameCount::try_from(delivered)
        .map_err(|_| RestorationError::RecordIdentityConflict)?;
    if delivered > 0 {
        facts.side_effect_certainty = SideEffectCertainty::Committed;
        facts.live_write_executed = true;
    } else if facts.live_write_executed && facts.side_effect_certainty == SideEffectCertainty::None
    {
        facts.side_effect_certainty = SideEffectCertainty::Possible;
    }
    facts.automatic_retry_safe = facts.automatic_retry_safe
        && !facts.live_write_executed
        && facts.delivered_frames.get() == 0
        && facts.side_effect_certainty == SideEffectCertainty::None;
    Ok(facts)
}

pub(super) fn unknown_completion(
    attempt: &RestoreAttempt,
    error_kind: ProtocolErrorKind,
) -> Result<RestoreCompletion, RestorationError> {
    Ok(RestoreCompletion {
        attempt_number: attempt.attempt_number,
        transaction_id: attempt.request.transaction_id.clone(),
        request_digest: attempt.request_digest.clone(),
        state: TransactionState::Failed,
        delivered_frames: DeliveredFrameCount::try_from(0_u16)
            .map_err(|_| RestorationError::RecordIdentityConflict)?,
        side_effect_certainty: SideEffectCertainty::Possible,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Unverified,
        error_kind: Some(error_kind),
    })
}

pub(super) fn completion_from_terminal(
    attempt: &RestoreAttempt,
    terminal: &TransactionTerminal,
) -> Result<RestoreCompletion, RestorationError> {
    if terminal.request_id != attempt.request.request_id
        || terminal.request_digest != attempt.request_digest
        || terminal.transaction_id != attempt.request.transaction_id
        || terminal.receiver_id != attempt.request.receiver_id
        || terminal.generation_id != attempt.request.generation_id
        || terminal.declared_frames.get() != 1
        || terminal.delivered_frames.get() > 1
    {
        return Err(RestorationError::RecordIdentityConflict);
    }
    Ok(RestoreCompletion {
        attempt_number: attempt.attempt_number,
        transaction_id: terminal.transaction_id.clone(),
        request_digest: terminal.request_digest.clone(),
        state: terminal.state,
        delivered_frames: terminal.delivered_frames,
        side_effect_certainty: terminal.side_effect_certainty,
        live_write_executed: terminal.live_write_executed,
        automatic_retry: terminal.automatic_retry,
        device_application: terminal.device_application,
        error_kind: terminal.error_kind,
    })
}

pub(super) fn finish_from_transaction<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    terminal: &TransactionTerminal,
    store: &mut S,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreAdvanceResult, RestorationError> {
    let attempt = active_attempt(&record.status).ok_or(RestorationError::RecordIdentityConflict)?;
    let completion = completion_from_terminal(attempt, terminal)?;
    finish_record(record, completion, store, events, sink)
}

pub(super) fn finish_record<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    mut completion: RestoreCompletion,
    store: &mut S,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreAdvanceResult, RestorationError> {
    if completion.state == TransactionState::Succeeded
        && completion.delivered_frames.get() == 1
        && completion.side_effect_certainty == SideEffectCertainty::Committed
        && completion.live_write_executed
        && completion.device_application != DeviceApplicationState::Rejected
        && completion.error_kind.is_none()
    {
        let completed =
            transition_record(record.clone(), RestoreRecordStatus::Succeeded(completion))?;
        persist_record(Some(record.revision), &completed, store)?;
        emit_terminal(&completed, events, sink)?;
        return Ok(RestoreAdvanceResult::Terminal(completed));
    }
    if completion.automatic_retry
        && !completion.live_write_executed
        && completion.delivered_frames.get() == 0
        && completion.side_effect_certainty == SideEffectCertainty::None
    {
        return defer(
            record,
            RestoreDeferReason::SafeTransactionFailure,
            Some(completion),
            store,
        );
    }
    if completion.state == TransactionState::Revoked
        && completion.error_kind == Some(ProtocolErrorKind::StaleGeneration)
    {
        return invalidate(
            record,
            RestoreInvalidationReason::StaleGeneration,
            store,
            events,
            sink,
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
    let failed = transition_record(record.clone(), RestoreRecordStatus::Failed(completion))?;
    persist_record(Some(record.revision), &failed, store)?;
    emit_terminal(&failed, events, sink)?;
    Ok(RestoreAdvanceResult::Terminal(failed))
}

pub(super) fn handle_submission_error<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    error: TransactionCoordinatorError,
    store: &mut S,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreAdvanceResult, RestorationError> {
    match error {
        TransactionCoordinatorError::SessionRevoked => {
            defer(record, RestoreDeferReason::SessionUnavailable, None, store)
        }
        TransactionCoordinatorError::OwnershipDenied => {
            defer(record, RestoreDeferReason::OwnershipConflict, None, store)
        }
        TransactionCoordinatorError::StaleGeneration => invalidate(
            record,
            RestoreInvalidationReason::StaleGeneration,
            store,
            events,
            sink,
        ),
        TransactionCoordinatorError::UnsupportedResource
        | TransactionCoordinatorError::ProfileBindingChanged => invalidate(
            record,
            RestoreInvalidationReason::ProfileChanged,
            store,
            events,
            sink,
        ),
        TransactionCoordinatorError::DeviceNotReady(readiness) => {
            let Some(reason) = readiness_defer_reason(readiness) else {
                return Err(RestorationError::Transaction(
                    TransactionCoordinatorError::DeviceNotReady(readiness),
                ));
            };
            defer(record, reason, None, store)
        }
        TransactionCoordinatorError::Queue(crate::TransactionQueueError::DeadlineElapsed) => {
            defer(record, RestoreDeferReason::DeadlineElapsed, None, store)
        }
        TransactionCoordinatorError::Queue(crate::TransactionQueueError::Full) => defer(
            record,
            RestoreDeferReason::SafeTransactionFailure,
            None,
            store,
        ),
        other => Err(RestorationError::Transaction(other)),
    }
}

pub(super) fn defer<S: PersistenceStore>(
    record: RestoreRecord,
    reason: RestoreDeferReason,
    prior_outcome: Option<RestoreCompletion>,
    store: &mut S,
) -> Result<RestoreAdvanceResult, RestorationError> {
    let expected_revision = record.revision;
    let deferred = transition_record(
        record,
        RestoreRecordStatus::Deferred(RestoreDeferred {
            reason,
            prior_outcome,
        }),
    )?;
    persist_record(Some(expected_revision), &deferred, store)?;
    Ok(RestoreAdvanceResult::Deferred(deferred))
}

pub(super) fn invalidate<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    reason: RestoreInvalidationReason,
    store: &mut S,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreAdvanceResult, RestorationError> {
    let expected_revision = record.revision;
    let invalidated = transition_record(
        record,
        RestoreRecordStatus::Invalidated(RestoreInvalidation { reason }),
    )?;
    persist_record(Some(expected_revision), &invalidated, store)?;
    emit_terminal(&invalidated, events, sink)?;
    Ok(RestoreAdvanceResult::Terminal(invalidated))
}

fn emit_terminal<E: EventSink>(
    record: &RestoreRecord,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<(), RestorationError> {
    let event = events.append(terminal_event_draft(record))?;
    let _ = sink.try_emit(&event);
    Ok(())
}

pub(super) fn terminal_event_draft(record: &RestoreRecord) -> EventDraft {
    let transaction_id = match &record.status {
        RestoreRecordStatus::Succeeded(completion) | RestoreRecordStatus::Failed(completion) => {
            Some(completion.transaction_id.clone())
        }
        _ => None,
    };
    EventDraft {
        kind: EventKind::RestoreCompleted,
        receiver_id: Some(record.receiver_id.clone()),
        generation_id: Some(record.generation_id),
        device_id: Some(record.device_id.clone()),
        lease_id: None,
        transaction_id,
        finding_id: None,
    }
}

pub(super) fn release_attempt_lease(
    attempt: &RestoreAttempt,
    leases: &mut LeaseManager,
    now: MonotonicMs,
) -> Result<(), RestorationError> {
    match leases.release(&attempt.request.client_id, &attempt.request.lease_id, now) {
        Ok(_) | Err(LeaseManagerError::UnknownLease) => Ok(()),
        Err(error) => Err(error.into()),
    }
}
