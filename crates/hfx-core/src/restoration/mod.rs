// SPDX-License-Identifier: GPL-2.0-only

//! Crash-safe, per-device restoration of stable lighting intent.

mod engine;
mod intent;

use crate::{EventLogError, LeaseManagerError, RestoreRecord, RestoreRecordStatus};
use hfx_domain::{
    GenerationId, IntentRevision, LogicalDeviceId, PersistenceRevision, PersistenceSchemaVersion,
    ReceiverId, RestoreRecordState,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fmt::{self, Write as _};

pub const CURRENT_PERSISTENCE_SCHEMA_VERSION: u16 = 1;
pub const MAX_STABLE_ENTRIES_PER_RECEIVER: usize = 32;
pub const MAX_RESTORE_RECORDS_PER_RECEIVER: usize = 256;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StableIntentCapture {
    pub device_id: LogicalDeviceId,
    pub lighting: crate::StableLighting,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RestorePlanResult {
    Disabled,
    NoStableIntents,
    Planned(Vec<RestoreRecord>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RestoreAdvanceResult {
    Deferred(RestoreRecord),
    Queued(RestoreRecord),
    Terminal(RestoreRecord),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RestoreGenerationRetirement {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub updated: Vec<RestoreRecord>,
    pub already_terminal: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RestorationAuthority {
    pub client_id: hfx_domain::ClientId,
    pub submission: crate::SubmissionBinding,
    pub lease_duration_ms: hfx_domain::LeaseDurationMs,
    pub deadline_ms: hfx_domain::MonotonicMs,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PersistenceOperation {
    LoadPolicy,
    SavePolicy,
    LoadIntent,
    SaveIntent,
    LoadRestore,
    SaveRestore,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RestorationError {
    InvalidSchemaVersion,
    StableEntryCapacity,
    RestoreRecordCapacity,
    ReceiverMismatch,
    DuplicateDevice,
    InvalidStableTransaction,
    InvalidTrigger,
    CaptureMismatch,
    IntentDigestMismatch,
    IntentMissing,
    UnknownClaim,
    PriorClaimUnresolved,
    PriorOutcomeUncertain,
    GenerationStillActive {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    },
    RecordIdentityConflict,
    InvalidTransition {
        from: RestoreRecordState,
        to: RestoreRecordState,
    },
    RevisionOverflow,
    Identifier,
    NonceOverflow,
    Persistence(PersistenceOperation),
    PersistenceConflict(PersistenceOperation),
    Lease(LeaseManagerError),
    Transaction(crate::TransactionCoordinatorError),
    Event(EventLogError),
}

impl fmt::Display for RestorationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTransition { from, to } => {
                write!(
                    formatter,
                    "invalid persisted restore transition from {from} to {to}"
                )
            }
            Self::InvalidSchemaVersion => {
                formatter.write_str("persistence record uses an incompatible schema")
            }
            Self::StableEntryCapacity => {
                formatter.write_str("stable intent entries exceed the receiver bound")
            }
            Self::RestoreRecordCapacity => {
                formatter.write_str("restore records exceed the receiver bound")
            }
            Self::ReceiverMismatch => {
                formatter.write_str("persistence returned a record for another receiver")
            }
            Self::DuplicateDevice => {
                formatter.write_str("persistence returned duplicate device records")
            }
            Self::InvalidStableTransaction => formatter.write_str(
                "only a definitive successful stable-lighting transaction can be persisted",
            ),
            Self::InvalidTrigger => {
                formatter.write_str("restore trigger scope does not match its lifecycle kind")
            }
            Self::CaptureMismatch => {
                formatter.write_str("stable intent capture does not match the transaction")
            }
            Self::IntentDigestMismatch => {
                formatter.write_str("persisted stable intent digest is invalid")
            }
            Self::IntentMissing => {
                formatter.write_str("the claim's stable intent is no longer active")
            }
            Self::UnknownClaim => formatter.write_str("restore claim does not exist"),
            Self::PriorClaimUnresolved => formatter
                .write_str("an earlier restore attempt must be reconciled before another write"),
            Self::PriorOutcomeUncertain => formatter.write_str(
                "an earlier restore outcome has possible side effects and blocks automatic replay",
            ),
            Self::GenerationStillActive {
                receiver_id,
                generation_id,
            } => write!(
                formatter,
                "receiver {receiver_id} generation {generation_id} is still active"
            ),
            Self::RecordIdentityConflict => {
                formatter.write_str("durable record identity conflicts with its key")
            }
            Self::RevisionOverflow => formatter.write_str("persistence revision cannot advance"),
            Self::Identifier => {
                formatter.write_str("deterministic persistence identity cannot be represented")
            }
            Self::NonceOverflow => formatter.write_str("restore dispatch nonce cannot advance"),
            Self::Persistence(_) => formatter.write_str("persistence operation failed"),
            Self::PersistenceConflict(_) => {
                formatter.write_str("persistence compare-and-set conflict")
            }
            Self::Lease(_) => formatter.write_str("restoration ownership operation failed"),
            Self::Transaction(_) => formatter.write_str("restoration transaction operation failed"),
            Self::Event(_) => formatter.write_str("restoration event recording failed"),
        }
    }
}

impl std::error::Error for RestorationError {}

impl From<LeaseManagerError> for RestorationError {
    fn from(value: LeaseManagerError) -> Self {
        Self::Lease(value)
    }
}

impl From<crate::TransactionCoordinatorError> for RestorationError {
    fn from(value: crate::TransactionCoordinatorError) -> Self {
        Self::Transaction(value)
    }
}

impl From<EventLogError> for RestorationError {
    fn from(value: EventLogError) -> Self {
        Self::Event(value)
    }
}

/// Stateless coordinator; durable storage is the restoration source of truth.
#[derive(Clone, Copy, Debug, Default)]
pub struct RestorationCoordinator;

pub(super) fn current_schema_version() -> Result<PersistenceSchemaVersion, RestorationError> {
    PersistenceSchemaVersion::try_from(CURRENT_PERSISTENCE_SCHEMA_VERSION)
        .map_err(|_| RestorationError::InvalidSchemaVersion)
}

pub(super) fn validate_schema(version: PersistenceSchemaVersion) -> Result<(), RestorationError> {
    if version == current_schema_version()? {
        Ok(())
    } else {
        Err(RestorationError::InvalidSchemaVersion)
    }
}

pub(super) fn next_intent_revision(
    current: Option<IntentRevision>,
) -> Result<IntentRevision, RestorationError> {
    let next = current.map_or(Ok(1), |revision| {
        revision
            .get()
            .checked_add(1)
            .ok_or(RestorationError::RevisionOverflow)
    })?;
    IntentRevision::try_from(next).map_err(|_| RestorationError::RevisionOverflow)
}

pub(super) fn next_persistence_revision(
    current: Option<PersistenceRevision>,
) -> Result<PersistenceRevision, RestorationError> {
    let next = current.map_or(Ok(1), |revision| {
        revision
            .get()
            .checked_add(1)
            .ok_or(RestorationError::RevisionOverflow)
    })?;
    PersistenceRevision::try_from(next).map_err(|_| RestorationError::RevisionOverflow)
}

pub(super) fn sha256_hex<T: Serialize>(value: &T) -> Result<String, RestorationError> {
    let bytes = serde_json::to_vec(value).map_err(|_| RestorationError::Identifier)?;
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut encoded, "{byte:02x}").map_err(|_| RestorationError::Identifier)?;
    }
    Ok(encoded)
}

pub(super) fn transition_record(
    mut record: RestoreRecord,
    status: RestoreRecordStatus,
) -> Result<RestoreRecord, RestorationError> {
    let from = record.status.state();
    let to = status.state();
    if !transition_allowed(from, to) {
        return Err(RestorationError::InvalidTransition { from, to });
    }
    record.revision = next_persistence_revision(Some(record.revision))?;
    record.status = status;
    Ok(record)
}

const fn transition_allowed(from: RestoreRecordState, to: RestoreRecordState) -> bool {
    matches!(
        (from, to),
        (
            RestoreRecordState::Planned | RestoreRecordState::Deferred,
            RestoreRecordState::Deferred
                | RestoreRecordState::Prepared
                | RestoreRecordState::Invalidated
        ) | (
            RestoreRecordState::Prepared | RestoreRecordState::Applying,
            RestoreRecordState::Prepared
                | RestoreRecordState::Deferred
                | RestoreRecordState::Queued
                | RestoreRecordState::Succeeded
                | RestoreRecordState::Failed
                | RestoreRecordState::Invalidated
        ) | (
            RestoreRecordState::Queued,
            RestoreRecordState::Prepared
                | RestoreRecordState::Queued
                | RestoreRecordState::Deferred
                | RestoreRecordState::Applying
                | RestoreRecordState::Succeeded
                | RestoreRecordState::Failed
                | RestoreRecordState::Invalidated
        )
    )
}
