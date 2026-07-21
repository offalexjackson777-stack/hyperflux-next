// SPDX-License-Identifier: GPL-2.0-only

use super::intent::load_entries;
use super::{
    MAX_RESTORE_RECORDS_PER_RECEIVER, PersistenceOperation, RestorationAuthority,
    RestorationCoordinator, RestorationError, RestoreAdvanceResult, RestoreGenerationRetirement,
    RestorePlanResult, current_schema_version, next_persistence_revision, sha256_hex,
    transition_record, validate_schema,
};
use crate::{
    BoundedEventLog, DeviceStateAuthority, DispatchResult, EventDraft, EventSink, LeaseManager,
    LeaseManagerError, OutcomeLookup, PersistedStableEntry, PersistedStableIntent,
    PersistenceCasOutcome, PersistenceStore, ProfileRegistry, ReceiverTransport, RestoreAttempt,
    RestoreCompletion, RestoreDeferred, RestoreInvalidation, RestoreRecord, RestoreRecordChange,
    RestoreRecordStatus, RestoreTrigger, SessionAuthority, SubmissionBinding,
    TransactionCoordinator, TransactionCoordinatorError, TransportDispatch, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal, canonical_request_digest,
};
use hfx_domain::{
    ColorChannel, DeliveredFrameCount, DeviceApplicationState, DeviceWriteReadiness, DispatchNonce,
    EventKind, FrameIndex, GenerationId, IntentRevision, LeaseId, LogicalDeviceId, MonotonicMs,
    PersistenceRevision, ProtocolErrorKind, ReceiverId, RequestId, ResourceKind,
    RestoreAttemptNumber, RestoreClaimId, RestoreDeferReason, RestoreInvalidationReason,
    SideEffectCertainty, TransactionClass, TransactionId, TransactionState,
};
use hfx_protocol::{
    DeviceProfileBinding, LeaseRequest, LeaseResult, LightingFrame, ResourceKey, RgbColor,
    TransactionRequest, TransactionResult, TransactionTerminal,
};
use serde::Serialize;
use std::collections::BTreeSet;

impl RestorationCoordinator {
    /// Creates one durable claim per active stable intent for a lifecycle trigger.
    ///
    /// Repeating the same trigger returns the same records. A newer trigger
    /// invalidates safely supersedable claims, while attempted claims remain a
    /// barrier until their exact transport outcome is reconciled.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid persisted records, capacity violations,
    /// storage failures, or compare-and-set conflicts.
    pub fn plan_restore<S: PersistenceStore>(
        &self,
        trigger: &RestoreTrigger,
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
        let entries = load_entries(&trigger.receiver_id, store)?;
        let intents = entries
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
        if intents.is_empty() {
            return Ok(RestorePlanResult::NoStableIntents);
        }
        let mut records = load_records(&trigger.receiver_id, store)?;
        validate_trigger_history(trigger, &records)?;
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

    /// Advances one durable claim to deferred, queued, or terminal state.
    ///
    /// Existing attempts are reconciled before current authority is considered.
    /// Only a durable transport `NotObserved` result may lead to a new write.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid persistence, CAS conflicts, internal identity
    /// failures, or core lease/transaction invariant failures.
    #[allow(clippy::too_many_arguments, clippy::too_many_lines)]
    pub fn advance_claim<A, D, P, T, S, E>(
        &self,
        claim_id: &RestoreClaimId,
        authority: &RestorationAuthority,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &T,
        store: &mut S,
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
        S: PersistenceStore,
        E: EventSink,
    {
        let mut record = load_record(claim_id, store)?;
        if record.status.is_terminal() {
            return Ok(RestoreAdvanceResult::Terminal(record));
        }
        enforce_prior_outcome_barrier(&record, store)?;
        if let Some(attempt) = active_attempt(&record.status).cloned() {
            match transport.reconcile(&dispatch_from_attempt(&attempt)) {
                TransportReconciliation::NotObserved => {}
                reconciliation => {
                    release_attempt_lease(&attempt, leases, now)?;
                    return reconcile_terminal(
                        record,
                        &attempt,
                        reconciliation,
                        store,
                        events,
                        sink,
                    );
                }
            }
        }

        let Some(intent) = current_intent(&record, store)? else {
            return invalidate(
                record,
                RestoreInvalidationReason::IntentChanged,
                store,
                events,
                sink,
            );
        };
        if !restore_enabled(&record.receiver_id, store)? {
            return invalidate(
                record,
                RestoreInvalidationReason::RestoreDisabled,
                store,
                events,
                sink,
            );
        }
        if transport.current_generation(&record.receiver_id) != Some(record.generation_id) {
            return invalidate(
                record,
                RestoreInvalidationReason::StaleGeneration,
                store,
                events,
                sink,
            );
        }
        let resource =
            lighting_resource(&record.receiver_id, record.generation_id, &record.device_id);
        if let Some(reason) = readiness_defer_reason(devices.write_readiness(&resource)) {
            return defer(record, reason, None, store);
        }
        if !profile_matches(&record, &intent, &resource, profiles) {
            return invalidate(
                record,
                RestoreInvalidationReason::ProfileChanged,
                store,
                events,
                sink,
            );
        }
        if !sessions.authorizes(
            &authority.submission.session_id,
            authority.submission.authorization_epoch,
        ) {
            return defer(record, RestoreDeferReason::SessionUnavailable, None, store);
        }
        if authority.deadline_ms <= now {
            return defer(record, RestoreDeferReason::DeadlineElapsed, None, store);
        }

        let reusable = active_attempt(&record.status).is_some_and(|attempt| {
            attempt.submission == authority.submission
                && attempt.request.client_id == authority.client_id
                && attempt.lease_request.duration_ms == authority.lease_duration_ms
                && attempt.request.deadline_ms > now
        });
        let attempt = if reusable {
            active_attempt(&record.status)
                .cloned()
                .ok_or(RestorationError::RecordIdentityConflict)?
        } else {
            let next = next_attempt_number(record.last_attempt)?;
            let attempt = build_attempt(&record, &intent, authority, next)?;
            record.last_attempt = Some(next);
            let expected_revision = record.revision;
            let prepared =
                transition_record(record, RestoreRecordStatus::Prepared(attempt.clone()))?;
            persist_record(Some(expected_revision), &prepared, store)?;
            record = prepared;
            attempt
        };

        match acquire_result(&attempt, now, leases)? {
            LeaseAcquireResult::Conflict => {
                return defer(record, RestoreDeferReason::OwnershipConflict, None, store);
            }
            LeaseAcquireResult::Granted => {}
        }
        if !leases.owns(
            &attempt.request.client_id,
            &attempt.request.lease_id,
            &attempt.request.resources,
            now,
        ) {
            return defer(record, RestoreDeferReason::OwnershipConflict, None, store);
        }
        match transactions.submit(
            attempt.request.clone(),
            attempt.submission.clone(),
            now,
            sessions,
            leases,
            profiles,
            devices,
            transport,
            events,
            sink,
        ) {
            Ok(
                crate::SubmissionResult::Queued(_)
                | crate::SubmissionResult::Replay(TransactionResult::Progress(_)),
            ) => {
                let expected_revision = record.revision;
                let queued = transition_record(record, RestoreRecordStatus::Queued(attempt))?;
                persist_record(Some(expected_revision), &queued, store)?;
                Ok(RestoreAdvanceResult::Queued(queued))
            }
            Ok(crate::SubmissionResult::Replay(TransactionResult::Terminal(terminal))) => {
                release_attempt_lease(&attempt, leases, now)?;
                finish_from_transaction(record, &terminal, store, events, sink)
            }
            Ok(crate::SubmissionResult::Replay(TransactionResult::Unavailable(_))) => {
                release_attempt_lease(&attempt, leases, now)?;
                let completion = unknown_completion(&attempt, ProtocolErrorKind::OutcomeUnknown)?;
                finish_record(record, completion, store, events, sink)
            }
            Err(error) => {
                release_attempt_lease(&attempt, leases, now)?;
                handle_submission_error(record, error, store, events, sink)
            }
        }
    }

    /// Persists the transition from queued to applying before hardware dispatch.
    ///
    /// # Errors
    ///
    /// Returns an error for unknown claims, invalid transitions, storage failure,
    /// or compare-and-set conflict.
    pub fn mark_applying<S: PersistenceStore>(
        &self,
        claim_id: &RestoreClaimId,
        store: &mut S,
    ) -> Result<RestoreRecord, RestorationError> {
        let record = load_record(claim_id, store)?;
        let RestoreRecordStatus::Queued(attempt) = &record.status else {
            return Err(RestorationError::InvalidTransition {
                from: record.status.state(),
                to: hfx_domain::RestoreRecordState::Applying,
            });
        };
        let applying = transition_record(
            record.clone(),
            RestoreRecordStatus::Applying(attempt.clone()),
        )?;
        persist_record(Some(record.revision), &applying, store)?;
        Ok(applying)
    }

    /// Rechecks one queued claim, persists `Applying`, and dispatches exactly
    /// that transaction before recording its terminal restore state.
    ///
    /// This is the hardware-facing restoration entry point. Callers must first
    /// obtain `Queued` from [`Self::advance_claim`].
    ///
    /// # Errors
    ///
    /// Returns a typed persistence, lease, transaction, or event failure. If a
    /// failure occurs after `Applying` is durable, the next advance reconciles
    /// the exact persisted attempt before considering another write.
    #[allow(clippy::too_many_arguments, clippy::too_many_lines)]
    pub fn dispatch_claim<A, D, P, T, S, E>(
        &self,
        claim_id: &RestoreClaimId,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &mut T,
        store: &mut S,
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
        S: PersistenceStore,
        E: EventSink,
    {
        let record = load_record(claim_id, store)?;
        let RestoreRecordStatus::Queued(attempt) = &record.status else {
            return Err(RestorationError::InvalidTransition {
                from: record.status.state(),
                to: hfx_domain::RestoreRecordState::Applying,
            });
        };
        let attempt = attempt.clone();
        match transport.reconcile(&dispatch_from_attempt(&attempt)) {
            TransportReconciliation::NotObserved => {}
            reconciliation => {
                release_attempt_lease(&attempt, leases, now)?;
                return Ok(record_from_advance(reconcile_terminal(
                    record,
                    &attempt,
                    reconciliation,
                    store,
                    events,
                    sink,
                )?));
            }
        }

        match dispatch_gate(
            &record, &attempt, now, sessions, devices, profiles, transport, store,
        )? {
            DispatchGate::Ready => {}
            DispatchGate::Deferred(reason) => {
                return cancel_and_defer(
                    record,
                    &attempt,
                    reason,
                    now,
                    store,
                    leases,
                    transactions,
                    events,
                    sink,
                );
            }
            DispatchGate::Invalidated(reason) => {
                transactions.cancel_transaction(&attempt.request.transaction_id, events, sink)?;
                release_attempt_lease(&attempt, leases, now)?;
                return Ok(record_from_advance(invalidate(
                    record, reason, store, events, sink,
                )?));
            }
        }
        if !leases.owns(
            &attempt.request.client_id,
            &attempt.request.lease_id,
            &attempt.request.resources,
            now,
        ) {
            return cancel_and_defer(
                record,
                &attempt,
                RestoreDeferReason::OwnershipConflict,
                now,
                store,
                leases,
                transactions,
                events,
                sink,
            );
        }

        let applying = self.mark_applying(claim_id, store)?;
        let result = transactions.dispatch_transaction(
            &attempt.request.transaction_id,
            now,
            sessions,
            leases,
            profiles,
            devices,
            transport,
            events,
            sink,
        )?;
        let records = self.observe_dispatch(&result, now, store, leases, events, sink)?;
        records
            .into_iter()
            .find(|updated| updated.claim_id == applying.claim_id)
            .ok_or(RestorationError::RecordIdentityConflict)
    }

    /// Reconciles transaction terminals into durable per-device restore records.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid stored records, lease failures, event failures,
    /// storage failures, or compare-and-set conflicts.
    pub fn observe_dispatch<S: PersistenceStore, E: EventSink>(
        &self,
        result: &DispatchResult,
        now: MonotonicMs,
        store: &mut S,
        leases: &mut LeaseManager,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        let mut updated = Vec::new();
        for terminal in result
            .expired
            .iter()
            .chain(result.completed.iter().map(|completed| &completed.terminal))
        {
            let records = load_records(&terminal.receiver_id, store)?;
            let Some(record) = records.into_iter().find(|record| {
                active_attempt(&record.status).is_some_and(|attempt| {
                    attempt.request.transaction_id == terminal.transaction_id
                })
            }) else {
                continue;
            };
            if let Some(attempt) = active_attempt(&record.status) {
                release_attempt_lease(attempt, leases, now)?;
            }
            let result = finish_from_transaction(record, terminal, store, events, sink)?;
            match result {
                RestoreAdvanceResult::Deferred(record)
                | RestoreAdvanceResult::Queued(record)
                | RestoreAdvanceResult::Terminal(record) => updated.push(record),
            }
        }
        Ok(updated)
    }

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LeaseAcquireResult {
    Granted,
    Conflict,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DispatchGate {
    Ready,
    Deferred(RestoreDeferReason),
    Invalidated(RestoreInvalidationReason),
}

#[allow(clippy::too_many_arguments)]
fn dispatch_gate<A, D, P, T, S>(
    record: &RestoreRecord,
    attempt: &RestoreAttempt,
    now: MonotonicMs,
    sessions: &A,
    devices: &D,
    profiles: &P,
    transport: &T,
    store: &S,
) -> Result<DispatchGate, RestorationError>
where
    A: SessionAuthority,
    D: DeviceStateAuthority,
    P: ProfileRegistry,
    T: ReceiverTransport,
    S: PersistenceStore,
{
    let Some(intent) = current_intent(record, store)? else {
        return Ok(DispatchGate::Invalidated(
            RestoreInvalidationReason::IntentChanged,
        ));
    };
    if !restore_enabled(&record.receiver_id, store)? {
        return Ok(DispatchGate::Invalidated(
            RestoreInvalidationReason::RestoreDisabled,
        ));
    }
    if transport.current_generation(&record.receiver_id) != Some(record.generation_id) {
        return Ok(DispatchGate::Invalidated(
            RestoreInvalidationReason::StaleGeneration,
        ));
    }
    let resource = lighting_resource(&record.receiver_id, record.generation_id, &record.device_id);
    if !profile_matches(record, &intent, &resource, profiles) {
        return Ok(DispatchGate::Invalidated(
            RestoreInvalidationReason::ProfileChanged,
        ));
    }
    if let Some(reason) = readiness_defer_reason(devices.write_readiness(&resource)) {
        return Ok(DispatchGate::Deferred(reason));
    }
    if !sessions.authorizes(
        &attempt.submission.session_id,
        attempt.submission.authorization_epoch,
    ) {
        return Ok(DispatchGate::Deferred(
            RestoreDeferReason::SessionUnavailable,
        ));
    }
    if attempt.request.deadline_ms <= now {
        return Ok(DispatchGate::Deferred(RestoreDeferReason::DeadlineElapsed));
    }
    Ok(DispatchGate::Ready)
}

#[allow(clippy::too_many_arguments)]
fn cancel_and_defer<S: PersistenceStore, E: EventSink>(
    record: RestoreRecord,
    attempt: &RestoreAttempt,
    reason: RestoreDeferReason,
    now: MonotonicMs,
    store: &mut S,
    leases: &mut LeaseManager,
    transactions: &mut TransactionCoordinator,
    events: &mut BoundedEventLog,
    sink: &mut E,
) -> Result<RestoreRecord, RestorationError> {
    let terminal =
        transactions.cancel_transaction(&attempt.request.transaction_id, events, sink)?;
    release_attempt_lease(attempt, leases, now)?;
    let completion = completion_from_terminal(attempt, &terminal)?;
    Ok(record_from_advance(defer(
        record,
        reason,
        Some(completion),
        store,
    )?))
}

fn record_from_advance(result: RestoreAdvanceResult) -> RestoreRecord {
    match result {
        RestoreAdvanceResult::Deferred(record)
        | RestoreAdvanceResult::Queued(record)
        | RestoreAdvanceResult::Terminal(record) => record,
    }
}

fn acquire_result(
    attempt: &RestoreAttempt,
    now: MonotonicMs,
    leases: &mut LeaseManager,
) -> Result<LeaseAcquireResult, RestorationError> {
    match leases.acquire(
        attempt.lease_request.clone(),
        attempt.request.lease_id.clone(),
        now,
    )? {
        LeaseResult::Granted(_) => Ok(LeaseAcquireResult::Granted),
        LeaseResult::Conflict(_) | LeaseResult::Rejected(_) => Ok(LeaseAcquireResult::Conflict),
    }
}

fn restore_enabled<S: PersistenceStore>(
    receiver_id: &ReceiverId,
    store: &S,
) -> Result<bool, RestorationError> {
    let policy = store
        .restore_policy(receiver_id)
        .map_err(|_| RestorationError::Persistence(PersistenceOperation::LoadPolicy))?;
    let Some(policy) = policy else {
        return Ok(false);
    };
    validate_schema(policy.schema_version)?;
    if &policy.receiver_id != receiver_id {
        return Err(RestorationError::ReceiverMismatch);
    }
    Ok(policy.enabled)
}

fn current_intent<S: PersistenceStore>(
    record: &RestoreRecord,
    store: &S,
) -> Result<Option<PersistedStableIntent>, RestorationError> {
    Ok(load_entries(&record.receiver_id, store)?
        .into_iter()
        .find_map(|entry| match entry {
            PersistedStableEntry::Present(intent)
                if intent.device_id == record.device_id
                    && intent.revision == record.intent_revision
                    && intent.content_digest == record.intent_digest =>
            {
                Some(intent)
            }
            PersistedStableEntry::Present(_) | PersistedStableEntry::Deleted(_) => None,
        }))
}

fn profile_matches<P: ProfileRegistry>(
    record: &RestoreRecord,
    intent: &PersistedStableIntent,
    resource: &ResourceKey,
    profiles: &P,
) -> bool {
    profiles.supports(resource)
        && profiles
            .receiver_profile(&record.receiver_id, record.generation_id)
            .is_some_and(|profile| {
                profile.profile_id == intent.receiver_profile_id
                    && profile.profile_digest == intent.receiver_profile_digest
            })
        && profiles.device_profile(resource).is_some_and(|profile| {
            profile.profile_id == intent.profile_id
                && profile.profile_digest == intent.profile_digest
                && profile.application_slot_count == intent.application_slot_count
        })
}

const fn readiness_defer_reason(readiness: DeviceWriteReadiness) -> Option<RestoreDeferReason> {
    match readiness {
        DeviceWriteReadiness::Ready => None,
        DeviceWriteReadiness::Sleeping => Some(RestoreDeferReason::DeviceSleeping),
        DeviceWriteReadiness::Unavailable => Some(RestoreDeferReason::DeviceUnavailable),
        DeviceWriteReadiness::Unknown => Some(RestoreDeferReason::DeviceUnknown),
    }
}

fn build_attempt(
    record: &RestoreRecord,
    intent: &PersistedStableIntent,
    authority: &RestorationAuthority,
    attempt_number: RestoreAttemptNumber,
) -> Result<RestoreAttempt, RestorationError> {
    let colors = lighting_colors(intent)?;
    let suffix = attempt_suffix(&record.claim_id, attempt_number);
    let transaction_id = typed_id::<TransactionId>("restore-tx", &suffix)?;
    let resource = lighting_resource(&record.receiver_id, record.generation_id, &record.device_id);
    let lease_request = LeaseRequest {
        request_id: typed_id::<RequestId>("restore-lease-request", &suffix)?,
        client_id: authority.client_id.clone(),
        resources: vec![resource.clone()],
        duration_ms: authority.lease_duration_ms,
    };
    let request = TransactionRequest {
        request_id: typed_id::<RequestId>("restore-request", &suffix)?,
        transaction_id,
        client_id: authority.client_id.clone(),
        lease_id: typed_id::<LeaseId>("restore-lease", &suffix)?,
        receiver_id: record.receiver_id.clone(),
        generation_id: record.generation_id,
        receiver_profile_id: intent.receiver_profile_id.clone(),
        receiver_profile_digest: intent.receiver_profile_digest.clone(),
        device_profiles: vec![DeviceProfileBinding {
            device_id: record.device_id.clone(),
            profile_id: intent.profile_id.clone(),
            profile_digest: intent.profile_digest.clone(),
            application_slot_count: intent.application_slot_count,
        }],
        transaction_class: TransactionClass::Restore,
        stable_intents: Vec::new(),
        deadline_ms: authority.deadline_ms,
        resources: vec![resource],
        frames: vec![LightingFrame {
            device_id: record.device_id.clone(),
            frame_index: FrameIndex::try_from(0_u32).map_err(|_| RestorationError::Identifier)?,
            colors,
        }],
    };
    let request_digest =
        canonical_request_digest(&request).map_err(|_| RestorationError::Identifier)?;
    let submission = SubmissionBinding {
        session_id: authority.submission.session_id.clone(),
        authorization_epoch: authority.submission.authorization_epoch,
        dispatch_nonce: attempt_nonce(authority.submission.dispatch_nonce, attempt_number)?,
    };
    Ok(RestoreAttempt {
        attempt_number,
        lease_request,
        request,
        request_digest,
        submission,
    })
}

fn lighting_colors(intent: &PersistedStableIntent) -> Result<Vec<RgbColor>, RestorationError> {
    let count = usize::from(intent.application_slot_count.get());
    if count == 0 {
        return Err(RestorationError::IntentDigestMismatch);
    }
    match &intent.lighting {
        crate::StableLighting::Static(colors) if colors.len() == count => Ok(colors.clone()),
        crate::StableLighting::Static(_) => Err(RestorationError::IntentDigestMismatch),
        crate::StableLighting::Off => {
            let zero = ColorChannel::try_from(0_u8).map_err(|_| RestorationError::Identifier)?;
            Ok(vec![
                RgbColor {
                    red: zero,
                    green: zero,
                    blue: zero,
                };
                count
            ])
        }
    }
}

fn active_attempt(status: &RestoreRecordStatus) -> Option<&RestoreAttempt> {
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

fn dispatch_from_attempt(attempt: &RestoreAttempt) -> TransportDispatch {
    TransportDispatch {
        session_id: attempt.submission.session_id.clone(),
        authorization_epoch: attempt.submission.authorization_epoch,
        dispatch_nonce: attempt.submission.dispatch_nonce,
        receiver_id: attempt.request.receiver_id.clone(),
        generation_id: attempt.request.generation_id,
        transaction_id: attempt.request.transaction_id.clone(),
        request_digest: attempt.request_digest.clone(),
        receiver_profile_id: attempt.request.receiver_profile_id.clone(),
        receiver_profile_digest: attempt.request.receiver_profile_digest.clone(),
        device_profiles: attempt.request.device_profiles.clone(),
        frames: attempt.request.frames.clone(),
    }
}

fn reconcile_terminal<S: PersistenceStore, E: EventSink>(
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

fn completion_from_receipt(
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

fn completion_from_failure(
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

fn unknown_completion(
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

fn completion_from_terminal(
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

fn finish_from_transaction<S: PersistenceStore, E: EventSink>(
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

fn finish_record<S: PersistenceStore, E: EventSink>(
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

fn handle_submission_error<S: PersistenceStore, E: EventSink>(
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

fn defer<S: PersistenceStore>(
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

fn invalidate<S: PersistenceStore, E: EventSink>(
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

fn terminal_event_draft(record: &RestoreRecord) -> EventDraft {
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

fn load_record<S: PersistenceStore>(
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

fn load_records<S: PersistenceStore>(
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

fn persist_record<S: PersistenceStore>(
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

fn supersede_prior_claims<S: PersistenceStore>(
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

fn enforce_prior_outcome_barrier<S: PersistenceStore>(
    candidate: &RestoreRecord,
    store: &S,
) -> Result<(), RestorationError> {
    for prior in load_records(&candidate.receiver_id, store)?
        .into_iter()
        .filter(|prior| {
            prior.claim_id != candidate.claim_id
                && prior.device_id == candidate.device_id
                && prior.intent_revision == candidate.intent_revision
                && prior.intent_digest == candidate.intent_digest
        })
    {
        if active_attempt(&prior.status).is_some() {
            return Err(RestorationError::PriorClaimUnresolved);
        }
        if let RestoreRecordStatus::Failed(completion) = prior.status
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

fn new_restore_record(
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

fn validate_same_claim(
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

fn lighting_resource(
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

fn next_attempt_number(
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

fn attempt_suffix(claim_id: &RestoreClaimId, attempt: RestoreAttemptNumber) -> String {
    format!("{}-{}", claim_id.as_str(), attempt.get())
}

fn attempt_nonce(
    base: DispatchNonce,
    attempt: RestoreAttemptNumber,
) -> Result<DispatchNonce, RestorationError> {
    let offset = u64::from(attempt.get() - 1);
    let value = base
        .get()
        .checked_add(offset)
        .ok_or(RestorationError::NonceOverflow)?;
    DispatchNonce::try_from(value).map_err(|_| RestorationError::NonceOverflow)
}

fn typed_id<T>(prefix: &str, suffix: &str) -> Result<T, RestorationError>
where
    T: TryFrom<String>,
{
    T::try_from(format!("{prefix}-{suffix}")).map_err(|_| RestorationError::Identifier)
}

fn release_attempt_lease(
    attempt: &RestoreAttempt,
    leases: &mut LeaseManager,
    now: MonotonicMs,
) -> Result<(), RestorationError> {
    match leases.release(&attempt.request.client_id, &attempt.request.lease_id, now) {
        Ok(_) | Err(LeaseManagerError::UnknownLease) => Ok(()),
        Err(error) => Err(error.into()),
    }
}
