// SPDX-License-Identifier: GPL-2.0-only

//! Trigger and claim schedule state for production restoration.

use super::{
    RestorationScheduleError, RestorationScheduler, ScheduledClaim, ScheduledTrigger,
    ScheduledTriggerPurpose,
};
use hfx_bridge::AuthorizedSession;
use hfx_core::{RestoreRecord, RestoreTrigger};
use hfx_domain::{
    GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs, ReceiverId, RestoreClaimId,
    RestoreDeferReason, RestoreTriggerId, RestoreTriggerKind, SequenceNumber,
};
use hfx_runtime::{
    RESTORATION_AUTHORITY_WINDOW_MS, RESTORATION_CLAIMS_PER_TICK, RESTORATION_LEASE_DURATION_MS,
    RESTORATION_MAX_PENDING_CLAIMS, RESTORATION_MAX_PENDING_TRIGGERS,
    RESTORATION_RETRY_INTERVAL_MS,
};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};

impl RestorationScheduler {
    /// Creates the canonical production scheduler from generated bounds.
    ///
    /// # Errors
    ///
    /// Rejects a missing internal authority in enabled mode, unexpected
    /// authority in disabled mode, or invalid generated numeric bounds.
    pub fn production(
        enabled: bool,
        session: Option<AuthorizedSession>,
        process_nonce: [u8; 32],
    ) -> Result<Self, RestorationScheduleError> {
        let lease_duration_ms = LeaseDurationMs::try_from(RESTORATION_LEASE_DURATION_MS)
            .map_err(|_| RestorationScheduleError::InvalidConfiguration)?;
        Self::new(
            enabled,
            session,
            process_nonce,
            lease_duration_ms,
            RESTORATION_AUTHORITY_WINDOW_MS,
            RESTORATION_RETRY_INTERVAL_MS,
            RESTORATION_CLAIMS_PER_TICK,
            RESTORATION_MAX_PENDING_TRIGGERS,
            RESTORATION_MAX_PENDING_CLAIMS,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn new(
        enabled: bool,
        session: Option<AuthorizedSession>,
        process_nonce: [u8; 32],
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
        retry_interval_ms: u64,
        claims_per_tick: usize,
        max_triggers: usize,
        max_claims: usize,
    ) -> Result<Self, RestorationScheduleError> {
        if enabled != session.is_some()
            || authority_window_ms <= u64::from(lease_duration_ms.get())
            || retry_interval_ms == 0
            || claims_per_tick == 0
            || max_triggers == 0
            || max_claims < max_triggers
        {
            return Err(RestorationScheduleError::InvalidConfiguration);
        }
        Ok(Self {
            enabled,
            session,
            process_nonce,
            lease_duration_ms,
            authority_window_ms,
            retry_interval_ms,
            claims_per_tick,
            max_triggers,
            max_claims,
            configured_receivers: BTreeSet::new(),
            ready_devices: BTreeMap::new(),
            return_epochs: BTreeMap::new(),
            triggers: BTreeMap::new(),
            claims: BTreeMap::new(),
        })
    }

    #[must_use]
    pub const fn is_enabled(&self) -> bool {
        self.enabled
    }

    /// Enqueues one process-scoped service-start trigger.
    ///
    /// # Errors
    ///
    /// Returns a trigger-capacity or generated-identity failure.
    pub fn schedule_service_start(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule(
            RestoreTriggerKind::ServiceStart,
            receiver_id,
            generation_id,
            None,
            None,
        )
    }

    /// Enqueues one physical receiver-generation trigger.
    ///
    /// # Errors
    ///
    /// Returns a trigger-capacity or generated-identity failure.
    pub fn schedule_receiver_generation(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    ) -> Result<(), RestorationScheduleError> {
        self.clear_readiness(&receiver_id, generation_id);
        self.schedule(
            RestoreTriggerKind::ReceiverGeneration,
            receiver_id,
            generation_id,
            None,
            None,
        )
    }

    /// Invalidates pre-suspend readiness and parks this generation's claims.
    /// Fresh device input observed after this boundary remains eligible when
    /// the matching resume trigger arrives.
    ///
    /// # Errors
    ///
    /// Returns a generated-time failure.
    pub fn schedule_system_suspend(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Result<(), RestorationScheduleError> {
        self.clear_readiness(receiver_id, generation_id);
        self.park(receiver_id, generation_id, None)?;
        Ok(())
    }

    /// Enqueues one exact system-resume trigger while retaining readiness
    /// observed after the matching suspend boundary.
    ///
    /// # Errors
    ///
    /// Returns a trigger-capacity or generated-identity failure.
    pub fn schedule_system_resume(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        sequence: SequenceNumber,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule(
            RestoreTriggerKind::SystemResume,
            receiver_id,
            generation_id,
            None,
            Some(sequence),
        )
    }

    /// Atomically installs the complete bounded retry envelope for one exact
    /// endpoint return epoch. A newer epoch supersedes only the same endpoint;
    /// equal or stale epochs are idempotent.
    ///
    /// # Errors
    ///
    /// Returns before changing scheduler state when the retry envelope is
    /// malformed, overflows monotonic time, or cannot fit within capacity.
    #[allow(clippy::too_many_arguments)]
    pub fn schedule_device_return_epoch_at(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        device_id: LogicalDeviceId,
        epoch_sequence: SequenceNumber,
        opened_at: MonotonicMs,
        retry_offsets_ms: &[u64],
    ) -> Result<(), RestorationScheduleError> {
        if !self.enabled {
            return Ok(());
        }
        if retry_offsets_ms.len() < 2
            || retry_offsets_ms.len() > 32
            || retry_offsets_ms.first() != Some(&0)
            || !retry_offsets_ms.windows(2).all(|pair| pair[0] < pair[1])
            || retry_offsets_ms
                .last()
                .is_none_or(|offset| *offset > 30_000)
        {
            return Err(RestorationScheduleError::InvalidConfiguration);
        }
        let readiness = (receiver_id, generation_id, device_id);
        if self
            .return_epochs
            .get(&readiness)
            .is_some_and(|current| epoch_sequence <= *current)
        {
            return Ok(());
        }

        let due = retry_offsets_ms
            .iter()
            .map(|offset| due_after(opened_at, *offset))
            .collect::<Result<Vec<_>, _>>()?;
        let replaced = self
            .triggers
            .values()
            .filter(|scheduled| {
                scheduled.trigger.receiver_id == readiness.0
                    && scheduled.trigger.generation_id == generation_id
                    && scheduled.trigger.kind == RestoreTriggerKind::DeviceReturn
                    && scheduled.trigger.target_device_id.as_ref() == Some(&readiness.2)
            })
            .count();
        let remaining = self.triggers.len().saturating_sub(replaced);
        if remaining
            .checked_add(due.len())
            .is_none_or(|total| total > self.max_triggers)
        {
            return Err(RestorationScheduleError::TriggerCapacity);
        }

        let staged = due
            .iter()
            .enumerate()
            .map(|(attempt, due)| {
                let attempt = u8::try_from(attempt)
                    .map_err(|_| RestorationScheduleError::InvalidConfiguration)?;
                let purpose = ScheduledTriggerPurpose::ReturnAttempt(attempt);
                let trigger_id = trigger_id(
                    RestoreTriggerKind::DeviceReturn,
                    &readiness.0,
                    generation_id,
                    Some(&readiness.2),
                    Some(epoch_sequence),
                    None,
                    purpose,
                )?;
                Ok((
                    trigger_id.clone(),
                    ScheduledTrigger {
                        trigger: RestoreTrigger {
                            trigger_id,
                            kind: RestoreTriggerKind::DeviceReturn,
                            receiver_id: readiness.0.clone(),
                            generation_id,
                            target_device_id: Some(readiness.2.clone()),
                        },
                        due: *due,
                        sequence: Some(epoch_sequence),
                        purpose,
                    },
                ))
            })
            .collect::<Result<Vec<_>, RestorationScheduleError>>()?;

        self.remove_device_return_triggers(&readiness.0, generation_id, &readiness.2, None);
        for (trigger_id, trigger) in staged {
            self.triggers.insert(trigger_id, trigger);
        }
        let first_due = due[0];
        self.ready_devices.insert(readiness.clone(), first_due);
        self.return_epochs.insert(readiness.clone(), epoch_sequence);
        self.wake(&readiness.0, generation_id, Some(&readiness.2), first_due);
        Ok(())
    }

    /// Pulls the earliest still-pending attempt for one active return epoch to
    /// the new evidence timestamp. It never postpones an attempt, creates a new
    /// attempt, or changes another endpoint.
    ///
    /// # Errors
    ///
    /// Returns an identifier error only if the selected pending trigger
    /// disappears from the scheduler before its due time can be updated.
    pub fn accelerate_device_return_epoch_at(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
        epoch_sequence: SequenceNumber,
        evidence_at: MonotonicMs,
    ) -> Result<(), RestorationScheduleError> {
        if !self.enabled {
            return Ok(());
        }
        let readiness = (receiver_id.clone(), generation_id, device_id.clone());
        if self.return_epochs.get(&readiness) != Some(&epoch_sequence) {
            return Ok(());
        }
        let candidate = self
            .triggers
            .iter()
            .filter(|(_, scheduled)| {
                scheduled.trigger.receiver_id == *receiver_id
                    && scheduled.trigger.generation_id == generation_id
                    && scheduled.trigger.kind == RestoreTriggerKind::DeviceReturn
                    && scheduled.trigger.target_device_id.as_ref() == Some(device_id)
                    && scheduled.sequence == Some(epoch_sequence)
                    && matches!(scheduled.purpose, ScheduledTriggerPurpose::ReturnAttempt(_))
                    && scheduled.due > evidence_at
            })
            .min_by_key(|(trigger_id, scheduled)| (scheduled.due, *trigger_id))
            .map(|(trigger_id, _)| trigger_id.clone());
        if let Some(trigger_id) = candidate {
            self.triggers
                .get_mut(&trigger_id)
                .ok_or(RestorationScheduleError::Identifier)?
                .due = evidence_at;
        }
        self.ready_devices
            .entry(readiness)
            .and_modify(|due| *due = (*due).min(evidence_at))
            .or_insert(evidence_at);
        self.wake_earlier(receiver_id, generation_id, device_id, evidence_at);
        Ok(())
    }

    pub fn retire_generation(&mut self, receiver_id: &ReceiverId, generation_id: GenerationId) {
        self.triggers.retain(|_, scheduled| {
            scheduled.trigger.receiver_id != *receiver_id
                || scheduled.trigger.generation_id != generation_id
        });
        self.claims.retain(|_, scheduled| {
            scheduled.receiver_id != *receiver_id || scheduled.generation_id != generation_id
        });
        self.ready_devices.retain(|(candidate, generation, _), _| {
            candidate != receiver_id || *generation != generation_id
        });
        self.return_epochs.retain(|(candidate, generation, _), _| {
            candidate != receiver_id || *generation != generation_id
        });
    }

    fn schedule(
        &mut self,
        kind: RestoreTriggerKind,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        target_device_id: Option<LogicalDeviceId>,
        sequence: Option<SequenceNumber>,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule_at(
            kind,
            receiver_id,
            generation_id,
            target_device_id,
            sequence,
            immediate_due()?,
        )
    }

    fn schedule_at(
        &mut self,
        kind: RestoreTriggerKind,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        target_device_id: Option<LogicalDeviceId>,
        sequence: Option<SequenceNumber>,
        due: MonotonicMs,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule_at_with_purpose(
            kind,
            receiver_id,
            generation_id,
            target_device_id,
            sequence,
            due,
            ScheduledTriggerPurpose::Primary,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn schedule_at_with_purpose(
        &mut self,
        kind: RestoreTriggerKind,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        target_device_id: Option<LogicalDeviceId>,
        sequence: Option<SequenceNumber>,
        due: MonotonicMs,
        purpose: ScheduledTriggerPurpose,
    ) -> Result<(), RestorationScheduleError> {
        if !self.enabled {
            return Ok(());
        }
        if matches!(purpose, ScheduledTriggerPurpose::ReturnAttempt(_))
            && kind != RestoreTriggerKind::DeviceReturn
        {
            return Err(RestorationScheduleError::InvalidConfiguration);
        }
        if is_broad_trigger(kind) {
            let woke = if readiness_gated_trigger(kind) {
                self.park(&receiver_id, generation_id, None)?;
                self.wake_ready_devices(&receiver_id, generation_id)
            } else {
                self.wake(&receiver_id, generation_id, None, immediate_due()?)
            };
            self.triggers.retain(|_, scheduled| {
                scheduled.trigger.receiver_id != receiver_id
                    || scheduled.trigger.generation_id != generation_id
                    || scheduled.trigger.kind != RestoreTriggerKind::DeviceReturn
                    || matches!(scheduled.purpose, ScheduledTriggerPurpose::ReturnAttempt(_))
            });
            if woke > 0 || self.has_pending_broad_trigger(&receiver_id, generation_id) {
                return Ok(());
            }
        } else if kind == RestoreTriggerKind::DeviceReturn
            && purpose == ScheduledTriggerPurpose::Primary
        {
            self.wake(&receiver_id, generation_id, target_device_id.as_ref(), due);
            if self.has_pending_broad_trigger(&receiver_id, generation_id) {
                return Ok(());
            }
            let same_scope = self.triggers.iter().find_map(|(trigger_id, scheduled)| {
                (scheduled.trigger.receiver_id == receiver_id
                    && scheduled.trigger.generation_id == generation_id
                    && scheduled.trigger.kind == RestoreTriggerKind::DeviceReturn
                    && scheduled.trigger.target_device_id == target_device_id
                    && scheduled.purpose == ScheduledTriggerPurpose::Primary)
                    .then(|| (trigger_id.clone(), scheduled.sequence))
            });
            if let Some((existing_id, existing_sequence)) = same_scope {
                if !sequence_is_newer(sequence, existing_sequence) {
                    return Ok(());
                }
                self.triggers.remove(&existing_id);
            }
        }
        let trigger_id = trigger_id(
            kind,
            &receiver_id,
            generation_id,
            target_device_id.as_ref(),
            sequence,
            (kind == RestoreTriggerKind::ServiceStart).then_some(&self.process_nonce),
            purpose,
        )?;
        if self.triggers.contains_key(&trigger_id) {
            return Ok(());
        }
        if self.triggers.len() >= self.max_triggers {
            return Err(RestorationScheduleError::TriggerCapacity);
        }
        self.triggers.insert(
            trigger_id.clone(),
            ScheduledTrigger {
                trigger: RestoreTrigger {
                    trigger_id,
                    kind,
                    receiver_id,
                    generation_id,
                    target_device_id,
                },
                due,
                sequence,
                purpose,
            },
        );
        Ok(())
    }

    pub(super) fn schedule_record(
        &mut self,
        record: RestoreRecord,
        due: MonotonicMs,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule_record_with_defer(record, due, 0)
    }

    pub(super) fn schedule_trigger_record(
        &mut self,
        record: RestoreRecord,
        trigger_kind: RestoreTriggerKind,
        now: MonotonicMs,
    ) -> Result<(), RestorationScheduleError> {
        let readiness_gated =
            readiness_gated_trigger(trigger_kind) || readiness_gated_trigger(record.trigger_kind);
        let readiness = (
            record.receiver_id.clone(),
            record.generation_id,
            record.device_id.clone(),
        );
        let due = if readiness_gated {
            self.ready_devices
                .get(&readiness)
                .map_or_else(parked_due, |ready_at| Ok(now.max(*ready_at)))?
        } else {
            now
        };
        self.schedule_record(record, due)
    }

    pub(super) fn schedule_record_with_defer(
        &mut self,
        record: RestoreRecord,
        due: MonotonicMs,
        defer_count: u8,
    ) -> Result<(), RestorationScheduleError> {
        if record.status.is_terminal() {
            self.claims.remove(&record.claim_id);
            return Ok(());
        }
        if !self.claims.contains_key(&record.claim_id) && self.claims.len() >= self.max_claims {
            return Err(RestorationScheduleError::ClaimCapacity);
        }
        self.claims.insert(
            record.claim_id,
            ScheduledClaim {
                receiver_id: record.receiver_id,
                generation_id: record.generation_id,
                device_id: record.device_id,
                revision: record.revision,
                due,
                defer_count,
            },
        );
        Ok(())
    }

    fn wake(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: Option<&LogicalDeviceId>,
        due: MonotonicMs,
    ) -> usize {
        let mut woken = 0;
        for claim in self.claims.values_mut().filter(|claim| {
            claim.receiver_id == *receiver_id
                && claim.generation_id == generation_id
                && device_id.is_none_or(|device_id| claim.device_id == *device_id)
        }) {
            claim.due = due;
            woken += 1;
        }
        woken
    }

    fn wake_earlier(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
        due: MonotonicMs,
    ) -> usize {
        let mut woken = 0;
        for claim in self.claims.values_mut().filter(|claim| {
            claim.receiver_id == *receiver_id
                && claim.generation_id == generation_id
                && claim.device_id == *device_id
        }) {
            claim.due = claim.due.min(due);
            woken += 1;
        }
        woken
    }

    fn wake_ready_devices(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> usize {
        let devices = self
            .ready_devices
            .iter()
            .filter(|((candidate, generation, _), _)| {
                candidate == receiver_id && *generation == generation_id
            })
            .map(|((_, _, device_id), due)| (device_id.clone(), *due))
            .collect::<Vec<_>>();
        devices
            .iter()
            .map(|(device_id, due)| self.wake(receiver_id, generation_id, Some(device_id), *due))
            .sum()
    }

    fn park(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: Option<&LogicalDeviceId>,
    ) -> Result<usize, RestorationScheduleError> {
        let due = parked_due()?;
        let mut parked = 0;
        for claim in self.claims.values_mut().filter(|claim| {
            claim.receiver_id == *receiver_id
                && claim.generation_id == generation_id
                && device_id.is_none_or(|device_id| claim.device_id == *device_id)
        }) {
            claim.due = due;
            parked += 1;
        }
        Ok(parked)
    }

    fn has_pending_broad_trigger(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> bool {
        self.triggers.values().any(|scheduled| {
            scheduled.trigger.receiver_id == *receiver_id
                && scheduled.trigger.generation_id == generation_id
                && is_broad_trigger(scheduled.trigger.kind)
        })
    }

    fn clear_readiness(&mut self, receiver_id: &ReceiverId, generation_id: GenerationId) {
        self.ready_devices.retain(|(candidate, generation, _), _| {
            candidate != receiver_id || *generation != generation_id
        });
        self.return_epochs.retain(|(candidate, generation, _), _| {
            candidate != receiver_id || *generation != generation_id
        });
        self.triggers.retain(|_, scheduled| {
            scheduled.trigger.receiver_id != *receiver_id
                || scheduled.trigger.generation_id != generation_id
                || scheduled.trigger.kind != RestoreTriggerKind::DeviceReturn
        });
    }

    pub(super) fn remove_device_return_triggers(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
        purpose: Option<ScheduledTriggerPurpose>,
    ) {
        self.triggers.retain(|_, scheduled| {
            scheduled.trigger.receiver_id != *receiver_id
                || scheduled.trigger.generation_id != generation_id
                || scheduled.trigger.kind != RestoreTriggerKind::DeviceReturn
                || scheduled.trigger.target_device_id.as_ref() != Some(device_id)
                || purpose.is_some_and(|purpose| scheduled.purpose != purpose)
        });
    }

    pub(super) fn consume_device_return_epoch(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
    ) {
        self.remove_device_return_triggers(receiver_id, generation_id, device_id, None);
        let key = (receiver_id.clone(), generation_id, device_id.clone());
        self.ready_devices.remove(&key);
        // Retain the generation-bound sequence watermark so duplicate or stale
        // readiness evidence cannot reopen an already consumed retry envelope.
    }
}

const fn is_broad_trigger(kind: RestoreTriggerKind) -> bool {
    matches!(
        kind,
        RestoreTriggerKind::ServiceStart
            | RestoreTriggerKind::ReceiverGeneration
            | RestoreTriggerKind::SystemResume
    )
}

const fn readiness_gated_trigger(kind: RestoreTriggerKind) -> bool {
    matches!(
        kind,
        RestoreTriggerKind::ReceiverGeneration | RestoreTriggerKind::SystemResume
    )
}

fn parked_due() -> Result<MonotonicMs, RestorationScheduleError> {
    MonotonicMs::try_from(u64::MAX).map_err(|_| RestorationScheduleError::TimeOverflow)
}

fn immediate_due() -> Result<MonotonicMs, RestorationScheduleError> {
    MonotonicMs::try_from(0_u64).map_err(|_| RestorationScheduleError::TimeOverflow)
}

pub(super) fn due_trigger_id(
    triggers: &BTreeMap<RestoreTriggerId, ScheduledTrigger>,
    now: MonotonicMs,
) -> Option<RestoreTriggerId> {
    triggers
        .iter()
        .filter(|(_, scheduled)| scheduled.due <= now)
        .min_by_key(|(trigger_id, scheduled)| (scheduled.due, *trigger_id))
        .map(|(trigger_id, _)| trigger_id.clone())
}

pub(super) fn due_claim_id(
    claims: &BTreeMap<RestoreClaimId, ScheduledClaim>,
    now: MonotonicMs,
) -> Option<RestoreClaimId> {
    claims
        .iter()
        .filter(|(_, scheduled)| scheduled.due <= now)
        .min_by_key(|(claim_id, scheduled)| (scheduled.due, scheduled.revision, *claim_id))
        .map(|(claim_id, _)| claim_id.clone())
}

pub(super) fn due_after(
    now: MonotonicMs,
    delay_ms: u64,
) -> Result<MonotonicMs, RestorationScheduleError> {
    now.get()
        .checked_add(delay_ms)
        .ok_or(RestorationScheduleError::TimeOverflow)
        .and_then(|value| {
            MonotonicMs::try_from(value).map_err(|_| RestorationScheduleError::TimeOverflow)
        })
}

pub(super) fn deferred_due(
    now: MonotonicMs,
    reason: RestoreDeferReason,
    retry_interval_ms: u64,
    defer_count: u8,
) -> Result<MonotonicMs, RestorationScheduleError> {
    if matches!(
        reason,
        RestoreDeferReason::DeviceSleeping
            | RestoreDeferReason::DeviceUnavailable
            | RestoreDeferReason::DeviceUnknown
    ) {
        return parked_due();
    }
    let exponent = u32::from(defer_count.saturating_sub(1).min(6));
    let multiplier = 1_u64
        .checked_shl(exponent)
        .ok_or(RestorationScheduleError::TimeOverflow)?;
    let delay = retry_interval_ms
        .checked_mul(multiplier)
        .ok_or(RestorationScheduleError::TimeOverflow)?;
    due_after(now, delay)
}

fn sequence_is_newer(candidate: Option<SequenceNumber>, existing: Option<SequenceNumber>) -> bool {
    match (candidate, existing) {
        (Some(candidate), Some(existing)) => candidate > existing,
        (Some(_), None) => true,
        _ => false,
    }
}

fn trigger_id(
    kind: RestoreTriggerKind,
    receiver_id: &ReceiverId,
    generation_id: GenerationId,
    target_device_id: Option<&LogicalDeviceId>,
    sequence: Option<SequenceNumber>,
    process_nonce: Option<&[u8; 32]>,
    purpose: ScheduledTriggerPurpose,
) -> Result<RestoreTriggerId, RestorationScheduleError> {
    let mut digest = Sha256::new();
    digest.update(b"hyperflux-restoration-trigger-v1\0");
    digest_field(&mut digest, kind.as_str().as_bytes())?;
    digest_field(&mut digest, receiver_id.as_str().as_bytes())?;
    digest.update(generation_id.get().to_be_bytes());
    if let Some(device_id) = target_device_id {
        digest_field(&mut digest, device_id.as_str().as_bytes())?;
    } else {
        digest_field(&mut digest, &[])?;
    }
    digest.update(sequence.map_or(0, SequenceNumber::get).to_be_bytes());
    if let Some(nonce) = process_nonce {
        digest.update(nonce);
    }
    match purpose {
        ScheduledTriggerPurpose::Primary => {}
        ScheduledTriggerPurpose::ReturnAttempt(attempt) => {
            digest_field(&mut digest, b"device-return-attempt")?;
            digest.update([attempt]);
        }
    }
    let encoded = digest
        .finalize()
        .iter()
        .fold(String::with_capacity(64), |mut output, byte| {
            use std::fmt::Write as _;
            write!(output, "{byte:02x}").expect("writing to a string cannot fail");
            output
        });
    RestoreTriggerId::try_from(format!("restore-{}-{encoded}", kind.as_str()))
        .map_err(|_| RestorationScheduleError::Identifier)
}

fn digest_field(digest: &mut Sha256, value: &[u8]) -> Result<(), RestorationScheduleError> {
    let length = u64::try_from(value.len()).map_err(|_| RestorationScheduleError::Identifier)?;
    digest.update(length.to_be_bytes());
    digest.update(value);
    Ok(())
}
