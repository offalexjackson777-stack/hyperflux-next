// SPDX-License-Identifier: GPL-2.0-only

//! Immutable lifecycle facts and passive presence projection.
//!
//! This module projects passive evidence only. It deliberately contains no
//! lease, session, transport, or write-authority type.

use hfx_domain::{
    ActivityState, BatteryCharge, BatteryPercent, ConnectionMode, ContactState, DeviceKind,
    EndpointId, EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId, LogicalDeviceId,
    MonotonicMs, PairingState, PowerState, PresenceState, ProductId, ReceiverLifecycleState,
    RouteKind, RouteState, SequenceNumber, SleepState,
};
use std::collections::BTreeMap;
use std::fmt;

pub const DEFAULT_MAX_LIFECYCLE_DEVICES: usize = 32;
pub const DEFAULT_MAX_DEVICE_ENDPOINTS: usize = 8;
pub(super) const MAX_RETIRED_GENERATIONS: usize = 256;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LifecycleLimits {
    pub(super) max_devices: usize,
    pub(super) max_endpoints_per_device: usize,
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
    InvalidReturnPolicy,
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
            Self::InvalidReturnPolicy => formatter.write_str("endpoint return policy is invalid"),
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

    pub(super) fn same_position(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence == other.sequence
            && self.observed_at_ms == other.observed_at_ms
    }

    pub(super) fn at_or_after(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence >= other.sequence
            && self.observed_at_ms >= other.observed_at_ms
    }

    pub(super) fn strictly_after(&self, other: &Self) -> bool {
        self.generation_id == other.generation_id
            && self.sequence > other.sequence
            && self.observed_at_ms >= other.observed_at_ms
    }

    pub(super) fn lifetime_at_or_after(&self, other: &Self) -> bool {
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

    pub(super) fn accepts(&self, stamp: &ObservationStamp) -> bool {
        self.stamp
            .as_ref()
            .is_none_or(|current| stamp.strictly_after(current))
    }

    pub(super) fn apply(&mut self, value: T, stamp: ObservationStamp) -> bool {
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
    pub(super) value: LifecycleFact<BatteryValue>,
    pub(super) freshness: LifecycleFact<FreshnessState>,
    /// Charge state annotating the current reported value. It exists only
    /// beside a reported value and is cleared when that value is gone.
    pub(super) charge: LifecycleFact<Option<BatteryCharge>>,
}

impl BatteryLifecycle {
    const fn unknown() -> Self {
        Self {
            value: LifecycleFact::unknown(BatteryValue::Unknown),
            freshness: LifecycleFact::unknown(FreshnessState::Unknown),
            charge: LifecycleFact::unknown(None),
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

    #[must_use]
    pub const fn charge(&self) -> &LifecycleFact<Option<BatteryCharge>> {
        &self.charge
    }

    pub(super) fn apply_value(&mut self, value: BatteryValue, stamp: ObservationStamp) -> bool {
        if !self.value.accepts(&stamp) || !self.freshness.accepts(&stamp) {
            return false;
        }
        if !matches!(value, BatteryValue::Reported(_)) {
            self.charge.value = None;
        }
        let value_applied = self.value.apply(value, stamp.clone());
        let freshness_applied = self.freshness.apply(FreshnessState::Fresh, stamp);
        debug_assert!(value_applied && freshness_applied);
        true
    }

    pub(super) fn mark_stale(&mut self, stamp: ObservationStamp) -> bool {
        if !self.value.is_observed() || !self.freshness.accepts(&stamp) {
            return false;
        }
        self.charge.value = None;
        self.freshness.apply(FreshnessState::Stale, stamp)
    }

    /// Records the charge state reported with the current value. Without a
    /// reported value there is nothing for the charge state to annotate.
    pub(super) fn apply_charge(
        &mut self,
        charge: Option<BatteryCharge>,
        stamp: ObservationStamp,
    ) -> bool {
        if !matches!(self.value.value(), BatteryValue::Reported(_)) {
            return false;
        }
        self.charge.apply(charge, stamp)
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
    pub(super) identity: EndpointIdentity,
    pub(super) registered_at: ObservationStamp,
    pub(super) route: LifecycleFact<RouteState>,
    pub(super) power: LifecycleFact<PowerState>,
    pub(super) sleep: LifecycleFact<SleepState>,
    pub(super) activity: LifecycleFact<ActivityState>,
    pub(super) contact: LifecycleFact<ContactState>,
    pub(super) freshness: LifecycleFact<FreshnessState>,
}

impl EndpointLifecycle {
    pub(super) fn new(
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
    pub(super) identity: ChildIdentity,
    pub(super) registered_at: ObservationStamp,
    latest_observation: ObservationStamp,
    pub(super) pairing: LifecycleFact<PairingState>,
    pub(super) battery: BatteryLifecycle,
    pub(super) endpoints: BTreeMap<EndpointId, EndpointLifecycle>,
}

impl DeviceLifecycle {
    pub(super) fn new(identity: ChildIdentity, registered_at: ObservationStamp) -> Self {
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

    pub(super) fn accepts(&self, stamp: &ObservationStamp) -> bool {
        stamp.same_position(&self.latest_observation)
            || stamp.strictly_after(&self.latest_observation)
    }

    pub(super) fn note(&mut self, stamp: &ObservationStamp) {
        if stamp.strictly_after(&self.latest_observation) {
            self.latest_observation = stamp.clone();
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverGenerationLifecycle {
    pub(super) generation_id: GenerationId,
    pub(super) activated_at: ObservationStamp,
    pub(super) lifecycle: LifecycleFact<ReceiverLifecycleState>,
    pub(super) devices: BTreeMap<LogicalDeviceId, DeviceLifecycle>,
}

impl ReceiverGenerationLifecycle {
    pub(super) fn new(stamp: ObservationStamp) -> Self {
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

    pub(super) fn accepts_receiver_observation(&self, stamp: &ObservationStamp) -> bool {
        self.lifecycle
            .stamp()
            .is_some_and(|current| stamp.strictly_after(current))
            && self
                .devices
                .values()
                .all(|device| stamp.at_or_after(device.latest_observation()))
    }

    pub(super) fn accepts_replacement(&self, stamp: &ObservationStamp) -> bool {
        self.lifecycle
            .stamp()
            .is_some_and(|current| stamp.lifetime_at_or_after(current))
            && self
                .devices
                .values()
                .all(|device| stamp.lifetime_at_or_after(device.latest_observation()))
    }
}
