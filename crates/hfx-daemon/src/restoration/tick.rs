// SPDX-License-Identifier: GPL-2.0-only

//! Bounded due-work execution for the restoration scheduler.

use super::schedule::{deferred_due, due_after, due_claim_id, due_trigger_id};
use super::{
    PendingClaimSchedule, RestorationController, RestorationScheduleError, RestorationScheduler,
    RestorationTickReport, ScheduledTrigger, ScheduledTriggerPurpose, TriggerPlanDisposition,
};
use crate::ProductionBackend;
use hfx_bridge::{DesiredEffectReplayStatus, SessionRegistry};
use hfx_core::{RestorationError, RestoreAdvanceResult, RestorePlanResult};
use hfx_domain::{MonotonicMs, ReceiverId, RestoreTriggerId};
use std::collections::BTreeSet;

impl RestorationScheduler {
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

    pub(super) fn tick_with<C: RestorationController>(
        &mut self,
        controller: &mut C,
        sessions: &SessionRegistry,
    ) -> Result<RestorationTickReport, RestorationScheduleError> {
        let now = controller.now();
        let mut report = RestorationTickReport::default();
        self.process_due_trigger(controller, sessions, now, &mut report)?;

        for _ in 0..self.claims_per_tick {
            if !self.process_due_claim(controller, sessions, now, &mut report)? {
                break;
            }
        }
        Ok(report)
    }

    pub(super) fn process_due_trigger<C: RestorationController>(
        &mut self,
        controller: &mut C,
        sessions: &SessionRegistry,
        now: MonotonicMs,
        report: &mut RestorationTickReport,
    ) -> Result<(), RestorationScheduleError> {
        if let Some(trigger_id) = due_trigger_id(&self.triggers, now) {
            let mut scheduled = self
                .triggers
                .remove(&trigger_id)
                .ok_or(RestorationScheduleError::Identifier)?;
            let receiver_id = scheduled.trigger.receiver_id.clone();
            let newly_configured = !self
                .configured_receivers
                .contains(&scheduled.trigger.receiver_id);
            if newly_configured {
                controller.set_enabled(&scheduled.trigger.receiver_id, true)?;
            }
            if matches!(scheduled.purpose, ScheduledTriggerPurpose::ReturnAttempt(_))
                && let Some(device_id) = scheduled.trigger.target_device_id.as_ref()
            {
                match controller.queue_desired_effect_replay(
                    &scheduled.trigger.receiver_id,
                    scheduled.trigger.generation_id,
                    device_id,
                    sessions,
                    self.authority_window_ms,
                )? {
                    DesiredEffectReplayStatus::Queued => {
                        report.effect_replays_queued += 1;
                        if newly_configured {
                            self.configured_receivers.insert(receiver_id);
                        }
                        return Ok(());
                    }
                    DesiredEffectReplayStatus::Deferred => {
                        report.effect_replays_deferred += 1;
                        if newly_configured {
                            self.configured_receivers.insert(receiver_id);
                        }
                        return Ok(());
                    }
                    DesiredEffectReplayStatus::NotAvailable
                    | DesiredEffectReplayStatus::Invalidated => {}
                }
            }
            let Some(migrated) = self.rebind_or_recover_blocking_claims(
                controller,
                &trigger_id,
                &mut scheduled,
                now,
                report,
            )?
            else {
                if newly_configured {
                    self.configured_receivers.insert(receiver_id);
                }
                return Ok(());
            };
            let pending = newly_configured
                .then(|| controller.pending_records(&scheduled.trigger.receiver_id))
                .transpose()?
                .unwrap_or_default();
            let disposition = self.plan_or_recover_trigger(
                controller,
                &trigger_id,
                &mut scheduled,
                pending.is_empty() || migrated > 0,
                now,
                report,
            )?;
            if disposition == TriggerPlanDisposition::NoRestorableIntent
                && matches!(scheduled.purpose, ScheduledTriggerPurpose::ReturnAttempt(_))
                && let Some(device_id) = scheduled.trigger.target_device_id.as_ref()
            {
                self.consume_device_return_epoch(
                    &scheduled.trigger.receiver_id,
                    scheduled.trigger.generation_id,
                    device_id,
                );
            }
            if newly_configured {
                self.configured_receivers.insert(receiver_id);
            }
        }
        Ok(())
    }

    fn plan_or_recover_trigger<C: RestorationController>(
        &mut self,
        controller: &mut C,
        trigger_id: &RestoreTriggerId,
        scheduled: &mut ScheduledTrigger,
        should_plan: bool,
        now: MonotonicMs,
        report: &mut RestorationTickReport,
    ) -> Result<TriggerPlanDisposition, RestorationScheduleError> {
        if !should_plan {
            report.claims_recovered += self.synchronize_pending_claims(
                controller,
                &scheduled.trigger.receiver_id,
                PendingClaimSchedule::Trigger(scheduled.trigger.kind),
                now,
            )?;
            return Ok(TriggerPlanDisposition::Continue);
        }
        match controller.plan_restore(&scheduled.trigger) {
            Ok(RestorePlanResult::Planned(_) | RestorePlanResult::ProfileAuthorityUnavailable) => {
                self.synchronize_pending_claims(
                    controller,
                    &scheduled.trigger.receiver_id,
                    PendingClaimSchedule::Trigger(scheduled.trigger.kind),
                    now,
                )?;
                report.triggers_planned += 1;
                Ok(TriggerPlanDisposition::Continue)
            }
            Ok(
                RestorePlanResult::NoStableIntents | RestorePlanResult::NoCompatibleStableIntents,
            ) => {
                self.synchronize_pending_claims(
                    controller,
                    &scheduled.trigger.receiver_id,
                    PendingClaimSchedule::Trigger(scheduled.trigger.kind),
                    now,
                )?;
                report.triggers_planned += 1;
                Ok(TriggerPlanDisposition::NoRestorableIntent)
            }
            Ok(RestorePlanResult::Disabled) => Err(RestorationError::RuntimeDisabled.into()),
            Err(
                RestorationError::PriorClaimUnresolved | RestorationError::PriorOutcomeUncertain,
            ) => {
                report.claims_recovered += self.synchronize_pending_claims(
                    controller,
                    &scheduled.trigger.receiver_id,
                    PendingClaimSchedule::At(now),
                    now,
                )?;
                if scheduled.purpose == ScheduledTriggerPurpose::Primary {
                    scheduled.due = due_after(now, self.retry_interval_ms)?;
                    self.triggers.insert(trigger_id.clone(), scheduled.clone());
                }
                Ok(TriggerPlanDisposition::Continue)
            }
            Err(error) => Err(error.into()),
        }
    }

    fn rebind_or_recover_blocking_claims<C: RestorationController>(
        &mut self,
        controller: &mut C,
        trigger_id: &RestoreTriggerId,
        scheduled: &mut ScheduledTrigger,
        now: MonotonicMs,
        report: &mut RestorationTickReport,
    ) -> Result<Option<usize>, RestorationScheduleError> {
        match controller.rebind_compatible_stable_intents(&scheduled.trigger) {
            Ok(migrated) => Ok(Some(migrated)),
            Err(
                RestorationError::PriorClaimUnresolved | RestorationError::PriorOutcomeUncertain,
            ) => {
                // Reconcile the blocking claim in this actor tick. The
                // migration retry remains delayed, so an equal due time cannot
                // starve the claim behind the trigger forever.
                report.claims_recovered += self.synchronize_pending_claims(
                    controller,
                    &scheduled.trigger.receiver_id,
                    PendingClaimSchedule::At(now),
                    now,
                )?;
                if scheduled.purpose == ScheduledTriggerPurpose::Primary {
                    scheduled.due = due_after(now, self.retry_interval_ms)?;
                    self.triggers.insert(trigger_id.clone(), scheduled.clone());
                }
                Ok(None)
            }
            Err(error) => Err(error.into()),
        }
    }

    pub(super) fn process_due_claim<C: RestorationController>(
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
                RestorationError::PriorClaimUnresolved
                | RestorationError::PriorOutcomeUncertain
                | RestorationError::UnknownClaim,
            ) => {
                let retry_due = due_after(now, self.retry_interval_ms)?;
                report.claims_recovered += self.synchronize_pending_claims(
                    controller,
                    &scheduled.receiver_id,
                    PendingClaimSchedule::At(retry_due),
                    now,
                )?;
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

    fn synchronize_pending_claims<C: RestorationController>(
        &mut self,
        controller: &C,
        receiver_id: &ReceiverId,
        schedule: PendingClaimSchedule,
        now: MonotonicMs,
    ) -> Result<usize, RestorationScheduleError> {
        let pending = controller.pending_records(receiver_id)?;
        let pending_ids = pending
            .iter()
            .map(|record| record.claim_id.clone())
            .collect::<BTreeSet<_>>();
        self.claims.retain(|claim_id, claim| {
            claim.receiver_id != *receiver_id || pending_ids.contains(claim_id)
        });
        let count = pending.len();
        for record in pending {
            if self.claims.contains_key(&record.claim_id) {
                continue;
            }
            match schedule {
                PendingClaimSchedule::At(due) => self.schedule_record(record, due)?,
                PendingClaimSchedule::Trigger(trigger_kind) => {
                    self.schedule_trigger_record(record, trigger_kind, now)?;
                }
            }
        }
        Ok(count)
    }
}
