// SPDX-License-Identifier: GPL-2.0-only

use crate::Simulator;
use crate::event::{SANITIZATION_POLICY, Scenario, SimulatorEvent};
use crate::replay::{EventQueue, MAX_REPLAY_BYTES, run_replay, validate_scenario};
use crate::state::SimulatorError;
use hfx_domain::{ApplyOutcome, CapabilityId, FixtureSource, PresenceState, ProfileId};
use hfx_profiles::{ProfileCatalogError, RuntimeProfileCatalog};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fmt::Write as _;

pub const SHADOW_FIXTURE_SCHEMA: &str = "hyperflux-shadow-comparison-fixture-v1";
pub const SHADOW_RESULT_SCHEMA: &str = "hyperflux-shadow-comparison-result-v1";
const MAX_CHECKPOINTS: usize = 256;
const MAX_SOURCE_RECORDS: usize = 32;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowFixture {
    #[serde(rename = "$schema", default, skip_serializing_if = "Option::is_none")]
    pub schema_uri: Option<String>,
    pub schema: String,
    pub comparison_id: String,
    pub provenance: ShadowProvenance,
    pub scenario: Scenario,
    pub legacy_decisions: Vec<LegacyDecision>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowProvenance {
    pub source_id: String,
    pub source_commit: String,
    pub source_records: Vec<LegacySourceRecord>,
    pub comparison_mode: String,
    pub boundary: ShadowExecutionBoundary,
    pub authority: ShadowAuthority,
    pub side_effects: ShadowSideEffects,
    pub sanitization: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowExecutionBoundary {
    pub test_fixture: bool,
    pub read_only: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowAuthority {
    pub hardware_claim_authority: bool,
    pub publication_authorized: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSideEffects {
    pub private_identifiers_exported: bool,
    pub hardware_queried: bool,
    pub hardware_writes_executed: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LegacySourceRecord {
    pub path: String,
    pub object: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyDecision {
    pub sequence: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selected_profiles: Option<BTreeMap<String, Option<ProfileId>>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub presence_states: Option<BTreeMap<String, PresenceState>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capabilities: Option<BTreeMap<String, Vec<CapabilityId>>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transaction_validation: Option<ApplyOutcome>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diagnostic_findings: Option<Vec<String>>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ShadowDomain {
    ProfileSelection,
    PresenceState,
    Capabilities,
    TransactionValidation,
    DiagnosticFindings,
}

impl ShadowDomain {
    const ALL: [Self; 5] = [
        Self::ProfileSelection,
        Self::PresenceState,
        Self::Capabilities,
        Self::TransactionValidation,
        Self::DiagnosticFindings,
    ];

    const fn label(self) -> &'static str {
        match self {
            Self::ProfileSelection => "profile-selection",
            Self::PresenceState => "presence-state",
            Self::Capabilities => "capabilities",
            Self::TransactionValidation => "transaction-validation",
            Self::DiagnosticFindings => "diagnostic-findings",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ShadowValueComparison<T> {
    pub matched: bool,
    pub legacy: T,
    #[serde(rename = "next")]
    pub next_value: T,
}

impl<T: Eq> ShadowValueComparison<T> {
    fn new(legacy: T, next_value: T) -> Self {
        Self {
            matched: legacy == next_value,
            legacy,
            next_value,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ShadowCheckpointComparison {
    pub sequence: u64,
    pub event_kind: String,
    pub matched: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_profiles: Option<ShadowValueComparison<BTreeMap<String, Option<ProfileId>>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub presence_states: Option<ShadowValueComparison<BTreeMap<String, PresenceState>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub capabilities: Option<ShadowValueComparison<BTreeMap<String, Vec<CapabilityId>>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transaction_validation: Option<ShadowValueComparison<ApplyOutcome>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostic_findings: Option<ShadowValueComparison<Vec<String>>>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ShadowDomainSummary {
    pub domain: ShadowDomain,
    pub compared_checkpoints: u64,
    pub mismatches: u64,
    pub matched: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ShadowDifference {
    pub sequence: u64,
    pub domain: ShadowDomain,
    pub description: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ShadowComparisonResult {
    pub schema: String,
    pub comparison_id: String,
    pub scenario_id: String,
    pub status: String,
    pub boundary: ShadowExecutionBoundary,
    pub authority: ShadowAuthority,
    pub side_effects: ShadowSideEffects,
    pub legacy_source: ShadowProvenance,
    pub simulator_content_sha256: String,
    pub domains: Vec<ShadowDomainSummary>,
    pub checkpoints: Vec<ShadowCheckpointComparison>,
    pub differences: Vec<ShadowDifference>,
    pub content_sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ShadowError {
    InvalidFixture(String),
    Simulation(SimulatorError),
    ProfileCatalog(ProfileCatalogError),
    Serialization(String),
}

impl fmt::Display for ShadowError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidFixture(message) => write!(formatter, "invalid shadow fixture: {message}"),
            Self::Simulation(error) => error.fmt(formatter),
            Self::ProfileCatalog(error) => error.fmt(formatter),
            Self::Serialization(message) => {
                write!(formatter, "shadow serialization failed: {message}")
            }
        }
    }
}

impl std::error::Error for ShadowError {}

impl From<SimulatorError> for ShadowError {
    fn from(value: SimulatorError) -> Self {
        Self::Simulation(value)
    }
}

impl From<ProfileCatalogError> for ShadowError {
    fn from(value: ProfileCatalogError) -> Self {
        Self::ProfileCatalog(value)
    }
}

/// Parses and validates a bounded, sanitized, read-only legacy decision fixture.
///
/// # Errors
///
/// Returns an error for malformed JSON, unsafe provenance, an invalid nested
/// simulator scenario, incomplete domain coverage, or noncanonical checkpoints.
pub fn parse_shadow_fixture(bytes: &[u8]) -> Result<ShadowFixture, ShadowError> {
    if bytes.is_empty() || bytes.len() > MAX_REPLAY_BYTES {
        return Err(ShadowError::InvalidFixture(format!(
            "serialized fixture size must be 1..={MAX_REPLAY_BYTES} bytes"
        )));
    }
    let fixture: ShadowFixture = serde_json::from_slice(bytes)
        .map_err(|error| ShadowError::InvalidFixture(error.to_string()))?;
    validate_shadow_fixture(&fixture)?;
    Ok(fixture)
}

/// Replays the new implementation beside a frozen legacy decision oracle.
///
/// This function has no hardware or wall-clock access. A divergence is returned
/// as typed data rather than an execution error.
///
/// # Errors
///
/// Returns an error only when the fixture, generated profile catalog, simulator,
/// or deterministic serialization contract is invalid.
pub fn run_shadow_comparison(
    fixture: &ShadowFixture,
) -> Result<ShadowComparisonResult, ShadowError> {
    validate_shadow_fixture(fixture)?;
    let catalog = RuntimeProfileCatalog::load()?;
    let replay_result = run_replay(&fixture.scenario)?;
    let decisions = fixture
        .legacy_decisions
        .iter()
        .map(|decision| (decision.sequence, decision))
        .collect::<BTreeMap<_, _>>();
    let mut queue = EventQueue::new(&fixture.scenario.events)?;
    let mut simulator = Simulator::new(
        fixture.scenario.provenance.source,
        &fixture.scenario.initial,
    )?;
    simulator.set_peak_queue_depth(queue.len())?;
    let mut checkpoints = Vec::with_capacity(decisions.len());
    let mut differences = Vec::new();

    while let Some(queued) = queue.pop() {
        simulator.advance_to(queued.applied_at_ms)?;
        let outcome = simulator.apply(&queued.event, queued.generation_id, queued.observed_at_ms);
        let Some(legacy) = decisions.get(&queued.sequence) else {
            continue;
        };
        let checkpoint = compare_checkpoint(
            queued.sequence,
            &queued.event,
            outcome,
            simulator.snapshot(),
            &catalog,
            legacy,
        )?;
        record_differences(&checkpoint, &mut differences);
        checkpoints.push(checkpoint);
    }

    let domains = summarize_domains(&checkpoints);
    let matched = differences.is_empty() && domains.iter().all(|domain| domain.matched);
    let mut result = ShadowComparisonResult {
        schema: SHADOW_RESULT_SCHEMA.to_owned(),
        comparison_id: fixture.comparison_id.clone(),
        scenario_id: fixture.scenario.scenario_id.to_string(),
        status: if matched { "matched" } else { "diverged" }.to_owned(),
        boundary: fixture.provenance.boundary.clone(),
        authority: fixture.provenance.authority.clone(),
        side_effects: fixture.provenance.side_effects.clone(),
        legacy_source: fixture.provenance.clone(),
        simulator_content_sha256: replay_result.content_sha256,
        domains,
        checkpoints,
        differences,
        content_sha256: String::new(),
    };
    result.content_sha256 = digest(&result)?;
    Ok(result)
}

fn validate_shadow_fixture(fixture: &ShadowFixture) -> Result<(), ShadowError> {
    if fixture.schema != SHADOW_FIXTURE_SCHEMA {
        return invalid("unsupported shadow fixture schema");
    }
    if fixture.comparison_id.is_empty()
        || fixture.comparison_id.len() > 128
        || !fixture.comparison_id.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
    {
        return invalid("comparison id is not a bounded lowercase identifier");
    }
    validate_shadow_provenance(&fixture.provenance)?;
    validate_scenario(&fixture.scenario)?;
    if fixture.scenario.provenance.source != FixtureSource::SanitizedReplay
        || fixture.scenario.provenance.hardware_claim_authority
        || fixture.scenario.provenance.private_identifiers_exported
    {
        return invalid("shadow scenario must be sanitized replay without hardware authority");
    }
    validate_legacy_decisions(fixture)
}

fn validate_shadow_provenance(provenance: &ShadowProvenance) -> Result<(), ShadowError> {
    if provenance.comparison_mode != "recorded-decisions-only"
        || !provenance.boundary.test_fixture
        || !provenance.boundary.read_only
        || provenance.authority.hardware_claim_authority
        || provenance.authority.publication_authorized
        || provenance.side_effects.private_identifiers_exported
        || provenance.side_effects.hardware_queried
        || provenance.side_effects.hardware_writes_executed
        || provenance.sanitization != SANITIZATION_POLICY
    {
        return invalid("provenance violates the read-only sanitized comparison boundary");
    }
    if provenance.source_id.is_empty() || !is_hex(&provenance.source_commit, 40) {
        return invalid("legacy source identity is malformed");
    }
    if provenance.source_records.is_empty() || provenance.source_records.len() > MAX_SOURCE_RECORDS
    {
        return invalid("legacy source records must contain 1..=32 entries");
    }
    let mut previous_path = None;
    for record in &provenance.source_records {
        if record.path.is_empty()
            || record.path.starts_with('/')
            || record
                .path
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || !record.path.is_ascii()
            || !is_hex(&record.object, 40)
        {
            return invalid("legacy source record is not a canonical Git tree entry");
        }
        if previous_path.is_some_and(|previous| previous >= record.path.as_str()) {
            return invalid("legacy source records must be unique and path-sorted");
        }
        previous_path = Some(record.path.as_str());
    }
    Ok(())
}

fn validate_legacy_decisions(fixture: &ShadowFixture) -> Result<(), ShadowError> {
    if fixture.legacy_decisions.is_empty() || fixture.legacy_decisions.len() > MAX_CHECKPOINTS {
        return invalid("legacy decisions must contain 1..=256 checkpoints");
    }
    let entity_ids = entity_ids(&fixture.scenario)?;
    let device_ids = fixture
        .scenario
        .initial
        .children
        .iter()
        .map(|child| child.logical_device_id.as_str().to_owned())
        .collect::<BTreeSet<_>>();
    let mut sequences = BTreeSet::new();
    let mut covered_domains = BTreeSet::new();
    for decision in &fixture.legacy_decisions {
        let Ok(sequence) = usize::try_from(decision.sequence) else {
            return invalid("legacy checkpoint sequence exceeds this platform");
        };
        if sequence >= fixture.scenario.events.len() || !sequences.insert(decision.sequence) {
            return invalid("legacy checkpoint sequence is duplicated or outside the scenario");
        }
        if decision.selected_profiles.is_none()
            && decision.presence_states.is_none()
            && decision.capabilities.is_none()
            && decision.transaction_validation.is_none()
            && decision.diagnostic_findings.is_none()
        {
            return invalid("legacy checkpoint compares no semantic domain");
        }
        if let Some(profiles) = &decision.selected_profiles {
            require_exact_keys(profiles.keys().map(String::as_str), &entity_ids, "profile")?;
            covered_domains.insert(ShadowDomain::ProfileSelection);
        }
        if let Some(presence) = &decision.presence_states {
            require_exact_keys(presence.keys().map(String::as_str), &device_ids, "presence")?;
            covered_domains.insert(ShadowDomain::PresenceState);
        }
        if let Some(capabilities) = &decision.capabilities {
            require_exact_keys(
                capabilities.keys().map(String::as_str),
                &entity_ids,
                "capability",
            )?;
            for values in capabilities.values() {
                if values.windows(2).any(|pair| pair[0] >= pair[1]) {
                    return invalid("capability expectations must be unique and sorted");
                }
            }
            covered_domains.insert(ShadowDomain::Capabilities);
        }
        if decision.transaction_validation.is_some() {
            if !matches!(
                fixture.scenario.events[sequence].event,
                SimulatorEvent::LightingFrame { .. }
                    | SimulatorEvent::RestoreStarted { .. }
                    | SimulatorEvent::RestoreTarget { .. }
            ) {
                return invalid("transaction validation checkpoint does not name a write event");
            }
            covered_domains.insert(ShadowDomain::TransactionValidation);
        }
        if let Some(findings) = &decision.diagnostic_findings {
            if findings.iter().any(|finding| {
                finding.is_empty()
                    || finding.len() > 128
                    || !finding.bytes().all(|byte| {
                        byte.is_ascii_lowercase()
                            || byte.is_ascii_digit()
                            || matches!(byte, b'.' | b'-')
                    })
            }) || findings.windows(2).any(|pair| pair[0] >= pair[1])
            {
                return invalid("diagnostic expectations must be bounded, unique, and sorted");
            }
            covered_domains.insert(ShadowDomain::DiagnosticFindings);
        }
    }
    if ShadowDomain::ALL
        .iter()
        .any(|domain| !covered_domains.contains(domain))
    {
        return invalid("legacy decisions do not cover all five shadow comparison domains");
    }
    Ok(())
}

fn entity_ids(scenario: &Scenario) -> Result<BTreeSet<String>, ShadowError> {
    let mut ids = BTreeSet::from(["receiver".to_owned()]);
    if scenario.initial.surface_profile_id.is_some() {
        ids.insert("surface".to_owned());
    }
    for child in &scenario.initial.children {
        let value = child.logical_device_id.as_str().to_owned();
        if !ids.insert(value) {
            return invalid("logical device id collides with a shadow entity id");
        }
    }
    Ok(ids)
}

fn require_exact_keys<'a>(
    keys: impl Iterator<Item = &'a str>,
    expected: &BTreeSet<String>,
    label: &str,
) -> Result<(), ShadowError> {
    let actual = keys.map(str::to_owned).collect::<BTreeSet<_>>();
    if actual != *expected {
        return invalid(&format!(
            "{label} expectations do not cover the exact topology"
        ));
    }
    Ok(())
}

fn compare_checkpoint(
    sequence: u64,
    event: &SimulatorEvent,
    outcome: ApplyOutcome,
    snapshot: &crate::StateSnapshot,
    catalog: &RuntimeProfileCatalog,
    legacy: &LegacyDecision,
) -> Result<ShadowCheckpointComparison, ShadowError> {
    let selected_profiles = legacy
        .selected_profiles
        .clone()
        .map(|expected| ShadowValueComparison::new(expected, project_profiles(snapshot)));
    let presence_states = legacy
        .presence_states
        .clone()
        .map(|expected| ShadowValueComparison::new(expected, project_presence(snapshot)));
    let capabilities = legacy
        .capabilities
        .clone()
        .map(|expected| {
            project_capabilities(snapshot, catalog)
                .map(|actual| ShadowValueComparison::new(expected, actual))
        })
        .transpose()?;
    let transaction_validation = legacy
        .transaction_validation
        .map(|expected| ShadowValueComparison::new(expected, outcome));
    let diagnostic_findings = legacy
        .diagnostic_findings
        .clone()
        .map(|expected| ShadowValueComparison::new(expected, diagnostics_for(outcome)));
    let matched = selected_profiles.as_ref().is_none_or(|value| value.matched)
        && presence_states.as_ref().is_none_or(|value| value.matched)
        && capabilities.as_ref().is_none_or(|value| value.matched)
        && transaction_validation
            .as_ref()
            .is_none_or(|value| value.matched)
        && diagnostic_findings
            .as_ref()
            .is_none_or(|value| value.matched);
    Ok(ShadowCheckpointComparison {
        sequence,
        event_kind: event.kind().to_owned(),
        matched,
        selected_profiles,
        presence_states,
        capabilities,
        transaction_validation,
        diagnostic_findings,
    })
}

fn project_profiles(snapshot: &crate::StateSnapshot) -> BTreeMap<String, Option<ProfileId>> {
    let mut profiles = BTreeMap::from([(
        "receiver".to_owned(),
        Some(snapshot.receiver_profile_id.clone()),
    )]);
    if let Some(surface) = &snapshot.surface_profile_id {
        profiles.insert("surface".to_owned(), Some(surface.clone()));
    }
    profiles.extend(
        snapshot
            .devices
            .iter()
            .map(|(device_id, device)| (device_id.as_str().to_owned(), device.profile_id.clone())),
    );
    profiles
}

fn project_presence(snapshot: &crate::StateSnapshot) -> BTreeMap<String, PresenceState> {
    snapshot
        .devices
        .iter()
        .map(|(device_id, device)| (device_id.as_str().to_owned(), device.presence))
        .collect()
}

fn project_capabilities(
    snapshot: &crate::StateSnapshot,
    catalog: &RuntimeProfileCatalog,
) -> Result<BTreeMap<String, Vec<CapabilityId>>, ShadowError> {
    let mut profiles = project_profiles(snapshot);
    let mut result = BTreeMap::new();
    for (entity, profile_id) in &mut profiles {
        let capabilities = if let Some(profile_id) = profile_id {
            catalog
                .profile(profile_id)
                .ok_or_else(|| {
                    ShadowError::InvalidFixture(format!(
                        "snapshot profile is absent from the runtime catalog: {profile_id}"
                    ))
                })?
                .capabilities
                .iter()
                .map(|capability| capability.id.clone())
                .collect()
        } else {
            Vec::new()
        };
        result.insert(entity.clone(), capabilities);
    }
    Ok(result)
}

fn diagnostics_for(outcome: ApplyOutcome) -> Vec<String> {
    let finding = match outcome {
        ApplyOutcome::Applied => return Vec::new(),
        ApplyOutcome::IgnoredOlderObservation => "shadow.observation.older-ignored",
        ApplyOutcome::RejectedStaleGeneration => "shadow.generation.stale",
        ApplyOutcome::RejectedReceiverAbsent => "shadow.receiver.absent",
        ApplyOutcome::RejectedUnknownDevice => "shadow.device.unknown",
        ApplyOutcome::RejectedUnavailableRoute => "shadow.route.unavailable",
        ApplyOutcome::RejectedUnqualifiedWrite => "shadow.profile.unqualified",
        ApplyOutcome::RejectedInvalidTransition => "shadow.transition.invalid",
        ApplyOutcome::RejectedTransportFailure => "shadow.transport.failed",
        ApplyOutcome::RecordedMalformedObservation => "shadow.observation.malformed-recorded",
    };
    vec![finding.to_owned()]
}

fn record_differences(
    checkpoint: &ShadowCheckpointComparison,
    differences: &mut Vec<ShadowDifference>,
) {
    let values = [
        (
            ShadowDomain::ProfileSelection,
            checkpoint
                .selected_profiles
                .as_ref()
                .map(|comparison| comparison.matched),
        ),
        (
            ShadowDomain::PresenceState,
            checkpoint
                .presence_states
                .as_ref()
                .map(|comparison| comparison.matched),
        ),
        (
            ShadowDomain::Capabilities,
            checkpoint
                .capabilities
                .as_ref()
                .map(|comparison| comparison.matched),
        ),
        (
            ShadowDomain::TransactionValidation,
            checkpoint
                .transaction_validation
                .as_ref()
                .map(|comparison| comparison.matched),
        ),
        (
            ShadowDomain::DiagnosticFindings,
            checkpoint
                .diagnostic_findings
                .as_ref()
                .map(|comparison| comparison.matched),
        ),
    ];
    for (domain, matched) in values {
        if matched == Some(false) {
            differences.push(ShadowDifference {
                sequence: checkpoint.sequence,
                domain,
                description: format!(
                    "legacy and next {} decisions differ at event {}",
                    domain.label(),
                    checkpoint.sequence
                ),
            });
        }
    }
}

fn summarize_domains(checkpoints: &[ShadowCheckpointComparison]) -> Vec<ShadowDomainSummary> {
    ShadowDomain::ALL
        .into_iter()
        .map(|domain| {
            let states = checkpoints.iter().filter_map(|checkpoint| match domain {
                ShadowDomain::ProfileSelection => checkpoint
                    .selected_profiles
                    .as_ref()
                    .map(|comparison| comparison.matched),
                ShadowDomain::PresenceState => checkpoint
                    .presence_states
                    .as_ref()
                    .map(|comparison| comparison.matched),
                ShadowDomain::Capabilities => checkpoint
                    .capabilities
                    .as_ref()
                    .map(|comparison| comparison.matched),
                ShadowDomain::TransactionValidation => checkpoint
                    .transaction_validation
                    .as_ref()
                    .map(|comparison| comparison.matched),
                ShadowDomain::DiagnosticFindings => checkpoint
                    .diagnostic_findings
                    .as_ref()
                    .map(|comparison| comparison.matched),
            });
            let values = states.collect::<Vec<_>>();
            let mismatches = values.iter().filter(|matched| !**matched).count() as u64;
            ShadowDomainSummary {
                domain,
                compared_checkpoints: values.len() as u64,
                mismatches,
                matched: mismatches == 0 && !values.is_empty(),
            }
        })
        .collect()
}

fn is_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn digest<T: Serialize>(value: &T) -> Result<String, ShadowError> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| ShadowError::Serialization(error.to_string()))?;
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut encoded, "{byte:02x}")
            .map_err(|error| ShadowError::Serialization(error.to_string()))?;
    }
    Ok(encoded)
}

fn invalid<T>(message: &str) -> Result<T, ShadowError> {
    Err(ShadowError::InvalidFixture(message.to_owned()))
}
