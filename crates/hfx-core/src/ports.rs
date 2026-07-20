// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DispatchNonce, GenerationId,
    LogicalDeviceId, MonotonicMs, ProfileDigest, ProfileId, ReceiverId, RestoreClaimId, SessionId,
    SideEffectCertainty, TransactionId, WallClockUnixMs,
};
use hfx_protocol::{BridgeEvent, LightingFrame, ResourceKey, RgbColor};

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

/// Allows adapter-specific errors to expose a common side-effect contract.
pub trait TransportFailure {
    fn facts(&self) -> TransportFailureFacts;
}

/// The sole hardware-facing core port. Raw reports remain behind its adapter.
pub trait ReceiverTransport {
    type Error: TransportFailure;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId>;

    /// Delivers one validated, fully bound semantic dispatch.
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

/// Resolves evidence-backed profile and capability facts without presentation data.
pub trait ProfileRegistry {
    fn supports(&self, resource: &ResourceKey) -> bool;

    fn profile_binding(
        &self,
        receiver_id: &ReceiverId,
        device_id: &LogicalDeviceId,
    ) -> Option<(ProfileId, ProfileDigest)>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StableLighting {
    Off,
    Static(Vec<RgbColor>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PersistedStableIntent {
    pub receiver_id: ReceiverId,
    pub device_id: LogicalDeviceId,
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub lighting: StableLighting,
    pub captured_at: WallClockUnixMs,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RestoreClaim {
    pub claim_id: RestoreClaimId,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub profile_digest: ProfileDigest,
    pub target_devices: Vec<LogicalDeviceId>,
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
