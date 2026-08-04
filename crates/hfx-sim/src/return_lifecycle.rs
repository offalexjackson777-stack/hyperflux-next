// SPDX-License-Identifier: GPL-2.0-only

//! Profile-driven harness around the production endpoint return machine.

use hfx_core::{
    EndpointReturnLifecycle, EndpointReturnPolicy, LifecycleError, RestoreDeliveryState,
    ReturnEpoch, ReturnEvidence, ReturnPhase, ReturnTransition,
};
use hfx_domain::{MonotonicMs, ProfileId, SequenceNumber};
use hfx_profiles::{RuntimeProfileCatalog, return_policy_by_id};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SimRetryEnvelope {
    pub epoch_sequence: SequenceNumber,
    pub opened_at_ms: MonotonicMs,
    pub due_at_ms: Vec<MonotonicMs>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SimReturnLifecycleError {
    MissingPolicy(String),
    InvalidPolicy(LifecycleError),
    TimeOverflow,
}

impl fmt::Display for SimReturnLifecycleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingPolicy(policy_id) => {
                write!(
                    formatter,
                    "profile references missing return policy {policy_id}"
                )
            }
            Self::InvalidPolicy(error) => error.fmt(formatter),
            Self::TimeOverflow => formatter.write_str("simulated retry time overflowed"),
        }
    }
}

impl std::error::Error for SimReturnLifecycleError {}

/// Deterministic simulator facade over the same generated policy and pure
/// lifecycle machine used by production observation translation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SimReturnLifecycle {
    profile_id: ProfileId,
    lifecycle: EndpointReturnLifecycle,
}

impl SimReturnLifecycle {
    /// Resolves one qualified profile without product-specific simulator code.
    /// Unknown profiles and profiles without writable lifecycle policy return
    /// `None`, matching production's fail-closed behavior.
    ///
    /// # Errors
    ///
    /// Returns an error when generated profile and policy authorities disagree.
    pub fn from_profile(
        catalog: &RuntimeProfileCatalog,
        profile_id: &ProfileId,
    ) -> Result<Option<Self>, SimReturnLifecycleError> {
        let Some(profile) = catalog.profile(profile_id) else {
            return Ok(None);
        };
        let Some(binding) = profile.lifecycle else {
            return Ok(None);
        };
        let policy = return_policy_by_id(binding.return_policy_id).ok_or_else(|| {
            SimReturnLifecycleError::MissingPolicy(binding.return_policy_id.to_owned())
        })?;
        if !binding
            .open_evidence
            .iter()
            .all(|evidence| policy.allowed_open_evidence.contains(evidence))
            || !binding
                .accelerator_evidence
                .iter()
                .all(|evidence| policy.allowed_accelerator_evidence.contains(evidence))
        {
            return Err(SimReturnLifecycleError::InvalidPolicy(
                LifecycleError::InvalidReturnPolicy,
            ));
        }
        let open_evidence = binding
            .open_evidence
            .iter()
            .copied()
            .map(hfx_profiles::ReturnEvidenceSource::domain)
            .collect::<Vec<_>>();
        let accelerator_evidence = binding
            .accelerator_evidence
            .iter()
            .copied()
            .map(hfx_profiles::ReturnEvidenceSource::domain)
            .collect::<Vec<_>>();
        let policy = EndpointReturnPolicy::new(
            policy.quiescence_candidate_ms,
            policy.observation_jitter_ms,
            policy.retry_offsets_ms,
            &open_evidence,
            &accelerator_evidence,
        )
        .map_err(SimReturnLifecycleError::InvalidPolicy)?;
        Ok(Some(Self {
            profile_id: profile.profile_id.clone(),
            lifecycle: EndpointReturnLifecycle::new(policy),
        }))
    }

    #[must_use]
    pub const fn profile_id(&self) -> &ProfileId {
        &self.profile_id
    }

    #[must_use]
    pub const fn phase(&self) -> ReturnPhase {
        self.lifecycle.phase()
    }

    #[must_use]
    pub const fn epoch(&self) -> Option<ReturnEpoch> {
        self.lifecycle.epoch()
    }

    pub fn reset_observation_anchor(&mut self) {
        self.lifecycle.reset_observation_anchor();
    }

    pub fn observe(
        &mut self,
        evidence: ReturnEvidence,
        sequence: SequenceNumber,
        observed_at_ms: MonotonicMs,
    ) -> ReturnTransition {
        self.lifecycle.observe(evidence, sequence, observed_at_ms)
    }

    pub fn note_delivery(
        &mut self,
        epoch_sequence: SequenceNumber,
        delivery: RestoreDeliveryState,
    ) -> ReturnTransition {
        self.lifecycle.note_delivery(epoch_sequence, delivery)
    }

    pub fn advance(&mut self, now: MonotonicMs) -> ReturnTransition {
        self.lifecycle.advance(now)
    }

    /// Projects the exact bounded retry envelope for the active epoch.
    ///
    /// # Errors
    ///
    /// Returns an error when simulated monotonic addition overflows.
    pub fn retry_envelope(&self) -> Result<Option<SimRetryEnvelope>, SimReturnLifecycleError> {
        let Some(epoch) = self.lifecycle.epoch() else {
            return Ok(None);
        };
        let due_at_ms = self
            .lifecycle
            .policy()
            .retry_offsets_ms()
            .iter()
            .map(|offset| {
                epoch
                    .opened_at_ms()
                    .get()
                    .checked_add(*offset)
                    .and_then(|value| MonotonicMs::try_from(value).ok())
                    .ok_or(SimReturnLifecycleError::TimeOverflow)
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Some(SimRetryEnvelope {
            epoch_sequence: epoch.sequence(),
            opened_at_ms: epoch.opened_at_ms(),
            due_at_ms,
        }))
    }
}
