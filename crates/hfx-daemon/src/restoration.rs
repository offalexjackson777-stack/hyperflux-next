// SPDX-License-Identifier: GPL-2.0-only

//! Bounded, event-driven production restoration scheduling.

use crate::ProductionBackend;
use hfx_bridge::{AuthorizedSession, CoreBridgeBackend, RestorationRuntime, SessionRegistry};
use hfx_core::{
    Clock, EventSink, ReceiverTransport, RestorationError, RestoreAdvanceResult, RestorePlanResult,
    RestoreRecord, RestoreTrigger, WallClock,
};
use hfx_domain::{
    GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs, PersistenceRevision, ReceiverId,
    RestoreClaimId, RestoreDeferReason, RestoreTriggerId, RestoreTriggerKind, SequenceNumber,
};
use hfx_runtime::{
    RESTORATION_AUTHORITY_WINDOW_MS, RESTORATION_CLAIMS_PER_TICK, RESTORATION_LEASE_DURATION_MS,
    RESTORATION_MAX_PENDING_CLAIMS, RESTORATION_MAX_PENDING_TRIGGERS,
    RESTORATION_RETRY_INTERVAL_MS,
};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct RestorationTickReport {
    pub triggers_planned: usize,
    pub claims_recovered: usize,
    pub claims_advanced: usize,
    pub claims_dispatched: usize,
    pub claims_deferred: usize,
    pub claims_terminal: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RestorationScheduleError {
    InvalidConfiguration,
    TriggerCapacity,
    ClaimCapacity,
    TimeOverflow,
    Identifier,
    Restoration(RestorationError),
}

impl fmt::Display for RestorationScheduleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidConfiguration => "restoration scheduler configuration is invalid",
            Self::TriggerCapacity => "restoration trigger capacity is exhausted",
            Self::ClaimCapacity => "restoration claim capacity is exhausted",
            Self::TimeOverflow => "restoration scheduler time cannot advance",
            Self::Identifier => "restoration lifecycle identity is invalid",
            Self::Restoration(_) => "durable restoration coordination failed",
        })
    }
}

impl std::error::Error for RestorationScheduleError {}

impl From<RestorationError> for RestorationScheduleError {
    fn from(error: RestorationError) -> Self {
        Self::Restoration(error)
    }
}

#[derive(Clone, Debug)]
struct ScheduledTrigger {
    trigger: RestoreTrigger,
    due: MonotonicMs,
    sequence: Option<SequenceNumber>,
}

#[derive(Clone, Debug)]
struct ScheduledClaim {
    receiver_id: ReceiverId,
    generation_id: GenerationId,
    device_id: LogicalDeviceId,
    revision: PersistenceRevision,
    due: MonotonicMs,
    defer_count: u8,
}

trait RestorationController {
    fn now(&self) -> MonotonicMs;
    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError>;
    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError>;
    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError>;
    fn advance_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        session: &AuthorizedSession,
        sessions: &SessionRegistry,
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError>;
    fn dispatch_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError>;
}

impl<C, W, T, R, S> RestorationController for CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
    S: EventSink,
{
    fn now(&self) -> MonotonicMs {
        self.now()
    }

    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        self.set_restoration_enabled(receiver_id, enabled)
    }

    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        self.pending_restoration_records(receiver_id)
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        self.plan_restoration(trigger)
    }

    fn advance_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        session: &AuthorizedSession,
        sessions: &SessionRegistry,
        lease_duration_ms: LeaseDurationMs,
        authority_window_ms: u64,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        self.advance_restoration(
            claim_id,
            session,
            sessions,
            lease_duration_ms,
            authority_window_ms,
        )
    }

    fn dispatch_claim(
        &mut self,
        claim_id: &RestoreClaimId,
        sessions: &SessionRegistry,
    ) -> Result<RestoreRecord, RestorationError> {
        self.dispatch_restoration(claim_id, sessions)
    }
}

/// Owns only volatile scheduling. Every semantic state transition and every
/// possible hardware write remains inside the durable coordinator invoked on
/// the bridge actor thread.
pub struct RestorationScheduler {
    enabled: bool,
    session: Option<AuthorizedSession>,
    process_nonce: [u8; 32],
    lease_duration_ms: LeaseDurationMs,
    authority_window_ms: u64,
    retry_interval_ms: u64,
    claims_per_tick: usize,
    max_triggers: usize,
    max_claims: usize,
    configured_receivers: BTreeSet<ReceiverId>,
    triggers: BTreeMap<RestoreTriggerId, ScheduledTrigger>,
    claims: BTreeMap<RestoreClaimId, ScheduledClaim>,
}

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
    fn new(
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
        self.schedule(
            RestoreTriggerKind::ReceiverGeneration,
            receiver_id,
            generation_id,
            None,
            None,
        )
    }

    /// Wakes matching claims and enqueues one exact system-resume trigger.
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

    /// Wakes one device's claims and enqueues its exact return trigger.
    ///
    /// # Errors
    ///
    /// Returns a trigger-capacity or generated-identity failure.
    pub fn schedule_device_return(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        device_id: LogicalDeviceId,
        sequence: SequenceNumber,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule(
            RestoreTriggerKind::DeviceReturn,
            receiver_id,
            generation_id,
            Some(device_id),
            Some(sequence),
        )
    }

    pub fn retire_generation(&mut self, receiver_id: &ReceiverId, generation_id: GenerationId) {
        self.triggers.retain(|_, scheduled| {
            scheduled.trigger.receiver_id != *receiver_id
                || scheduled.trigger.generation_id != generation_id
        });
        self.claims.retain(|_, scheduled| {
            scheduled.receiver_id != *receiver_id || scheduled.generation_id != generation_id
        });
    }

    /// Advances at most the generated number of due claims and one due trigger.
    /// Deferred claims are retried after a bounded backoff or immediately when
    /// matching lifecycle evidence wakes them.
    ///
    /// # Errors
    ///
    /// Returns a capacity, time, identity, persistence, authority, transaction,
    /// or transport failure without suppressing uncertain side effects.
    pub fn tick(
        &mut self,
        backend: &mut ProductionBackend,
        sessions: &SessionRegistry,
    ) -> Result<RestorationTickReport, RestorationScheduleError> {
        if !self.enabled {
            return Ok(RestorationTickReport::default());
        }
        self.tick_with(backend, sessions)
    }

    fn tick_with<C: RestorationController>(
        &mut self,
        controller: &mut C,
        sessions: &SessionRegistry,
    ) -> Result<RestorationTickReport, RestorationScheduleError> {
        let now = controller.now();
        let mut report = RestorationTickReport::default();
        self.process_due_trigger(controller, now, &mut report)?;

        for _ in 0..self.claims_per_tick {
            if !self.process_due_claim(controller, sessions, now, &mut report)? {
                break;
            }
        }
        Ok(report)
    }

    fn process_due_trigger<C: RestorationController>(
        &mut self,
        controller: &mut C,
        now: MonotonicMs,
        report: &mut RestorationTickReport,
    ) -> Result<(), RestorationScheduleError> {
        if let Some(trigger_id) = due_trigger_id(&self.triggers, now) {
            let mut scheduled = self
                .triggers
                .remove(&trigger_id)
                .ok_or(RestorationScheduleError::Identifier)?;
            let newly_configured = !self
                .configured_receivers
                .contains(&scheduled.trigger.receiver_id);
            if newly_configured {
                controller.set_enabled(&scheduled.trigger.receiver_id, true)?;
                self.configured_receivers
                    .insert(scheduled.trigger.receiver_id.clone());
            }
            let pending = newly_configured
                .then(|| controller.pending_records(&scheduled.trigger.receiver_id))
                .transpose()?
                .unwrap_or_default();
            if pending.is_empty() {
                match controller.plan_restore(&scheduled.trigger) {
                    Ok(RestorePlanResult::Planned(records)) => {
                        for record in records {
                            self.schedule_record(record, now)?;
                        }
                        report.triggers_planned += 1;
                    }
                    Ok(RestorePlanResult::NoStableIntents) => {
                        report.triggers_planned += 1;
                    }
                    Ok(RestorePlanResult::Disabled) => {
                        return Err(RestorationError::RuntimeDisabled.into());
                    }
                    Err(
                        RestorationError::PriorClaimUnresolved
                        | RestorationError::PriorOutcomeUncertain,
                    ) => {
                        let recovered =
                            controller.pending_records(&scheduled.trigger.receiver_id)?;
                        report.claims_recovered += recovered.len();
                        for record in recovered {
                            self.schedule_record(record, now)?;
                        }
                        scheduled.due = due_after(now, self.retry_interval_ms)?;
                        self.triggers.insert(trigger_id, scheduled);
                    }
                    Err(error) => return Err(error.into()),
                }
            } else {
                report.claims_recovered += pending.len();
                for record in pending {
                    self.schedule_record(record, now)?;
                }
            }
        }
        Ok(())
    }

    fn process_due_claim<C: RestorationController>(
        &mut self,
        controller: &mut C,
        sessions: &SessionRegistry,
        now: MonotonicMs,
        report: &mut RestorationTickReport,
    ) -> Result<bool, RestorationScheduleError> {
        let Some(claim_id) = due_claim_id(&self.claims, now) else {
            return Ok(false);
        };
        let scheduled = self
            .claims
            .remove(&claim_id)
            .ok_or(RestorationScheduleError::Identifier)?;
        let session = self
            .session
            .as_ref()
            .ok_or(RestorationScheduleError::InvalidConfiguration)?;
        let advance = match controller.advance_claim(
            &claim_id,
            session,
            sessions,
            self.lease_duration_ms,
            self.authority_window_ms,
        ) {
            Ok(advance) => advance,
            Err(
                RestorationError::PriorClaimUnresolved | RestorationError::PriorOutcomeUncertain,
            ) => {
                let recovered = controller.pending_records(&scheduled.receiver_id)?;
                report.claims_recovered += recovered.len();
                if recovered.is_empty() {
                    self.claims.insert(
                        claim_id,
                        ScheduledClaim {
                            due: due_after(now, self.retry_interval_ms)?,
                            ..scheduled
                        },
                    );
                } else {
                    let retry_due = due_after(now, self.retry_interval_ms)?;
                    for record in recovered {
                        self.schedule_record(record, retry_due)?;
                    }
                }
                return Ok(true);
            }
            Err(error) => return Err(error.into()),
        };
        report.claims_advanced += 1;
        match advance {
            RestoreAdvanceResult::Deferred(record) => {
                let reason = match &record.status {
                    hfx_core::RestoreRecordStatus::Deferred(deferred) => deferred.reason,
                    _ => return Err(RestorationScheduleError::Identifier),
                };
                let defer_count = scheduled.defer_count.saturating_add(1);
                let due = deferred_due(now, reason, self.retry_interval_ms, defer_count)?;
                self.schedule_record_with_defer(record, due, defer_count)?;
                report.claims_deferred += 1;
            }
            RestoreAdvanceResult::Queued(record) => {
                let terminal = controller.dispatch_claim(&record.claim_id, sessions)?;
                report.claims_dispatched += 1;
                if terminal.status.is_terminal() {
                    report.claims_terminal += 1;
                } else {
                    self.schedule_record(terminal, due_after(now, self.retry_interval_ms)?)?;
                }
            }
            RestoreAdvanceResult::Terminal(_) => {
                report.claims_terminal += 1;
            }
        }
        Ok(true)
    }

    fn schedule(
        &mut self,
        kind: RestoreTriggerKind,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        target_device_id: Option<LogicalDeviceId>,
        sequence: Option<SequenceNumber>,
    ) -> Result<(), RestorationScheduleError> {
        if !self.enabled {
            return Ok(());
        }
        if is_broad_trigger(kind) {
            let woke = self.wake(&receiver_id, generation_id, None);
            self.triggers.retain(|_, scheduled| {
                scheduled.trigger.receiver_id != receiver_id
                    || scheduled.trigger.generation_id != generation_id
                    || scheduled.trigger.kind != RestoreTriggerKind::DeviceReturn
            });
            if woke > 0 || self.has_pending_broad_trigger(&receiver_id, generation_id) {
                return Ok(());
            }
        } else if kind == RestoreTriggerKind::DeviceReturn {
            let woke = self.wake(&receiver_id, generation_id, target_device_id.as_ref());
            if woke > 0 || self.has_pending_broad_trigger(&receiver_id, generation_id) {
                return Ok(());
            }
            let same_scope = self.triggers.iter().find_map(|(trigger_id, scheduled)| {
                (scheduled.trigger.receiver_id == receiver_id
                    && scheduled.trigger.generation_id == generation_id
                    && scheduled.trigger.kind == RestoreTriggerKind::DeviceReturn
                    && scheduled.trigger.target_device_id == target_device_id)
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
                due: MonotonicMs::try_from(0_u64)
                    .map_err(|_| RestorationScheduleError::TimeOverflow)?,
                sequence,
            },
        );
        Ok(())
    }

    fn schedule_record(
        &mut self,
        record: RestoreRecord,
        due: MonotonicMs,
    ) -> Result<(), RestorationScheduleError> {
        self.schedule_record_with_defer(record, due, 0)
    }

    fn schedule_record_with_defer(
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
    ) -> usize {
        let mut woken = 0;
        for claim in self.claims.values_mut().filter(|claim| {
            claim.receiver_id == *receiver_id
                && claim.generation_id == generation_id
                && device_id.is_none_or(|device_id| claim.device_id == *device_id)
        }) {
            claim.due = MonotonicMs::try_from(0_u64).expect("zero monotonic time is valid");
            woken += 1;
        }
        woken
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
}

const fn is_broad_trigger(kind: RestoreTriggerKind) -> bool {
    matches!(
        kind,
        RestoreTriggerKind::ServiceStart
            | RestoreTriggerKind::ReceiverGeneration
            | RestoreTriggerKind::SystemResume
    )
}

fn due_trigger_id(
    triggers: &BTreeMap<RestoreTriggerId, ScheduledTrigger>,
    now: MonotonicMs,
) -> Option<RestoreTriggerId> {
    triggers
        .iter()
        .filter(|(_, scheduled)| scheduled.due <= now)
        .min_by_key(|(trigger_id, scheduled)| (scheduled.due, *trigger_id))
        .map(|(trigger_id, _)| trigger_id.clone())
}

fn due_claim_id(
    claims: &BTreeMap<RestoreClaimId, ScheduledClaim>,
    now: MonotonicMs,
) -> Option<RestoreClaimId> {
    claims
        .iter()
        .filter(|(_, scheduled)| scheduled.due <= now)
        .min_by_key(|(claim_id, scheduled)| (scheduled.due, scheduled.revision, *claim_id))
        .map(|(claim_id, _)| claim_id.clone())
}

fn due_after(now: MonotonicMs, delay_ms: u64) -> Result<MonotonicMs, RestorationScheduleError> {
    now.get()
        .checked_add(delay_ms)
        .ok_or(RestorationScheduleError::TimeOverflow)
        .and_then(|value| {
            MonotonicMs::try_from(value).map_err(|_| RestorationScheduleError::TimeOverflow)
        })
}

fn deferred_due(
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
        return MonotonicMs::try_from(u64::MAX).map_err(|_| RestorationScheduleError::TimeOverflow);
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ProductionRestoration;
    use hfx_bridge::{
        AtomicFileCommitter, CoreBridgeConfig, DurableRestorationRuntime, FilePersistenceConfig,
        FilePersistenceError, FilePersistenceStore, PersistenceCommitter, RuntimeProfileAuthority,
        SessionIdentityError, SessionIdentitySource,
    };
    use hfx_core::{
        ChildIdentity, CompletedTransaction, EndpointIdentity, EventDelivery, LifecycleLimits,
        ObservationStamp, PersistenceStore, ProfileRegistry, ReceiverLifecycleMachine,
        ReceiverLifecycleRegistry, RestorationCoordinator, RestoreDeferred, RestoreRecordStatus,
        StableCommitOutcome, TransportDispatch, TransportFailure, TransportFailureFacts,
        TransportReceipt, TransportReconciliation, TransportTerminal, canonical_request_digest,
    };
    use hfx_domain::{
        ActivityState, ApplyOutcome, AuthorizationEpoch, ColorChannel, ConnectionMode,
        DeliveredFrameCount, DeviceApplicationState, DeviceKind, EvidenceClaimId,
        EvidenceConfidence, FrameCount, FrameIndex, FreshnessState, IntentRevision,
        LogicalDeviceId, PairingState, PersistenceRevision, PersistenceSchemaVersion, PowerState,
        ProductId, ProjectionRevision, ProtocolVersion, QueueCapacity, ResourceKind,
        RestoreDeferReason, RouteKind, RouteState, SessionId, SideEffectCertainty, SleepState,
        StableLightingMode, StreamEpoch, StreamId, TransactionClass, TransactionState, VendorId,
        WallClockUnixMs,
    };
    use hfx_protocol::{
        DeviceProfileBinding, LightingFrame, ResourceKey, RgbColor, StableLightingIntent,
        TransactionRequest, TransactionTerminal,
    };
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    struct FakeController {
        now: MonotonicMs,
        plans: Vec<Result<RestorePlanResult, RestorationError>>,
        pending: Vec<RestoreRecord>,
        advances: Vec<Result<RestoreAdvanceResult, RestorationError>>,
        dispatches: Vec<Result<RestoreRecord, RestorationError>>,
        policy_calls: usize,
        advance_calls: usize,
        dispatch_calls: usize,
    }

    impl RestorationController for FakeController {
        fn now(&self) -> MonotonicMs {
            self.now
        }

        fn set_enabled(
            &mut self,
            _receiver_id: &ReceiverId,
            _enabled: bool,
        ) -> Result<(), RestorationError> {
            self.policy_calls += 1;
            Ok(())
        }

        fn pending_records(
            &self,
            _receiver_id: &ReceiverId,
        ) -> Result<Vec<RestoreRecord>, RestorationError> {
            Ok(self.pending.clone())
        }

        fn plan_restore(
            &mut self,
            _trigger: &RestoreTrigger,
        ) -> Result<RestorePlanResult, RestorationError> {
            self.plans.remove(0)
        }

        fn advance_claim(
            &mut self,
            _claim_id: &RestoreClaimId,
            _session: &AuthorizedSession,
            _sessions: &SessionRegistry,
            _lease_duration_ms: LeaseDurationMs,
            _authority_window_ms: u64,
        ) -> Result<RestoreAdvanceResult, RestorationError> {
            self.advance_calls += 1;
            self.advances.remove(0)
        }

        fn dispatch_claim(
            &mut self,
            _claim_id: &RestoreClaimId,
            _sessions: &SessionRegistry,
        ) -> Result<RestoreRecord, RestorationError> {
            self.dispatch_calls += 1;
            self.dispatches.remove(0)
        }
    }

    fn id<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test id")
    }

    fn generation(value: u64) -> GenerationId {
        GenerationId::try_from(value).expect("generation")
    }

    fn time(value: u64) -> MonotonicMs {
        MonotonicMs::try_from(value).expect("time")
    }

    fn stamp(sequence: u64) -> ObservationStamp {
        ObservationStamp::new(
            generation(1),
            SequenceNumber::try_from(sequence).expect("sequence"),
            time(sequence),
            EvidenceConfidence::Observed,
            id::<EvidenceClaimId>(&format!("evidence-{sequence}")),
        )
        .expect("observation stamp")
    }

    fn record(claim: &str, status: RestoreRecordStatus) -> RestoreRecord {
        RestoreRecord {
            schema_version: PersistenceSchemaVersion::try_from(1_u16).expect("schema"),
            claim_id: id(claim),
            trigger_id: id("trigger-1"),
            trigger_kind: RestoreTriggerKind::ServiceStart,
            receiver_id: id("receiver-1"),
            generation_id: generation(1),
            device_id: id("mouse-1"),
            intent_revision: IntentRevision::try_from(1_u64).expect("intent revision"),
            intent_digest: id(&"11".repeat(32)),
            revision: PersistenceRevision::try_from(1_u64).expect("revision"),
            last_attempt: None,
            status,
        }
    }

    fn session() -> AuthorizedSession {
        AuthorizedSession {
            client_id: id("bridge-restoration"),
            selected_version: ProtocolVersion::try_from(5_u16).expect("protocol"),
            session_id: SessionId::try_from("restore-session-test").expect("session"),
            authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("epoch"),
        }
    }

    fn scheduler() -> RestorationScheduler {
        RestorationScheduler::new(
            true,
            Some(session()),
            [0x55; 32],
            LeaseDurationMs::try_from(10_000_u32).expect("lease"),
            30_000,
            1_000,
            4,
            8,
            16,
        )
        .expect("scheduler")
    }

    fn sessions() -> SessionRegistry {
        let mut sessions =
            SessionRegistry::new(hfx_domain::QueueCapacity::try_from(2_u16).expect("capacity"));
        sessions.register(session()).expect("session registers");
        sessions
    }

    #[derive(Clone, Copy, Debug)]
    struct TestClock(MonotonicMs);

    impl Clock for TestClock {
        fn now(&self) -> MonotonicMs {
            self.0
        }
    }

    #[derive(Clone, Copy, Debug)]
    struct TestWallClock(WallClockUnixMs);

    impl WallClock for TestWallClock {
        fn now_unix_ms(&self) -> WallClockUnixMs {
            self.0
        }
    }

    #[derive(Clone, Debug)]
    struct TestTransport {
        dispatches: Vec<TransportDispatch>,
    }

    #[derive(Clone, Copy, Debug)]
    struct TestTransportError;

    impl TransportFailure for TestTransportError {
        fn facts(&self) -> TransportFailureFacts {
            TransportFailureFacts {
                delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("zero frames"),
                side_effect_certainty: SideEffectCertainty::None,
                live_write_executed: false,
                automatic_retry_safe: true,
                device_application: DeviceApplicationState::Unverified,
            }
        }
    }

    impl ReceiverTransport for TestTransport {
        type Error = TestTransportError;

        fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
            (receiver_id == &id::<ReceiverId>("receiver-1")).then_some(generation(1))
        }

        fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
            TransportReconciliation::NotObserved
        }

        fn dispatch(
            &mut self,
            dispatch: &TransportDispatch,
        ) -> Result<TransportReceipt, Self::Error> {
            self.dispatches.push(dispatch.clone());
            Ok(TransportReceipt {
                terminal: TransportTerminal::Delivered,
                delivered_frames: DeliveredFrameCount::try_from(
                    u16::try_from(dispatch.frames.len()).expect("frame count fits"),
                )
                .expect("delivered frames"),
                side_effect_certainty: SideEffectCertainty::Committed,
                live_write_executed: true,
                automatic_retry_safe: false,
                device_application: DeviceApplicationState::Confirmed,
            })
        }
    }

    #[derive(Debug, Default)]
    struct TestEventSink(Vec<hfx_protocol::BridgeEvent>);

    impl EventSink for TestEventSink {
        fn try_emit(&mut self, event: &hfx_protocol::BridgeEvent) -> EventDelivery {
            self.0.push(event.clone());
            EventDelivery::Accepted
        }
    }

    #[derive(Debug)]
    struct DeterministicIdentities(u8);

    impl SessionIdentitySource for DeterministicIdentities {
        fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
            for byte in destination {
                *byte = self.0;
                self.0 = self.0.wrapping_add(1);
            }
            Ok(())
        }
    }

    fn runtime_state() -> (ReceiverLifecycleRegistry, RuntimeProfileAuthority) {
        let mut machine =
            ReceiverLifecycleMachine::new(id("receiver-1"), LifecycleLimits::default())
                .expect("lifecycle initializes");
        machine.discover(stamp(1));
        let device_id: LogicalDeviceId = id("mouse-1");
        machine
            .register_device(
                ChildIdentity::new(
                    device_id.clone(),
                    DeviceKind::Mouse,
                    ProductId::try_from(0x00cd_u16).expect("product id"),
                )
                .expect("mouse identity"),
                stamp(2),
            )
            .expect("mouse registers");
        assert_eq!(
            machine.observe_pairing(&device_id, PairingState::Paired, stamp(3)),
            ApplyOutcome::Applied
        );
        machine
            .register_endpoint(
                &device_id,
                EndpointIdentity::new(
                    id("mouse-hyperflux"),
                    RouteKind::HyperfluxWireless,
                    ConnectionMode::Hyperflux24ghz,
                )
                .expect("endpoint identity"),
                stamp(4),
            )
            .expect("endpoint registers");
        machine
            .observe_route(
                &device_id,
                &id("mouse-hyperflux"),
                RouteState::Available,
                stamp(5),
            )
            .expect("route becomes available");
        machine
            .observe_power(&device_id, &id("mouse-hyperflux"), PowerState::On, stamp(6))
            .expect("power observation");
        machine
            .observe_sleep(
                &device_id,
                &id("mouse-hyperflux"),
                SleepState::Awake,
                stamp(7),
            )
            .expect("sleep observation");
        machine
            .observe_activity(
                &device_id,
                &id("mouse-hyperflux"),
                ActivityState::Active,
                stamp(8),
            )
            .expect("activity observation");
        machine
            .observe_freshness(
                &device_id,
                &id("mouse-hyperflux"),
                FreshnessState::Fresh,
                stamp(9),
            )
            .expect("freshness observation");
        let mut receivers = ReceiverLifecycleRegistry::default();
        receivers.register(machine).expect("receiver registers");
        let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
        profiles
            .bind_receiver(
                id("receiver-1"),
                generation(1),
                VendorId::try_from(0x1532_u16).expect("vendor id"),
                ProductId::try_from(0x00cf_u16).expect("receiver product id"),
            )
            .expect("receiver profile binds");
        (receivers, profiles)
    }

    fn stable_completion(
        receivers: &ReceiverLifecycleRegistry,
        profiles: &RuntimeProfileAuthority,
    ) -> CompletedTransaction {
        let resource = ResourceKey {
            receiver_id: id("receiver-1"),
            generation_id: generation(1),
            device_id: id("mouse-1"),
            kind: ResourceKind::Lighting,
        };
        let view = profiles.view(receivers);
        let receiver = view
            .receiver_profile(&resource.receiver_id, resource.generation_id)
            .expect("receiver profile");
        let device = view.device_profile(&resource).expect("device profile");
        let request = TransactionRequest {
            request_id: id("stable-request-1"),
            transaction_id: id("stable-transaction-1"),
            client_id: id("client-1"),
            lease_id: id("lease-1"),
            receiver_id: resource.receiver_id.clone(),
            generation_id: resource.generation_id,
            receiver_profile_id: receiver.profile_id,
            receiver_profile_digest: receiver.profile_digest,
            device_profiles: vec![DeviceProfileBinding {
                device_id: resource.device_id.clone(),
                profile_id: device.profile_id,
                profile_digest: device.profile_digest,
                application_slot_count: device.application_slot_count,
            }],
            transaction_class: TransactionClass::StaticLighting,
            stable_intents: vec![StableLightingIntent {
                device_id: resource.device_id.clone(),
                mode: StableLightingMode::Static,
            }],
            deadline_ms: time(1_000),
            resources: vec![resource],
            frames: vec![LightingFrame {
                device_id: id("mouse-1"),
                frame_index: FrameIndex::try_from(0_u32).expect("frame index"),
                colors: (0..device.application_slot_count.get())
                    .map(|_| RgbColor {
                        red: ColorChannel::try_from(0_u8).expect("red"),
                        green: ColorChannel::try_from(0_u8).expect("green"),
                        blue: ColorChannel::try_from(255_u8).expect("blue"),
                    })
                    .collect(),
            }],
        };
        let terminal = TransactionTerminal {
            request_id: request.request_id.clone(),
            request_digest: canonical_request_digest(&request).expect("request digest"),
            transaction_id: request.transaction_id.clone(),
            receiver_id: request.receiver_id.clone(),
            generation_id: request.generation_id,
            state: TransactionState::Succeeded,
            declared_frames: FrameCount::try_from(1_u16).expect("declared frames"),
            delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("delivered frames"),
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry: false,
            device_application: DeviceApplicationState::Confirmed,
            terminal_sequence: SequenceNumber::try_from(1_u64).expect("terminal sequence"),
            error_kind: None,
            superseded_by: None,
        };
        CompletedTransaction { request, terminal }
    }

    fn temporary_directory() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "hfx-restoration-integration-{}-{nonce}",
            std::process::id()
        ))
    }

    type DurableTestBackend<R> =
        CoreBridgeBackend<TestClock, TestWallClock, TestTransport, R, TestEventSink>;

    fn compose_backend<R: RestorationRuntime>(
        restoration: R,
        identity_seed: u8,
    ) -> DurableTestBackend<R> {
        let capacity = QueueCapacity::try_from(16_u16).expect("capacity");
        let (receivers, profiles) = runtime_state();
        CoreBridgeBackend::new(
            CoreBridgeConfig {
                lifecycle_limits: LifecycleLimits::default(),
                lease_capacity: capacity,
                lease_history_capacity: capacity,
                transaction_capacity: capacity,
                event_capacity: capacity,
                diagnostic_capacity: capacity,
                subscription_capacity: capacity,
                stream_id: id::<StreamId>("stream-1"),
                stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
                projection_revision: ProjectionRevision::try_from(1_u32)
                    .expect("projection revision"),
            },
            TestClock(time(100)),
            TestWallClock(WallClockUnixMs::try_from(20_u64).expect("wall clock")),
            TestTransport {
                dispatches: Vec::new(),
            },
            restoration,
            &mut DeterministicIdentities(identity_seed),
            receivers,
            profiles,
            TestEventSink::default(),
        )
        .expect("backend composes")
    }

    fn durable_with_stable<S: PersistenceStore>(store: S) -> DurableRestorationRuntime<S> {
        let (receivers, profiles) = runtime_state();
        let completion = stable_completion(&receivers, &profiles);
        let mut restoration = DurableRestorationRuntime::new(store);
        assert!(matches!(
            restoration
                .capture_completed(
                    &completion,
                    WallClockUnixMs::try_from(10_u64).expect("capture time")
                )
                .expect("stable intent captures"),
            StableCommitOutcome::Captured(ref intents) if intents.len() == 1
        ));
        restoration
    }

    fn plan_durable_restore<R: RestorationRuntime>(
        backend: &mut DurableTestBackend<R>,
    ) -> RestoreClaimId {
        backend
            .set_restoration_enabled(&id("receiver-1"), true)
            .expect("restoration enables");
        let trigger = RestoreTrigger {
            trigger_id: id("restore-trigger-crash-recovery"),
            kind: RestoreTriggerKind::ServiceStart,
            receiver_id: id("receiver-1"),
            generation_id: generation(1),
            target_device_id: None,
        };
        let RestorePlanResult::Planned(records) =
            backend.plan_restoration(&trigger).expect("restore plans")
        else {
            panic!("stable state must produce one restore claim")
        };
        assert_eq!(records.len(), 1);
        records[0].claim_id.clone()
    }

    fn advance_durable_restore<R: RestorationRuntime>(
        backend: &mut DurableTestBackend<R>,
        claim_id: &RestoreClaimId,
    ) -> Result<RestoreAdvanceResult, RestorationError> {
        backend.advance_restoration(
            claim_id,
            &session(),
            &sessions(),
            LeaseDurationMs::try_from(10_000_u32).expect("lease"),
            30_000,
        )
    }

    fn recover_file_checkpoint(state_path: &Path) {
        let store = FilePersistenceStore::open(FilePersistenceConfig::new(state_path))
            .expect("checkpoint reopens");
        let mut backend = compose_backend(DurableRestorationRuntime::new(store), 1);
        let mut scheduler = scheduler();
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("recovery trigger schedules");
        let report = scheduler
            .tick_with(&mut backend, &sessions())
            .expect("checkpoint recovers");
        assert_eq!(report.claims_recovered, 1);
        assert_eq!(report.claims_dispatched, 1);
        assert_eq!(report.claims_terminal, 1);
        assert_eq!(backend.transport().dispatches.len(), 1);
        assert!(
            backend
                .pending_restoration_records(&id("receiver-1"))
                .expect("pending records load")
                .is_empty()
        );
        drop(backend);

        let reopened = FilePersistenceStore::open(FilePersistenceConfig::new(state_path))
            .expect("terminal state reopens");
        let records = reopened
            .restore_records(&id("receiver-1"))
            .expect("terminal record reloads");
        assert_eq!(records.len(), 1);
        assert!(matches!(
            records[0].status,
            RestoreRecordStatus::Succeeded(_)
        ));
    }

    #[derive(Debug)]
    struct FailNthCommitter {
        inner: AtomicFileCommitter,
        call: usize,
        fail_on: usize,
    }

    impl FailNthCommitter {
        fn new(fail_on: usize) -> Self {
            Self {
                inner: AtomicFileCommitter::default(),
                call: 0,
                fail_on,
            }
        }
    }

    impl PersistenceCommitter for FailNthCommitter {
        fn commit(&mut self, path: &Path, bytes: &[u8]) -> Result<(), FilePersistenceError> {
            self.call += 1;
            if self.call == self.fail_on {
                Err(FilePersistenceError::Capacity)
            } else {
                self.inner.commit(path, bytes)
            }
        }
    }

    #[test]
    fn one_trigger_plans_and_dispatches_one_claim_on_the_actor_tick() {
        let planned = record("claim-1", RestoreRecordStatus::Planned);
        let terminal = record(
            "claim-1",
            RestoreRecordStatus::Invalidated(hfx_core::RestoreInvalidation {
                reason: hfx_domain::RestoreInvalidationReason::IntentChanged,
            }),
        );
        let mut controller = FakeController {
            now: MonotonicMs::try_from(10_u64).expect("time"),
            plans: vec![Ok(RestorePlanResult::Planned(vec![planned.clone()]))],
            pending: Vec::new(),
            advances: vec![Ok(RestoreAdvanceResult::Queued(planned))],
            dispatches: vec![Ok(terminal)],
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        };
        let mut scheduler = scheduler();
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("trigger schedules");
        let report = scheduler
            .tick_with(&mut controller, &sessions())
            .expect("tick succeeds");
        assert_eq!(report.triggers_planned, 1);
        assert_eq!(report.claims_dispatched, 1);
        assert_eq!(report.claims_terminal, 1);
        assert_eq!(controller.policy_calls, 1);
        assert_eq!(controller.advance_calls, 1);
        assert_eq!(controller.dispatch_calls, 1);
    }

    #[test]
    fn pending_claim_is_recovered_before_a_fresh_trigger_is_planned() {
        let prior = record("claim-prior", RestoreRecordStatus::Planned);
        let terminal = RestoreAdvanceResult::Terminal(record(
            "claim-prior",
            RestoreRecordStatus::Invalidated(hfx_core::RestoreInvalidation {
                reason: hfx_domain::RestoreInvalidationReason::StaleGeneration,
            }),
        ));
        let mut controller = FakeController {
            now: MonotonicMs::try_from(10_u64).expect("time"),
            plans: vec![Err(RestorationError::PriorClaimUnresolved)],
            pending: vec![prior],
            advances: vec![Ok(terminal)],
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        };
        let mut scheduler = scheduler();
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("trigger schedules");
        let report = scheduler
            .tick_with(&mut controller, &sessions())
            .expect("barrier is reconciled");
        assert_eq!(report.claims_recovered, 1);
        assert_eq!(report.claims_terminal, 1);
        assert!(scheduler.triggers.is_empty());
        assert_eq!(controller.plans.len(), 1);
    }

    #[test]
    fn advance_barrier_reloads_pending_claim_without_stopping_the_scheduler() {
        let planned = record("claim-prior", RestoreRecordStatus::Planned);
        let mut controller = FakeController {
            now: time(10),
            plans: vec![Ok(RestorePlanResult::Planned(vec![planned.clone()]))],
            pending: vec![planned],
            advances: vec![Err(RestorationError::PriorOutcomeUncertain)],
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        };
        let mut scheduler = scheduler();
        scheduler.configured_receivers.insert(id("receiver-1"));
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("trigger schedules");

        let report = scheduler
            .tick_with(&mut controller, &sessions())
            .expect("advance barrier is recoverable");

        assert_eq!(report.triggers_planned, 1);
        assert_eq!(report.claims_recovered, 1);
        assert_eq!(report.claims_advanced, 0);
        assert_eq!(scheduler.claims.len(), 1);
        assert_eq!(controller.advance_calls, 1);
    }

    #[test]
    fn overlapping_resume_and_device_return_leave_one_broad_trigger() {
        let mut scheduler = scheduler();
        scheduler
            .schedule_device_return(
                id("receiver-1"),
                generation(1),
                id("mouse-1"),
                SequenceNumber::try_from(1_u64).expect("sequence"),
            )
            .expect("device return schedules");
        scheduler
            .schedule_system_resume(
                id("receiver-1"),
                generation(1),
                SequenceNumber::try_from(2_u64).expect("sequence"),
            )
            .expect("resume schedules");
        scheduler
            .schedule_device_return(
                id("receiver-1"),
                generation(1),
                id("mouse-1"),
                SequenceNumber::try_from(3_u64).expect("sequence"),
            )
            .expect("later device return coalesces");

        assert_eq!(scheduler.triggers.len(), 1);
        assert!(matches!(
            scheduler
                .triggers
                .values()
                .next()
                .expect("one trigger")
                .trigger
                .kind,
            RestoreTriggerKind::SystemResume
        ));
    }

    #[test]
    fn repeated_device_returns_coalesce_without_trigger_overflow() {
        let mut scheduler = scheduler();
        for sequence in 1_u64..=65 {
            scheduler
                .schedule_device_return(
                    id("receiver-1"),
                    generation(1),
                    id("mouse-1"),
                    SequenceNumber::try_from(sequence).expect("sequence"),
                )
                .expect("same-scope return coalesces");
        }

        assert_eq!(scheduler.triggers.len(), 1);
        assert_eq!(
            scheduler
                .triggers
                .values()
                .next()
                .expect("one trigger")
                .sequence,
            Some(SequenceNumber::try_from(65_u64).expect("sequence"))
        );
    }

    #[test]
    fn deferred_claim_waits_for_backoff_but_device_return_wakes_it() {
        let deferred = record(
            "claim-deferred",
            RestoreRecordStatus::Deferred(RestoreDeferred {
                reason: RestoreDeferReason::DeviceSleeping,
                prior_outcome: None,
            }),
        );
        let mut controller = FakeController {
            now: MonotonicMs::try_from(10_u64).expect("time"),
            plans: vec![Ok(RestorePlanResult::Planned(vec![deferred.clone()]))],
            pending: Vec::new(),
            advances: vec![Ok(RestoreAdvanceResult::Deferred(deferred.clone()))],
            dispatches: Vec::new(),
            policy_calls: 0,
            advance_calls: 0,
            dispatch_calls: 0,
        };
        let mut scheduler = scheduler();
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("trigger schedules");
        scheduler
            .tick_with(&mut controller, &sessions())
            .expect("claim defers");
        assert_eq!(controller.advance_calls, 1);
        assert_eq!(
            scheduler
                .claims
                .get(&id("claim-deferred"))
                .expect("claim retained")
                .due
                .get(),
            u64::MAX
        );
        scheduler
            .schedule_device_return(
                id("receiver-1"),
                generation(1),
                id("mouse-1"),
                SequenceNumber::try_from(9_u64).expect("sequence"),
            )
            .expect("return schedules");
        assert_eq!(
            scheduler
                .claims
                .get(&id("claim-deferred"))
                .expect("claim retained")
                .due
                .get(),
            0
        );
    }

    #[test]
    fn transient_deferral_uses_bounded_exponential_backoff() {
        assert_eq!(
            deferred_due(time(10), RestoreDeferReason::OwnershipConflict, 1_000, 1)
                .expect("first retry")
                .get(),
            1_010
        );
        assert_eq!(
            deferred_due(time(10), RestoreDeferReason::OwnershipConflict, 1_000, 2)
                .expect("second retry")
                .get(),
            2_010
        );
        assert_eq!(
            deferred_due(
                time(10),
                RestoreDeferReason::OwnershipConflict,
                1_000,
                u8::MAX
            )
            .expect("bounded retry")
            .get(),
            64_010
        );
    }

    #[test]
    fn disabled_scheduler_has_no_session_and_no_work() {
        let scheduler = RestorationScheduler::new(
            false,
            None,
            [0; 32],
            LeaseDurationMs::try_from(10_000_u32).expect("lease"),
            30_000,
            1_000,
            4,
            8,
            16,
        )
        .expect("disabled scheduler");
        assert!(!scheduler.is_enabled());
    }

    #[test]
    fn durable_service_start_restore_uses_the_normal_backend_and_survives_reopen() {
        let root = temporary_directory();
        fs::create_dir(&root).expect("state directory creates");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
            .expect("state directory is private");
        let state_path = root.join("bridge-state.json");
        let (receivers, profiles) = runtime_state();
        let completion = stable_completion(&receivers, &profiles);
        let mut restoration = ProductionRestoration::durable(&state_path, 4, 1024 * 1024)
            .expect("durable runtime opens");
        assert!(matches!(
            restoration
                .capture_completed(
                    &completion,
                    WallClockUnixMs::try_from(10_u64).expect("capture time")
                )
                .expect("stable intent captures"),
            StableCommitOutcome::Captured(ref intents) if intents.len() == 1
        ));

        let capacity = QueueCapacity::try_from(16_u16).expect("capacity");
        let mut identities = DeterministicIdentities(1);
        let mut backend = CoreBridgeBackend::new(
            CoreBridgeConfig {
                lifecycle_limits: LifecycleLimits::default(),
                lease_capacity: capacity,
                lease_history_capacity: capacity,
                transaction_capacity: capacity,
                event_capacity: capacity,
                diagnostic_capacity: capacity,
                subscription_capacity: capacity,
                stream_id: id::<StreamId>("stream-1"),
                stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
                projection_revision: ProjectionRevision::try_from(1_u32)
                    .expect("projection revision"),
            },
            TestClock(time(100)),
            TestWallClock(WallClockUnixMs::try_from(20_u64).expect("wall clock")),
            TestTransport {
                dispatches: Vec::new(),
            },
            restoration,
            &mut identities,
            receivers,
            profiles,
            TestEventSink::default(),
        )
        .expect("backend composes");
        let mut scheduler = scheduler();
        scheduler
            .schedule_service_start(id("receiver-1"), generation(1))
            .expect("service-start trigger schedules");
        let report = scheduler
            .tick_with(&mut backend, &sessions())
            .expect("restore tick succeeds");
        assert_eq!(
            report,
            RestorationTickReport {
                triggers_planned: 1,
                claims_recovered: 0,
                claims_advanced: 1,
                claims_dispatched: 1,
                claims_deferred: 0,
                claims_terminal: 1,
            }
        );
        assert_eq!(backend.transport().dispatches.len(), 1);
        assert_eq!(backend.transport().dispatches[0].frames.len(), 1);
        assert!(
            backend.transport().dispatches[0].frames[0]
                .colors
                .iter()
                .all(|color| color.blue.get() == 255)
        );
        assert!(
            backend
                .pending_restoration_records(&id("receiver-1"))
                .expect("pending records load")
                .is_empty()
        );
        drop(backend);

        let reopened = hfx_bridge::FilePersistenceStore::open(
            hfx_bridge::FilePersistenceConfig::new(&state_path),
        )
        .expect("state reopens after process boundary");
        let records = reopened
            .restore_records(&id("receiver-1"))
            .expect("records reload");
        assert_eq!(records.len(), 1);
        assert!(matches!(
            records[0].status,
            RestoreRecordStatus::Succeeded(_)
        ));
        assert_eq!(
            reopened
                .stable_entries(&id("receiver-1"))
                .expect("stable intent reloads")
                .len(),
            1
        );
        drop(reopened);
        fs::remove_dir_all(root).expect("state directory removes");
    }

    #[test]
    fn production_scheduler_recovers_prepared_queued_and_applying_file_checkpoints() {
        let prepared_root = temporary_directory();
        fs::create_dir(&prepared_root).expect("prepared directory creates");
        fs::set_permissions(&prepared_root, fs::Permissions::from_mode(0o700))
            .expect("prepared directory is private");
        let prepared_path = prepared_root.join("bridge-state.json");
        let prepared_store = FilePersistenceStore::open_with_committer(
            FilePersistenceConfig::new(&prepared_path),
            FailNthCommitter::new(5),
        )
        .expect("prepared store opens");
        let mut prepared_backend = compose_backend(durable_with_stable(prepared_store), 1);
        let prepared_claim = plan_durable_restore(&mut prepared_backend);
        assert!(matches!(
            advance_durable_restore(&mut prepared_backend, &prepared_claim),
            Err(RestorationError::Persistence(
                hfx_core::PersistenceOperation::SaveRestore
            ))
        ));
        assert!(matches!(
            prepared_backend
                .restoration()
                .store()
                .restore_record(&prepared_claim)
                .expect("prepared record reads")
                .expect("prepared record exists")
                .status,
            RestoreRecordStatus::Prepared(_)
        ));
        assert!(prepared_backend.transport().dispatches.is_empty());
        drop(prepared_backend);
        recover_file_checkpoint(&prepared_path);
        fs::remove_dir_all(prepared_root).expect("prepared directory removes");

        for checkpoint in ["queued", "applying"] {
            let root = temporary_directory().with_extension(checkpoint);
            fs::create_dir(&root).expect("checkpoint directory creates");
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .expect("checkpoint directory is private");
            let state_path = root.join("bridge-state.json");
            let store = FilePersistenceStore::open(FilePersistenceConfig::new(&state_path))
                .expect("checkpoint store opens");
            let mut backend = compose_backend(durable_with_stable(store), 1);
            let claim_id = plan_durable_restore(&mut backend);
            assert!(matches!(
                advance_durable_restore(&mut backend, &claim_id),
                Ok(RestoreAdvanceResult::Queued(_))
            ));
            if checkpoint == "applying" {
                RestorationCoordinator
                    .mark_applying(&claim_id, backend.restoration_mut().store_mut())
                    .expect("applying checkpoint persists");
            }
            let record = backend
                .restoration()
                .store()
                .restore_record(&claim_id)
                .expect("checkpoint record reads")
                .expect("checkpoint record exists");
            assert!(
                matches!(&record.status, RestoreRecordStatus::Queued(_))
                    == (checkpoint == "queued")
            );
            assert!(
                matches!(&record.status, RestoreRecordStatus::Applying(_))
                    == (checkpoint == "applying")
            );
            assert!(backend.transport().dispatches.is_empty());
            drop(backend);

            recover_file_checkpoint(&state_path);
            fs::remove_dir_all(root).expect("checkpoint directory removes");
        }
    }
}
