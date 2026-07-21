// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DeviceWriteReadiness,
    DispatchNonce, GenerationId, IntentDigest, IntentRevision, LedCount, LogicalDeviceId,
    MonotonicMs, PersistenceRevision, PersistenceSchemaVersion, ProfileDigest, ProfileId,
    ProtocolErrorKind, ReceiverId, RequestDigest, RestoreAttemptNumber, RestoreClaimId,
    RestoreDeferReason, RestoreInvalidationReason, RestoreRecordState, RestoreTriggerId,
    RestoreTriggerKind, SessionId, SideEffectCertainty, TransactionId, TransactionState,
    WallClockUnixMs,
};
use hfx_protocol::{
    BridgeEvent, DeviceProfileBinding, LeaseRequest, LightingFrame, ResourceKey, RgbColor,
    TransactionRequest,
};
use serde::{Deserialize, Serialize};

/// Supplies monotonic time for deadlines, leases, and deterministic tests.
pub trait Clock {
    fn now(&self) -> MonotonicMs;
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SubmissionBinding {
    pub session_id: SessionId,
    pub authorization_epoch: AuthorizationEpoch,
    pub dispatch_nonce: DispatchNonce,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransportDispatch {
    pub session_id: SessionId,
    pub authorization_epoch: AuthorizationEpoch,
    pub dispatch_nonce: DispatchNonce,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub transaction_id: TransactionId,
    pub request_digest: RequestDigest,
    pub receiver_profile_id: ProfileId,
    pub receiver_profile_digest: ProfileDigest,
    pub device_profiles: Vec<DeviceProfileBinding>,
    pub frames: Vec<LightingFrame>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum TransportTerminal {
    Delivered,
    Failed,
    Revoked,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransportReceipt {
    pub terminal: TransportTerminal,
    pub delivered_frames: DeliveredFrameCount,
    pub side_effect_certainty: SideEffectCertainty,
    pub live_write_executed: bool,
    pub automatic_retry_safe: bool,
    pub device_application: DeviceApplicationState,
}

/// Truthful hardware-side facts carried by every typed transport failure.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransportFailureFacts {
    pub delivered_frames: DeliveredFrameCount,
    pub side_effect_certainty: SideEffectCertainty,
    pub live_write_executed: bool,
    pub automatic_retry_safe: bool,
    pub device_application: DeviceApplicationState,
}

/// Durable adapter knowledge for one exact semantic dispatch.
///
/// `NotObserved` is the only state that permits a new write. `Evicted`,
/// `Unavailable`, and `Conflict` all preserve uncertainty and therefore forbid
/// automatic replay.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportReconciliation {
    NotObserved,
    Retained(TransportReceipt),
    RetainedFailure(TransportFailureFacts),
    Evicted,
    Unavailable,
    Conflict,
}

/// Allows adapter-specific errors to expose a common side-effect contract.
pub trait TransportFailure {
    fn facts(&self) -> TransportFailureFacts;
}

/// The sole hardware-facing core port. Raw reports remain behind its adapter.
pub trait ReceiverTransport {
    type Error: TransportFailure;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId>;

    /// Reconciles one exact dispatch against the adapter's durable outcome log.
    ///
    /// The adapter must bind the lookup to session, authorization epoch,
    /// generation, transaction, nonce, request digest, profile bindings, and
    /// frames. A conflicting identity must never be reported as `NotObserved`.
    fn reconcile(&self, dispatch: &TransportDispatch) -> TransportReconciliation;

    /// Delivers one validated, fully bound semantic dispatch.
    ///
    /// Before any hardware side effect, the adapter must durably reserve the
    /// exact dispatch identity. Repeating an identical dispatch must return the
    /// retained terminal facts rather than execute a second physical write.
    ///
    /// # Errors
    ///
    /// Returns the adapter's typed transport failure. Callers must preserve
    /// possible side effects and must not infer that retry is safe.
    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error>;
}

/// Confirms that queued work still belongs to one live writer session.
pub trait SessionAuthority {
    fn authorizes(&self, session_id: &SessionId, authorization_epoch: AuthorizationEpoch) -> bool;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QualifiedReceiverProfile {
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QualifiedDeviceProfile {
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub application_slot_count: LedCount,
}

/// Resolves evidence-backed profile and capability facts without presentation data.
pub trait ProfileRegistry {
    fn supports(&self, resource: &ResourceKey) -> bool;

    fn receiver_profile(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Option<QualifiedReceiverProfile>;

    fn device_profile(&self, resource: &ResourceKey) -> Option<QualifiedDeviceProfile>;
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "mode", content = "colors", rename_all = "kebab-case")]
pub enum StableLighting {
    Off,
    Static(Vec<RgbColor>),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PersistedStableIntent {
    pub schema_version: PersistenceSchemaVersion,
    pub receiver_id: ReceiverId,
    pub device_id: LogicalDeviceId,
    pub receiver_profile_id: ProfileId,
    pub receiver_profile_digest: ProfileDigest,
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub application_slot_count: LedCount,
    pub revision: IntentRevision,
    pub content_digest: IntentDigest,
    pub source_transaction_id: TransactionId,
    pub source_request_digest: RequestDigest,
    pub lighting: StableLighting,
    pub captured_at: WallClockUnixMs,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StableIntentTombstone {
    pub schema_version: PersistenceSchemaVersion,
    pub receiver_id: ReceiverId,
    pub device_id: LogicalDeviceId,
    pub revision: IntentRevision,
    pub previous_content_digest: Option<IntentDigest>,
    pub deleted_at: WallClockUnixMs,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "record_state", content = "record", rename_all = "kebab-case")]
pub enum PersistedStableEntry {
    Present(PersistedStableIntent),
    Deleted(StableIntentTombstone),
}

impl PersistedStableEntry {
    #[must_use]
    pub const fn revision(&self) -> IntentRevision {
        match self {
            Self::Present(intent) => intent.revision,
            Self::Deleted(tombstone) => tombstone.revision,
        }
    }

    #[must_use]
    pub const fn receiver_id(&self) -> &ReceiverId {
        match self {
            Self::Present(intent) => &intent.receiver_id,
            Self::Deleted(tombstone) => &tombstone.receiver_id,
        }
    }

    #[must_use]
    pub const fn device_id(&self) -> &LogicalDeviceId {
        match self {
            Self::Present(intent) => &intent.device_id,
            Self::Deleted(tombstone) => &tombstone.device_id,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StableIntentChange {
    pub expected_revision: Option<IntentRevision>,
    pub entry: PersistedStableEntry,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PersistedRestorePolicy {
    pub schema_version: PersistenceSchemaVersion,
    pub receiver_id: ReceiverId,
    pub enabled: bool,
    pub revision: PersistenceRevision,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreTrigger {
    pub trigger_id: RestoreTriggerId,
    pub kind: RestoreTriggerKind,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub target_device_id: Option<LogicalDeviceId>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreAttempt {
    pub attempt_number: RestoreAttemptNumber,
    pub lease_request: LeaseRequest,
    pub request: TransactionRequest,
    pub request_digest: RequestDigest,
    pub submission: SubmissionBinding,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreCompletion {
    pub attempt_number: RestoreAttemptNumber,
    pub transaction_id: TransactionId,
    pub request_digest: RequestDigest,
    pub state: TransactionState,
    pub delivered_frames: DeliveredFrameCount,
    pub side_effect_certainty: SideEffectCertainty,
    pub live_write_executed: bool,
    pub automatic_retry: bool,
    pub device_application: DeviceApplicationState,
    pub error_kind: Option<ProtocolErrorKind>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreDeferred {
    pub reason: RestoreDeferReason,
    pub prior_outcome: Option<RestoreCompletion>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreInvalidation {
    pub reason: RestoreInvalidationReason,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "state", content = "detail", rename_all = "kebab-case")]
pub enum RestoreRecordStatus {
    Planned,
    Deferred(RestoreDeferred),
    Prepared(RestoreAttempt),
    Queued(RestoreAttempt),
    Applying(RestoreAttempt),
    Succeeded(RestoreCompletion),
    Failed(RestoreCompletion),
    Invalidated(RestoreInvalidation),
}

impl RestoreRecordStatus {
    #[must_use]
    pub const fn state(&self) -> RestoreRecordState {
        match self {
            Self::Planned => RestoreRecordState::Planned,
            Self::Deferred(_) => RestoreRecordState::Deferred,
            Self::Prepared(_) => RestoreRecordState::Prepared,
            Self::Queued(_) => RestoreRecordState::Queued,
            Self::Applying(_) => RestoreRecordState::Applying,
            Self::Succeeded(_) => RestoreRecordState::Succeeded,
            Self::Failed(_) => RestoreRecordState::Failed,
            Self::Invalidated(_) => RestoreRecordState::Invalidated,
        }
    }

    #[must_use]
    pub const fn is_terminal(&self) -> bool {
        matches!(
            self,
            Self::Succeeded(_) | Self::Failed(_) | Self::Invalidated(_)
        )
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreRecord {
    pub schema_version: PersistenceSchemaVersion,
    pub claim_id: RestoreClaimId,
    pub trigger_id: RestoreTriggerId,
    pub trigger_kind: RestoreTriggerKind,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub device_id: LogicalDeviceId,
    pub intent_revision: IntentRevision,
    pub intent_digest: IntentDigest,
    pub revision: PersistenceRevision,
    pub last_attempt: Option<RestoreAttemptNumber>,
    pub status: RestoreRecordStatus,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RestoreRecordChange {
    pub expected_revision: Option<PersistenceRevision>,
    pub record: RestoreRecord,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PersistenceCasOutcome {
    Applied,
    Conflict,
}

/// Supplies the current generation-bound device write readiness.
pub trait DeviceStateAuthority {
    fn write_readiness(&self, resource: &ResourceKey) -> DeviceWriteReadiness;
}

/// Stores versioned semantic intent and durable restore records, never live authority.
pub trait PersistenceStore {
    type Error;

    /// Loads the optional restore policy for one receiver. Missing means disabled.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or schema failure.
    fn restore_policy(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Option<PersistedRestorePolicy>, Self::Error>;

    /// Atomically compare-and-sets one restore policy record.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or migration failure.
    fn compare_and_set_restore_policy(
        &mut self,
        expected_revision: Option<PersistenceRevision>,
        policy: &PersistedRestorePolicy,
    ) -> Result<PersistenceCasOutcome, Self::Error>;

    /// Loads bounded active intent records and tombstones for one receiver.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or schema failure.
    fn stable_entries(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<PersistedStableEntry>, Self::Error>;

    /// Atomically compare-and-sets a canonical batch of intent records.
    ///
    /// # Errors
    ///
    /// Returns a typed storage, migration, or validation failure.
    fn compare_and_set_stable_entries(
        &mut self,
        changes: &[StableIntentChange],
    ) -> Result<PersistenceCasOutcome, Self::Error>;

    /// Loads bounded durable restoration history for one receiver.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or schema failure.
    fn restore_records(&self, receiver_id: &ReceiverId) -> Result<Vec<RestoreRecord>, Self::Error>;

    /// Loads one durable restoration record by claim identity.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or schema failure.
    fn restore_record(
        &self,
        claim_id: &RestoreClaimId,
    ) -> Result<Option<RestoreRecord>, Self::Error>;

    /// Atomically creates or advances a batch of durable per-device restoration records.
    ///
    /// The complete batch must compare and commit as one operation. This is
    /// required when retiring a generation so one failure cannot leave sibling
    /// device claims split across old and new lifecycle truth.
    ///
    /// # Errors
    ///
    /// Returns a typed storage, migration, or validation failure.
    fn compare_and_set_restore_records(
        &mut self,
        changes: &[RestoreRecordChange],
    ) -> Result<PersistenceCasOutcome, Self::Error>;

    /// Atomically creates or advances one durable per-device restoration record.
    ///
    /// # Errors
    ///
    /// Returns the same failures as [`Self::compare_and_set_restore_records`].
    fn compare_and_set_restore_record(
        &mut self,
        expected_revision: Option<PersistenceRevision>,
        record: &RestoreRecord,
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        self.compare_and_set_restore_records(&[RestoreRecordChange {
            expected_revision,
            record: record.clone(),
        }])
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EventDelivery {
    Accepted,
    Full,
    Closed,
}

/// Best-effort event output. Callers never wait for capacity.
pub trait EventSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery;
}
