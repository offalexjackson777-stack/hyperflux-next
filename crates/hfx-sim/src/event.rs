// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ActivityState, BatteryPercent, ContactState, DeviceKind, FixtureSource, FreshnessState,
    GenerationId, LogicalDeviceId, PairingState, PowerState, ProfileId, ReceiverLifecycleState,
    RestoreId, RouteState, ScenarioId, SleepState, TransactionId, TransportOutcome,
};
use serde::{Deserialize, Serialize};

pub const SCENARIO_SCHEMA: &str = "hyperflux-simulator-scenario-v1";
pub const SANITIZATION_POLICY: &str = "no-private-identifiers-v1";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    #[serde(rename = "$schema", default, skip_serializing_if = "Option::is_none")]
    pub schema_uri: Option<String>,
    pub schema: String,
    pub scenario_id: ScenarioId,
    pub provenance: Provenance,
    pub initial: InitialState,
    pub events: Vec<ScheduledEvent>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Provenance {
    pub source: FixtureSource,
    pub test_fixture: bool,
    pub hardware_claim_authority: bool,
    pub private_identifiers_exported: bool,
    pub sanitization: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InitialState {
    pub receiver_profile_id: ProfileId,
    pub receiver_generation: GenerationId,
    pub surface_profile_id: Option<ProfileId>,
    pub children: Vec<InitialChild>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InitialChild {
    pub logical_device_id: LogicalDeviceId,
    pub device_kind: DeviceKind,
    pub product_id: hfx_domain::ProductId,
    pub profile_id: Option<ProfileId>,
    pub pairing: PairingState,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduledEvent {
    pub observed_at_ms: u64,
    #[serde(default)]
    pub delay_ms: u64,
    pub generation_id: GenerationId,
    pub event: SimulatorEvent,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum SimulatorEvent {
    ReceiverDisconnected,
    ReceiverConnected,
    ReceiverLifecycle {
        state: ReceiverLifecycleState,
    },
    DevicePairing {
        device_id: LogicalDeviceId,
        state: PairingState,
    },
    RouteObserved {
        device_id: LogicalDeviceId,
        state: RouteState,
    },
    PowerObserved {
        device_id: LogicalDeviceId,
        state: PowerState,
    },
    SleepObserved {
        device_id: LogicalDeviceId,
        state: SleepState,
    },
    ContactObserved {
        device_id: LogicalDeviceId,
        state: ContactState,
    },
    ActivityObserved {
        device_id: LogicalDeviceId,
        state: ActivityState,
    },
    FreshnessObserved {
        device_id: LogicalDeviceId,
        state: FreshnessState,
    },
    BatteryReported {
        device_id: LogicalDeviceId,
        percentage: BatteryPercent,
    },
    BatteryUnavailable {
        device_id: LogicalDeviceId,
    },
    MalformedObservation {
        device_id: LogicalDeviceId,
        dimension: MalformedDimension,
        reason: MalformedReason,
    },
    LightingFrame {
        transaction_id: TransactionId,
        frame_index: u32,
        targets: Vec<LogicalDeviceId>,
        outcome: TransportOutcome,
    },
    RestoreStarted {
        restore_id: RestoreId,
        targets: Vec<LogicalDeviceId>,
    },
    RestoreTarget {
        restore_id: RestoreId,
        device_id: LogicalDeviceId,
        outcome: TransportOutcome,
    },
}

impl SimulatorEvent {
    #[must_use]
    pub const fn kind(&self) -> &'static str {
        match self {
            Self::ReceiverDisconnected => "receiver-disconnected",
            Self::ReceiverConnected => "receiver-connected",
            Self::ReceiverLifecycle { .. } => "receiver-lifecycle",
            Self::DevicePairing { .. } => "device-pairing",
            Self::RouteObserved { .. } => "route-observed",
            Self::PowerObserved { .. } => "power-observed",
            Self::SleepObserved { .. } => "sleep-observed",
            Self::ContactObserved { .. } => "contact-observed",
            Self::ActivityObserved { .. } => "activity-observed",
            Self::FreshnessObserved { .. } => "freshness-observed",
            Self::BatteryReported { .. } => "battery-reported",
            Self::BatteryUnavailable { .. } => "battery-unavailable",
            Self::MalformedObservation { .. } => "malformed-observation",
            Self::LightingFrame { .. } => "lighting-frame",
            Self::RestoreStarted { .. } => "restore-started",
            Self::RestoreTarget { .. } => "restore-target",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum MalformedDimension {
    Identity,
    Battery,
    Activity,
    Contact,
    Route,
    Power,
    Sleep,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum MalformedReason {
    Truncated,
    InvalidLength,
    InvalidValue,
    Unsupported,
}
