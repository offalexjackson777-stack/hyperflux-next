// SPDX-License-Identifier: GPL-2.0-only

//! Durable restoration controller port and production bridge adapter.

use super::RestorationScheduleError;
use hfx_bridge::{
    AuthorizedSession, CoreBridgeBackend, DesiredEffectReplayStatus, RestorationRuntime,
    SessionRegistry,
};
use hfx_core::{
    Clock, EventSink, ReceiverTransport, RestorationError, RestoreAdvanceResult, RestorePlanResult,
    RestoreRecord, RestoreTrigger, WallClock,
};
use hfx_domain::{
    GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs, ReceiverId, RestoreClaimId,
};

pub(super) trait RestorationController {
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
    fn rebind_compatible_stable_intents(
        &mut self,
        _trigger: &RestoreTrigger,
    ) -> Result<usize, RestorationError> {
        Ok(0)
    }
    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError>;
    fn queue_desired_effect_replay(
        &mut self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
        _device_id: &LogicalDeviceId,
        _sessions: &SessionRegistry,
        _authority_window_ms: u64,
    ) -> Result<DesiredEffectReplayStatus, RestorationScheduleError> {
        Ok(DesiredEffectReplayStatus::NotAvailable)
    }
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

    fn rebind_compatible_stable_intents(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<usize, RestorationError> {
        CoreBridgeBackend::rebind_compatible_stable_intents(self, trigger)
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        self.plan_restoration(trigger)
    }

    fn queue_desired_effect_replay(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
        sessions: &SessionRegistry,
        authority_window_ms: u64,
    ) -> Result<DesiredEffectReplayStatus, RestorationScheduleError> {
        CoreBridgeBackend::queue_desired_effect_replay(
            self,
            receiver_id,
            generation_id,
            device_id,
            sessions,
            authority_window_ms,
        )
        .map_err(|_| RestorationScheduleError::EffectReplay)
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
