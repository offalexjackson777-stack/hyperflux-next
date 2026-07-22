// SPDX-License-Identifier: GPL-2.0-only

use crate::Simulator;
use crate::event::{
    SANITIZATION_POLICY, SCENARIO_SCHEMA, Scenario, ScheduledEvent, SimulatorEvent,
};
use crate::state::{ReplayResult, SimulatorError, TraceRecord, sha256};
use hfx_domain::{
    ActivityState, ContactState, FixtureSource, FreshnessState, PairingState, PowerState,
    ReceiverLifecycleState, RouteState, SleepState,
};
use std::collections::{BTreeMap, BTreeSet};

pub const MAX_REPLAY_BYTES: usize = 1_048_576;
pub const MAX_EVENTS: usize = 4_096;
pub const MAX_SCENARIO_TIME_MS: u64 = 86_400_000;
pub const RESULT_SCHEMA: &str = "hyperflux-simulator-result-v1";

/// Parses a bounded, privacy-declared external replay fixture.
///
/// # Errors
///
/// Returns an error for empty or oversized input, malformed JSON, an invalid
/// scenario contract, or a fixture that is not explicitly sanitized replay.
pub fn parse_replay(bytes: &[u8]) -> Result<Scenario, SimulatorError> {
    if bytes.is_empty() || bytes.len() > MAX_REPLAY_BYTES {
        return Err(SimulatorError::InvalidReplay(format!(
            "serialized replay size must be 1..={MAX_REPLAY_BYTES} bytes"
        )));
    }
    let scenario: Scenario = serde_json::from_slice(bytes)
        .map_err(|error| SimulatorError::InvalidReplay(error.to_string()))?;
    validate_scenario(&scenario)?;
    if scenario.provenance.source != FixtureSource::SanitizedReplay {
        return Err(SimulatorError::InvalidReplay(
            "serialized fixtures must use sanitized-replay provenance".to_owned(),
        ));
    }
    Ok(scenario)
}

/// Executes a scenario with deterministic virtual time and event ordering.
///
/// # Errors
///
/// Returns an error when the scenario violates bounds, profile constraints,
/// event validity, virtual-clock ordering, or deterministic serialization.
pub fn run_replay(scenario: &Scenario) -> Result<ReplayResult, SimulatorError> {
    validate_scenario(scenario)?;
    let mut queue = EventQueue::new(&scenario.events)?;
    let mut simulator = Simulator::new(scenario.provenance.source, &scenario.initial)?;
    simulator.set_peak_queue_depth(queue.len())?;
    let mut trace = Vec::with_capacity(queue.len());

    while let Some(queued) = queue.pop() {
        simulator.advance_to(queued.applied_at_ms)?;
        let outcome = simulator.apply(&queued.event, queued.generation_id, queued.observed_at_ms);
        let snapshot = simulator.snapshot();
        trace.push(TraceRecord {
            sequence: queued.sequence,
            observed_at_ms: queued.observed_at_ms,
            applied_at_ms: queued.applied_at_ms,
            event_generation: queued.generation_id,
            event_kind: queued.event.kind().to_owned(),
            outcome,
            resulting_generation: snapshot.receiver_generation,
            snapshot_sha256: sha256(snapshot)?,
        });
    }

    let final_snapshot = simulator.snapshot().clone();
    let metrics = simulator.metrics();
    let content_sha256 = sha256(&(
        RESULT_SCHEMA,
        &scenario.scenario_id,
        scenario.provenance.source,
        &final_snapshot,
        &trace,
        metrics,
    ))?;
    Ok(ReplayResult {
        schema: RESULT_SCHEMA.to_owned(),
        scenario_id: scenario.scenario_id.clone(),
        source: scenario.provenance.source,
        test_fixture: true,
        hardware_claim_authority: false,
        final_snapshot,
        trace,
        metrics,
        content_sha256,
    })
}

pub(crate) struct QueuedEvent {
    pub(crate) sequence: u64,
    pub(crate) observed_at_ms: u64,
    pub(crate) applied_at_ms: u64,
    pub(crate) generation_id: hfx_domain::GenerationId,
    pub(crate) event: SimulatorEvent,
}

pub(crate) struct EventQueue {
    entries: BTreeMap<(u64, u64), QueuedEvent>,
}

impl EventQueue {
    pub(crate) fn new(events: &[ScheduledEvent]) -> Result<Self, SimulatorError> {
        let mut entries = BTreeMap::new();
        for (index, scheduled) in events.iter().enumerate() {
            let sequence = u64::try_from(index).map_err(|_| {
                SimulatorError::InvalidScenario("event sequence exceeds u64".to_owned())
            })?;
            let applied_at_ms = scheduled
                .observed_at_ms
                .checked_add(scheduled.delay_ms)
                .ok_or_else(|| {
                    SimulatorError::InvalidScenario("event time overflowed".to_owned())
                })?;
            entries.insert(
                (applied_at_ms, sequence),
                QueuedEvent {
                    sequence,
                    observed_at_ms: scheduled.observed_at_ms,
                    applied_at_ms,
                    generation_id: scheduled.generation_id,
                    event: scheduled.event.clone(),
                },
            );
        }
        Ok(Self { entries })
    }

    pub(crate) fn len(&self) -> usize {
        self.entries.len()
    }

    pub(crate) fn pop(&mut self) -> Option<QueuedEvent> {
        self.entries.pop_first().map(|(_, event)| event)
    }
}

pub(crate) fn validate_scenario(scenario: &Scenario) -> Result<(), SimulatorError> {
    if scenario.schema != SCENARIO_SCHEMA {
        return Err(SimulatorError::InvalidScenario(
            "unsupported simulator scenario schema".to_owned(),
        ));
    }
    if !scenario.provenance.test_fixture
        || scenario.provenance.hardware_claim_authority
        || scenario.provenance.private_identifiers_exported
        || scenario.provenance.sanitization != SANITIZATION_POLICY
    {
        return Err(SimulatorError::InvalidScenario(
            "fixture provenance violates the non-production privacy boundary".to_owned(),
        ));
    }
    if scenario.initial.children.len() > 32 {
        return Err(SimulatorError::InvalidScenario(
            "initial topology exceeds 32 children".to_owned(),
        ));
    }
    let child_ids = scenario
        .initial
        .children
        .iter()
        .map(|child| child.logical_device_id.clone())
        .collect::<BTreeSet<_>>();
    if child_ids.len() != scenario.initial.children.len() {
        return Err(SimulatorError::InvalidScenario(
            "initial topology repeats a logical device id".to_owned(),
        ));
    }
    if scenario.events.len() > MAX_EVENTS {
        return Err(SimulatorError::InvalidScenario(format!(
            "scenario exceeds {MAX_EVENTS} events"
        )));
    }
    for event in &scenario.events {
        validate_event(event)?;
    }
    Ok(())
}

fn validate_event(scheduled: &ScheduledEvent) -> Result<(), SimulatorError> {
    let applied_at_ms = scheduled
        .observed_at_ms
        .checked_add(scheduled.delay_ms)
        .ok_or_else(|| SimulatorError::InvalidScenario("event time overflowed".to_owned()))?;
    if scheduled.observed_at_ms > MAX_SCENARIO_TIME_MS
        || scheduled.delay_ms > MAX_SCENARIO_TIME_MS
        || applied_at_ms > MAX_SCENARIO_TIME_MS
    {
        return Err(SimulatorError::InvalidScenario(
            "event time exceeds the bounded scenario window".to_owned(),
        ));
    }
    match &scheduled.event {
        SimulatorEvent::ReceiverLifecycle { state }
            if *state == ReceiverLifecycleState::Unknown =>
        {
            invalid_unknown("receiver lifecycle")?;
        }
        SimulatorEvent::DevicePairing { state, .. } if *state == PairingState::Unknown => {
            invalid_unknown("pairing")?;
        }
        SimulatorEvent::RouteObserved { state, .. } if *state == RouteState::Unknown => {
            invalid_unknown("route")?;
        }
        SimulatorEvent::PowerObserved { state, .. } if *state == PowerState::Unknown => {
            invalid_unknown("power")?;
        }
        SimulatorEvent::SleepObserved { state, .. } if *state == SleepState::Unknown => {
            invalid_unknown("sleep")?;
        }
        SimulatorEvent::ContactObserved {
            state: ContactState::Unknown | ContactState::NotApplicable,
            ..
        } => {
            invalid_unknown("contact")?;
        }
        SimulatorEvent::ActivityObserved { state, .. } if *state == ActivityState::Unknown => {
            invalid_unknown("activity")?;
        }
        SimulatorEvent::FreshnessObserved { state, .. } if *state == FreshnessState::Unknown => {
            invalid_unknown("freshness")?;
        }
        SimulatorEvent::LightingFrame { targets, .. }
        | SimulatorEvent::RestoreStarted { targets, .. } => validate_targets(targets)?,
        _ => {}
    }
    Ok(())
}

fn invalid_unknown(label: &str) -> Result<(), SimulatorError> {
    Err(SimulatorError::InvalidScenario(format!(
        "{label} event requires a known state"
    )))
}

fn validate_targets(targets: &[hfx_domain::LogicalDeviceId]) -> Result<(), SimulatorError> {
    if targets.is_empty() || targets.len() > 32 {
        return Err(SimulatorError::InvalidScenario(
            "target list must contain 1..=32 logical devices".to_owned(),
        ));
    }
    if targets.iter().collect::<BTreeSet<_>>().len() != targets.len() {
        return Err(SimulatorError::InvalidScenario(
            "target list repeats a logical device".to_owned(),
        ));
    }
    Ok(())
}
