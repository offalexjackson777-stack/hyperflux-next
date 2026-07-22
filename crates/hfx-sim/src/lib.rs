// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod clock;
mod engine;
mod event;
mod persistence;
mod replay;
mod restoration;
mod shadow;
mod state;
mod transport;

pub use clock::{ClockError, VirtualClock};
pub use engine::Simulator;
pub use event::{
    InitialChild, InitialState, MalformedDimension, MalformedReason, Provenance, Scenario,
    ScheduledEvent, SimulatorEvent,
};
pub use persistence::{SimPersistenceError, SimPersistenceStore};
pub use replay::{MAX_EVENTS, MAX_REPLAY_BYTES, MAX_SCENARIO_TIME_MS, parse_replay, run_replay};
pub use restoration::{
    CrashCheckpoint, CrashExecution, SimDeviceProfile, SimRestorationConfig, SimRestorationError,
    SimRestorationHarness,
};
pub use shadow::{
    SHADOW_FIXTURE_SCHEMA, SHADOW_RESULT_SCHEMA, ShadowAuthority, ShadowCheckpointComparison,
    ShadowComparisonResult, ShadowDifference, ShadowDomain, ShadowDomainSummary, ShadowError,
    ShadowExecutionBoundary, ShadowFixture, ShadowSideEffects, ShadowValueComparison,
    parse_shadow_fixture, run_shadow_comparison,
};
pub use state::{
    BatteryState, DeviceSnapshot, EvidenceCell, ReplayMetrics, ReplayResult, RestoreSnapshot,
    SimulatorError, StateSnapshot, TraceRecord,
};
pub use transport::{
    SimJournalState, SimReceiverTransport, SimTransportConfigError, SimTransportCrashPoint,
    SimTransportError, SimTransportErrorKind, SimTransportFailurePlan, SimTransportJournalRecord,
    SimTransportMetrics,
};
