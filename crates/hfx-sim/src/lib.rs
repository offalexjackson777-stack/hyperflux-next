// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod clock;
mod engine;
mod event;
mod replay;
mod state;

pub use clock::{ClockError, VirtualClock};
pub use engine::Simulator;
pub use event::{
    InitialChild, InitialState, MalformedDimension, MalformedReason, Provenance, Scenario,
    ScheduledEvent, SimulatorEvent,
};
pub use replay::{MAX_EVENTS, MAX_REPLAY_BYTES, MAX_SCENARIO_TIME_MS, parse_replay, run_replay};
pub use state::{
    BatteryState, DeviceSnapshot, EvidenceCell, ReplayMetrics, ReplayResult, RestoreSnapshot,
    SimulatorError, StateSnapshot, TraceRecord,
};
