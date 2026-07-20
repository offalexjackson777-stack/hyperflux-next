// SPDX-License-Identifier: GPL-2.0-only

//! Deterministic, crash-injectable implementation of the receiver transport port.

use hfx_core::{
    ReceiverTransport, TransportDispatch, TransportFailure, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal,
};
use hfx_domain::{
    DeliveredFrameCount, DeviceApplicationState, GenerationId, ReceiverId, RestoreRecordState,
    SideEffectCertainty, TransactionId,
};
use hfx_protocol::RgbColor;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::{self, Write as _};
use std::panic::panic_any;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SimTransportCrashPoint {
    AfterReservation,
    AfterPhysicalWrite,
    AfterTerminal,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SimCrashSignal {
    BeforeRestoreRecordCas(RestoreRecordState),
    AfterRestoreRecordCas(RestoreRecordState),
    Transport(SimTransportCrashPoint),
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SimTransportErrorKind {
    IdentityConflict,
    RouteUnavailable,
    JournalCapacity,
    LookupUnavailable,
    OutcomeUncertain,
    Serialization,
    FrameCount,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SimTransportConfigError {
    InvalidCapacity,
    DomainInvariant,
    GenerationNotNewer,
}

impl fmt::Display for SimTransportConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "transport journal capacities must be nonzero",
            Self::DomainInvariant => "shared transport facts cannot represent zero frames",
            Self::GenerationNotNewer => "a receiver reconnect must use a strictly newer generation",
        })
    }
}

impl std::error::Error for SimTransportConfigError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SimTransportError {
    kind: SimTransportErrorKind,
    facts: TransportFailureFacts,
}

impl SimTransportError {
    #[must_use]
    pub const fn kind(&self) -> SimTransportErrorKind {
        self.kind
    }

    #[must_use]
    pub const fn failure_facts(&self) -> TransportFailureFacts {
        self.facts
    }
}

impl fmt::Display for SimTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self.kind {
            SimTransportErrorKind::IdentityConflict => {
                "transport identity conflicts with retained durable evidence"
            }
            SimTransportErrorKind::RouteUnavailable => {
                "transport dispatch does not target the active receiver generation"
            }
            SimTransportErrorKind::JournalCapacity => {
                "transport journal has no safely evictable capacity"
            }
            SimTransportErrorKind::LookupUnavailable => "transport outcome lookup is unavailable",
            SimTransportErrorKind::OutcomeUncertain => {
                "transport outcome may include an unrecorded side effect"
            }
            SimTransportErrorKind::Serialization => {
                "transport dispatch cannot be given a durable fingerprint"
            }
            SimTransportErrorKind::FrameCount => "transport frame count cannot be represented",
        })
    }
}

impl std::error::Error for SimTransportError {}

impl TransportFailure for SimTransportError {
    fn facts(&self) -> TransportFailureFacts {
        self.facts
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SimTransportFailurePlan {
    pub facts: TransportFailureFacts,
    pub apply_physical_state: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SimJournalState {
    Reserved,
    WriteStarted,
    Retained(TransportReceipt),
    RetainedFailure(TransportFailureFacts),
}

impl SimJournalState {
    const fn is_terminal(self) -> bool {
        matches!(self, Self::Retained(_) | Self::RetainedFailure(_))
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SimTransportJournalRecord {
    pub dispatch: TransportDispatch,
    pub fingerprint: String,
    pub state: SimJournalState,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SimTransportMetrics {
    pub reconciliations: u64,
    pub dispatch_calls: u64,
    pub physical_dispatches: u64,
    pub physical_frames: u64,
    pub evicted_entries: u64,
    pub forgotten_tombstones: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct JournalTombstone {
    fingerprint: String,
    generation_id: GenerationId,
}

/// A bounded virtual receiver whose durable journal survives process restarts.
///
/// Every new dispatch is durably reserved before the simulated side effect. An
/// armed crash deliberately unwinds at a named boundary; callers should use the
/// restoration harness, which catches only this simulator-owned signal.
#[derive(Debug)]
pub struct SimReceiverTransport {
    receiver_id: ReceiverId,
    generation_id: Option<GenerationId>,
    latest_generation_id: GenerationId,
    max_entries: usize,
    max_tombstones: usize,
    journal: BTreeMap<TransactionId, SimTransportJournalRecord>,
    order: VecDeque<TransactionId>,
    tombstones: BTreeMap<TransactionId, JournalTombstone>,
    tombstone_order: VecDeque<TransactionId>,
    incomplete_generations: BTreeSet<GenerationId>,
    lookup_available: bool,
    physical_state: BTreeMap<hfx_domain::LogicalDeviceId, Vec<RgbColor>>,
    zero_delivered: DeliveredFrameCount,
    metrics: Cell<SimTransportMetrics>,
    next_failure: Option<SimTransportFailurePlan>,
    crash_point: Option<SimTransportCrashPoint>,
}

impl SimReceiverTransport {
    /// Creates one generation-bound virtual receiver with bounded journals.
    ///
    /// # Errors
    ///
    /// Returns an error when either durable bound is zero.
    pub fn new(
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        max_entries: usize,
        max_tombstones: usize,
    ) -> Result<Self, SimTransportConfigError> {
        if max_entries == 0 || max_tombstones == 0 {
            return Err(SimTransportConfigError::InvalidCapacity);
        }
        let zero_delivered = DeliveredFrameCount::try_from(0_u16)
            .map_err(|_| SimTransportConfigError::DomainInvariant)?;
        Ok(Self {
            receiver_id,
            generation_id: Some(generation_id),
            latest_generation_id: generation_id,
            max_entries,
            max_tombstones,
            journal: BTreeMap::new(),
            order: VecDeque::new(),
            tombstones: BTreeMap::new(),
            tombstone_order: VecDeque::new(),
            incomplete_generations: BTreeSet::new(),
            lookup_available: true,
            physical_state: BTreeMap::new(),
            zero_delivered,
            metrics: Cell::new(SimTransportMetrics::default()),
            next_failure: None,
            crash_point: None,
        })
    }

    #[must_use]
    pub const fn receiver_id(&self) -> &ReceiverId {
        &self.receiver_id
    }

    #[must_use]
    pub const fn generation_id(&self) -> Option<GenerationId> {
        self.generation_id
    }

    #[must_use]
    pub fn metrics(&self) -> SimTransportMetrics {
        self.metrics.get()
    }

    #[must_use]
    pub fn history_complete(&self, generation_id: GenerationId) -> bool {
        !self.incomplete_generations.contains(&generation_id)
    }

    #[must_use]
    pub fn journal_record(
        &self,
        transaction_id: &TransactionId,
    ) -> Option<&SimTransportJournalRecord> {
        self.journal.get(transaction_id)
    }

    #[must_use]
    pub fn physical_colors(&self, device_id: &hfx_domain::LogicalDeviceId) -> Option<&[RgbColor]> {
        self.physical_state.get(device_id).map(Vec::as_slice)
    }

    pub fn set_lookup_available(&mut self, available: bool) {
        self.lookup_available = available;
    }

    pub fn disconnect(&mut self) {
        self.generation_id = None;
    }

    /// Connects a strictly newer physical receiver generation.
    ///
    /// # Errors
    ///
    /// Returns an error when a stale or repeated generation would be installed.
    pub fn connect_generation(
        &mut self,
        generation_id: GenerationId,
    ) -> Result<(), SimTransportConfigError> {
        if generation_id <= self.latest_generation_id {
            return Err(SimTransportConfigError::GenerationNotNewer);
        }
        self.latest_generation_id = generation_id;
        self.generation_id = Some(generation_id);
        Ok(())
    }

    pub fn fail_next_dispatch(&mut self, plan: SimTransportFailurePlan) {
        self.next_failure = Some(plan);
    }

    pub fn arm_crash(&mut self, point: SimTransportCrashPoint) {
        self.crash_point = Some(point);
    }

    pub fn evict_terminal(&mut self, transaction_id: &TransactionId) -> bool {
        let Some(record) = self.journal.get(transaction_id) else {
            return false;
        };
        if !record.state.is_terminal() {
            return false;
        }
        let Some(record) = self.journal.remove(transaction_id) else {
            return false;
        };
        self.order.retain(|candidate| candidate != transaction_id);
        self.remember_tombstone(
            transaction_id.clone(),
            record.fingerprint,
            record.dispatch.generation_id,
        );
        true
    }

    fn reconcile_exact(
        &self,
        dispatch: &TransportDispatch,
    ) -> Result<TransportReconciliation, SimTransportError> {
        if !self.lookup_available {
            return Ok(TransportReconciliation::Unavailable);
        }
        let fingerprint = dispatch_fingerprint(dispatch)
            .map_err(|()| safe_error(SimTransportErrorKind::Serialization, self.zero_delivered))?;
        if let Some(record) = self.journal.get(&dispatch.transaction_id) {
            if record.fingerprint != fingerprint || record.dispatch != *dispatch {
                return Ok(TransportReconciliation::Conflict);
            }
            return Ok(match record.state {
                SimJournalState::Reserved | SimJournalState::WriteStarted => {
                    TransportReconciliation::Unavailable
                }
                SimJournalState::Retained(receipt) => TransportReconciliation::Retained(receipt),
                SimJournalState::RetainedFailure(facts) => {
                    TransportReconciliation::RetainedFailure(facts)
                }
            });
        }
        if let Some(tombstone) = self.tombstones.get(&dispatch.transaction_id) {
            return Ok(if tombstone.fingerprint == fingerprint {
                TransportReconciliation::Evicted
            } else {
                TransportReconciliation::Conflict
            });
        }
        Ok(
            if self
                .incomplete_generations
                .contains(&dispatch.generation_id)
            {
                TransportReconciliation::Unavailable
            } else {
                TransportReconciliation::NotObserved
            },
        )
    }

    fn reserve(&mut self, dispatch: &TransportDispatch) -> Result<(), SimTransportError> {
        self.ensure_capacity()?;
        let record = SimTransportJournalRecord {
            dispatch: dispatch.clone(),
            fingerprint: dispatch_fingerprint(dispatch).map_err(|()| {
                safe_error(SimTransportErrorKind::Serialization, self.zero_delivered)
            })?,
            state: SimJournalState::Reserved,
        };
        self.order.push_back(dispatch.transaction_id.clone());
        self.journal.insert(dispatch.transaction_id.clone(), record);
        Ok(())
    }

    fn ensure_capacity(&mut self) -> Result<(), SimTransportError> {
        while self.journal.len() >= self.max_entries {
            let candidate = self.order.iter().find_map(|transaction_id| {
                self.journal
                    .get(transaction_id)
                    .is_some_and(|record| record.state.is_terminal())
                    .then(|| transaction_id.clone())
            });
            let Some(candidate) = candidate else {
                return Err(safe_error(
                    SimTransportErrorKind::JournalCapacity,
                    self.zero_delivered,
                ));
            };
            let _ = self.evict_terminal(&candidate);
        }
        Ok(())
    }

    fn remember_tombstone(
        &mut self,
        transaction_id: TransactionId,
        fingerprint: String,
        generation_id: GenerationId,
    ) {
        while self.tombstones.len() >= self.max_tombstones {
            if let Some(oldest) = self.tombstone_order.pop_front() {
                if let Some(forgotten) = self.tombstones.remove(&oldest) {
                    self.incomplete_generations.insert(forgotten.generation_id);
                }
                update_metrics(&self.metrics, |metrics| {
                    metrics.forgotten_tombstones = metrics.forgotten_tombstones.saturating_add(1);
                });
            }
        }
        self.tombstones.insert(
            transaction_id.clone(),
            JournalTombstone {
                fingerprint,
                generation_id,
            },
        );
        self.tombstone_order.push_back(transaction_id);
        update_metrics(&self.metrics, |metrics| {
            metrics.evicted_entries = metrics.evicted_entries.saturating_add(1);
        });
    }

    fn set_state(&mut self, transaction_id: &TransactionId, state: SimJournalState) {
        if let Some(record) = self.journal.get_mut(transaction_id) {
            record.state = state;
        }
    }

    fn apply_physical(&mut self, dispatch: &TransportDispatch) {
        for frame in &dispatch.frames {
            self.physical_state
                .insert(frame.device_id.clone(), frame.colors.clone());
        }
        update_metrics(&self.metrics, |metrics| {
            metrics.physical_dispatches = metrics.physical_dispatches.saturating_add(1);
            metrics.physical_frames = metrics
                .physical_frames
                .saturating_add(u64::try_from(dispatch.frames.len()).unwrap_or(u64::MAX));
        });
    }

    fn crash_if_armed(&mut self, point: SimTransportCrashPoint) {
        if self.crash_point == Some(point) {
            self.crash_point = None;
            panic_any(SimCrashSignal::Transport(point));
        }
    }
}

impl ReceiverTransport for SimReceiverTransport {
    type Error = SimTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        (receiver_id == &self.receiver_id)
            .then_some(self.generation_id)
            .flatten()
    }

    fn reconcile(&self, dispatch: &TransportDispatch) -> TransportReconciliation {
        update_metrics(&self.metrics, |metrics| {
            metrics.reconciliations = metrics.reconciliations.saturating_add(1);
        });
        self.reconcile_exact(dispatch)
            .unwrap_or(TransportReconciliation::Unavailable)
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        update_metrics(&self.metrics, |metrics| {
            metrics.dispatch_calls = metrics.dispatch_calls.saturating_add(1);
        });
        match self.reconcile_exact(dispatch)? {
            TransportReconciliation::Retained(receipt) => return Ok(receipt),
            TransportReconciliation::RetainedFailure(facts) => {
                return Err(SimTransportError {
                    kind: SimTransportErrorKind::OutcomeUncertain,
                    facts,
                });
            }
            TransportReconciliation::NotObserved => {
                if dispatch.receiver_id != self.receiver_id
                    || self.generation_id != Some(dispatch.generation_id)
                {
                    return Err(safe_error(
                        SimTransportErrorKind::RouteUnavailable,
                        self.zero_delivered,
                    ));
                }
            }
            TransportReconciliation::Evicted => {
                return Err(uncertain_error(
                    SimTransportErrorKind::OutcomeUncertain,
                    self.zero_delivered,
                ));
            }
            TransportReconciliation::Unavailable => {
                return Err(uncertain_error(
                    SimTransportErrorKind::LookupUnavailable,
                    self.zero_delivered,
                ));
            }
            TransportReconciliation::Conflict => {
                return Err(uncertain_error(
                    SimTransportErrorKind::IdentityConflict,
                    self.zero_delivered,
                ));
            }
        }

        let frame_count = u16::try_from(dispatch.frames.len())
            .map_err(|_| safe_error(SimTransportErrorKind::FrameCount, self.zero_delivered))?;
        let delivered_frames = DeliveredFrameCount::try_from(frame_count)
            .map_err(|_| safe_error(SimTransportErrorKind::FrameCount, self.zero_delivered))?;

        self.reserve(dispatch)?;
        self.crash_if_armed(SimTransportCrashPoint::AfterReservation);
        self.set_state(&dispatch.transaction_id, SimJournalState::WriteStarted);

        let failure = self.next_failure.take();
        if failure.is_none_or(|plan| plan.apply_physical_state) {
            self.apply_physical(dispatch);
        } else if failure.is_some_and(|plan| plan.facts.live_write_executed) {
            update_metrics(&self.metrics, |metrics| {
                metrics.physical_dispatches = metrics.physical_dispatches.saturating_add(1);
            });
        }
        self.crash_if_armed(SimTransportCrashPoint::AfterPhysicalWrite);

        if let Some(plan) = failure {
            self.set_state(
                &dispatch.transaction_id,
                SimJournalState::RetainedFailure(plan.facts),
            );
            self.crash_if_armed(SimTransportCrashPoint::AfterTerminal);
            return Err(SimTransportError {
                kind: SimTransportErrorKind::OutcomeUncertain,
                facts: plan.facts,
            });
        }

        let receipt = TransportReceipt {
            terminal: TransportTerminal::Delivered,
            delivered_frames,
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry_safe: false,
            device_application: DeviceApplicationState::Confirmed,
        };
        self.set_state(&dispatch.transaction_id, SimJournalState::Retained(receipt));
        self.crash_if_armed(SimTransportCrashPoint::AfterTerminal);
        Ok(receipt)
    }
}

fn dispatch_fingerprint(dispatch: &TransportDispatch) -> Result<String, ()> {
    let bytes = serde_json::to_vec(dispatch).map_err(|_| ())?;
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut encoded, "{byte:02x}").map_err(|_| ())?;
    }
    Ok(encoded)
}

const fn safe_error(
    kind: SimTransportErrorKind,
    zero_delivered: DeliveredFrameCount,
) -> SimTransportError {
    SimTransportError {
        kind,
        facts: TransportFailureFacts {
            delivered_frames: zero_delivered,
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: DeviceApplicationState::Unverified,
        },
    }
}

const fn uncertain_error(
    kind: SimTransportErrorKind,
    zero_delivered: DeliveredFrameCount,
) -> SimTransportError {
    SimTransportError {
        kind,
        facts: TransportFailureFacts {
            delivered_frames: zero_delivered,
            side_effect_certainty: SideEffectCertainty::Possible,
            live_write_executed: true,
            automatic_retry_safe: false,
            device_application: DeviceApplicationState::Unverified,
        },
    }
}

fn update_metrics(
    metrics: &Cell<SimTransportMetrics>,
    update: impl FnOnce(&mut SimTransportMetrics),
) {
    let mut current = metrics.get();
    update(&mut current);
    metrics.set(current);
}
