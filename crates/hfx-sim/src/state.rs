// SPDX-License-Identifier: GPL-2.0-only

use crate::clock::ClockError;
use hfx_domain::{
    ActivityState, ApplyOutcome, BatteryPercent, ContactState, DeviceKind, FixtureSource,
    FreshnessState, GenerationId, LogicalDeviceId, PairingState, PowerState, PresenceState,
    ProfileId, ReceiverLifecycleState, RestoreId, RouteState, SleepState,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fmt::Write as _;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct EvidenceCell<T> {
    pub value: T,
    pub observed_at_ms: Option<u64>,
}

impl<T> EvidenceCell<T> {
    #[must_use]
    pub const fn unknown(value: T) -> Self {
        Self {
            value,
            observed_at_ms: None,
        }
    }

    pub fn apply(&mut self, value: T, observed_at_ms: u64) -> bool {
        if self
            .observed_at_ms
            .is_some_and(|current| observed_at_ms < current)
        {
            return false;
        }
        self.value = value;
        self.observed_at_ms = Some(observed_at_ms);
        true
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "kebab-case")]
pub enum BatteryState {
    Unknown,
    Unavailable,
    Reported { percentage: BatteryPercent },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct DeviceSnapshot {
    pub logical_device_id: LogicalDeviceId,
    pub device_kind: DeviceKind,
    pub product_id: hfx_domain::ProductId,
    pub profile_id: Option<ProfileId>,
    pub pairing: EvidenceCell<PairingState>,
    pub route: EvidenceCell<RouteState>,
    pub power: EvidenceCell<PowerState>,
    pub sleep: EvidenceCell<SleepState>,
    pub contact: EvidenceCell<ContactState>,
    pub activity: EvidenceCell<ActivityState>,
    pub freshness: EvidenceCell<FreshnessState>,
    pub battery: EvidenceCell<BatteryState>,
    pub presence: PresenceState,
}

impl DeviceSnapshot {
    pub(crate) fn reset_generation_evidence(&mut self) {
        self.pairing = EvidenceCell::unknown(PairingState::Unknown);
        self.route = EvidenceCell::unknown(RouteState::Unknown);
        self.power = EvidenceCell::unknown(PowerState::Unknown);
        self.sleep = EvidenceCell::unknown(SleepState::Unknown);
        self.contact = EvidenceCell::unknown(if self.device_kind == DeviceKind::Mouse {
            ContactState::Unknown
        } else {
            ContactState::NotApplicable
        });
        self.activity = EvidenceCell::unknown(ActivityState::Unknown);
        self.freshness = EvidenceCell::unknown(FreshnessState::Unknown);
        self.battery = EvidenceCell::unknown(BatteryState::Unknown);
        self.presence = PresenceState::Unknown;
    }

    pub(crate) fn derive_presence(&mut self) {
        self.presence = if self.pairing.value == PairingState::Unpaired
            || self.route.value == RouteState::Unavailable
            || self.power.value == PowerState::Off
        {
            PresenceState::Unavailable
        } else if self.sleep.value == SleepState::Asleep {
            PresenceState::Sleeping
        } else if self.pairing.value == PairingState::Paired
            && self.route.value == RouteState::Available
        {
            PresenceState::Available
        } else {
            PresenceState::Unknown
        };
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RestoreSnapshot {
    pub restore_id: RestoreId,
    pub generation_id: GenerationId,
    pub targets: BTreeSet<LogicalDeviceId>,
    pub delivered: BTreeSet<LogicalDeviceId>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct StateSnapshot {
    pub source: FixtureSource,
    pub test_fixture: bool,
    pub hardware_claim_authority: bool,
    pub receiver_profile_id: ProfileId,
    pub receiver_generation: GenerationId,
    pub receiver_connected: bool,
    pub receiver_lifecycle: EvidenceCell<ReceiverLifecycleState>,
    pub surface_profile_id: Option<ProfileId>,
    pub devices: BTreeMap<LogicalDeviceId, DeviceSnapshot>,
    pub pending_restore: Option<RestoreSnapshot>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct ReplayMetrics {
    pub events: u64,
    pub applied: u64,
    pub ignored_older_observations: u64,
    pub rejected: u64,
    pub malformed_observations: u64,
    pub delivered_frames: u64,
    pub failed_frames: u64,
    pub completed_restores: u64,
    pub invalidated_restores: u64,
    pub peak_queue_depth: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TraceRecord {
    pub sequence: u64,
    pub observed_at_ms: u64,
    pub applied_at_ms: u64,
    pub event_generation: GenerationId,
    pub event_kind: String,
    pub outcome: ApplyOutcome,
    pub resulting_generation: GenerationId,
    pub snapshot_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ReplayResult {
    pub schema: String,
    pub scenario_id: hfx_domain::ScenarioId,
    pub source: FixtureSource,
    pub test_fixture: bool,
    pub hardware_claim_authority: bool,
    pub final_snapshot: StateSnapshot,
    pub trace: Vec<TraceRecord>,
    pub metrics: ReplayMetrics,
    pub content_sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SimulatorError {
    InvalidScenario(String),
    InvalidReplay(String),
    Serialization(String),
    Clock(ClockError),
}

impl fmt::Display for SimulatorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidScenario(message) => write!(formatter, "invalid scenario: {message}"),
            Self::InvalidReplay(message) => write!(formatter, "invalid replay: {message}"),
            Self::Serialization(message) => write!(formatter, "serialization failed: {message}"),
            Self::Clock(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for SimulatorError {}

impl From<ClockError> for SimulatorError {
    fn from(value: ClockError) -> Self {
        Self::Clock(value)
    }
}

pub(crate) fn sha256<T: Serialize>(value: &T) -> Result<String, SimulatorError> {
    let bytes = serde_json::to_vec(value)
        .map_err(|error| SimulatorError::Serialization(error.to_string()))?;
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut encoded, "{byte:02x}")
            .map_err(|error| SimulatorError::Serialization(error.to_string()))?;
    }
    Ok(encoded)
}
