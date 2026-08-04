// SPDX-License-Identifier: GPL-2.0-only

use crate::SubmissionBinding;
use hfx_domain::{
    AuthorizationEpoch, DispatchNonce, FrameIndex, GenerationId, LeaseId, LogicalDeviceId,
    MonotonicMs, ReceiverId, RequestId, SessionId, SideEffectCertainty, TransactionClass,
    TransactionId, TransactionState,
};
use hfx_protocol::{
    DeviceProfileBinding, LightingFrame, ProtocolValidationError, ResourceKey, TransactionRequest,
    TransactionTerminal, validate_transaction,
};
use std::collections::BTreeMap;
use std::fmt;

/// Exact lifecycle scope of one volatile software-effect frame.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EffectFrameKey {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub device_id: LogicalDeviceId,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct EffectFrameOwner {
    session_id: SessionId,
    authorization_epoch: AuthorizationEpoch,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DesiredEffectFrame {
    client_id: hfx_domain::ClientId,
    lease_id: LeaseId,
    receiver_profile_id: hfx_domain::ProfileId,
    receiver_profile_digest: hfx_domain::ProfileDigest,
    device_profile: DeviceProfileBinding,
    resource: ResourceKey,
    frame: LightingFrame,
    owner: EffectFrameOwner,
    revision: u64,
}

/// Fresh transaction material for one owner-bound effect replay.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EffectReplay {
    pub request: TransactionRequest,
    pub submission: SubmissionBinding,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EffectCapture {
    pub captured: usize,
    pub evicted: Vec<EffectFrameKey>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DesiredEffectError {
    InvalidCapacity,
    InvalidTransaction(ProtocolValidationError),
    RevisionExhausted,
}

impl fmt::Display for DesiredEffectError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "desired effect frame capacity is invalid",
            Self::InvalidTransaction(_) => "desired effect transaction is invalid",
            Self::RevisionExhausted => "desired effect frame revision is exhausted",
        })
    }
}

impl std::error::Error for DesiredEffectError {}

/// Bounded process-local desired state for software-generated RGB frames.
///
/// This registry never grants authority and never writes hardware. A replay
/// must pass through the ordinary session, lease, generation, profile, device,
/// transaction, and transport checks again. Process exit deliberately drops
/// every entry because software effects belong to a live application owner.
#[derive(Clone, Debug)]
pub struct VolatileEffectRegistry {
    capacity: usize,
    next_revision: u64,
    entries: BTreeMap<EffectFrameKey, DesiredEffectFrame>,
}

impl VolatileEffectRegistry {
    /// Creates one bounded registry.
    ///
    /// # Errors
    ///
    /// Rejects capacities outside the shared transaction bound.
    pub fn new(capacity: usize) -> Result<Self, DesiredEffectError> {
        if !(1..=4096).contains(&capacity) {
            return Err(DesiredEffectError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            next_revision: 1,
            entries: BTreeMap::new(),
        })
    }

    /// Records the latest admitted effect frame for each target.
    ///
    /// Non-effect requests are ignored. Capacity pressure evicts the oldest
    /// desired target deterministically; it never changes transaction outcome.
    ///
    /// # Errors
    ///
    /// Returns an error when the request is malformed or the finite process
    /// revision is exhausted. The registry remains unchanged on error.
    pub fn capture_admitted(
        &mut self,
        request: &TransactionRequest,
        submission: &SubmissionBinding,
    ) -> Result<EffectCapture, DesiredEffectError> {
        if request.transaction_class != TransactionClass::EffectFrame {
            return Ok(EffectCapture {
                captured: 0,
                evicted: Vec::new(),
            });
        }
        self.validate_admission(request)?;
        let revision = self
            .next_revision
            .checked_add(1)
            .ok_or(DesiredEffectError::RevisionExhausted)?;
        let mut staged = Vec::with_capacity(request.frames.len());
        for frame in &request.frames {
            let device_profile = request
                .device_profiles
                .iter()
                .find(|profile| profile.device_id == frame.device_id)
                .ok_or(DesiredEffectError::InvalidTransaction(
                    ProtocolValidationError::FrameWithoutProfileBinding,
                ))?
                .clone();
            let resource = request
                .resources
                .iter()
                .find(|resource| resource.device_id == frame.device_id)
                .ok_or(DesiredEffectError::InvalidTransaction(
                    ProtocolValidationError::FrameWithoutLightingLease,
                ))?
                .clone();
            let key = EffectFrameKey {
                receiver_id: request.receiver_id.clone(),
                generation_id: request.generation_id,
                device_id: frame.device_id.clone(),
            };
            let desired = DesiredEffectFrame {
                client_id: request.client_id.clone(),
                lease_id: request.lease_id.clone(),
                receiver_profile_id: request.receiver_profile_id.clone(),
                receiver_profile_digest: request.receiver_profile_digest.clone(),
                device_profile,
                resource,
                frame: frame.clone(),
                owner: EffectFrameOwner {
                    session_id: submission.session_id.clone(),
                    authorization_epoch: submission.authorization_epoch,
                },
                revision: self.next_revision,
            };
            staged.push((key, desired));
        }

        self.next_revision = revision;
        let captured = staged.len();
        for (key, desired) in staged {
            self.entries.insert(key, desired);
        }
        let mut evicted = Vec::new();
        while self.entries.len() > self.capacity {
            let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(key, desired)| (desired.revision, *key))
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            self.entries.remove(&oldest);
            evicted.push(oldest);
        }
        Ok(EffectCapture { captured, evicted })
    }

    /// Preflights effect capture without changing registry state.
    ///
    /// # Errors
    ///
    /// Returns an error when the effect request is malformed or no further
    /// process-local revision can be represented.
    pub fn validate_admission(
        &self,
        request: &TransactionRequest,
    ) -> Result<(), DesiredEffectError> {
        if request.transaction_class != TransactionClass::EffectFrame {
            return Ok(());
        }
        validate_transaction(request).map_err(DesiredEffectError::InvalidTransaction)?;
        self.next_revision
            .checked_add(1)
            .ok_or(DesiredEffectError::RevisionExhausted)?;
        Ok(())
    }

    /// Builds a fresh, single-target replay without changing registry state.
    /// The caller must submit it through the ordinary transaction authority.
    ///
    /// # Errors
    ///
    /// Returns an error only if internally retained validated state can no
    /// longer form a valid request.
    #[allow(clippy::too_many_arguments)]
    pub fn plan_replay(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
        request_id: RequestId,
        transaction_id: TransactionId,
        dispatch_nonce: DispatchNonce,
        deadline_ms: MonotonicMs,
    ) -> Result<Option<EffectReplay>, DesiredEffectError> {
        let key = EffectFrameKey {
            receiver_id: receiver_id.clone(),
            generation_id,
            device_id: device_id.clone(),
        };
        let Some(desired) = self.entries.get(&key) else {
            return Ok(None);
        };
        let mut frame = desired.frame.clone();
        frame.frame_index = FrameIndex::try_from(FrameIndex::MIN).map_err(|_| {
            DesiredEffectError::InvalidTransaction(ProtocolValidationError::NonCanonicalFrameOrder)
        })?;
        let request = TransactionRequest {
            request_id,
            transaction_id,
            client_id: desired.client_id.clone(),
            lease_id: desired.lease_id.clone(),
            receiver_id: receiver_id.clone(),
            generation_id,
            receiver_profile_id: desired.receiver_profile_id.clone(),
            receiver_profile_digest: desired.receiver_profile_digest.clone(),
            device_profiles: vec![desired.device_profile.clone()],
            transaction_class: TransactionClass::EffectFrame,
            stable_intents: Vec::new(),
            deadline_ms,
            resources: vec![desired.resource.clone()],
            frames: vec![frame],
        };
        validate_transaction(&request).map_err(DesiredEffectError::InvalidTransaction)?;
        Ok(Some(EffectReplay {
            request,
            submission: SubmissionBinding {
                session_id: desired.owner.session_id.clone(),
                authorization_epoch: desired.owner.authorization_epoch,
                dispatch_nonce,
            },
        }))
    }

    /// Drops volatile effect intent after a definitive user-authored stable
    /// transaction replaces it.
    pub fn observe_completed(
        &mut self,
        request: &TransactionRequest,
        terminal: &TransactionTerminal,
    ) {
        if request.transaction_class != TransactionClass::StaticLighting
            || !is_definitive_success(request, terminal)
        {
            return;
        }
        for frame in &request.frames {
            self.remove_target(
                &request.receiver_id,
                request.generation_id,
                &frame.device_id,
            );
        }
    }

    pub fn remove_target(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device_id: &LogicalDeviceId,
    ) -> bool {
        self.entries
            .remove(&EffectFrameKey {
                receiver_id: receiver_id.clone(),
                generation_id,
                device_id: device_id.clone(),
            })
            .is_some()
    }

    pub fn remove_session(&mut self, session_id: &SessionId) -> usize {
        let before = self.entries.len();
        self.entries
            .retain(|_, desired| desired.owner.session_id != *session_id);
        before - self.entries.len()
    }

    pub fn remove_lease(&mut self, lease_id: &LeaseId) -> usize {
        let before = self.entries.len();
        self.entries
            .retain(|_, desired| desired.lease_id != *lease_id);
        before - self.entries.len()
    }

    pub fn remove_generation(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> usize {
        let before = self.entries.len();
        self.entries
            .retain(|key, _| key.receiver_id != *receiver_id || key.generation_id != generation_id);
        before - self.entries.len()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

fn is_definitive_success(request: &TransactionRequest, terminal: &TransactionTerminal) -> bool {
    let Ok(frame_count) = u16::try_from(request.frames.len()) else {
        return false;
    };
    terminal.request_id == request.request_id
        && terminal.transaction_id == request.transaction_id
        && terminal.receiver_id == request.receiver_id
        && terminal.generation_id == request.generation_id
        && terminal.state == TransactionState::Succeeded
        && terminal.declared_frames.get() == frame_count
        && terminal.delivered_frames.get() == frame_count
        && terminal.side_effect_certainty == SideEffectCertainty::Committed
        && terminal.live_write_executed
        && terminal.device_application != hfx_domain::DeviceApplicationState::Rejected
        && terminal.error_kind.is_none()
}
