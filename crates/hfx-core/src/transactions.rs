// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    AuthorizationEpoch, ClientId, DispatchNonce, GenerationId, MonotonicMs, QueueAdmission,
    ReceiverId, RequestDigest, RequestId, SessionId, TransactionClass, TransactionId,
    TransactionState,
};
use hfx_protocol::{
    ProtocolValidationError, TransactionRequest, TransactionResult, validate_transaction,
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::fmt::{self, Write as _};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RequestDigestError {
    InvalidRequest(ProtocolValidationError),
    Serialization,
    InvalidDigest,
}

impl fmt::Display for RequestDigestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidRequest(_) => "request digest input is structurally invalid",
            Self::Serialization => "request digest serialization failed",
            Self::InvalidDigest => "request digest could not be represented",
        })
    }
}

impl std::error::Error for RequestDigestError {}

/// Computes the schema-bound SHA-256 identity of one validated request.
///
/// The generated Rust request has a fixed field order and every set-like field
/// is required to be canonical before serialization, making the compact JSON
/// encoding deterministic for this protocol version.
///
/// # Errors
///
/// Returns an error when the request is invalid, cannot be serialized, or the
/// resulting digest cannot satisfy the domain contract.
pub fn canonical_request_digest(
    request: &TransactionRequest,
) -> Result<RequestDigest, RequestDigestError> {
    validate_transaction(request).map_err(RequestDigestError::InvalidRequest)?;
    let bytes = serde_json::to_vec(request).map_err(|_| RequestDigestError::Serialization)?;
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut encoded, "{byte:02x}").map_err(|_| RequestDigestError::Serialization)?;
    }
    RequestDigest::try_from(encoded).map_err(|_| RequestDigestError::InvalidDigest)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransactionTransitionError {
    InvalidTransition {
        from: TransactionState,
        to: TransactionState,
    },
}

impl fmt::Display for TransactionTransitionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTransition { from, to } => {
                write!(
                    formatter,
                    "invalid transaction transition from {from} to {to}"
                )
            }
        }
    }
}

impl std::error::Error for TransactionTransitionError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransactionMachine {
    state: TransactionState,
}

impl Default for TransactionMachine {
    fn default() -> Self {
        Self {
            state: TransactionState::Created,
        }
    }
}

impl TransactionMachine {
    #[must_use]
    pub const fn state(self) -> TransactionState {
        self.state
    }

    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(
            self.state,
            TransactionState::Succeeded
                | TransactionState::Failed
                | TransactionState::Revoked
                | TransactionState::Superseded
        )
    }

    /// Advances through one declared transaction transition.
    ///
    /// # Errors
    ///
    /// Returns an error when the requested edge skips required authority or
    /// attempts to leave an immutable terminal state.
    pub fn advance(&mut self, next: TransactionState) -> Result<(), TransactionTransitionError> {
        let allowed = matches!(
            (self.state, next),
            (TransactionState::Created, TransactionState::Validated)
                | (
                    TransactionState::Validated,
                    TransactionState::OwnershipBound | TransactionState::Revoked
                )
                | (
                    TransactionState::OwnershipBound,
                    TransactionState::GenerationBound | TransactionState::Revoked
                )
                | (
                    TransactionState::GenerationBound,
                    TransactionState::Queued | TransactionState::Revoked
                )
                | (
                    TransactionState::Queued,
                    TransactionState::Sent
                        | TransactionState::Revoked
                        | TransactionState::Superseded
                )
                | (
                    TransactionState::Sent,
                    TransactionState::HealthPending
                        | TransactionState::Succeeded
                        | TransactionState::Failed
                        | TransactionState::Revoked
                )
                | (
                    TransactionState::HealthPending,
                    TransactionState::Succeeded
                        | TransactionState::Failed
                        | TransactionState::Revoked
                )
        );
        if !allowed {
            return Err(TransactionTransitionError::InvalidTransition {
                from: self.state,
                to: next,
            });
        }
        self.state = next;
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueuedTransaction {
    pub request: TransactionRequest,
    pub request_digest: RequestDigest,
    pub session_id: SessionId,
    pub authorization_epoch: AuthorizationEpoch,
    pub dispatch_nonce: DispatchNonce,
    pub admission: QueueAdmission,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueueDecision {
    pub admission: QueueAdmission,
    pub superseded: Option<QueuedTransaction>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DequeueDecision {
    pub expired: Vec<QueuedTransaction>,
    pub next: Option<QueuedTransaction>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TransactionQueueError {
    InvalidCapacity,
    InvalidRequest(ProtocolValidationError),
    DeadlineElapsed,
    Full,
}

impl fmt::Display for TransactionQueueError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "transaction queue capacity is invalid",
            Self::InvalidRequest(_) => "transaction request is structurally invalid",
            Self::DeadlineElapsed => "transaction deadline elapsed before admission",
            Self::Full => "transaction queue has no admissible capacity",
        })
    }
}

impl std::error::Error for TransactionQueueError {}

#[derive(Clone, Debug)]
pub struct BoundedTransactionQueue {
    capacity: usize,
    pending: VecDeque<QueuedTransaction>,
}

impl BoundedTransactionQueue {
    /// Creates one bounded FIFO transaction queue.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is outside the shared queue bound.
    pub fn new(capacity: usize) -> Result<Self, TransactionQueueError> {
        if !(1..=4096).contains(&capacity) {
            return Err(TransactionQueueError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            pending: VecDeque::with_capacity(capacity),
        })
    }

    /// Admits one validated request, coalescing only an unsent obsolete effect
    /// frame with exactly the same authority and resource scope.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed requests, elapsed deadlines, or full
    /// queues without an eligible obsolete effect frame.
    pub fn admit(
        &mut self,
        mut transaction: QueuedTransaction,
        now: MonotonicMs,
    ) -> Result<QueueDecision, TransactionQueueError> {
        validate_transaction(&transaction.request)
            .map_err(TransactionQueueError::InvalidRequest)?;
        if transaction.request.deadline_ms <= now {
            return Err(TransactionQueueError::DeadlineElapsed);
        }

        if transaction.request.transaction_class == TransactionClass::EffectFrame
            && let Some(position) = self
                .pending
                .iter()
                .position(|candidate| coalescing_scope(candidate, &transaction))
            && let Some(superseded) = self.pending.remove(position)
        {
            transaction.admission = QueueAdmission::Coalesced;
            self.pending.push_back(transaction);
            return Ok(QueueDecision {
                admission: QueueAdmission::Coalesced,
                superseded: Some(superseded),
            });
        }
        if self.pending.len() == self.capacity {
            return Err(TransactionQueueError::Full);
        }
        transaction.admission = QueueAdmission::Enqueued;
        self.pending.push_back(transaction);
        Ok(QueueDecision {
            admission: QueueAdmission::Enqueued,
            superseded: None,
        })
    }

    pub fn take_next(&mut self, now: MonotonicMs) -> DequeueDecision {
        let expired = self.expire_pending(now);
        DequeueDecision {
            expired,
            next: self.pending.pop_front(),
        }
    }

    /// Removes one exact transaction after expiring elapsed work.
    ///
    /// Other live queue entries retain their relative order.
    pub fn take_transaction(
        &mut self,
        transaction_id: &TransactionId,
        now: MonotonicMs,
    ) -> DequeueDecision {
        let expired = self.expire_pending(now);
        let next = self
            .pending
            .iter()
            .position(|queued| &queued.request.transaction_id == transaction_id)
            .and_then(|position| self.pending.remove(position));
        DequeueDecision { expired, next }
    }

    pub fn invalidate_generation(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Vec<QueuedTransaction> {
        self.remove_matching(|transaction| {
            &transaction.request.receiver_id == receiver_id
                && transaction.request.generation_id == generation_id
        })
    }

    pub fn invalidate_session(&mut self, session_id: &SessionId) -> Vec<QueuedTransaction> {
        self.remove_matching(|transaction| &transaction.session_id == session_id)
    }

    pub fn remove_transaction(
        &mut self,
        transaction_id: &TransactionId,
    ) -> Option<QueuedTransaction> {
        let position = self
            .pending
            .iter()
            .position(|queued| &queued.request.transaction_id == transaction_id)?;
        self.pending.remove(position)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.pending.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    fn remove_matching(
        &mut self,
        predicate: impl Fn(&QueuedTransaction) -> bool,
    ) -> Vec<QueuedTransaction> {
        let mut removed = Vec::new();
        self.pending.retain(|transaction| {
            if predicate(transaction) {
                removed.push(transaction.clone());
                false
            } else {
                true
            }
        });
        removed
    }

    fn expire_pending(&mut self, now: MonotonicMs) -> Vec<QueuedTransaction> {
        let mut expired = Vec::new();
        self.pending.retain(|transaction| {
            if transaction.request.deadline_ms <= now {
                expired.push(transaction.clone());
                false
            } else {
                true
            }
        });
        expired
    }
}

fn coalescing_scope(left: &QueuedTransaction, right: &QueuedTransaction) -> bool {
    left.request.transaction_class == TransactionClass::EffectFrame
        && left.session_id == right.session_id
        && left.authorization_epoch == right.authorization_epoch
        && left.request.client_id == right.request.client_id
        && left.request.lease_id == right.request.lease_id
        && left.request.receiver_id == right.request.receiver_id
        && left.request.generation_id == right.request.generation_id
        && left.request.resources == right.request.resources
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutcomeLookup<'a> {
    Retained(&'a TransactionResult),
    Evicted,
    Unknown,
    Forbidden,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestReplay<'a> {
    Retained(&'a TransactionResult),
    Evicted(&'a TransactionId),
    Unknown,
    Conflict,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OutcomeJournalError {
    InvalidCapacity,
    CapacityExhausted,
    UnavailableOutcome,
    IdentityChanged,
    InvalidProgression,
    RequestIdReused,
    TerminalOutcomeChanged,
}

impl fmt::Display for OutcomeJournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "outcome journal capacity is invalid",
            Self::CapacityExhausted => "outcome journal is full of nonterminal records",
            Self::UnavailableOutcome => "unavailable lookup results are not retained outcomes",
            Self::IdentityChanged => "transaction outcome identity changed",
            Self::InvalidProgression => "transaction outcome regressed or skipped a required state",
            Self::RequestIdReused => "request identity was reused with different content",
            Self::TerminalOutcomeChanged => "terminal transaction outcome is immutable",
        })
    }
}

impl std::error::Error for OutcomeJournalError {}

#[derive(Clone, Debug)]
struct OutcomeRecord {
    client_id: ClientId,
    request_id: RequestId,
    request_digest: RequestDigest,
    result: TransactionResult,
}

#[derive(Clone, Debug)]
struct EvictedOutcome {
    client_id: ClientId,
    request_id: RequestId,
    request_digest: RequestDigest,
    transaction_id: TransactionId,
}

#[derive(Clone, Debug)]
pub struct BoundedOutcomeJournal {
    capacity: usize,
    retained: BTreeMap<TransactionId, OutcomeRecord>,
    order: VecDeque<TransactionId>,
    evicted: VecDeque<EvictedOutcome>,
}

impl BoundedOutcomeJournal {
    /// Creates a bounded outcome journal and equally bounded eviction memory.
    ///
    /// # Errors
    ///
    /// Returns an error for zero or excessive capacity.
    pub fn new(capacity: usize) -> Result<Self, OutcomeJournalError> {
        if !(1..=4096).contains(&capacity) {
            return Err(OutcomeJournalError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            retained: BTreeMap::new(),
            order: VecDeque::with_capacity(capacity),
            evicted: VecDeque::with_capacity(capacity),
        })
    }

    /// Inserts or advances one retained transaction outcome.
    ///
    /// # Errors
    ///
    /// Returns an error if an unavailable result is stored, identity changes,
    /// a terminal result changes, or all bounded slots are active.
    pub fn record(
        &mut self,
        client_id: ClientId,
        result: TransactionResult,
    ) -> Result<(), OutcomeJournalError> {
        let Some(transaction_id) = transaction_id(&result).cloned() else {
            return Err(OutcomeJournalError::UnavailableOutcome);
        };
        let request_id = request_id(&result)
            .cloned()
            .ok_or(OutcomeJournalError::UnavailableOutcome)?;
        let request_digest = request_digest(&result)
            .cloned()
            .ok_or(OutcomeJournalError::UnavailableOutcome)?;
        if let Some(existing) = self.retained.get(&transaction_id) {
            if existing.client_id != client_id || !same_identity(&existing.result, &result) {
                return Err(OutcomeJournalError::IdentityChanged);
            }
            if is_terminal(&existing.result) {
                return if existing.result == result {
                    Ok(())
                } else {
                    Err(OutcomeJournalError::TerminalOutcomeChanged)
                };
            }
            if !valid_progression(&existing.result, &result) {
                return Err(OutcomeJournalError::InvalidProgression);
            }
            self.retained.insert(
                transaction_id,
                OutcomeRecord {
                    client_id,
                    request_id,
                    request_digest,
                    result,
                },
            );
            return Ok(());
        }
        if self.request_identity_conflicts(
            &client_id,
            &request_id,
            &request_digest,
            &transaction_id,
        ) {
            return Err(OutcomeJournalError::RequestIdReused);
        }

        if self.retained.len() == self.capacity {
            let Some(position) = self.order.iter().position(|id| {
                self.retained
                    .get(id)
                    .is_some_and(|record| is_terminal(&record.result))
            }) else {
                return Err(OutcomeJournalError::CapacityExhausted);
            };
            let Some(evicted_id) = self.order.remove(position) else {
                return Err(OutcomeJournalError::CapacityExhausted);
            };
            let Some(evicted) = self.retained.remove(&evicted_id) else {
                return Err(OutcomeJournalError::CapacityExhausted);
            };
            if self.evicted.len() == self.capacity {
                self.evicted.pop_front();
            }
            self.evicted.push_back(EvictedOutcome {
                client_id: evicted.client_id,
                request_id: evicted.request_id,
                request_digest: evicted.request_digest,
                transaction_id: evicted_id,
            });
        }
        self.order.push_back(transaction_id.clone());
        self.retained.insert(
            transaction_id,
            OutcomeRecord {
                client_id,
                request_id,
                request_digest,
                result,
            },
        );
        Ok(())
    }

    #[must_use]
    pub fn lookup(
        &self,
        client_id: &ClientId,
        transaction_id: &TransactionId,
    ) -> OutcomeLookup<'_> {
        if let Some(record) = self.retained.get(transaction_id) {
            return if &record.client_id == client_id {
                OutcomeLookup::Retained(&record.result)
            } else {
                OutcomeLookup::Forbidden
            };
        }
        self.evicted
            .iter()
            .find(|record| &record.transaction_id == transaction_id)
            .map_or(OutcomeLookup::Unknown, |record| {
                if &record.client_id == client_id {
                    OutcomeLookup::Evicted
                } else {
                    OutcomeLookup::Forbidden
                }
            })
    }

    #[must_use]
    pub fn replay(
        &self,
        client_id: &ClientId,
        request_id: &RequestId,
        request_digest: &RequestDigest,
    ) -> RequestReplay<'_> {
        if let Some(record) = self
            .retained
            .values()
            .find(|record| &record.client_id == client_id && &record.request_id == request_id)
        {
            return if &record.request_digest == request_digest {
                RequestReplay::Retained(&record.result)
            } else {
                RequestReplay::Conflict
            };
        }
        self.evicted
            .iter()
            .find(|record| &record.client_id == client_id && &record.request_id == request_id)
            .map_or(RequestReplay::Unknown, |record| {
                if &record.request_digest == request_digest {
                    RequestReplay::Evicted(&record.transaction_id)
                } else {
                    RequestReplay::Conflict
                }
            })
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.retained.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.retained.is_empty()
    }

    fn request_identity_conflicts(
        &self,
        client_id: &ClientId,
        request_id: &RequestId,
        request_digest: &RequestDigest,
        target_transaction_id: &TransactionId,
    ) -> bool {
        self.retained.values().any(|record| {
            &record.client_id == client_id
                && &record.request_id == request_id
                && (&record.request_digest != request_digest
                    || transaction_id(&record.result) != Some(target_transaction_id))
        }) || self.evicted.iter().any(|record| {
            &record.client_id == client_id
                && &record.request_id == request_id
                && (&record.request_digest != request_digest
                    || &record.transaction_id != target_transaction_id)
        })
    }
}

fn transaction_id(result: &TransactionResult) -> Option<&TransactionId> {
    match result {
        TransactionResult::Progress(progress) => Some(&progress.transaction_id),
        TransactionResult::Terminal(terminal) => Some(&terminal.transaction_id),
        TransactionResult::Unavailable(_) => None,
    }
}

fn request_digest(result: &TransactionResult) -> Option<&RequestDigest> {
    match result {
        TransactionResult::Progress(progress) => Some(&progress.request_digest),
        TransactionResult::Terminal(terminal) => Some(&terminal.request_digest),
        TransactionResult::Unavailable(_) => None,
    }
}

fn request_id(result: &TransactionResult) -> Option<&RequestId> {
    match result {
        TransactionResult::Progress(progress) => Some(&progress.request_id),
        TransactionResult::Terminal(terminal) => Some(&terminal.request_id),
        TransactionResult::Unavailable(_) => None,
    }
}

fn same_identity(left: &TransactionResult, right: &TransactionResult) -> bool {
    transaction_id(left) == transaction_id(right) && request_digest(left) == request_digest(right)
}

fn is_terminal(result: &TransactionResult) -> bool {
    matches!(result, TransactionResult::Terminal(_))
}

fn valid_progression(previous: &TransactionResult, next: &TransactionResult) -> bool {
    match (previous, next) {
        (TransactionResult::Progress(previous), TransactionResult::Progress(next)) => {
            let state_valid = previous.state == next.state
                || matches!(
                    (previous.state, next.state),
                    (TransactionState::Queued, TransactionState::Sent)
                        | (TransactionState::Sent, TransactionState::HealthPending)
                );
            state_valid
                && previous.admission == next.admission
                && previous.declared_frames == next.declared_frames
                && previous.delivered_frames <= next.delivered_frames
                && (!previous.live_write_executed || next.live_write_executed)
                && certainty_rank(previous.side_effect_certainty)
                    <= certainty_rank(next.side_effect_certainty)
        }
        (TransactionResult::Progress(previous), TransactionResult::Terminal(next)) => {
            let state_valid = matches!(
                (previous.state, next.state),
                (
                    TransactionState::Queued,
                    TransactionState::Revoked | TransactionState::Superseded
                ) | (
                    TransactionState::Sent | TransactionState::HealthPending,
                    TransactionState::Succeeded
                        | TransactionState::Failed
                        | TransactionState::Revoked
                )
            );
            state_valid
                && previous.declared_frames == next.declared_frames
                && previous.delivered_frames <= next.delivered_frames
                && (!previous.live_write_executed || next.live_write_executed)
                && certainty_rank(previous.side_effect_certainty)
                    <= certainty_rank(next.side_effect_certainty)
        }
        (TransactionResult::Terminal(_) | TransactionResult::Unavailable(_), _)
        | (_, TransactionResult::Unavailable(_)) => false,
    }
}

const fn certainty_rank(certainty: hfx_domain::SideEffectCertainty) -> u8 {
    match certainty {
        hfx_domain::SideEffectCertainty::None => 0,
        hfx_domain::SideEffectCertainty::Possible => 1,
        hfx_domain::SideEffectCertainty::Partial => 2,
        hfx_domain::SideEffectCertainty::Committed => 3,
    }
}
