// SPDX-License-Identifier: GPL-2.0-only

//! Endpoint-local return epoch and bounded replay-window policy.

use super::LifecycleError;
use hfx_domain::{MonotonicMs, RestoreDeliveryState, ReturnEvidence, ReturnPhase, SequenceNumber};

pub const MAX_RETURN_RETRY_ATTEMPTS: usize = 32;
pub const MAX_RETURN_RETRY_WINDOW_MS: u64 = 30_000;

/// One immutable opening boundary plus its truthful, evolving delivery state.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReturnEpoch {
    sequence: SequenceNumber,
    opened_at_ms: MonotonicMs,
    opening_evidence: ReturnEvidence,
    phase: ReturnPhase,
    delivery: RestoreDeliveryState,
}

impl ReturnEpoch {
    #[must_use]
    pub const fn sequence(self) -> SequenceNumber {
        self.sequence
    }

    #[must_use]
    pub const fn opened_at_ms(self) -> MonotonicMs {
        self.opened_at_ms
    }

    #[must_use]
    pub const fn opening_evidence(self) -> ReturnEvidence {
        self.opening_evidence
    }

    #[must_use]
    pub const fn phase(self) -> ReturnPhase {
        self.phase
    }

    #[must_use]
    pub const fn delivery(self) -> RestoreDeliveryState {
        self.delivery
    }
}

/// A pure transition emitted by [`EndpointReturnLifecycle`].
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReturnTransition {
    None,
    QuiescenceCandidate,
    BecameUnavailable,
    EpochOpened(ReturnEpoch),
    EpochAccelerated(ReturnEpoch),
    Synchronized(ReturnEpoch),
    WindowCompleted(ReturnEpoch),
}

/// Validated universal policy for one qualified endpoint profile.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EndpointReturnPolicy {
    quiescence_candidate_ms: u64,
    observation_jitter_ms: u64,
    retry_offsets_ms: Vec<u64>,
    open_evidence: Vec<ReturnEvidence>,
    accelerator_evidence: Vec<ReturnEvidence>,
}

impl EndpointReturnPolicy {
    /// Builds a bounded policy from generated profile data.
    ///
    /// # Errors
    ///
    /// Rejects empty evidence, unordered or unbounded retries, zero
    /// quiescence, duplicate evidence, or opening evidence that cannot also
    /// accelerate its own active epoch.
    pub fn new(
        quiescence_candidate_ms: u64,
        observation_jitter_ms: u64,
        retry_offsets_ms: &[u64],
        open_evidence: &[ReturnEvidence],
        accelerator_evidence: &[ReturnEvidence],
    ) -> Result<Self, LifecycleError> {
        let retry_offsets_are_valid = (2..=MAX_RETURN_RETRY_ATTEMPTS)
            .contains(&retry_offsets_ms.len())
            && retry_offsets_ms.first() == Some(&0)
            && retry_offsets_ms.windows(2).all(|pair| pair[0] < pair[1])
            && retry_offsets_ms
                .last()
                .is_some_and(|offset| *offset <= MAX_RETURN_RETRY_WINDOW_MS);
        let open_evidence_is_valid = !open_evidence.is_empty()
            && evidence_is_unique(open_evidence)
            && open_evidence
                .iter()
                .all(|evidence| accelerator_evidence.contains(evidence));
        if quiescence_candidate_ms == 0
            || observation_jitter_ms >= quiescence_candidate_ms
            || !retry_offsets_are_valid
            || !open_evidence_is_valid
            || accelerator_evidence.is_empty()
            || !evidence_is_unique(accelerator_evidence)
        {
            return Err(LifecycleError::InvalidReturnPolicy);
        }
        Ok(Self {
            quiescence_candidate_ms,
            observation_jitter_ms,
            retry_offsets_ms: retry_offsets_ms.to_vec(),
            open_evidence: open_evidence.to_vec(),
            accelerator_evidence: accelerator_evidence.to_vec(),
        })
    }

    #[must_use]
    pub const fn quiescence_candidate_ms(&self) -> u64 {
        self.quiescence_candidate_ms
    }

    #[must_use]
    pub const fn observation_jitter_ms(&self) -> u64 {
        self.observation_jitter_ms
    }

    #[must_use]
    pub const fn effective_quiescence_ms(&self) -> u64 {
        self.quiescence_candidate_ms - self.observation_jitter_ms
    }

    #[must_use]
    pub fn retry_offsets_ms(&self) -> &[u64] {
        &self.retry_offsets_ms
    }

    #[must_use]
    pub fn open_evidence(&self) -> &[ReturnEvidence] {
        &self.open_evidence
    }

    #[must_use]
    pub fn accelerator_evidence(&self) -> &[ReturnEvidence] {
        &self.accelerator_evidence
    }
}

fn evidence_is_unique(evidence: &[ReturnEvidence]) -> bool {
    evidence
        .iter()
        .enumerate()
        .all(|(index, item)| !evidence[index + 1..].contains(item))
}

/// Pure, endpoint-local detector for return epochs and bounded replay windows.
///
/// It never infers an exact sleep timer. Endpoint activity opens an epoch only
/// after the policy's conservative quiescence candidate; explicit route return
/// can open immediately. Other profile-selected facts accelerate an existing
/// epoch without extending its deadline.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EndpointReturnLifecycle {
    policy: EndpointReturnPolicy,
    phase: ReturnPhase,
    last_sequence: Option<SequenceNumber>,
    last_observed_at_ms: Option<MonotonicMs>,
    last_activity_ms: Option<MonotonicMs>,
    epoch: Option<ReturnEpoch>,
    seen_accelerators: u16,
}

impl EndpointReturnLifecycle {
    #[must_use]
    pub const fn new(policy: EndpointReturnPolicy) -> Self {
        Self {
            policy,
            phase: ReturnPhase::Stable,
            last_sequence: None,
            last_observed_at_ms: None,
            last_activity_ms: None,
            epoch: None,
            seen_accelerators: 0,
        }
    }

    #[must_use]
    pub const fn policy(&self) -> &EndpointReturnPolicy {
        &self.policy
    }

    #[must_use]
    pub const fn epoch(&self) -> Option<ReturnEpoch> {
        self.epoch
    }

    #[must_use]
    pub const fn phase(&self) -> ReturnPhase {
        self.phase
    }

    /// Clears volatile activity/epoch anchors at a receiver suspend boundary.
    pub fn reset_observation_anchor(&mut self) {
        self.last_sequence = None;
        self.last_observed_at_ms = None;
        self.last_activity_ms = None;
        self.epoch = None;
        self.seen_accelerators = 0;
        self.phase = ReturnPhase::Stable;
    }

    /// Applies one ordered passive fact and returns only a typed lifecycle edge.
    pub fn observe(
        &mut self,
        evidence: ReturnEvidence,
        sequence: SequenceNumber,
        observed_at_ms: MonotonicMs,
    ) -> ReturnTransition {
        if self
            .last_sequence
            .is_some_and(|current| sequence <= current)
            || self
                .last_observed_at_ms
                .is_some_and(|current| observed_at_ms < current)
        {
            return ReturnTransition::None;
        }

        let completed = self.complete_if_expired(observed_at_ms);
        if evidence == ReturnEvidence::RouteDeparture {
            self.last_sequence = Some(sequence);
            self.last_observed_at_ms = Some(observed_at_ms);
            self.epoch = None;
            self.seen_accelerators = 0;
            self.phase = ReturnPhase::Unavailable;
            return ReturnTransition::BecameUnavailable;
        }
        if evidence == ReturnEvidence::ApplicationOwnershipLoss {
            self.last_sequence = Some(sequence);
            self.last_observed_at_ms = Some(observed_at_ms);
            self.epoch = None;
            self.seen_accelerators = 0;
            self.phase = ReturnPhase::Stable;
            return completed.map_or(ReturnTransition::None, ReturnTransition::WindowCompleted);
        }
        let activity_opens = if evidence == ReturnEvidence::EndpointActivity {
            self.phase == ReturnPhase::Unavailable
                || self.last_activity_ms.is_none_or(|prior| {
                    observed_at_ms.get().saturating_sub(prior.get())
                        >= self.policy.effective_quiescence_ms()
                })
        } else {
            true
        };
        if evidence == ReturnEvidence::EndpointActivity {
            self.last_activity_ms = Some(observed_at_ms);
        }
        self.last_sequence = Some(sequence);
        self.last_observed_at_ms = Some(observed_at_ms);

        if activity_opens && self.policy.open_evidence.contains(&evidence) {
            let epoch = ReturnEpoch {
                sequence,
                opened_at_ms: observed_at_ms,
                opening_evidence: evidence,
                phase: ReturnPhase::Returning,
                delivery: RestoreDeliveryState::NotAttempted,
            };
            self.epoch = Some(epoch);
            self.seen_accelerators = if evidence == ReturnEvidence::EndpointActivity {
                0
            } else {
                evidence_bit(evidence)
            };
            self.phase = ReturnPhase::Returning;
            return ReturnTransition::EpochOpened(epoch);
        }

        if let Some(epoch) = self.epoch
            && epoch.phase == ReturnPhase::Returning
            && self.policy.accelerator_evidence.contains(&evidence)
            && (evidence == ReturnEvidence::EndpointActivity
                || self.seen_accelerators & evidence_bit(evidence) == 0)
        {
            if evidence != ReturnEvidence::EndpointActivity {
                self.seen_accelerators |= evidence_bit(evidence);
            }
            return ReturnTransition::EpochAccelerated(epoch);
        }
        completed.map_or(ReturnTransition::None, ReturnTransition::WindowCompleted)
    }

    /// Records receiver-level delivery for the matching epoch only.
    ///
    /// A receiver-delivered result intentionally leaves the phase in
    /// `Returning`: the receiver acknowledgement is not proof that the child
    /// applied lighting.
    pub fn note_delivery(
        &mut self,
        epoch_sequence: SequenceNumber,
        delivery: RestoreDeliveryState,
    ) -> ReturnTransition {
        let Some(epoch) = self.epoch.as_mut() else {
            return ReturnTransition::None;
        };
        if epoch.sequence != epoch_sequence || epoch.phase != ReturnPhase::Returning {
            return ReturnTransition::None;
        }
        epoch.delivery = merge_delivery(epoch.delivery, delivery);
        if epoch.delivery == RestoreDeliveryState::ChildConfirmed {
            epoch.phase = ReturnPhase::Synchronized;
            self.phase = ReturnPhase::Synchronized;
            return ReturnTransition::Synchronized(*epoch);
        }
        ReturnTransition::None
    }

    /// Closes an elapsed retry window without making a physical-state claim.
    pub fn advance(&mut self, now: MonotonicMs) -> ReturnTransition {
        if let Some(epoch) = self.complete_if_expired(now) {
            return ReturnTransition::WindowCompleted(epoch);
        }
        if matches!(self.phase, ReturnPhase::Stable | ReturnPhase::Synchronized)
            && self.last_activity_ms.is_some_and(|activity| {
                now.get().saturating_sub(activity.get()) >= self.policy.quiescence_candidate_ms
            })
        {
            self.phase = ReturnPhase::QuiescentCandidate;
            return ReturnTransition::QuiescenceCandidate;
        }
        ReturnTransition::None
    }

    fn complete_if_expired(&mut self, now: MonotonicMs) -> Option<ReturnEpoch> {
        let epoch = self.epoch.as_mut()?;
        if epoch.phase != ReturnPhase::Returning {
            return None;
        }
        let final_offset = *self
            .policy
            .retry_offsets_ms
            .last()
            .expect("validated return policy has retries");
        let deadline = epoch.opened_at_ms.get().saturating_add(final_offset);
        if now.get() <= deadline {
            return None;
        }
        epoch.phase = ReturnPhase::Stable;
        self.phase = ReturnPhase::Stable;
        Some(*epoch)
    }
}

const fn evidence_bit(evidence: ReturnEvidence) -> u16 {
    match evidence {
        ReturnEvidence::ApplicationOwnershipLoss => 1 << 0,
        ReturnEvidence::BatteryReport => 1 << 1,
        ReturnEvidence::ContactReport => 1 << 2,
        ReturnEvidence::EndpointActivity => 1 << 3,
        ReturnEvidence::EndpointIdentity => 1 << 4,
        ReturnEvidence::EndpointStatus => 1 << 5,
        ReturnEvidence::ReceiverGeneration => 1 << 6,
        ReturnEvidence::RouteDeparture => 1 << 7,
        ReturnEvidence::RouteReturn => 1 << 8,
        ReturnEvidence::SystemResume => 1 << 9,
    }
}

const fn merge_delivery(
    current: RestoreDeliveryState,
    candidate: RestoreDeliveryState,
) -> RestoreDeliveryState {
    use RestoreDeliveryState::{
        ChildConfirmed, ChildRejected, NotAttempted, OutcomeUncertain, ReceiverDelivered,
        ReceiverRejected,
    };
    match (current, candidate) {
        (ChildConfirmed, _) | (_, ChildConfirmed) => ChildConfirmed,
        (ChildRejected, _) | (_, ChildRejected) => ChildRejected,
        (ReceiverDelivered, _) | (_, ReceiverDelivered) => ReceiverDelivered,
        (OutcomeUncertain, _) | (_, OutcomeUncertain) => OutcomeUncertain,
        (ReceiverRejected, _) | (_, ReceiverRejected) => ReceiverRejected,
        (NotAttempted, NotAttempted) => NotAttempted,
    }
}
