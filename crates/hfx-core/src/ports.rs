// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DispatchNonce, GenerationId,
    LedCount, LogicalDeviceId, MonotonicMs, PersistenceSchemaVersion, ProfileDigest, ProfileId,
    ReceiverId, RequestDigest, RestoreClaimId, SessionId, SideEffectCertainty, TransactionId,
    WallClockUnixMs,
};
use hfx_protocol::{BridgeEvent, DeviceProfileBinding, LightingFrame, ResourceKey, RgbColor};
use serde::{Deserialize, Serialize};

/// Supplies monotonic time for deadlines, leases, and deterministic tests.
pub trait Clock {
    fn now(&self) -> MonotonicMs;
}

#[derive(Clone, Debug, Eq, PartialEq)]
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportTerminal {
    Delivered,
    Failed,
    Revoked,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransportReceipt {
    pub terminal: TransportTerminal,
    pub delivered_frames: DeliveredFrameCount,
    pub side_effect_certainty: SideEffectCertainty,
    pub live_write_executed: bool,
    pub automatic_retry_safe: bool,
    pub device_application: DeviceApplicationState,
}

/// Truthful hardware-side facts carried by every typed transport failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
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
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub lighting: StableLighting,
    pub captured_at: WallClockUnixMs,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreClaimTarget {
    pub device_id: LogicalDeviceId,
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreClaim {
    pub schema_version: PersistenceSchemaVersion,
    pub claim_id: RestoreClaimId,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub plan_digest: RequestDigest,
    pub targets: Vec<RestoreClaimTarget>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RestoreClaimDisposition {
    Claimed,
    AlreadyClaimed,
    ConflictingClaim,
}

/// Stores semantic stable intent and lifecycle claims, never live authority.
pub trait PersistenceStore {
    type Error;

    /// Loads the bounded semantic stable intents for one receiver.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or schema failure.
    fn stable_intents(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<PersistedStableIntent>, Self::Error>;

    /// Transactionally stores one validated stable intent.
    ///
    /// # Errors
    ///
    /// Returns a typed storage, migration, or validation failure.
    fn save_stable_intent(&mut self, intent: &PersistedStableIntent) -> Result<(), Self::Error>;

    /// Durably claims one generation-bound restoration attempt.
    ///
    /// # Errors
    ///
    /// Returns a typed storage or validation failure.
    fn claim_restore(
        &mut self,
        claim: &RestoreClaim,
    ) -> Result<RestoreClaimDisposition, Self::Error>;

    /// Marks a previously durable restoration claim complete.
    ///
    /// # Errors
    ///
    /// Returns a typed storage failure or unknown-claim error.
    fn complete_restore(&mut self, claim_id: &RestoreClaimId) -> Result<(), Self::Error>;
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
