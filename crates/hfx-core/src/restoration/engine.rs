// SPDX-License-Identifier: GPL-2.0-only

use super::completion::{
    completion_from_terminal, defer, finish_from_transaction, finish_record,
    handle_submission_error, invalidate, readiness_defer_reason, reconcile_terminal,
    release_attempt_lease, unknown_completion,
};
use super::intent::load_entries;
use super::planning::profile_matches;
use super::records::{
    active_attempt, attempt_suffix, enforce_prior_outcome_barrier, lighting_resource, load_record,
    load_records, next_attempt_number, persist_record, typed_id,
};
use super::{
    PersistenceOperation, RestorationAuthority, RestorationCoordinator, RestorationError,
    RestoreAdvanceResult, transition_record, validate_schema,
};
use crate::{
    BoundedEventLog, DeviceStateAuthority, DispatchResult, EventSink, LeaseManager,
    PersistedStableEntry, PersistedStableIntent, PersistenceStore, ProfileRegistry,
    ReceiverTransport, RestoreAttempt, RestoreRecord, RestoreRecordStatus, SessionAuthority,
    SubmissionBinding, TransactionCoordinator, TransportDispatch, TransportReconciliation,
    canonical_request_digest,
};
use hfx_domain::{
    ColorChannel, FrameIndex, LeaseId, MonotonicMs, ProtocolErrorKind, ReceiverId, RequestId,
    RestoreAttemptNumber, RestoreClaimId, RestoreDeferReason, RestoreInvalidationReason,
    TransactionClass, TransactionId,
};
use hfx_protocol::{
    DeviceProfileBinding, LeaseRequest, LeaseResult, LightingFrame, RgbColor, TransactionRequest,
    TransactionResult,
};

impl RestorationCoordinator {
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
        dispatch_nonce: authority.submission.dispatch_nonce,
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

pub(super) fn dispatch_from_attempt(attempt: &RestoreAttempt) -> TransportDispatch {
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
