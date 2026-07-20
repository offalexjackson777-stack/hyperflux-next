// SPDX-License-Identifier: GPL-2.0-only

//! Process-crash harness for the production restoration coordinator.

use crate::persistence::SimPersistenceStore;
use crate::transport::{SimCrashSignal, SimReceiverTransport, SimTransportCrashPoint};
use hfx_core::{
    BoundedEventLog, DeviceStateAuthority, EventDelivery, EventSink, LeaseManager,
    PersistedStableIntent, ProfileRegistry, QualifiedDeviceProfile, QualifiedReceiverProfile,
    RestorationAuthority, RestorationCoordinator, RestorationError, RestoreAdvanceResult,
    RestorePlanResult, RestoreRecord, RestoreTrigger, SessionAuthority, StableIntentCapture,
    StableLighting, SubmissionBinding, TransactionCoordinator, canonical_request_digest,
};
use hfx_domain::{
    AuthorizationEpoch, ClientId, ColorChannel, DeliveredFrameCount, DeviceApplicationState,
    DeviceWriteReadiness, DispatchNonce, FrameCount, FrameIndex, GenerationId, LeaseDurationMs,
    LeaseId, LedCount, LogicalDeviceId, MonotonicMs, ProfileDigest, ProfileId, ProjectionRevision,
    ReceiverId, RequestId, ResourceKind, RestoreClaimId, RestoreRecordState, RestoreTriggerId,
    RestoreTriggerKind, SequenceNumber, SessionId, SideEffectCertainty, StreamEpoch, StreamId,
    TransactionClass, TransactionId, TransactionState, WallClockUnixMs,
};
use hfx_protocol::{
    BridgeEvent, DeviceProfileBinding, LightingFrame, ResourceKey, RgbColor, TransactionRequest,
    TransactionTerminal,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CrashCheckpoint {
    BeforeRestoreRecordCas(RestoreRecordState),
    AfterRestoreRecordCas(RestoreRecordState),
    AfterTransportReservation,
    AfterPhysicalWrite,
    AfterTransportTerminal,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CrashExecution<T> {
    Completed(T),
    Crashed(CrashCheckpoint),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SimDeviceProfile {
    pub device_id: LogicalDeviceId,
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub application_slot_count: LedCount,
    pub readiness: DeviceWriteReadiness,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SimRestorationConfig {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub receiver_profile_id: ProfileId,
    pub receiver_profile_digest: ProfileDigest,
    pub devices: Vec<SimDeviceProfile>,
    pub transport_journal_capacity: usize,
    pub transport_tombstone_capacity: usize,
    pub stable_entry_capacity: usize,
    pub restore_record_capacity: usize,
    pub lease_capacity: usize,
    pub lease_history_capacity: usize,
    pub transaction_capacity: usize,
    pub event_capacity: usize,
    pub lease_duration_ms: LeaseDurationMs,
    pub authority_window_ms: u64,
}

impl SimRestorationConfig {
    /// Builds a configuration with conservative bounded defaults.
    ///
    /// # Errors
    ///
    /// Returns an error only if a schema-owned default cannot be represented by
    /// the generated domain types.
    pub fn new(
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        receiver_profile_id: ProfileId,
        receiver_profile_digest: ProfileDigest,
        devices: Vec<SimDeviceProfile>,
    ) -> Result<Self, SimRestorationError> {
        Ok(Self {
            receiver_id,
            generation_id,
            receiver_profile_id,
            receiver_profile_digest,
            devices,
            transport_journal_capacity: 64,
            transport_tombstone_capacity: 256,
            stable_entry_capacity: hfx_core::MAX_STABLE_ENTRIES_PER_RECEIVER,
            restore_record_capacity: hfx_core::MAX_RESTORE_RECORDS_PER_RECEIVER,
            lease_capacity: 64,
            lease_history_capacity: 128,
            transaction_capacity: 64,
            event_capacity: 256,
            lease_duration_ms: LeaseDurationMs::try_from(30_000_u32)
                .map_err(|_| SimRestorationError::Identifier)?,
            authority_window_ms: 60_000,
        })
    }
}

#[derive(Debug)]
pub enum SimRestorationError {
    InvalidConfig(&'static str),
    Identifier,
    TimeOverflow,
    Restoration(RestorationError),
    UnexpectedCrash(CrashCheckpoint),
}

impl fmt::Display for SimRestorationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig(message) => {
                write!(formatter, "invalid restoration simulation: {message}")
            }
            Self::Identifier => formatter.write_str("simulator identifier cannot be represented"),
            Self::TimeOverflow => formatter.write_str("simulator time cannot advance safely"),
            Self::Restoration(error) => error.fmt(formatter),
            Self::UnexpectedCrash(checkpoint) => {
                write!(
                    formatter,
                    "simulator crashed at unhandled checkpoint {checkpoint:?}"
                )
            }
        }
    }
}

impl std::error::Error for SimRestorationError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Restoration(error) => Some(error),
            Self::InvalidConfig(_)
            | Self::Identifier
            | Self::TimeOverflow
            | Self::UnexpectedCrash(_) => None,
        }
    }
}

impl From<RestorationError> for SimRestorationError {
    fn from(value: RestorationError) -> Self {
        Self::Restoration(value)
    }
}

#[derive(Clone, Debug)]
struct SimSessionAuthority {
    session_id: SessionId,
    authorization_epoch: AuthorizationEpoch,
}

impl SessionAuthority for SimSessionAuthority {
    fn authorizes(&self, session_id: &SessionId, authorization_epoch: AuthorizationEpoch) -> bool {
        session_id == &self.session_id && authorization_epoch == self.authorization_epoch
    }
}

#[derive(Clone, Debug)]
struct SimDeviceAuthority {
    readiness: BTreeMap<LogicalDeviceId, DeviceWriteReadiness>,
}

impl DeviceStateAuthority for SimDeviceAuthority {
    fn write_readiness(&self, resource: &ResourceKey) -> DeviceWriteReadiness {
        self.readiness
            .get(&resource.device_id)
            .copied()
            .unwrap_or(DeviceWriteReadiness::Unknown)
    }
}

#[derive(Clone, Debug)]
struct SimProfileAuthority {
    receiver_id: ReceiverId,
    generation_id: GenerationId,
    receiver_profile: QualifiedReceiverProfile,
    devices: BTreeMap<LogicalDeviceId, SimDeviceProfile>,
}

impl ProfileRegistry for SimProfileAuthority {
    fn supports(&self, resource: &ResourceKey) -> bool {
        resource.receiver_id == self.receiver_id
            && resource.generation_id == self.generation_id
            && resource.kind == ResourceKind::Lighting
            && self.devices.contains_key(&resource.device_id)
    }

    fn receiver_profile(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Option<QualifiedReceiverProfile> {
        (receiver_id == &self.receiver_id && generation_id == self.generation_id)
            .then(|| self.receiver_profile.clone())
    }

    fn device_profile(&self, resource: &ResourceKey) -> Option<QualifiedDeviceProfile> {
        self.supports(resource).then_some(())?;
        self.devices
            .get(&resource.device_id)
            .map(|profile| QualifiedDeviceProfile {
                profile_id: profile.profile_id.clone(),
                profile_digest: profile.profile_digest.clone(),
                application_slot_count: profile.application_slot_count,
            })
    }
}

#[derive(Debug, Default)]
struct SimEventSink {
    events: Vec<BridgeEvent>,
}

impl EventSink for SimEventSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.events.push(event.clone());
        EventDelivery::Accepted
    }
}

/// Runs production restoration logic while keeping durable and volatile state separate.
#[derive(Debug)]
pub struct SimRestorationHarness {
    config: SimRestorationConfig,
    now: MonotonicMs,
    wall_clock: WallClockUnixMs,
    sequence: u64,
    process_incarnation: u64,
    store: SimPersistenceStore,
    transport: SimReceiverTransport,
    profiles: SimProfileAuthority,
    devices: SimDeviceAuthority,
    sessions: SimSessionAuthority,
    leases: LeaseManager,
    transactions: TransactionCoordinator,
    events: BoundedEventLog,
    sink: SimEventSink,
}

impl SimRestorationHarness {
    /// Creates a deterministic process around one virtual receiver generation.
    ///
    /// # Errors
    ///
    /// Returns an error for duplicate devices, invalid capacities, or identifiers
    /// that cannot be represented by the shared domain contract.
    pub fn new(config: SimRestorationConfig) -> Result<Self, SimRestorationError> {
        validate_config(&config)?;
        let profiles = config
            .devices
            .iter()
            .cloned()
            .map(|profile| (profile.device_id.clone(), profile))
            .collect::<BTreeMap<_, _>>();
        let devices = config
            .devices
            .iter()
            .map(|profile| (profile.device_id.clone(), profile.readiness))
            .collect::<BTreeMap<_, _>>();
        let transport = SimReceiverTransport::new(
            config.receiver_id.clone(),
            config.generation_id,
            config.transport_journal_capacity,
            config.transport_tombstone_capacity,
        )
        .map_err(|_| SimRestorationError::InvalidConfig("invalid transport journal bounds"))?;
        let now = MonotonicMs::try_from(1_u64).map_err(|_| SimRestorationError::TimeOverflow)?;
        let wall_clock =
            WallClockUnixMs::try_from(1_u64).map_err(|_| SimRestorationError::TimeOverflow)?;
        let sessions = session_authority(1)?;
        let leases = LeaseManager::new(config.lease_capacity, config.lease_history_capacity)
            .map_err(|_| SimRestorationError::InvalidConfig("invalid lease bounds"))?;
        let transactions = TransactionCoordinator::new(config.transaction_capacity)
            .map_err(|_| SimRestorationError::InvalidConfig("invalid transaction bounds"))?;
        let events = event_log(1, config.event_capacity)?;
        Ok(Self {
            store: SimPersistenceStore::new(
                config.stable_entry_capacity,
                config.restore_record_capacity,
            ),
            transport,
            profiles: SimProfileAuthority {
                receiver_id: config.receiver_id.clone(),
                generation_id: config.generation_id,
                receiver_profile: QualifiedReceiverProfile {
                    profile_id: config.receiver_profile_id.clone(),
                    profile_digest: config.receiver_profile_digest.clone(),
                },
                devices: profiles,
            },
            devices: SimDeviceAuthority { readiness: devices },
            sessions,
            leases,
            transactions,
            events,
            sink: SimEventSink::default(),
            config,
            now,
            wall_clock,
            sequence: 1,
            process_incarnation: 1,
        })
    }

    #[must_use]
    pub const fn store(&self) -> &SimPersistenceStore {
        &self.store
    }

    #[must_use]
    pub const fn transport(&self) -> &SimReceiverTransport {
        &self.transport
    }

    pub fn transport_mut(&mut self) -> &mut SimReceiverTransport {
        &mut self.transport
    }

    #[must_use]
    pub const fn now(&self) -> MonotonicMs {
        self.now
    }

    #[must_use]
    pub const fn process_incarnation(&self) -> u64 {
        self.process_incarnation
    }

    #[must_use]
    pub fn queued_transactions(&self) -> usize {
        self.transactions.queued_len()
    }

    #[must_use]
    pub fn emitted_events(&self) -> &[BridgeEvent] {
        &self.sink.events
    }

    /// Advances only the deterministic monotonic clock.
    ///
    /// # Errors
    ///
    /// Returns an error when addition or domain conversion overflows.
    pub fn advance_time(&mut self, delta_ms: u64) -> Result<MonotonicMs, SimRestorationError> {
        let value = self
            .now
            .get()
            .checked_add(delta_ms)
            .ok_or(SimRestorationError::TimeOverflow)?;
        self.now = MonotonicMs::try_from(value).map_err(|_| SimRestorationError::TimeOverflow)?;
        Ok(self.now)
    }

    pub fn set_device_readiness(
        &mut self,
        device_id: &LogicalDeviceId,
        readiness: DeviceWriteReadiness,
    ) -> bool {
        let Some(current) = self.devices.readiness.get_mut(device_id) else {
            return false;
        };
        *current = readiness;
        true
    }

    /// Captures one already-delivered Static or Off request as durable intent.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown device, dimension mismatch, identifier
    /// overflow, or a rejected production persistence invariant.
    pub fn capture_stable(
        &mut self,
        device_id: &LogicalDeviceId,
        lighting: StableLighting,
    ) -> Result<PersistedStableIntent, SimRestorationError> {
        let profile = self
            .profiles
            .devices
            .get(device_id)
            .cloned()
            .ok_or(SimRestorationError::InvalidConfig("unknown capture device"))?;
        let colors = lighting_colors(&lighting, profile.application_slot_count)?;
        let sequence = self.next_sequence()?;
        let request_id = typed_id::<RequestId>("capture-request", sequence)?;
        let transaction_id = typed_id::<TransactionId>("capture-transaction", sequence)?;
        let resource = ResourceKey {
            receiver_id: self.config.receiver_id.clone(),
            generation_id: self.profiles.generation_id,
            device_id: device_id.clone(),
            kind: ResourceKind::Lighting,
        };
        let request = TransactionRequest {
            request_id: request_id.clone(),
            transaction_id: transaction_id.clone(),
            client_id: typed_id::<ClientId>("capture-client", sequence)?,
            lease_id: typed_id::<LeaseId>("capture-lease", sequence)?,
            receiver_id: self.config.receiver_id.clone(),
            generation_id: self.profiles.generation_id,
            receiver_profile_id: self.config.receiver_profile_id.clone(),
            receiver_profile_digest: self.config.receiver_profile_digest.clone(),
            device_profiles: vec![DeviceProfileBinding {
                device_id: device_id.clone(),
                profile_id: profile.profile_id,
                profile_digest: profile.profile_digest,
                application_slot_count: profile.application_slot_count,
            }],
            transaction_class: TransactionClass::StaticLighting,
            deadline_ms: deadline(self.now, self.config.authority_window_ms)?,
            resources: vec![resource],
            frames: vec![LightingFrame {
                device_id: device_id.clone(),
                frame_index: FrameIndex::try_from(0_u32)
                    .map_err(|_| SimRestorationError::Identifier)?,
                colors,
            }],
        };
        let terminal = TransactionTerminal {
            request_id,
            request_digest: canonical_request_digest(&request)
                .map_err(|_| SimRestorationError::Identifier)?,
            transaction_id,
            receiver_id: self.config.receiver_id.clone(),
            generation_id: self.profiles.generation_id,
            state: TransactionState::Succeeded,
            declared_frames: FrameCount::try_from(1_u16)
                .map_err(|_| SimRestorationError::Identifier)?,
            delivered_frames: DeliveredFrameCount::try_from(1_u16)
                .map_err(|_| SimRestorationError::Identifier)?,
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry: false,
            device_application: DeviceApplicationState::Confirmed,
            terminal_sequence: SequenceNumber::try_from(sequence)
                .map_err(|_| SimRestorationError::Identifier)?,
            error_kind: None,
            superseded_by: None,
        };
        let captures = [StableIntentCapture {
            device_id: device_id.clone(),
            lighting,
        }];
        let mut intents = RestorationCoordinator.commit_stable_transaction(
            &request,
            &terminal,
            &captures,
            self.wall_clock,
            &mut self.store,
        )?;
        intents.pop().ok_or(SimRestorationError::InvalidConfig(
            "capture produced no intent",
        ))
    }

    /// Enables or disables stable restoration through the production CAS path.
    ///
    /// # Errors
    ///
    /// Returns the production restoration error on invalid or conflicting state.
    pub fn set_restore_enabled(&mut self, enabled: bool) -> Result<(), SimRestorationError> {
        RestorationCoordinator.set_restore_enabled(
            &self.config.receiver_id,
            enabled,
            &mut self.store,
        )?;
        Ok(())
    }

    /// Creates a deterministic lifecycle trigger with a caller-selected identity.
    #[must_use]
    pub fn trigger(
        &self,
        trigger_id: RestoreTriggerId,
        kind: RestoreTriggerKind,
        target_device_id: Option<LogicalDeviceId>,
    ) -> RestoreTrigger {
        RestoreTrigger {
            trigger_id,
            kind,
            receiver_id: self.config.receiver_id.clone(),
            generation_id: self.profiles.generation_id,
            target_device_id,
        }
    }

    pub fn arm_crash(&mut self, checkpoint: CrashCheckpoint) {
        match checkpoint {
            CrashCheckpoint::BeforeRestoreRecordCas(state) => {
                self.store.arm_before_restore_record_cas(state);
            }
            CrashCheckpoint::AfterRestoreRecordCas(state) => {
                self.store.arm_after_restore_record_cas(state);
            }
            CrashCheckpoint::AfterTransportReservation => {
                self.transport
                    .arm_crash(SimTransportCrashPoint::AfterReservation);
            }
            CrashCheckpoint::AfterPhysicalWrite => {
                self.transport
                    .arm_crash(SimTransportCrashPoint::AfterPhysicalWrite);
            }
            CrashCheckpoint::AfterTransportTerminal => {
                self.transport
                    .arm_crash(SimTransportCrashPoint::AfterTerminal);
            }
        }
    }

    /// Plans claims while converting an armed simulator crash into data.
    ///
    /// # Errors
    ///
    /// Returns only production restoration or volatile-runtime reconstruction failures.
    pub fn plan_restore_crashable(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<CrashExecution<RestorePlanResult>, SimRestorationError> {
        let result = catch_unwind(AssertUnwindSafe(|| {
            RestorationCoordinator.plan_restore(trigger, &mut self.store)
        }));
        self.finish_crashable(result)
    }

    /// Advances one claim while converting an armed simulator crash into data.
    ///
    /// # Errors
    ///
    /// Returns only production restoration or volatile-runtime reconstruction failures.
    pub fn advance_claim_crashable(
        &mut self,
        claim_id: &RestoreClaimId,
    ) -> Result<CrashExecution<RestoreAdvanceResult>, SimRestorationError> {
        let authority = self.next_authority()?;
        let result = catch_unwind(AssertUnwindSafe(|| {
            RestorationCoordinator.advance_claim(
                claim_id,
                &authority,
                self.now,
                &self.sessions,
                &self.devices,
                &self.profiles,
                &self.transport,
                &mut self.store,
                &mut self.leases,
                &mut self.transactions,
                &mut self.events,
                &mut self.sink,
            )
        }));
        self.finish_crashable(result)
    }

    /// Dispatches one queued claim while converting an armed crash into data.
    ///
    /// # Errors
    ///
    /// Returns only production restoration or volatile-runtime reconstruction failures.
    pub fn dispatch_claim_crashable(
        &mut self,
        claim_id: &RestoreClaimId,
    ) -> Result<CrashExecution<RestoreRecord>, SimRestorationError> {
        let result = catch_unwind(AssertUnwindSafe(|| {
            RestorationCoordinator.dispatch_claim(
                claim_id,
                self.now,
                &self.sessions,
                &self.devices,
                &self.profiles,
                &mut self.transport,
                &mut self.store,
                &mut self.leases,
                &mut self.transactions,
                &mut self.events,
                &mut self.sink,
            )
        }));
        self.finish_crashable(result)
    }

    /// Plans without expecting a fault-injection checkpoint.
    ///
    /// # Errors
    ///
    /// Returns production failures or an unexpected armed crash.
    pub fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, SimRestorationError> {
        completed_or_error(self.plan_restore_crashable(trigger)?)
    }

    /// Advances without expecting a fault-injection checkpoint.
    ///
    /// # Errors
    ///
    /// Returns production failures or an unexpected armed crash.
    pub fn advance_claim(
        &mut self,
        claim_id: &RestoreClaimId,
    ) -> Result<RestoreAdvanceResult, SimRestorationError> {
        completed_or_error(self.advance_claim_crashable(claim_id)?)
    }

    /// Dispatches without expecting a fault-injection checkpoint.
    ///
    /// # Errors
    ///
    /// Returns production failures or an unexpected armed crash.
    pub fn dispatch_claim(
        &mut self,
        claim_id: &RestoreClaimId,
    ) -> Result<RestoreRecord, SimRestorationError> {
        completed_or_error(self.dispatch_claim_crashable(claim_id)?)
    }

    /// Replaces only volatile process state; receiver generation and durable
    /// persistence remain unchanged.
    ///
    /// # Errors
    ///
    /// Returns an error if the next bounded runtime cannot be constructed.
    pub fn restart_process(&mut self) -> Result<(), SimRestorationError> {
        self.rebuild_volatile()
    }

    fn finish_crashable<T>(
        &mut self,
        result: std::thread::Result<Result<T, RestorationError>>,
    ) -> Result<CrashExecution<T>, SimRestorationError> {
        match result {
            Ok(Ok(value)) => Ok(CrashExecution::Completed(value)),
            Ok(Err(error)) => Err(SimRestorationError::Restoration(error)),
            Err(payload) => {
                let Some(signal) = payload.downcast_ref::<SimCrashSignal>().copied() else {
                    resume_unwind(payload);
                };
                let checkpoint = crash_checkpoint(signal);
                self.rebuild_volatile()?;
                Ok(CrashExecution::Crashed(checkpoint))
            }
        }
    }

    fn rebuild_volatile(&mut self) -> Result<(), SimRestorationError> {
        self.process_incarnation = self
            .process_incarnation
            .checked_add(1)
            .ok_or(SimRestorationError::Identifier)?;
        self.sessions = session_authority(self.process_incarnation)?;
        self.leases = LeaseManager::new(
            self.config.lease_capacity,
            self.config.lease_history_capacity,
        )
        .map_err(|_| SimRestorationError::InvalidConfig("invalid lease bounds"))?;
        self.transactions = TransactionCoordinator::new(self.config.transaction_capacity)
            .map_err(|_| SimRestorationError::InvalidConfig("invalid transaction bounds"))?;
        self.events = event_log(self.process_incarnation, self.config.event_capacity)?;
        self.sink = SimEventSink::default();
        Ok(())
    }

    fn next_authority(&mut self) -> Result<RestorationAuthority, SimRestorationError> {
        let sequence = self.next_sequence()?;
        Ok(RestorationAuthority {
            client_id: typed_id::<ClientId>("restore-client", self.process_incarnation)?,
            submission: SubmissionBinding {
                session_id: self.sessions.session_id.clone(),
                authorization_epoch: self.sessions.authorization_epoch,
                dispatch_nonce: DispatchNonce::try_from(sequence)
                    .map_err(|_| SimRestorationError::Identifier)?,
            },
            lease_duration_ms: self.config.lease_duration_ms,
            deadline_ms: deadline(self.now, self.config.authority_window_ms)?,
        })
    }

    fn next_sequence(&mut self) -> Result<u64, SimRestorationError> {
        let current = self.sequence;
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or(SimRestorationError::Identifier)?;
        Ok(current)
    }
}

fn validate_config(config: &SimRestorationConfig) -> Result<(), SimRestorationError> {
    let unique = config
        .devices
        .iter()
        .map(|profile| &profile.device_id)
        .collect::<BTreeSet<_>>();
    if unique.len() != config.devices.len() {
        return Err(SimRestorationError::InvalidConfig(
            "duplicate device profile",
        ));
    }
    if config.transport_journal_capacity == 0
        || config.transport_tombstone_capacity == 0
        || config.stable_entry_capacity == 0
        || config.restore_record_capacity == 0
        || config.transaction_capacity == 0
        || config.event_capacity == 0
        || config.lease_capacity == 0
        || config.lease_history_capacity < config.lease_capacity
        || config.authority_window_ms == 0
    {
        return Err(SimRestorationError::InvalidConfig(
            "a bounded capacity is invalid",
        ));
    }
    Ok(())
}

fn lighting_colors(
    lighting: &StableLighting,
    slot_count: LedCount,
) -> Result<Vec<RgbColor>, SimRestorationError> {
    let count = usize::from(slot_count.get());
    match lighting {
        StableLighting::Static(colors) if colors.len() == count => Ok(colors.clone()),
        StableLighting::Static(_) => Err(SimRestorationError::InvalidConfig(
            "stable colors do not match the profile slot count",
        )),
        StableLighting::Off => {
            let zero = ColorChannel::try_from(0_u8).map_err(|_| SimRestorationError::Identifier)?;
            Ok(vec![
                RgbColor {
                    red: zero,
                    green: zero,
                    blue: zero,
                };
                count
            ])
        }
    }
}

fn session_authority(incarnation: u64) -> Result<SimSessionAuthority, SimRestorationError> {
    Ok(SimSessionAuthority {
        session_id: typed_id::<SessionId>("sim-session", incarnation)?,
        authorization_epoch: AuthorizationEpoch::try_from(incarnation)
            .map_err(|_| SimRestorationError::Identifier)?,
    })
}

fn event_log(incarnation: u64, capacity: usize) -> Result<BoundedEventLog, SimRestorationError> {
    BoundedEventLog::new(
        typed_id::<StreamId>("sim-stream", incarnation)?,
        StreamEpoch::try_from(incarnation).map_err(|_| SimRestorationError::Identifier)?,
        ProjectionRevision::try_from(1_u32).map_err(|_| SimRestorationError::Identifier)?,
        capacity,
    )
    .map_err(|_| SimRestorationError::InvalidConfig("invalid event-log bounds"))
}

fn deadline(now: MonotonicMs, window_ms: u64) -> Result<MonotonicMs, SimRestorationError> {
    let value = now
        .get()
        .checked_add(window_ms)
        .ok_or(SimRestorationError::TimeOverflow)?;
    MonotonicMs::try_from(value).map_err(|_| SimRestorationError::TimeOverflow)
}

fn typed_id<T>(prefix: &str, sequence: u64) -> Result<T, SimRestorationError>
where
    T: TryFrom<String>,
{
    T::try_from(format!("{prefix}-{sequence}")).map_err(|_| SimRestorationError::Identifier)
}

fn crash_checkpoint(signal: SimCrashSignal) -> CrashCheckpoint {
    match signal {
        SimCrashSignal::BeforeRestoreRecordCas(state) => {
            CrashCheckpoint::BeforeRestoreRecordCas(state)
        }
        SimCrashSignal::AfterRestoreRecordCas(state) => {
            CrashCheckpoint::AfterRestoreRecordCas(state)
        }
        SimCrashSignal::Transport(SimTransportCrashPoint::AfterReservation) => {
            CrashCheckpoint::AfterTransportReservation
        }
        SimCrashSignal::Transport(SimTransportCrashPoint::AfterPhysicalWrite) => {
            CrashCheckpoint::AfterPhysicalWrite
        }
        SimCrashSignal::Transport(SimTransportCrashPoint::AfterTerminal) => {
            CrashCheckpoint::AfterTransportTerminal
        }
    }
}

fn completed_or_error<T>(execution: CrashExecution<T>) -> Result<T, SimRestorationError> {
    match execution {
        CrashExecution::Completed(value) => Ok(value),
        CrashExecution::Crashed(checkpoint) => {
            Err(SimRestorationError::UnexpectedCrash(checkpoint))
        }
    }
}
