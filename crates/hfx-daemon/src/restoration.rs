// SPDX-License-Identifier: GPL-2.0-only

//! Bounded, event-driven production restoration scheduling.

mod controller;
mod schedule;
mod tick;

use controller::RestorationController;
use hfx_bridge::AuthorizedSession;
use hfx_core::{RestorationError, RestoreTrigger};
use hfx_domain::{
    GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs, PersistenceRevision, ReceiverId,
    RestoreClaimId, RestoreTriggerId, RestoreTriggerKind, SequenceNumber,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct RestorationTickReport {
    pub triggers_planned: usize,
    pub effect_replays_queued: usize,
    pub effect_replays_deferred: usize,
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
    EffectReplay,
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
            Self::EffectReplay => "owner-bound effect replay coordination failed",
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
    purpose: ScheduledTriggerPurpose,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ScheduledTriggerPurpose {
    Primary,
    ReturnAttempt(u8),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TriggerPlanDisposition {
    Continue,
    NoRestorableIntent,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PendingClaimSchedule {
    At(MonotonicMs),
    Trigger(RestoreTriggerKind),
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
    ready_devices: BTreeMap<(ReceiverId, GenerationId, LogicalDeviceId), MonotonicMs>,
    return_epochs: BTreeMap<(ReceiverId, GenerationId, LogicalDeviceId), SequenceNumber>,
    triggers: BTreeMap<RestoreTriggerId, ScheduledTrigger>,
    claims: BTreeMap<RestoreClaimId, ScheduledClaim>,
}

#[cfg(test)]
mod tests;
