// SPDX-License-Identifier: GPL-2.0-only

//! Generation-bound receiver and logical-device lifecycle policy.
//!
//! This module projects passive evidence only. It deliberately contains no
//! lease, session, transport, or write-authority type.

use hfx_domain::{
    ActivityState, ApplyOutcome, BatteryPercent, ConnectionMode, ContactState, DeviceKind,
    EndpointId, EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId, LogicalDeviceId,
    MonotonicMs, PairingState, PowerState, PresenceState, ProductId, ReceiverId,
    ReceiverLifecycleState, RouteKind, RouteState, SequenceNumber, SleepState,
};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;

pub const DEFAULT_MAX_LIFECYCLE_DEVICES: usize = 32;
pub const DEFAULT_MAX_DEVICE_ENDPOINTS: usize = 8;
const MAX_RETIRED_GENERATIONS: usize = 256;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LifecycleLimits {
    max_devices: usize,
    max_endpoints_per_device: usize,
}

impl LifecycleLimits {
    /// Creates bounds that cannot exceed the public snapshot contract.
    ///
    /// # Errors
    ///
    /// Returns [`LifecycleError::InvalidLimits`] for zero or oversized bounds.
    pub fn new(
        max_devices: usize,
        max_endpoints_per_device: usize,
    ) -> Result<Self, LifecycleError> {
        if max_devices == 0
            || max_devices > DEFAULT_MAX_LIFECYCLE_DEVICES
            || max_endpoints_per_device == 0
            || max_endpoints_per_device > DEFAULT_MAX_DEVICE_ENDPOINTS
        {
            return Err(LifecycleError::InvalidLimits);
        }
        Ok(Self {
            max_devices,
            max_endpoints_per_device,
        })
    }

    #[must_use]
    pub const fn max_devices(self) -> usize {
        self.max_devices
    }

    #[must_use]
    pub const fn max_endpoints_per_device(self) -> usize {
        self.max_endpoints_per_device
    }
}

impl Default for LifecycleLimits {
    fn default() -> Self {
        Self {
            max_devices: DEFAULT_MAX_LIFECYCLE_DEVICES,
            max_endpoints_per_device: DEFAULT_MAX_DEVICE_ENDPOINTS,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LifecycleError {
    InvalidLimits,
    UnknownEvidenceConfidence,
    InvalidChildKind(DeviceKind),
    DeviceCapacity,
    EndpointCapacity(LogicalDeviceId),
    DeviceIdentityConflict(LogicalDeviceId),
    EndpointIdentityConflict(EndpointId),
    UnknownEndpoint(EndpointId),
    InvalidEndpointMode {
        route_kind: RouteKind,
        connection_mode: ConnectionMode,
    },
}

impl fmt::Display for LifecycleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidLimits => formatter.write_str("lifecycle bounds are invalid"),
            Self::UnknownEvidenceConfidence => {
                formatter.write_str("lifecycle evidence must declare a known confidence")
            }
            Self::InvalidChildKind(kind) => {
                write!(formatter, "{kind} is not a receiver child kind")
            }
            Self::DeviceCapacity => formatter.write_str("lifecycle device capacity is exhausted"),
            Self::EndpointCapacity(device_id) => {
                write!(formatter, "endpoint capacity is exhausted for {device_id}")
            }
            Self::DeviceIdentityConflict(device_id) => {
                write!(formatter, "logical device identity changed for {device_id}")
            }
            Self::EndpointIdentityConflict(endpoint_id) => {
                write!(formatter, "endpoint identity changed for {endpoint_id}")
            }
            Self::UnknownEndpoint(endpoint_id) => {
                write!(formatter, "endpoint is unknown: {endpoint_id}")
            }
            Self::InvalidEndpointMode {
                route_kind,
                connection_mode,
            } => write!(
                formatter,
                "route {route_kind} cannot use connection mode {connection_mode}"
            ),
        }
    }
}

impl std::error::Error for LifecycleError {}

/// Generation-bound, provenance-carrying order for one passive observation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObservationStamp {
    generation_id: GenerationId,
    sequence: SequenceNumber,
    observed_at_ms: MonotonicMs,
    confidence: EvidenceConfidence,
    evidence_claim_id: EvidenceClaimId,
}

impl ObservationStamp {
    /// Creates a stamp with explicit provenance and confidence.
    ///
    /// # Errors
    ///
    /// Returns [`LifecycleError::UnknownEvidenceConfidence`] when the caller
    /// cannot state how directly the evidence was established.
    pub fn new(
        generation_id: GenerationId,
        sequence: SequenceNumber,
        observed_at_ms: MonotonicMs,
        confidence: EvidenceConfidence,
        evidence_claim_id: EvidenceClaimId,
    ) -> Result<Self, LifecycleError> {
        if confidence == EvidenceConfidence::Unknown {
            return Err(LifecycleError::UnknownEvidenceConfidence);
        }
        Ok(Self {
            generation_id,
            sequence,
            observed_at_ms,
            confidence,
            evidence_claim_id,
        })
    }

    #[must_use]
    pub const fn generation_id(&self) -> GenerationId {
        self.generation_id
    }

    #[must_use]
    pub const fn sequence(&self) -> SequenceNumber {
        self.sequence
    }

    #[must_use]
    pub const fn observed_at_ms(&self) -> MonotonicMs {
        self.observed_at_ms
    }

    #[must_use]
    pub const fn confidence(&self) -> EvidenceConfidence {
        self.confidence
    }

    #[must_use]
    pub const fn evidence_claim_id(&self) -> &EvidenceClaimId {
        &self.evidence_claim_id
    }

    fn same_position(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence == other.sequence
            && self.observed_at_ms == other.observed_at_ms
    }

    fn at_or_after(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence >= other.sequence
            && self.observed_at_ms >= other.observed_at_ms
    }

    fn strictly_after(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence > other.sequence
            && self.observed_at_ms >= other.observed_at_ms
    }

    fn lifetime_at_or_after(&self, other: &Self) -> bool {
        self.generation_id != other.generation_id && self.observed_at_ms >= other.observed_at_ms
    }
}

/// One independent fact and the exact evidence that last established it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LifecycleFact<T> {
    value: T,
    stamp: Option<ObservationStamp>,
}

impl<T> LifecycleFact<T>
where
    T: Copy,
{
    const fn unknown(value: T) -> Self {
        Self { value, stamp: None }
    }

    fn observed(value: T, stamp: ObservationStamp) -> Self {
        Self {
            value,
            stamp: Some(stamp),
        }
    }

    #[must_use]
    pub const fn value(&self) -> T {
        self.value
    }

    #[must_use]
    pub const fn stamp(&self) -> Option<&ObservationStamp> {
        self.stamp.as_ref()
    }

    #[must_use]
    pub const fn is_observed(&self) -> bool {
        self.stamp.is_some()
    }

    fn accepts(&self, stamp: &ObservationStamp) -> bool {
        self.stamp
            .as_ref()
            .is_none_or(|current| stamp.strictly_after(current))
    }

    fn apply(&mut self, value: T, stamp: ObservationStamp) -> bool {
        if !self.accepts(&stamp) {
            return false;
        }
        self.value = value;
        self.stamp = Some(stamp);
        true
    }
}

/// Battery-value semantics kept distinct from freshness and from zero percent.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BatteryValue {
    Unknown,
    Unavailable,
    Reported(BatteryPercent),
}

/// Independently inspectable battery value and freshness evidence.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BatteryLifecycle {
    value: LifecycleFact<BatteryValue>,
    freshness: LifecycleFact<FreshnessState>,
}

impl BatteryLifecycle {
    const fn unknown() -> Self {
        Self {
            value: LifecycleFact::unknown(BatteryValue::Unknown),
            freshness: LifecycleFact::unknown(FreshnessState::Unknown),
        }
    }

    #[must_use]
    pub const fn value(&self) -> &LifecycleFact<BatteryValue> {
        &self.value
    }

    #[must_use]
    pub const fn freshness(&self) -> &LifecycleFact<FreshnessState> {
        &self.freshness
    }

    fn apply_value(&mut self, value: BatteryValue, stamp: ObservationStamp) -> bool {
        if !self.value.accepts(&stamp) || !self.freshness.accepts(&stamp) {
            return false;
        }
        let value_applied = self.value.apply(value, stamp.clone());
        let freshness_applied = self.freshness.apply(FreshnessState::Fresh, stamp);
        debug_assert!(value_applied && freshness_applied);
        true
    }

    fn mark_stale(&mut self, stamp: ObservationStamp) -> bool {
        if !self.value.is_observed() || !self.freshness.accepts(&stamp) {
            return false;
        }
        self.freshness.apply(FreshnessState::Stale, stamp)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ChildIdentity {
    device_id: LogicalDeviceId,
    device_kind: DeviceKind,
    product_id: ProductId,
}

impl ChildIdentity {
    /// Creates an immutable logical child identity.
    ///
    /// # Errors
    ///
    /// Receiver and mat identities are rejected because they are not children.
    pub fn new(
        device_id: LogicalDeviceId,
        device_kind: DeviceKind,
        product_id: ProductId,
    ) -> Result<Self, LifecycleError> {
        if matches!(device_kind, DeviceKind::Receiver | DeviceKind::Mat) {
            return Err(LifecycleError::InvalidChildKind(device_kind));
        }
        Ok(Self {
            device_id,
            device_kind,
            product_id,
        })
    }

    #[must_use]
    pub const fn device_id(&self) -> &LogicalDeviceId {
        &self.device_id
    }

    #[must_use]
    pub const fn device_kind(&self) -> DeviceKind {
        self.device_kind
    }

    #[must_use]
    pub const fn product_id(&self) -> ProductId {
        self.product_id
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EndpointIdentity {
    endpoint_id: EndpointId,
    route_kind: RouteKind,
    connection_mode: ConnectionMode,
}

impl EndpointIdentity {
    /// Creates a route identity with a non-contradictory presentation mode.
    ///
    /// # Errors
    ///
    /// Returns [`LifecycleError::InvalidEndpointMode`] when route and mode are
    /// from different transports.
    pub fn new(
        endpoint_id: EndpointId,
        route_kind: RouteKind,
        connection_mode: ConnectionMode,
    ) -> Result<Self, LifecycleError> {
        let valid = matches!(
            (route_kind, connection_mode),
            (RouteKind::HyperfluxWireless, ConnectionMode::Hyperflux24ghz)
                | (RouteKind::DirectUsb, ConnectionMode::DirectUsb)
                | (RouteKind::Bluetooth, ConnectionMode::Bluetooth)
        );
        if !valid {
            return Err(LifecycleError::InvalidEndpointMode {
                route_kind,
                connection_mode,
            });
        }
        Ok(Self {
            endpoint_id,
            route_kind,
            connection_mode,
        })
    }

    #[must_use]
    pub const fn endpoint_id(&self) -> &EndpointId {
        &self.endpoint_id
    }

    #[must_use]
    pub const fn route_kind(&self) -> RouteKind {
        self.route_kind
    }

    #[must_use]
    pub const fn connection_mode(&self) -> ConnectionMode {
        self.connection_mode
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EndpointLifecycle {
    identity: EndpointIdentity,
    registered_at: ObservationStamp,
    route: LifecycleFact<RouteState>,
    power: LifecycleFact<PowerState>,
    sleep: LifecycleFact<SleepState>,
    activity: LifecycleFact<ActivityState>,
    contact: LifecycleFact<ContactState>,
    freshness: LifecycleFact<FreshnessState>,
}

impl EndpointLifecycle {
    fn new(
        identity: EndpointIdentity,
        registered_at: ObservationStamp,
        device_kind: DeviceKind,
    ) -> Self {
        let contact = if device_kind == DeviceKind::Keyboard {
            ContactState::NotApplicable
        } else {
            ContactState::Unknown
        };
        Self {
            identity,
            registered_at,
            route: LifecycleFact::unknown(RouteState::Unknown),
            power: LifecycleFact::unknown(PowerState::Unknown),
            sleep: LifecycleFact::unknown(SleepState::Unknown),
            activity: LifecycleFact::unknown(ActivityState::Unknown),
            contact: LifecycleFact::unknown(contact),
            freshness: LifecycleFact::unknown(FreshnessState::Unknown),
        }
    }

    #[must_use]
    pub const fn identity(&self) -> &EndpointIdentity {
        &self.identity
    }

    #[must_use]
    pub const fn registered_at(&self) -> &ObservationStamp {
        &self.registered_at
    }

    #[must_use]
    pub const fn route(&self) -> &LifecycleFact<RouteState> {
        &self.route
    }

    #[must_use]
    pub const fn power(&self) -> &LifecycleFact<PowerState> {
        &self.power
    }

    #[must_use]
    pub const fn sleep(&self) -> &LifecycleFact<SleepState> {
        &self.sleep
    }

    #[must_use]
    pub const fn activity(&self) -> &LifecycleFact<ActivityState> {
        &self.activity
    }

    #[must_use]
    pub const fn contact(&self) -> &LifecycleFact<ContactState> {
        &self.contact
    }

    #[must_use]
    pub const fn freshness(&self) -> &LifecycleFact<FreshnessState> {
        &self.freshness
    }

    fn availability_candidate(&self, pairing: &LifecycleFact<PairingState>) -> PresenceState {
        let mut candidates = Vec::with_capacity(5);
        if let Some(stamp) = self.route.stamp() {
            let state = match self.route.value() {
                RouteState::Available => PresenceState::Available,
                RouteState::Unavailable => PresenceState::Unavailable,
                RouteState::Stale | RouteState::Unknown => PresenceState::Unknown,
            };
            candidates.push((state, stamp));
        }
        if self.power.value() == PowerState::Off
            && let Some(stamp) = self.power.stamp()
        {
            candidates.push((PresenceState::Unavailable, stamp));
        }
        if self.activity.value() == ActivityState::Active
            && let Some(stamp) = self.activity.stamp()
        {
            candidates.push((PresenceState::Available, stamp));
        }
        if self.freshness.value() == FreshnessState::Stale
            && let Some(stamp) = self.freshness.stamp()
        {
            candidates.push((PresenceState::Unknown, stamp));
        }
        if self.identity.route_kind == RouteKind::HyperfluxWireless
            && pairing.value() == PairingState::Unpaired
            && let Some(stamp) = pairing.stamp()
        {
            candidates.push((PresenceState::Unavailable, stamp));
        }
        select_latest_state(&candidates).unwrap_or(PresenceState::Unknown)
    }

    fn presence_candidate(&self, pairing: &LifecycleFact<PairingState>) -> PresenceState {
        let availability = self.availability_candidate(pairing);
        if availability != PresenceState::Available {
            return availability;
        }

        let mut activity = Vec::with_capacity(2);
        if let Some(stamp) = self.sleep.stamp() {
            let state = match self.sleep.value() {
                SleepState::Asleep => PresenceState::Sleeping,
                SleepState::Awake => PresenceState::Available,
                SleepState::Unknown => PresenceState::Unknown,
            };
            activity.push((state, stamp));
        }
        if self.activity.value() == ActivityState::Active
            && let Some(stamp) = self.activity.stamp()
        {
            activity.push((PresenceState::Available, stamp));
        }
        select_latest_state(&activity).unwrap_or(PresenceState::Available)
    }
}

fn select_latest_state(candidates: &[(PresenceState, &ObservationStamp)]) -> Option<PresenceState> {
    let latest = candidates
        .iter()
        .map(|(_, stamp)| *stamp)
        .max_by_key(|stamp| (stamp.sequence(), stamp.observed_at_ms()))?;
    let mut selected = None;
    for (state, stamp) in candidates {
        if stamp.same_position(latest) {
            match selected {
                None => selected = Some(*state),
                Some(current) if current == *state => {}
                Some(_) => return Some(PresenceState::Unknown),
            }
        }
    }
    selected
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeviceLifecycle {
    identity: ChildIdentity,
    registered_at: ObservationStamp,
    latest_observation: ObservationStamp,
    pairing: LifecycleFact<PairingState>,
    battery: BatteryLifecycle,
    endpoints: BTreeMap<EndpointId, EndpointLifecycle>,
}

impl DeviceLifecycle {
    fn new(identity: ChildIdentity, registered_at: ObservationStamp) -> Self {
        Self {
            identity,
            latest_observation: registered_at.clone(),
            registered_at,
            pairing: LifecycleFact::unknown(PairingState::Unknown),
            battery: BatteryLifecycle::unknown(),
            endpoints: BTreeMap::new(),
        }
    }

    #[must_use]
    pub const fn identity(&self) -> &ChildIdentity {
        &self.identity
    }

    #[must_use]
    pub const fn registered_at(&self) -> &ObservationStamp {
        &self.registered_at
    }

    #[must_use]
    pub const fn latest_observation(&self) -> &ObservationStamp {
        &self.latest_observation
    }

    #[must_use]
    pub const fn pairing(&self) -> &LifecycleFact<PairingState> {
        &self.pairing
    }

    #[must_use]
    pub const fn battery(&self) -> &BatteryLifecycle {
        &self.battery
    }

    #[must_use]
    pub fn endpoint(&self, endpoint_id: &EndpointId) -> Option<&EndpointLifecycle> {
        self.endpoints.get(endpoint_id)
    }

    #[must_use]
    pub fn endpoints(&self) -> impl ExactSizeIterator<Item = &EndpointLifecycle> {
        self.endpoints.values()
    }

    #[must_use]
    pub fn presence(&self) -> PresenceState {
        if self.endpoints.is_empty() {
            return PresenceState::Unknown;
        }
        let mut saw_sleeping = false;
        let mut saw_unknown = false;
        for endpoint in self.endpoints.values() {
            match endpoint.presence_candidate(&self.pairing) {
                PresenceState::Available => return PresenceState::Available,
                PresenceState::Sleeping => saw_sleeping = true,
                PresenceState::Unknown => saw_unknown = true,
                PresenceState::Unavailable => {}
            }
        }
        if saw_sleeping {
            PresenceState::Sleeping
        } else if saw_unknown {
            PresenceState::Unknown
        } else {
            PresenceState::Unavailable
        }
    }

    fn accepts(&self, stamp: &ObservationStamp) -> bool {
        stamp.same_position(&self.latest_observation)
            || stamp.strictly_after(&self.latest_observation)
    }

    fn note(&mut self, stamp: &ObservationStamp) {
        if stamp.strictly_after(&self.latest_observation) {
            self.latest_observation = stamp.clone();
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverGenerationLifecycle {
    generation_id: GenerationId,
    activated_at: ObservationStamp,
    lifecycle: LifecycleFact<ReceiverLifecycleState>,
    devices: BTreeMap<LogicalDeviceId, DeviceLifecycle>,
}

impl ReceiverGenerationLifecycle {
    fn new(stamp: ObservationStamp) -> Self {
        Self {
            generation_id: stamp.generation_id(),
            activated_at: stamp.clone(),
            lifecycle: LifecycleFact::observed(ReceiverLifecycleState::Active, stamp),
            devices: BTreeMap::new(),
        }
    }

    #[must_use]
    pub const fn generation_id(&self) -> GenerationId {
        self.generation_id
    }

    #[must_use]
    pub const fn activated_at(&self) -> &ObservationStamp {
        &self.activated_at
    }

    #[must_use]
    pub const fn lifecycle(&self) -> &LifecycleFact<ReceiverLifecycleState> {
        &self.lifecycle
    }

    #[must_use]
    pub fn device(&self, device_id: &LogicalDeviceId) -> Option<&DeviceLifecycle> {
        self.devices.get(device_id)
    }

    #[must_use]
    pub fn devices(&self) -> impl ExactSizeIterator<Item = &DeviceLifecycle> {
        self.devices.values()
    }

    fn accepts_receiver_observation(&self, stamp: &ObservationStamp) -> bool {
        self.lifecycle
            .stamp()
            .is_some_and(|current| stamp.strictly_after(current))
            && self
                .devices
                .values()
                .all(|device| stamp.at_or_after(device.latest_observation()))
    }

    fn accepts_replacement(&self, stamp: &ObservationStamp) -> bool {
        self.lifecycle
            .stamp()
            .is_some_and(|current| stamp.lifetime_at_or_after(current))
            && self
                .devices
                .values()
                .all(|device| stamp.lifetime_at_or_after(device.latest_observation()))
    }
}

/// Explicit receiver lifecycle with irreversible generation replacement.
///
/// The machine exposes passive state only. A current generation, qualified
/// child, or available route does not confer permission to write hardware.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverLifecycleMachine {
    receiver_id: ReceiverId,
    limits: LifecycleLimits,
    latest_generation: Option<GenerationId>,
    retired_generations: VecDeque<GenerationId>,
    last_receiver_observation: Option<ObservationStamp>,
    current: Option<ReceiverGenerationLifecycle>,
}

impl ReceiverLifecycleMachine {
    /// Creates an absent receiver lifecycle.
    ///
    /// # Errors
    ///
    /// This constructor is fallible to keep future limit validation at the
    /// boundary; currently all [`LifecycleLimits`] values are prevalidated.
    pub fn new(receiver_id: ReceiverId, limits: LifecycleLimits) -> Result<Self, LifecycleError> {
        if limits.max_devices == 0 || limits.max_endpoints_per_device == 0 {
            return Err(LifecycleError::InvalidLimits);
        }
        Ok(Self {
            receiver_id,
            limits,
            latest_generation: None,
            retired_generations: VecDeque::new(),
            last_receiver_observation: None,
            current: None,
        })
    }

    #[must_use]
    pub const fn receiver_id(&self) -> &ReceiverId {
        &self.receiver_id
    }

    #[must_use]
    pub const fn latest_generation(&self) -> Option<GenerationId> {
        self.latest_generation
    }

    #[must_use]
    pub const fn current(&self) -> Option<&ReceiverGenerationLifecycle> {
        self.current.as_ref()
    }

    /// Discovers a generation only while the receiver is absent.
    pub fn discover(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        if self.latest_generation == Some(stamp.generation_id())
            || self.retired_generations.contains(&stamp.generation_id())
        {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if self.current.is_some() {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if self
            .last_receiver_observation
            .as_ref()
            .is_some_and(|last| !stamp.lifetime_at_or_after(last))
        {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        self.latest_generation = Some(stamp.generation_id());
        self.last_receiver_observation = Some(stamp.clone());
        self.current = Some(ReceiverGenerationLifecycle::new(stamp));
        ApplyOutcome::Applied
    }

    /// Atomically retires the current generation and starts a newer empty one.
    pub fn replace_generation(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        let Some(current) = self.current.as_ref() else {
            return if self.latest_generation == Some(stamp.generation_id()) {
                ApplyOutcome::RejectedStaleGeneration
            } else {
                ApplyOutcome::RejectedReceiverAbsent
            };
        };
        if stamp.generation_id() == current.generation_id
            || self.retired_generations.contains(&stamp.generation_id())
        {
            return ApplyOutcome::RejectedStaleGeneration;
        }
        if !current.accepts_replacement(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        let retired = current.generation_id;
        self.remember_retired_generation(retired);
        self.latest_generation = Some(stamp.generation_id());
        self.last_receiver_observation = Some(stamp.clone());
        self.current = Some(ReceiverGenerationLifecycle::new(stamp));
        ApplyOutcome::Applied
    }

    /// Applies one declared receiver transition for the current generation.
    pub fn transition_receiver(
        &mut self,
        target: ReceiverLifecycleState,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.generation_rejection(stamp.generation_id()) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        if !current.accepts_receiver_observation(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        let from = current.lifecycle.value();
        if !valid_receiver_transition(from, target) {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        let changed = current.lifecycle.apply(target, stamp.clone());
        debug_assert!(changed, "receiver ordering was checked before mutation");
        self.last_receiver_observation = Some(stamp);
        ApplyOutcome::Applied
    }

    /// Completes an already-declared disconnect and retires its generation.
    pub fn complete_disconnect(&mut self, stamp: ObservationStamp) -> ApplyOutcome {
        if let Some(outcome) = self.generation_rejection(stamp.generation_id()) {
            return outcome;
        }
        let Some(current) = self.current.as_ref() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        if !current.accepts_receiver_observation(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if current.lifecycle.value() != ReceiverLifecycleState::Disconnecting {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        let retired = current.generation_id;
        self.last_receiver_observation = Some(stamp);
        self.current = None;
        self.remember_retired_generation(retired);
        ApplyOutcome::Applied
    }

    /// Registers or refreshes one independent logical child.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid child identity, immutable identity changes,
    /// or exhausted bounded capacity.
    pub fn register_device(
        &mut self,
        identity: ChildIdentity,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        if !stamp.at_or_after(&current.activated_at) {
            return Ok(ApplyOutcome::IgnoredOlderObservation);
        }
        if let Some(device) = current.devices.get_mut(identity.device_id()) {
            if !device.accepts(&stamp) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            if device.identity != identity {
                return Err(LifecycleError::DeviceIdentityConflict(
                    identity.device_id().clone(),
                ));
            }
            if !stamp.strictly_after(&device.registered_at) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            device.note(&stamp);
            device.registered_at = stamp;
            return Ok(ApplyOutcome::Applied);
        }
        if current.devices.len() >= self.limits.max_devices {
            return Err(LifecycleError::DeviceCapacity);
        }
        current.devices.insert(
            identity.device_id().clone(),
            DeviceLifecycle::new(identity, stamp),
        );
        Ok(ApplyOutcome::Applied)
    }

    /// Applies receiver-inventory pairing evidence without changing routes.
    pub fn observe_pairing(
        &mut self,
        device_id: &LogicalDeviceId,
        value: PairingState,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if value == PairingState::Unknown {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        self.observe_device_fact(device_id, value, stamp, |device| &mut device.pairing)
    }

    /// Applies a reported battery percentage and marks it fresh atomically.
    pub fn observe_battery_reported(
        &mut self,
        device_id: &LogicalDeviceId,
        percentage: BatteryPercent,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        self.observe_battery_value(device_id, BatteryValue::Reported(percentage), stamp)
    }

    /// Records that the receiver cannot currently provide a battery value.
    pub fn observe_battery_unavailable(
        &mut self,
        device_id: &LogicalDeviceId,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        self.observe_battery_value(device_id, BatteryValue::Unavailable, stamp)
    }

    /// Marks an existing battery observation stale without discarding its value.
    pub fn mark_battery_stale(
        &mut self,
        device_id: &LogicalDeviceId,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if !device.battery.value.is_observed() {
            return ApplyOutcome::RejectedInvalidTransition;
        }
        if !device.battery.freshness.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        if device.battery.mark_stale(stamp) {
            ApplyOutcome::Applied
        } else {
            ApplyOutcome::IgnoredOlderObservation
        }
    }

    /// Registers or refreshes one route without modifying its independent facts.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown child, immutable identity change, or
    /// exhausted endpoint capacity.
    pub fn register_endpoint(
        &mut self,
        device_id: &LogicalDeviceId,
        identity: EndpointIdentity,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        if !device.accepts(&stamp) {
            return Ok(ApplyOutcome::IgnoredOlderObservation);
        }
        if let Some(endpoint) = device.endpoints.get(identity.endpoint_id()) {
            if endpoint.identity != identity {
                return Err(LifecycleError::EndpointIdentityConflict(
                    identity.endpoint_id().clone(),
                ));
            }
            if !stamp.strictly_after(&endpoint.registered_at) {
                return Ok(ApplyOutcome::IgnoredOlderObservation);
            }
            device.note(&stamp);
            let Some(endpoint) = device.endpoints.get_mut(identity.endpoint_id()) else {
                return Err(LifecycleError::UnknownEndpoint(
                    identity.endpoint_id().clone(),
                ));
            };
            endpoint.registered_at = stamp;
            return Ok(ApplyOutcome::Applied);
        }
        if device.endpoints.len() >= self.limits.max_endpoints_per_device {
            return Err(LifecycleError::EndpointCapacity(device_id.clone()));
        }
        device.note(&stamp);
        device.endpoints.insert(
            identity.endpoint_id().clone(),
            EndpointLifecycle::new(identity, stamp, device.identity.device_kind),
        );
        Ok(ApplyOutcome::Applied)
    }

    /// Applies passive route availability evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_route(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: RouteState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == RouteState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.route
        })
    }

    /// Applies passive endpoint power evidence without deriving other facts.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_power(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: PowerState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == PowerState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.power
        })
    }

    /// Applies passive endpoint sleep evidence without changing pairing.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_sleep(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: SleepState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == SleepState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.sleep
        })
    }

    /// Applies passive endpoint activity evidence.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_activity(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: ActivityState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == ActivityState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.activity
        })
    }

    /// Applies mouse-only mat-contact evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_contact(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: ContactState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if matches!(value, ContactState::Unknown | ContactState::NotApplicable) {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        if device.identity.device_kind != DeviceKind::Mouse {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        apply_endpoint_fact(device, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.contact
        })
    }

    /// Applies passive freshness evidence to one known endpoint.
    ///
    /// # Errors
    ///
    /// Returns an error when the endpoint identity is unknown.
    pub fn observe_freshness(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: FreshnessState,
        stamp: ObservationStamp,
    ) -> Result<ApplyOutcome, LifecycleError> {
        if value == FreshnessState::Unknown {
            return Ok(ApplyOutcome::RejectedInvalidTransition);
        }
        self.observe_endpoint_fact(device_id, endpoint_id, value, stamp, |endpoint| {
            &mut endpoint.freshness
        })
    }

    fn generation_rejection(&self, generation_id: GenerationId) -> Option<ApplyOutcome> {
        match self.current.as_ref() {
            Some(current) if generation_id == current.generation_id => None,
            Some(_) => Some(ApplyOutcome::RejectedStaleGeneration),
            None if self.latest_generation == Some(generation_id)
                || self.retired_generations.contains(&generation_id) =>
            {
                Some(ApplyOutcome::RejectedStaleGeneration)
            }
            None => Some(ApplyOutcome::RejectedReceiverAbsent),
        }
    }

    fn remember_retired_generation(&mut self, generation_id: GenerationId) {
        if self.retired_generations.contains(&generation_id) {
            return;
        }
        if self.retired_generations.len() == MAX_RETIRED_GENERATIONS {
            self.retired_generations.pop_front();
        }
        self.retired_generations.push_back(generation_id);
    }

    fn device_observation_rejection(&self, stamp: &ObservationStamp) -> Option<ApplyOutcome> {
        let rejection = self.generation_rejection(stamp.generation_id());
        if rejection.is_some() {
            return rejection;
        }
        self.current.as_ref().and_then(|current| {
            (current.lifecycle.value() == ReceiverLifecycleState::Disconnecting)
                .then_some(ApplyOutcome::RejectedReceiverAbsent)
        })
    }

    fn observe_device_fact<T>(
        &mut self,
        device_id: &LogicalDeviceId,
        value: T,
        stamp: ObservationStamp,
        select: fn(&mut DeviceLifecycle) -> &mut LifecycleFact<T>,
    ) -> ApplyOutcome
    where
        T: Copy,
    {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        let applied = select(device).apply(value, stamp);
        if !applied {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        ApplyOutcome::Applied
    }

    fn observe_battery_value(
        &mut self,
        device_id: &LogicalDeviceId,
        value: BatteryValue,
        stamp: ObservationStamp,
    ) -> ApplyOutcome {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return outcome;
        }
        let Some(current) = self.current.as_mut() else {
            return ApplyOutcome::RejectedReceiverAbsent;
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return ApplyOutcome::RejectedUnknownDevice;
        };
        if !device.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        if !device.battery.value.accepts(&stamp) || !device.battery.freshness.accepts(&stamp) {
            return ApplyOutcome::IgnoredOlderObservation;
        }
        device.note(&stamp);
        if device.battery.apply_value(value, stamp) {
            ApplyOutcome::Applied
        } else {
            ApplyOutcome::IgnoredOlderObservation
        }
    }

    fn observe_endpoint_fact<T>(
        &mut self,
        device_id: &LogicalDeviceId,
        endpoint_id: &EndpointId,
        value: T,
        stamp: ObservationStamp,
        select: fn(&mut EndpointLifecycle) -> &mut LifecycleFact<T>,
    ) -> Result<ApplyOutcome, LifecycleError>
    where
        T: Copy,
    {
        if let Some(outcome) = self.device_observation_rejection(&stamp) {
            return Ok(outcome);
        }
        let Some(current) = self.current.as_mut() else {
            return Ok(ApplyOutcome::RejectedReceiverAbsent);
        };
        let Some(device) = current.devices.get_mut(device_id) else {
            return Ok(ApplyOutcome::RejectedUnknownDevice);
        };
        apply_endpoint_fact(device, endpoint_id, value, stamp, select)
    }
}

fn apply_endpoint_fact<T>(
    device: &mut DeviceLifecycle,
    endpoint_id: &EndpointId,
    value: T,
    stamp: ObservationStamp,
    select: fn(&mut EndpointLifecycle) -> &mut LifecycleFact<T>,
) -> Result<ApplyOutcome, LifecycleError>
where
    T: Copy,
{
    if !device.accepts(&stamp) {
        return Ok(ApplyOutcome::IgnoredOlderObservation);
    }
    if !device.endpoints.contains_key(endpoint_id) {
        return Err(LifecycleError::UnknownEndpoint(endpoint_id.clone()));
    }
    device.note(&stamp);
    let Some(endpoint) = device.endpoints.get_mut(endpoint_id) else {
        return Err(LifecycleError::UnknownEndpoint(endpoint_id.clone()));
    };
    let applied = select(endpoint).apply(value, stamp);
    if !applied {
        return Ok(ApplyOutcome::IgnoredOlderObservation);
    }
    Ok(ApplyOutcome::Applied)
}

const fn valid_receiver_transition(
    from: ReceiverLifecycleState,
    to: ReceiverLifecycleState,
) -> bool {
    if matches!(from, ReceiverLifecycleState::Unknown)
        || matches!(to, ReceiverLifecycleState::Unknown)
    {
        return false;
    }
    !matches!(from, ReceiverLifecycleState::Disconnecting)
        || matches!(to, ReceiverLifecycleState::Disconnecting)
}
