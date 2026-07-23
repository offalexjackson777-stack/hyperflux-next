// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::{SYSTEM_RESUME_RETRY_EVIDENCE, StageCheckpoint, StageProgress};
use crate::qualification_event_evidence::{
    cursor_evidence, device_return_restore, renewed_generation_restores,
    resumed_generation_restores,
};
use crate::qualification_runner::{ObservationVerdict, RunnerTransition};
use crate::{
    BatteryAvailability, BridgeIntegration, CompanionState, PresenceView, ProfileBindingView,
    QualificationPlan, QualificationStage, QualificationView, ReceiverLifecycleView, ReceiverView,
    RiskLevel, StageResult, StageStatus, qualification_generated_at,
};
use hfx_domain::{ControllerAvailability, InventoryAvailability, ReceiverLifecycleState};
use hfx_protocol::EventBatch;
use rustix::time::{ClockId, clock_gettime};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fmt;
use std::fs;

const MAX_GENERATION_HISTORY: usize = 16;
const MAX_RUN_PLANS: usize = 16;
const MAX_RUN_DEVICES: usize = 32;
const MAX_EVIDENCE_REFS: usize = 64;
const MINIMUM_SUSPEND_GAP_MS: u64 = 1_000;
const BOOT_ID_PATH: &str = "/proc/sys/kernel/random/boot_id";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum LifecyclePhase {
    DeviceDeparture,
    DeviceReturn,
    ReceiverDeparture,
    ReceiverReturn,
    SuspendAuthorization,
    SystemResumePending,
    SystemResumeObservation,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SuspendStamp {
    boot_id_sha256: String,
    suspend_gap_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LifecycleStageState {
    pub stage_id: String,
    pub device_id: String,
    pub device_kind: String,
    pub phase: LifecyclePhase,
    pub baseline_generation_id: u64,
    pub evidence_refs: Vec<String>,
    pub suspend_stamp: Option<SuspendStamp>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LifecycleRunState {
    pub receiver: ReceiverView,
    pub plans: Vec<QualificationPlan>,
    pub generation_history: Vec<u64>,
    pub active: Option<LifecycleStageState>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ObservedDevice {
    device_id: String,
    profile: Option<ProfileBindingView>,
    availability: InventoryAvailability,
    controller_ready: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LifecycleObservation {
    receiver_id: String,
    receiver_present: bool,
    generation_id: Option<u64>,
    receiver_profile: Option<ProfileBindingView>,
    receiver_lifecycle: Option<ReceiverLifecycleState>,
    stable_restore_enabled: bool,
    selected: Option<ObservedDevice>,
    siblings: Vec<ObservedDevice>,
    cursor_ref: Option<String>,
    events: Option<EventBatch>,
}

pub(crate) enum PreparedLifecycleAction {
    Record {
        run: LifecycleRunState,
        transition: RunnerTransition,
    },
    Suspend {
        run: LifecycleRunState,
        transition: RunnerTransition,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LifecycleRunnerError {
    message: String,
}

impl LifecycleRunnerError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for LifecycleRunnerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for LifecycleRunnerError {}

impl LifecycleRunState {
    pub(crate) fn validate(&self) -> Result<(), LifecycleRunnerError> {
        if self.plans.is_empty()
            || self.plans.len() > MAX_RUN_PLANS
            || self.receiver.devices.len() > MAX_RUN_DEVICES
            || self.generation_history.is_empty()
            || self.generation_history.len() > MAX_GENERATION_HISTORY
            || self
                .generation_history
                .windows(2)
                .any(|window| window[0] >= window[1])
            || self.generation_history.last().copied() != Some(self.receiver.generation_id)
        {
            return Err(LifecycleRunnerError::new(
                "The saved lifecycle run is outside its bounded generation contract.",
            ));
        }
        let mut devices = BTreeSet::new();
        for plan in &self.plans {
            if plan.receiver_id != self.receiver.receiver_id
                || plan.generation_id != self.receiver.generation_id
                || !devices.insert(plan.device_id.as_str())
            {
                return Err(LifecycleRunnerError::new(
                    "The saved lifecycle plans do not share one exact receiver generation.",
                ));
            }
        }
        if let Some(active) = &self.active {
            let stage_plan = self.plans.iter().find(|plan| {
                plan.device_id == active.device_id
                    && plan
                        .groups
                        .iter()
                        .flat_map(|group| &group.stages)
                        .any(|stage| stage.stage_id == active.stage_id)
            });
            let expected_kind = self
                .receiver
                .devices
                .iter()
                .find(|device| device.device_id == active.device_id)
                .map(|device| device.kind.as_str());
            let suspend_phase = matches!(
                active.phase,
                LifecyclePhase::SystemResumePending | LifecyclePhase::SystemResumeObservation
            );
            let stage_exists_anywhere = self.plans.iter().any(|plan| {
                plan.groups
                    .iter()
                    .flat_map(|group| &group.stages)
                    .any(|stage| stage.stage_id == active.stage_id)
            });
            if !stage_exists_anywhere
                || stage_plan.is_none()
                || !matches!(active.device_kind.as_str(), "mouse" | "keyboard")
                || expected_kind != Some(active.device_kind.as_str())
                || active.evidence_refs.len() > MAX_EVIDENCE_REFS
                || !self
                    .generation_history
                    .contains(&active.baseline_generation_id)
                || suspend_phase != active.suspend_stamp.is_some()
            {
                return Err(LifecycleRunnerError::new(
                    "The saved lifecycle checkpoint is not bound to this run.",
                ));
            }
            if let Some(stamp) = &active.suspend_stamp
                && !valid_digest(&stamp.boot_id_sha256)
            {
                return Err(LifecycleRunnerError::new(
                    "The saved suspend checkpoint is invalid.",
                ));
            }
        }
        Ok(())
    }
}

pub(crate) fn capture_lifecycle_run(
    view: &QualificationView,
    stage: &QualificationStage,
) -> Result<LifecycleRunState, LifecycleRunnerError> {
    let plan = plan_for_stage(view, stage)?;
    let receiver = view
        .receivers
        .iter()
        .find(|receiver| {
            receiver.receiver_id == plan.receiver_id && receiver.generation_id == plan.generation_id
        })
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The selected lifecycle stage has no current receiver generation.",
            )
        })?;
    let plans = view
        .plans
        .iter()
        .filter(|candidate| {
            candidate.receiver_id == plan.receiver_id
                && candidate.generation_id == plan.generation_id
        })
        .cloned()
        .collect::<Vec<_>>();
    if plans.iter().any(|candidate| {
        candidate
            .groups
            .iter()
            .filter(|group| group.group_id != "lifecycle")
            .flat_map(|group| &group.stages)
            .any(|candidate_stage| candidate_stage.status != StageStatus::Passed)
    }) {
        return Err(LifecycleRunnerError::new(
            "Complete every identity and lighting check on this receiver before lifecycle testing.",
        ));
    }
    let run = LifecycleRunState {
        receiver: receiver.clone(),
        plans,
        generation_history: vec![receiver.generation_id],
        active: None,
    };
    run.validate()?;
    Ok(run)
}

pub(crate) fn observe_lifecycle(
    integration: Option<&BridgeIntegration>,
    run: &LifecycleRunState,
    selected_device_id: &str,
) -> LifecycleObservation {
    let expected_siblings = run
        .plans
        .iter()
        .map(|plan| plan.device_id.as_str())
        .filter(|device_id| *device_id != selected_device_id)
        .collect::<BTreeSet<_>>();
    let receiver = integration.and_then(|value| {
        value
            .view
            .receivers
            .iter()
            .find(|receiver| receiver.receiver_id.as_str() == run.receiver.receiver_id)
    });
    let selected = receiver.and_then(|receiver| {
        observed_device(receiver, selected_device_id).or_else(|| {
            expected_device(run, selected_device_id)
                .ok()
                .map(|expected| ObservedDevice {
                    device_id: expected.device_id.clone(),
                    profile: expected.profile.clone(),
                    availability: InventoryAvailability::Unavailable,
                    controller_ready: false,
                })
        })
    });
    let siblings = receiver.map_or_else(Vec::new, |receiver| {
        expected_siblings
            .iter()
            .filter_map(|device_id| observed_device(receiver, device_id))
            .collect()
    });
    LifecycleObservation {
        receiver_id: run.receiver.receiver_id.clone(),
        receiver_present: receiver.is_some(),
        generation_id: receiver.map(|receiver| receiver.generation_id.get()),
        receiver_profile: receiver
            .and_then(|receiver| receiver.profile.as_ref())
            .map(profile_binding),
        receiver_lifecycle: receiver.map(|receiver| receiver.lifecycle),
        stable_restore_enabled: receiver.is_some_and(|receiver| receiver.stable_restore_enabled),
        selected,
        siblings,
        cursor_ref: integration.map(|value| cursor_evidence(&value.view.cursor)),
        events: integration.map(|value| value.events.clone()),
    }
}

pub(crate) fn start_lifecycle_stage(
    mut run: LifecycleRunState,
    stage: &QualificationStage,
    observation: &LifecycleObservation,
) -> Result<(LifecycleRunState, RunnerTransition), LifecycleRunnerError> {
    if run.active.is_some() {
        return Err(LifecycleRunnerError::new(
            "Another lifecycle stage is already active in this receiver campaign.",
        ));
    }
    let plan = run
        .plans
        .iter()
        .find(|plan| stage_belongs_to_plan(plan, stage))
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The lifecycle stage does not belong to the retained receiver campaign.",
            )
        })?;
    let device = run
        .receiver
        .devices
        .iter()
        .find(|device| device.device_id == plan.device_id)
        .ok_or_else(|| LifecycleRunnerError::new("The lifecycle target is no longer retained."))?;
    if !matches!(device.kind.as_str(), "mouse" | "keyboard")
        || !matches!(
            stage.risk,
            RiskLevel::DeviceLifecycle | RiskLevel::SystemLifecycle
        )
    {
        return Err(LifecycleRunnerError::new(
            "This stage is not a supported controller lifecycle campaign.",
        ));
    }
    validate_ready_identity(&run, plan.device_id.as_str(), observation)?;
    if !observation.stable_restore_enabled {
        return Err(LifecycleRunnerError::new(
            "Stable restoration is disabled; enable it before a watched lifecycle campaign.",
        ));
    }
    let mut evidence_refs = vec![format!(
        "receiver-generation:{}",
        observation.generation_id.unwrap_or_default()
    )];
    if let Some(cursor) = &observation.cursor_ref {
        evidence_refs.push(cursor.clone());
    }
    run.active = Some(LifecycleStageState {
        stage_id: stage.stage_id.clone(),
        device_id: plan.device_id.clone(),
        device_kind: device.kind.clone(),
        phase: LifecyclePhase::DeviceDeparture,
        baseline_generation_id: observation.generation_id.unwrap_or_default(),
        evidence_refs,
        suspend_stamp: None,
    });
    let transition = awaiting(device_departure_checkpoint(&run));
    run.validate()?;
    Ok((run, transition))
}

pub(crate) fn prepare_lifecycle_checkpoint(
    mut run: LifecycleRunState,
    stage: &QualificationStage,
    current: &StageProgress,
    verdict: ObservationVerdict,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    let checkpoint = current.checkpoint.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new(
            "The lifecycle stage has no resumable checkpoint; no action was repeated.",
        )
    })?;
    let active = run.active.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The retained lifecycle campaign has no active stage.")
    })?;
    if active.stage_id != stage.stage_id {
        return Err(LifecycleRunnerError::new(
            "The watched checkpoint belongs to a different lifecycle stage.",
        ));
    }
    if verdict != ObservationVerdict::Pass {
        let retryable_resume = checkpoint.checkpoint_id == "system-resume-observation";
        let mut evidence = active.evidence_refs.clone();
        if retryable_resume
            && !evidence
                .iter()
                .any(|value| value == SYSTEM_RESUME_RETRY_EVIDENCE)
        {
            evidence.push(SYSTEM_RESUME_RETRY_EVIDENCE.to_owned());
        }
        if retryable_resume {
            let active = run.active.as_mut().expect("active lifecycle stage exists");
            active.phase = LifecyclePhase::SuspendAuthorization;
            active.suspend_stamp = None;
            active.evidence_refs.clone_from(&evidence);
        } else {
            run.active = None;
        }
        let (status, summary) = if verdict == ObservationVerdict::Fail {
            (
                StageStatus::Failed,
                "A watched lifecycle result did not match the required physical state.",
            )
        } else {
            (
                StageStatus::Blocked,
                "The watched lifecycle result was inconclusive; no physical step was repeated.",
            )
        };
        let transition = if retryable_resume {
            retryable_failure(status, summary, evidence)
        } else {
            finished(status, summary, evidence)
        };
        return Ok(PreparedLifecycleAction::Record { run, transition });
    }
    match checkpoint.checkpoint_id.as_str() {
        "device-departure" => record_device_departure(run, observation),
        "device-return" => record_device_return(run, observation),
        "receiver-departure" => record_receiver_departure(run, observation),
        "receiver-return" => record_receiver_return(run, observation),
        "system-suspend-authorization" => prepare_system_suspend(run, observation),
        "system-resume-observation" => record_system_resume(run, observation),
        _ => Err(LifecycleRunnerError::new(
            "The lifecycle checkpoint is not recognized; no physical step was repeated.",
        )),
    }
}

pub(crate) fn recover_retryable_resume_failure(
    run: &mut LifecycleRunState,
    stage_id: &str,
    progress: &mut StageProgress,
) -> Result<bool, LifecycleRunnerError> {
    if run.active.is_some()
        || progress.checkpoint.is_some()
        || !matches!(progress.status, StageStatus::Failed | StageStatus::Blocked)
    {
        return Ok(false);
    }
    let Some(result) = progress.result.as_mut() else {
        return Ok(false);
    };
    let measured_resume = result
        .evidence_refs
        .iter()
        .any(|value| value == "system-suspend:authorized")
        && result
            .evidence_refs
            .iter()
            .any(|value| value.starts_with("system-suspend-gap-ms:"));
    if !measured_resume {
        return Ok(false);
    }
    let plan = run
        .plans
        .iter()
        .find(|plan| {
            plan.groups
                .iter()
                .flat_map(|group| &group.stages)
                .any(|stage| stage.stage_id == stage_id && stage.risk == RiskLevel::SystemLifecycle)
        })
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The failed resume result is not bound to a retained system lifecycle stage.",
            )
        })?;
    let device = run
        .receiver
        .devices
        .iter()
        .find(|device| device.device_id == plan.device_id)
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The failed resume result has no retained controller identity.",
            )
        })?;
    if !result
        .evidence_refs
        .iter()
        .any(|value| value == SYSTEM_RESUME_RETRY_EVIDENCE)
    {
        result
            .evidence_refs
            .push(SYSTEM_RESUME_RETRY_EVIDENCE.to_owned());
    }
    let evidence_refs = result.evidence_refs.clone();
    progress.checkpoint = Some(system_suspend_checkpoint(evidence_refs.clone()));
    run.active = Some(LifecycleStageState {
        stage_id: stage_id.to_owned(),
        device_id: plan.device_id.clone(),
        device_kind: device.kind.clone(),
        phase: LifecyclePhase::SuspendAuthorization,
        baseline_generation_id: run.receiver.generation_id,
        evidence_refs,
        suspend_stamp: None,
    });
    run.validate()?;
    Ok(true)
}

pub(crate) fn reconcile_system_resume(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
    current_stamp: &SuspendStamp,
) -> Result<Option<(LifecycleRunState, RunnerTransition)>, LifecycleRunnerError> {
    let Some(active) = run.active.as_mut() else {
        return Ok(None);
    };
    if active.phase != LifecyclePhase::SystemResumePending {
        return Ok(None);
    }
    let baseline = active.suspend_stamp.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The system-resume checkpoint has no suspend clock anchor.")
    })?;
    if baseline.boot_id_sha256 != current_stamp.boot_id_sha256 {
        let evidence = active.evidence_refs.clone();
        run.active = None;
        return Ok(Some((
            run,
            finished(
                StageStatus::Blocked,
                "The computer rebooted instead of resuming the authorized suspend.",
                evidence,
            ),
        )));
    }
    let suspended_ms = current_stamp
        .suspend_gap_ms
        .saturating_sub(baseline.suspend_gap_ms);
    if suspended_ms < MINIMUM_SUSPEND_GAP_MS
        || validate_restored_identity(&run, observation, false).is_err()
    {
        return Ok(None);
    }
    let device_ids = run
        .plans
        .iter()
        .map(|plan| plan.device_id.clone())
        .collect::<Vec<_>>();
    let generation = observation
        .generation_id
        .ok_or_else(|| LifecycleRunnerError::new("The resumed receiver has no generation."))?;
    let restore_evidence = observation
        .events
        .as_ref()
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The bridge event history is unavailable for this lifecycle checkpoint.",
            )
        })
        .and_then(|events| {
            resumed_generation_restores(
                events,
                &run.active
                    .as_ref()
                    .expect("active lifecycle stage exists")
                    .evidence_refs,
                &observation.receiver_id,
                generation,
                &device_ids,
            )
            .map_err(|error| LifecycleRunnerError::new(error.to_string()))
        });
    let Ok(restore_evidence) = restore_evidence else {
        return Ok(None);
    };
    let active = run.active.as_mut().expect("active lifecycle stage exists");
    active.phase = LifecyclePhase::SystemResumeObservation;
    active
        .evidence_refs
        .push(format!("system-suspend-gap-ms:{suspended_ms}"));
    active.evidence_refs.extend(restore_evidence);
    if let Some(cursor) = &observation.cursor_ref {
        active.evidence_refs.push(cursor.clone());
    }
    accept_generation(&mut run, observation)?;
    let evidence = run
        .active
        .as_ref()
        .expect("active lifecycle stage exists")
        .evidence_refs
        .clone();
    let has_siblings = run.plans.len() > 1;
    Ok(Some((
        run,
        awaiting(system_resume_checkpoint(evidence, has_siblings)),
    )))
}

pub(crate) fn overlay_lifecycle_run(view: &mut QualificationView, run: &LifecycleRunState) {
    let live_receiver = view
        .receivers
        .iter()
        .find(|receiver| receiver.receiver_id == run.receiver.receiver_id)
        .cloned();
    let mut receiver = run.receiver.clone();
    if let Some(live) = &live_receiver {
        receiver.generation_id = live.generation_id;
        receiver.lifecycle = live.lifecycle;
        receiver.profile.clone_from(&live.profile);
        merge_live_devices(&mut receiver, live);
    } else {
        receiver.lifecycle = ReceiverLifecycleView::Unavailable;
        for device in &mut receiver.devices {
            device.presence = PresenceView::Unavailable;
            device.battery.availability = if device.battery.percentage.is_some() {
                BatteryAvailability::Stale
            } else {
                BatteryAvailability::Unavailable
            };
        }
        if view.companion.state == CompanionState::NoReceiver {
            view.companion.state = CompanionState::LifecycleTransition;
        }
    }
    let generation_id = receiver.generation_id;
    let history = run
        .generation_history
        .iter()
        .map(u64::to_string)
        .collect::<Vec<_>>()
        .join(" -> ");
    let mut plans = run.plans.clone();
    for plan in &mut plans {
        plan.generation_id = generation_id;
        plan.summary = format!(
            "Lifecycle campaign generations {history}; evidence remains bound to each recorded checkpoint."
        );
    }
    view.receivers
        .retain(|candidate| candidate.receiver_id != run.receiver.receiver_id);
    view.plans
        .retain(|candidate| candidate.receiver_id != run.receiver.receiver_id);
    view.receivers.push(receiver);
    view.plans.extend(plans);
}

pub(crate) fn identity_transition_matches(
    view: &QualificationView,
    run: &LifecycleRunState,
) -> bool {
    if run.validate().is_err() {
        return false;
    }
    let receiver = view
        .receivers
        .iter()
        .find(|receiver| receiver.receiver_id == run.receiver.receiver_id);
    let Some(active) = &run.active else {
        return receiver.is_some_and(|receiver| {
            receiver.generation_id == run.receiver.generation_id
                && receiver_identity_matches(receiver, run, false)
        });
    };
    let Some(receiver) = receiver else {
        return matches!(
            active.phase,
            LifecyclePhase::ReceiverDeparture
                | LifecyclePhase::ReceiverReturn
                | LifecyclePhase::SystemResumePending
        );
    };
    let generation_matches = match active.phase {
        LifecyclePhase::ReceiverReturn | LifecyclePhase::SystemResumePending => {
            receiver.generation_id >= active.baseline_generation_id
        }
        LifecyclePhase::SystemResumeObservation => {
            receiver.generation_id == run.receiver.generation_id
        }
        LifecyclePhase::DeviceDeparture
        | LifecyclePhase::DeviceReturn
        | LifecyclePhase::ReceiverDeparture
        | LifecyclePhase::SuspendAuthorization => {
            receiver.generation_id == active.baseline_generation_id
        }
    };
    if !generation_matches || !receiver_identity_matches(receiver, run, true) {
        return false;
    }
    match active.phase {
        LifecyclePhase::DeviceDeparture | LifecyclePhase::DeviceReturn => run
            .receiver
            .devices
            .iter()
            .filter(|device| device.device_id != active.device_id)
            .all(|expected| receiver_has_exact_device(receiver, expected)),
        LifecyclePhase::SuspendAuthorization | LifecyclePhase::SystemResumeObservation => run
            .receiver
            .devices
            .iter()
            .all(|expected| receiver_has_exact_device(receiver, expected)),
        LifecyclePhase::ReceiverDeparture
        | LifecyclePhase::ReceiverReturn
        | LifecyclePhase::SystemResumePending => true,
    }
}

pub(crate) fn current_suspend_stamp() -> Result<SuspendStamp, LifecycleRunnerError> {
    let boot_id = fs::read_to_string(BOOT_ID_PATH).map_err(|_| {
        LifecycleRunnerError::new("The current Linux boot identity is unavailable.")
    })?;
    let boot_id = boot_id.trim();
    if boot_id.len() != 36
        || !boot_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() || byte == b'-')
    {
        return Err(LifecycleRunnerError::new(
            "The current Linux boot identity is malformed.",
        ));
    }
    let boottime = clock_milliseconds(ClockId::Boottime)?;
    let monotonic = clock_milliseconds(ClockId::Monotonic)?;
    Ok(SuspendStamp {
        boot_id_sha256: digest_hex(boot_id.as_bytes()),
        suspend_gap_ms: boottime.saturating_sub(monotonic),
    })
}

fn record_device_departure(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    validate_same_generation(&run, observation)?;
    let selected = selected_observation(&run, observation)?;
    if selected.controller_ready
        || !matches!(
            selected.availability,
            InventoryAvailability::Sleeping
                | InventoryAvailability::Unavailable
                | InventoryAvailability::ReceiverUnavailable
        )
        || !siblings_ready(&run, observation)
    {
        return Err(LifecycleRunnerError::new(
            "The selected controller has not departed cleanly while its qualified siblings remain ready.",
        ));
    }
    let active = run.active.as_mut().expect("active lifecycle stage exists");
    active.phase = LifecyclePhase::DeviceReturn;
    active.evidence_refs.push(format!(
        "device-departure:{}",
        selected.availability.as_str()
    ));
    let evidence = active.evidence_refs.clone();
    let has_siblings = run.plans.len() > 1;
    Ok(PreparedLifecycleAction::Record {
        run,
        transition: awaiting(device_return_checkpoint(evidence, has_siblings)),
    })
}

fn record_device_return(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    validate_ready_identity_for_active(&run, observation)?;
    if !observation.stable_restore_enabled {
        return Err(LifecycleRunnerError::new(
            "The controller returned, but stable restoration is no longer enabled.",
        ));
    }
    let active = run.active.as_ref().expect("active lifecycle stage exists");
    let restore_evidence = device_return_restore(
        observation.events.as_ref().ok_or_else(|| {
            LifecycleRunnerError::new(
                "The bridge event history is unavailable for this lifecycle checkpoint.",
            )
        })?,
        &active.evidence_refs,
        &observation.receiver_id,
        active.baseline_generation_id,
        &active.device_id,
    )
    .map_err(|error| LifecycleRunnerError::new(error.to_string()))?;
    let active = run.active.as_mut().expect("active lifecycle stage exists");
    active.evidence_refs.push("device-return:ready".to_owned());
    active.evidence_refs.push(restore_evidence);
    if let Some(cursor) = &observation.cursor_ref {
        active.evidence_refs.push(cursor.clone());
    }
    let checkpoint = if active.device_kind == "keyboard" {
        active.phase = LifecyclePhase::SuspendAuthorization;
        system_suspend_checkpoint(active.evidence_refs.clone())
    } else {
        active.phase = LifecyclePhase::ReceiverDeparture;
        receiver_departure_checkpoint(active.evidence_refs.clone())
    };
    Ok(PreparedLifecycleAction::Record {
        run,
        transition: awaiting(checkpoint),
    })
}

fn record_receiver_departure(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    if observation.receiver_present
        && !matches!(
            observation.receiver_lifecycle,
            Some(ReceiverLifecycleState::Disconnecting | ReceiverLifecycleState::Unknown)
        )
    {
        return Err(LifecycleRunnerError::new(
            "The exact receiver generation has not disappeared yet.",
        ));
    }
    let active = run.active.as_mut().expect("active lifecycle stage exists");
    active.phase = LifecyclePhase::ReceiverReturn;
    active
        .evidence_refs
        .push("receiver-departure:observed".to_owned());
    if let Some(cursor) = &observation.cursor_ref {
        active.evidence_refs.push(cursor.clone());
    }
    let evidence = active.evidence_refs.clone();
    let has_siblings = run.plans.len() > 1;
    Ok(PreparedLifecycleAction::Record {
        run,
        transition: awaiting(receiver_return_checkpoint(evidence, has_siblings)),
    })
}

fn record_receiver_return(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    validate_restored_identity(&run, observation, true)?;
    let generation = observation
        .generation_id
        .ok_or_else(|| LifecycleRunnerError::new("The renewed receiver has no generation."))?;
    let active = run.active.as_ref().expect("active lifecycle stage exists");
    if generation <= active.baseline_generation_id {
        return Err(LifecycleRunnerError::new(
            "Receiver return did not create a newer generation.",
        ));
    }
    let device_ids = run
        .plans
        .iter()
        .map(|plan| plan.device_id.clone())
        .collect::<Vec<_>>();
    let restore_evidence = renewed_generation_restores(
        observation.events.as_ref().ok_or_else(|| {
            LifecycleRunnerError::new(
                "The bridge event history is unavailable for this lifecycle checkpoint.",
            )
        })?,
        &active.evidence_refs,
        &observation.receiver_id,
        generation,
        &device_ids,
    )
    .map_err(|error| LifecycleRunnerError::new(error.to_string()))?;
    let mut evidence = active.evidence_refs.clone();
    evidence.push(format!("receiver-generation:{generation}"));
    evidence.extend(restore_evidence);
    evidence.push("stable-restore:succeeded".to_owned());
    if let Some(cursor) = &observation.cursor_ref {
        evidence.push(cursor.clone());
    }
    accept_generation(&mut run, observation)?;
    run.active = None;
    let summary = if run.plans.len() > 1 {
        "Controller return, sibling isolation, and a newer restored receiver generation were confirmed."
    } else {
        "Controller return and a newer restored receiver generation were confirmed."
    };
    Ok(PreparedLifecycleAction::Record {
        run,
        transition: finished(StageStatus::Passed, summary, evidence),
    })
}

fn prepare_system_suspend(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    validate_ready_identity_for_active(&run, observation)?;
    let stamp = current_suspend_stamp()?;
    let active = run.active.as_mut().expect("active lifecycle stage exists");
    if active.phase != LifecyclePhase::SuspendAuthorization {
        return Err(LifecycleRunnerError::new(
            "The lifecycle campaign is not awaiting a system suspend.",
        ));
    }
    active.phase = LifecyclePhase::SystemResumePending;
    active.suspend_stamp = Some(stamp);
    active
        .evidence_refs
        .push("system-suspend:authorized".to_owned());
    if let Some(cursor) = &observation.cursor_ref {
        active.evidence_refs.push(cursor.clone());
    }
    let checkpoint = StageCheckpoint {
        checkpoint_id: "system-resume-pending".to_owned(),
        instructions: vec![
            "The authorized suspend is in progress. Wake this computer normally and wait for the bridge and controller routes to return."
                .to_owned(),
        ],
        observations: Vec::new(),
        evidence_refs: active.evidence_refs.clone(),
    };
    Ok(PreparedLifecycleAction::Suspend {
        run,
        transition: RunnerTransition {
            progress: StageProgress {
                status: StageStatus::Running,
                result: None,
                checkpoint: Some(checkpoint),
            },
            hardware_write_executed: false,
        },
    })
}

fn record_system_resume(
    mut run: LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<PreparedLifecycleAction, LifecycleRunnerError> {
    validate_restored_identity(&run, observation, false)?;
    let active = run.active.as_ref().expect("active lifecycle stage exists");
    if active.phase != LifecyclePhase::SystemResumeObservation {
        return Err(LifecycleRunnerError::new(
            "A measured system resume has not reached its observation checkpoint.",
        ));
    }
    let mut evidence = active.evidence_refs.clone();
    evidence.push("stable-restore:succeeded".to_owned());
    run.active = None;
    let summary = if run.plans.len() > 1 {
        "Controller return, sibling isolation, measured suspend, and restored system resume were confirmed."
    } else {
        "Controller return, measured suspend, and restored system resume were confirmed."
    };
    Ok(PreparedLifecycleAction::Record {
        run,
        transition: finished(StageStatus::Passed, summary, evidence),
    })
}

fn validate_ready_identity_for_active(
    run: &LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<(), LifecycleRunnerError> {
    let active = run.active.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The retained lifecycle campaign has no active stage.")
    })?;
    validate_ready_identity(run, &active.device_id, observation)
}

fn validate_ready_identity(
    run: &LifecycleRunState,
    selected_device_id: &str,
    observation: &LifecycleObservation,
) -> Result<(), LifecycleRunnerError> {
    validate_receiver_identity(run, observation)?;
    let selected = observation.selected.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The exact selected controller is not currently observable.")
    })?;
    let expected = expected_device(run, selected_device_id)?;
    if selected.device_id != selected_device_id
        || selected.profile.as_ref() != expected.profile.as_ref()
        || selected.availability != InventoryAvailability::Available
        || !selected.controller_ready
        || !siblings_ready(run, observation)
    {
        return Err(LifecycleRunnerError::new(
            "The selected controller and every qualified sibling are not ready on the exact retained profiles.",
        ));
    }
    Ok(())
}

fn validate_restored_identity(
    run: &LifecycleRunState,
    observation: &LifecycleObservation,
    require_new_generation: bool,
) -> Result<(), LifecycleRunnerError> {
    validate_ready_identity_for_active(run, observation)?;
    let generation = observation
        .generation_id
        .ok_or_else(|| LifecycleRunnerError::new("The active receiver has no generation."))?;
    let baseline = run
        .active
        .as_ref()
        .expect("active lifecycle stage exists")
        .baseline_generation_id;
    if generation < baseline || (require_new_generation && generation <= baseline) {
        return Err(LifecycleRunnerError::new(
            "The active receiver generation does not satisfy this lifecycle checkpoint.",
        ));
    }
    if !observation.stable_restore_enabled {
        return Err(LifecycleRunnerError::new(
            "The exact generation is ready, but stable restoration is no longer enabled.",
        ));
    }
    Ok(())
}

fn validate_receiver_identity(
    run: &LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<(), LifecycleRunnerError> {
    if observation.receiver_id != run.receiver.receiver_id
        || !observation.receiver_present
        || observation.receiver_profile.as_ref() != run.receiver.profile.as_ref()
        || !matches!(
            observation.receiver_lifecycle,
            Some(ReceiverLifecycleState::Active | ReceiverLifecycleState::PartiallySuspended)
        )
    {
        return Err(LifecycleRunnerError::new(
            "The current receiver does not match the retained profile-bound lifecycle authority.",
        ));
    }
    Ok(())
}

fn validate_same_generation(
    run: &LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<(), LifecycleRunnerError> {
    validate_receiver_identity(run, observation)?;
    let active = run.active.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The retained lifecycle campaign has no active stage.")
    })?;
    if observation.generation_id != Some(active.baseline_generation_id) {
        return Err(LifecycleRunnerError::new(
            "The receiver generation changed before the controller-return checkpoint.",
        ));
    }
    Ok(())
}

fn selected_observation<'a>(
    run: &LifecycleRunState,
    observation: &'a LifecycleObservation,
) -> Result<&'a ObservedDevice, LifecycleRunnerError> {
    let active = run.active.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new("The retained lifecycle campaign has no active stage.")
    })?;
    observation.selected.as_ref().ok_or_else(|| {
        LifecycleRunnerError::new(format!(
            "The selected controller {} is not retained in the current receiver inventory.",
            active.device_id
        ))
    })
}

fn siblings_ready(run: &LifecycleRunState, observation: &LifecycleObservation) -> bool {
    let selected_id = run
        .active
        .as_ref()
        .map(|active| active.device_id.as_str())
        .or_else(|| {
            observation
                .selected
                .as_ref()
                .map(|selected| selected.device_id.as_str())
        })
        .unwrap_or("");
    let expected = run
        .plans
        .iter()
        .filter(|plan| plan.device_id != selected_id)
        .collect::<Vec<_>>();
    expected.iter().all(|plan| {
        let profile = run
            .receiver
            .devices
            .iter()
            .find(|device| device.device_id == plan.device_id)
            .and_then(|device| device.profile.as_ref());
        observation.siblings.iter().any(|sibling| {
            sibling.device_id == plan.device_id
                && sibling.profile.as_ref() == profile
                && sibling.availability == InventoryAvailability::Available
                && sibling.controller_ready
        })
    })
}

fn accept_generation(
    run: &mut LifecycleRunState,
    observation: &LifecycleObservation,
) -> Result<(), LifecycleRunnerError> {
    let generation = observation
        .generation_id
        .ok_or_else(|| LifecycleRunnerError::new("The accepted receiver has no generation."))?;
    if generation < run.receiver.generation_id {
        return Err(LifecycleRunnerError::new(
            "A stale receiver generation cannot enter the lifecycle history.",
        ));
    }
    if generation > run.receiver.generation_id {
        run.generation_history.push(generation);
        if run.generation_history.len() > MAX_GENERATION_HISTORY {
            return Err(LifecycleRunnerError::new(
                "The lifecycle generation history exceeded its bound.",
            ));
        }
        run.receiver.generation_id = generation;
        for plan in &mut run.plans {
            plan.generation_id = generation;
        }
    }
    Ok(())
}

fn plan_for_stage<'a>(
    view: &'a QualificationView,
    stage: &QualificationStage,
) -> Result<&'a QualificationPlan, LifecycleRunnerError> {
    view.plans
        .iter()
        .find(|plan| stage_belongs_to_plan(plan, stage))
        .ok_or_else(|| {
            LifecycleRunnerError::new(
                "The selected lifecycle stage no longer belongs to a current plan.",
            )
        })
}

fn stage_belongs_to_plan(plan: &QualificationPlan, stage: &QualificationStage) -> bool {
    plan.groups
        .iter()
        .flat_map(|group| &group.stages)
        .any(|candidate| candidate.stage_id == stage.stage_id)
}

fn expected_device<'a>(
    run: &'a LifecycleRunState,
    device_id: &str,
) -> Result<&'a crate::DeviceView, LifecycleRunnerError> {
    run.receiver
        .devices
        .iter()
        .find(|device| device.device_id == device_id)
        .ok_or_else(|| LifecycleRunnerError::new("The expected controller identity is missing."))
}

fn observed_device(
    receiver: &hfx_protocol::IntegrationReceiverView,
    device_id: &str,
) -> Option<ObservedDevice> {
    let inventory = receiver
        .inventory
        .iter()
        .find(|device| device.device_id.as_str() == device_id)?;
    let controller_ready = receiver.controllers.iter().any(|controller| {
        controller.device_id == inventory.device_id
            && controller.availability == ControllerAvailability::Ready
    });
    Some(ObservedDevice {
        device_id: inventory.device_id.as_str().to_owned(),
        profile: inventory.profile.as_ref().map(profile_binding),
        availability: inventory.availability,
        controller_ready,
    })
}

fn profile_binding(binding: &hfx_protocol::ProfileBindingView) -> ProfileBindingView {
    ProfileBindingView {
        id: binding.profile_id.as_str().to_owned(),
        digest: binding.profile_digest.as_str().to_owned(),
    }
}

fn merge_live_devices(receiver: &mut ReceiverView, live: &ReceiverView) {
    for device in &mut receiver.devices {
        if let Some(current) = live
            .devices
            .iter()
            .find(|current| current.device_id == device.device_id)
        {
            *device = current.clone();
        } else {
            device.presence = PresenceView::Unavailable;
            device.battery.availability = BatteryAvailability::Unavailable;
        }
    }
    for device in &live.devices {
        if !receiver
            .devices
            .iter()
            .any(|retained| retained.device_id == device.device_id)
        {
            receiver.devices.push(device.clone());
        }
    }
}

fn receiver_identity_matches(
    receiver: &ReceiverView,
    run: &LifecycleRunState,
    allow_missing_devices: bool,
) -> bool {
    if receiver.receiver_id != run.receiver.receiver_id
        || receiver.profile != run.receiver.profile
        || receiver.devices.iter().any(|device| {
            run.receiver
                .devices
                .iter()
                .find(|expected| expected.device_id == device.device_id)
                .is_none_or(|expected| {
                    expected.kind != device.kind
                        || expected.product_id != device.product_id
                        || expected.profile != device.profile
                })
        })
    {
        return false;
    }
    allow_missing_devices
        || run
            .receiver
            .devices
            .iter()
            .all(|expected| receiver_has_exact_device(receiver, expected))
}

fn receiver_has_exact_device(receiver: &ReceiverView, expected: &crate::DeviceView) -> bool {
    receiver.devices.iter().any(|device| {
        device.device_id == expected.device_id
            && device.kind == expected.kind
            && device.product_id == expected.product_id
            && device.profile == expected.profile
    })
}

fn device_departure_checkpoint(run: &LifecycleRunState) -> StageCheckpoint {
    let has_siblings = run.plans.len() > 1;
    StageCheckpoint {
        checkpoint_id: "device-departure".to_owned(),
        instructions: vec![
            "Power off the selected controller or allow it to enter its normal sleep state. Keep every sibling controller awake."
                .to_owned(),
            "Wait until the selected controller is unlit, then record this checkpoint once."
                .to_owned(),
        ],
        observations: lifecycle_observations(
            "Is the selected controller asleep or powered off?",
            has_siblings,
        ),
        evidence_refs: run
            .active
            .as_ref()
            .expect("active lifecycle stage exists")
            .evidence_refs
            .clone(),
    }
}

fn device_return_checkpoint(evidence_refs: Vec<String>, has_siblings: bool) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "device-return".to_owned(),
        instructions: vec![
            "Wake or power on the selected controller and exercise its ordinary controls."
                .to_owned(),
            "Wait for its saved solid-blue state to return, then record this checkpoint once."
                .to_owned(),
        ],
        observations: lifecycle_observations(
            "Did the selected controller return with solid blue lighting?",
            has_siblings,
        ),
        evidence_refs,
    }
}

fn receiver_departure_checkpoint(evidence_refs: Vec<String>) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "receiver-departure".to_owned(),
        instructions: vec![
            "Unplug only the HyperFlux receiver. The retained campaign will remain visible while its generation is absent."
                .to_owned(),
            "After the receiver and both controller routes disappear, record this checkpoint once."
                .to_owned(),
        ],
        observations: vec![physical_observation(
            "Was the exact HyperFlux receiver physically unplugged?",
        )],
        evidence_refs,
    }
}

fn receiver_return_checkpoint(evidence_refs: Vec<String>, has_siblings: bool) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "receiver-return".to_owned(),
        instructions: vec![
            "Replug the receiver, wake every paired qualified controller, and exercise the selected controller."
                .to_owned(),
            "Wait for a newer generation and the saved solid-blue state, then record this checkpoint once."
                .to_owned(),
        ],
        observations: lifecycle_observations(
            "Did the selected controller return with solid blue lighting on the renewed receiver?",
            has_siblings,
        ),
        evidence_refs,
    }
}

fn system_suspend_checkpoint(evidence_refs: Vec<String>) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "system-suspend-authorization".to_owned(),
        instructions: vec![
            "Save unrelated work. The next action requests exactly one system suspend and never repeats it automatically."
                .to_owned(),
            "Wake the computer normally after it suspends; the private checkpoint survives the request."
                .to_owned(),
        ],
        observations: Vec::new(),
        evidence_refs,
    }
}

fn system_resume_checkpoint(evidence_refs: Vec<String>, has_siblings: bool) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "system-resume-observation".to_owned(),
        instructions: vec![
            "A same-boot suspend gap and restored receiver route were detected. Exercise both controllers once."
                .to_owned(),
            "Confirm the selected controller's saved solid-blue state and sibling isolation."
                .to_owned(),
        ],
        observations: lifecycle_observations(
            "Did solid blue return on the selected controller after resume?",
            has_siblings,
        ),
        evidence_refs,
    }
}

fn lifecycle_observations(prompt: &str, has_siblings: bool) -> Vec<crate::ObservationPrompt> {
    let mut prompts = vec![physical_observation(prompt), controls_observation()];
    if has_siblings {
        prompts.push(crate::ObservationPrompt {
            id: "siblings".to_owned(),
            prompt: "Did every sibling controller remain isolated and responsive?".to_owned(),
            choices: choices("Siblings remained correct", "A sibling changed"),
        });
    }
    prompts
}

fn physical_observation(prompt: &str) -> crate::ObservationPrompt {
    crate::ObservationPrompt {
        id: "physical".to_owned(),
        prompt: prompt.to_owned(),
        choices: choices("Physical state matches", "Physical state differs"),
    }
}

fn controls_observation() -> crate::ObservationPrompt {
    crate::ObservationPrompt {
        id: "controls".to_owned(),
        prompt: "Do all available controllers' ordinary controls still work correctly?".to_owned(),
        choices: choices("Controls work", "A control changed"),
    }
}

fn choices(pass: &str, fail: &str) -> Vec<crate::ObservationChoice> {
    vec![
        crate::ObservationChoice {
            id: "pass".to_owned(),
            label: pass.to_owned(),
            outcome: "pass".to_owned(),
        },
        crate::ObservationChoice {
            id: "fail".to_owned(),
            label: fail.to_owned(),
            outcome: "fail".to_owned(),
        },
        crate::ObservationChoice {
            id: "unclear".to_owned(),
            label: "Unclear".to_owned(),
            outcome: "unclear".to_owned(),
        },
    ]
}

fn awaiting(checkpoint: StageCheckpoint) -> RunnerTransition {
    RunnerTransition {
        progress: StageProgress {
            status: StageStatus::AwaitingObservation,
            result: None,
            checkpoint: Some(checkpoint),
        },
        hardware_write_executed: false,
    }
}

fn finished(status: StageStatus, summary: &str, evidence_refs: Vec<String>) -> RunnerTransition {
    RunnerTransition {
        progress: StageProgress {
            status,
            result: Some(StageResult {
                summary: summary.to_owned(),
                completed_at: qualification_generated_at(),
                evidence_refs,
            }),
            checkpoint: None,
        },
        hardware_write_executed: false,
    }
}

fn retryable_failure(
    status: StageStatus,
    summary: &str,
    evidence_refs: Vec<String>,
) -> RunnerTransition {
    RunnerTransition {
        progress: StageProgress {
            status,
            result: Some(StageResult {
                summary: summary.to_owned(),
                completed_at: qualification_generated_at(),
                evidence_refs: evidence_refs.clone(),
            }),
            checkpoint: Some(system_suspend_checkpoint(evidence_refs)),
        },
        hardware_write_executed: false,
    }
}

fn clock_milliseconds(clock: ClockId) -> Result<u64, LifecycleRunnerError> {
    let stamp = clock_gettime(clock);
    let seconds = u64::try_from(stamp.tv_sec).map_err(|_| {
        LifecycleRunnerError::new("A required Linux lifecycle clock is unavailable.")
    })?;
    let nanos = u64::try_from(stamp.tv_nsec).map_err(|_| {
        LifecycleRunnerError::new("A required Linux lifecycle clock is unavailable.")
    })?;
    seconds
        .checked_mul(1_000)
        .and_then(|value| value.checked_add(nanos / 1_000_000))
        .ok_or_else(|| LifecycleRunnerError::new("A Linux lifecycle clock overflowed."))
}

fn digest_hex(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        DroppedEventCount, EventKind, GenerationId, LogicalDeviceId, ProjectionRevision,
        ReceiverId, SequenceNumber, StreamEpoch, StreamId, SubscriptionId, TransactionId,
    };
    use hfx_protocol::{BridgeEvent, EventCursor};
    use std::path::PathBuf;

    fn fixture_path(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../apps/device-qualification/tests/fixtures")
            .join(name)
    }

    fn fixture_view(name: &str, prerequisites_passed: bool) -> QualificationView {
        let mut view: QualificationView = serde_json::from_slice(
            &fs::read(fixture_path(name)).expect("qualification fixture reads"),
        )
        .expect("qualification fixture parses");
        if prerequisites_passed {
            for group in view.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
                for stage in &mut group.stages {
                    stage.status = if group.group_id == "lifecycle" {
                        StageStatus::Ready
                    } else {
                        StageStatus::Passed
                    };
                }
            }
        }
        view
    }

    fn lifecycle_stage(view: &QualificationView, device_id: &str) -> QualificationStage {
        view.plans
            .iter()
            .find(|plan| plan.device_id == device_id)
            .expect("device plan exists")
            .groups
            .iter()
            .find(|group| group.group_id == "lifecycle")
            .expect("lifecycle group exists")
            .stages[0]
            .clone()
    }

    fn observed_device(
        run: &LifecycleRunState,
        device_id: &str,
        availability: InventoryAvailability,
        controller_ready: bool,
    ) -> ObservedDevice {
        let expected = expected_device(run, device_id).expect("expected device exists");
        ObservedDevice {
            device_id: device_id.to_owned(),
            profile: expected.profile.clone(),
            availability,
            controller_ready,
        }
    }

    fn observation(
        run: &LifecycleRunState,
        selected_device_id: &str,
        generation_id: u64,
        selected_availability: InventoryAvailability,
        selected_ready: bool,
    ) -> LifecycleObservation {
        LifecycleObservation {
            receiver_id: run.receiver.receiver_id.clone(),
            receiver_present: true,
            generation_id: Some(generation_id),
            receiver_profile: run.receiver.profile.clone(),
            receiver_lifecycle: Some(ReceiverLifecycleState::Active),
            stable_restore_enabled: true,
            selected: Some(observed_device(
                run,
                selected_device_id,
                selected_availability,
                selected_ready,
            )),
            siblings: run
                .plans
                .iter()
                .filter(|plan| plan.device_id != selected_device_id)
                .map(|plan| {
                    observed_device(run, &plan.device_id, InventoryAvailability::Available, true)
                })
                .collect(),
            cursor_ref: Some(cursor_evidence(&test_cursor(0))),
            events: Some(test_events(run, selected_device_id, generation_id)),
        }
    }

    fn absent_receiver(run: &LifecycleRunState) -> LifecycleObservation {
        LifecycleObservation {
            receiver_id: run.receiver.receiver_id.clone(),
            receiver_present: false,
            generation_id: None,
            receiver_profile: None,
            receiver_lifecycle: None,
            stable_restore_enabled: false,
            selected: None,
            siblings: Vec::new(),
            cursor_ref: Some(cursor_evidence(&test_cursor(0))),
            events: Some(test_events(
                run,
                &run.active
                    .as_ref()
                    .expect("active lifecycle stage exists")
                    .device_id,
                run.receiver.generation_id,
            )),
        }
    }

    fn test_cursor(sequence: u64) -> EventCursor {
        EventCursor {
            stream_id: StreamId::try_from("lifecycle-test-stream").expect("stream id"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("projection revision"),
            sequence: SequenceNumber::try_from(sequence).expect("sequence"),
        }
    }

    fn test_events(
        run: &LifecycleRunState,
        selected_device_id: &str,
        generation_id: u64,
    ) -> EventBatch {
        let receiver_id =
            ReceiverId::try_from(run.receiver.receiver_id.as_str()).expect("receiver");
        let baseline = GenerationId::try_from(run.receiver.generation_id).expect("generation");
        let renewed = GenerationId::try_from(generation_id).expect("generation");
        let mut sequence = 0_u64;
        let mut events = Vec::new();
        let mut push = |kind: EventKind,
                        generation: GenerationId,
                        device_id: Option<&str>,
                        transaction: bool| {
            sequence += 1;
            events.push(BridgeEvent {
                sequence: SequenceNumber::try_from(sequence).expect("sequence"),
                kind,
                receiver_id: Some(receiver_id.clone()),
                generation_id: Some(generation),
                device_id: device_id
                    .map(|value| LogicalDeviceId::try_from(value).expect("logical device id")),
                lease_id: None,
                transaction_id: transaction.then(|| {
                    TransactionId::try_from(format!("restore-test-{sequence}"))
                        .expect("transaction id")
                }),
                finding_id: None,
            });
        };
        push(
            EventKind::DeviceSleeping,
            baseline,
            Some(selected_device_id),
            false,
        );
        push(
            EventKind::DeviceAvailable,
            baseline,
            Some(selected_device_id),
            false,
        );
        push(
            EventKind::RestoreCompleted,
            baseline,
            Some(selected_device_id),
            true,
        );
        for plan in &run.plans {
            push(
                EventKind::RestoreCompleted,
                baseline,
                Some(&plan.device_id),
                true,
            );
        }
        push(EventKind::ReceiverSuspended, baseline, None, false);
        push(EventKind::GenerationReplaced, renewed, None, false);
        push(EventKind::ReceiverAvailable, renewed, None, false);
        for plan in &run.plans {
            push(
                EventKind::RestoreCompleted,
                renewed,
                Some(&plan.device_id),
                true,
            );
        }
        let cursor = test_cursor(sequence);
        EventBatch {
            subscription_id: SubscriptionId::try_from("lifecycle-test-subscription")
                .expect("subscription id"),
            next_cursor: cursor,
            events,
            oldest_available: SequenceNumber::try_from(1_u64).expect("sequence"),
            latest_available: SequenceNumber::try_from(sequence).expect("sequence"),
            dropped_events: DroppedEventCount::try_from(0_u64).expect("drop count"),
            cursor_gap: false,
            has_more: false,
        }
    }

    fn unwrap_record(action: PreparedLifecycleAction) -> (LifecycleRunState, RunnerTransition) {
        match action {
            PreparedLifecycleAction::Record { run, transition } => (run, transition),
            PreparedLifecycleAction::Suspend { .. } => panic!("record action expected"),
        }
    }

    fn unwrap_suspend(action: PreparedLifecycleAction) -> (LifecycleRunState, RunnerTransition) {
        match action {
            PreparedLifecycleAction::Suspend { run, transition } => (run, transition),
            PreparedLifecycleAction::Record { .. } => panic!("suspend action expected"),
        }
    }

    fn start_stage(device_id: &str) -> (LifecycleRunState, QualificationStage, RunnerTransition) {
        let view = fixture_view("rust-qualified-pair.json", true);
        let stage = lifecycle_stage(&view, device_id);
        let run = capture_lifecycle_run(&view, &stage).expect("campaign captures");
        let ready = observation(
            &run,
            device_id,
            run.receiver.generation_id,
            InventoryAvailability::Available,
            true,
        );
        let (run, transition) = start_lifecycle_stage(run, &stage, &ready).expect("stage starts");
        (run, stage, transition)
    }

    fn advance_to_device_return(
        run: LifecycleRunState,
        stage: &QualificationStage,
        progress: &StageProgress,
    ) -> (LifecycleRunState, RunnerTransition) {
        let generation = run.receiver.generation_id;
        let device_id = run
            .active
            .as_ref()
            .expect("stage is active")
            .device_id
            .clone();
        let departure = observation(
            &run,
            &device_id,
            generation,
            InventoryAvailability::Sleeping,
            false,
        );
        unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                stage,
                progress,
                ObservationVerdict::Pass,
                &departure,
            )
            .expect("departure records"),
        )
    }

    #[test]
    fn campaign_requires_every_non_lifecycle_prerequisite() {
        let view = fixture_view("rust-qualified-pair.json", false);
        let stage = lifecycle_stage(&view, "mouse-test");
        let error = capture_lifecycle_run(&view, &stage).expect_err("prerequisites must gate");
        assert!(error.to_string().contains("identity and lighting"));
    }

    #[test]
    fn mouse_lifecycle_accepts_only_a_newer_profile_bound_generation() {
        let (run, stage, departure) = start_stage("mouse-test");
        assert_eq!(
            departure
                .progress
                .checkpoint
                .as_ref()
                .expect("departure checkpoint")
                .checkpoint_id,
            "device-departure"
        );
        let (run, device_return) = advance_to_device_return(run, &stage, &departure.progress);
        let ready = observation(
            &run,
            "mouse-test",
            7,
            InventoryAvailability::Available,
            true,
        );
        let (run, receiver_departure) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &device_return.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("device return records"),
        );
        let absent = absent_receiver(&run);
        let (run, receiver_return) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &receiver_departure.progress,
                ObservationVerdict::Pass,
                &absent,
            )
            .expect("receiver departure records"),
        );

        let same_generation = observation(
            &run,
            "mouse-test",
            7,
            InventoryAvailability::Available,
            true,
        );
        assert!(
            prepare_lifecycle_checkpoint(
                run.clone(),
                &stage,
                &receiver_return.progress,
                ObservationVerdict::Pass,
                &same_generation,
            )
            .is_err()
        );

        let mut wrong_profile = observation(
            &run,
            "mouse-test",
            8,
            InventoryAvailability::Available,
            true,
        );
        wrong_profile
            .receiver_profile
            .as_mut()
            .expect("profile exists")
            .digest = "0".repeat(64);
        assert!(
            prepare_lifecycle_checkpoint(
                run.clone(),
                &stage,
                &receiver_return.progress,
                ObservationVerdict::Pass,
                &wrong_profile,
            )
            .is_err()
        );

        let renewed = observation(
            &run,
            "mouse-test",
            8,
            InventoryAvailability::Available,
            true,
        );
        let (run, completed) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &receiver_return.progress,
                ObservationVerdict::Pass,
                &renewed,
            )
            .expect("new generation records"),
        );
        assert_eq!(completed.progress.status, StageStatus::Passed);
        assert_eq!(run.generation_history, vec![7, 8]);
        assert_eq!(run.receiver.generation_id, 8);
        assert!(run.active.is_none());
        run.validate().expect("completed run remains valid");
    }

    #[test]
    fn sibling_loss_blocks_selected_device_departure() {
        let (run, stage, departure) = start_stage("mouse-test");
        let mut sleeping = observation(
            &run,
            "mouse-test",
            7,
            InventoryAvailability::Sleeping,
            false,
        );
        sleeping.siblings[0].controller_ready = false;
        assert!(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &departure.progress,
                ObservationVerdict::Pass,
                &sleeping,
            )
            .is_err()
        );
    }

    #[test]
    fn failed_or_unclear_observation_finishes_without_replay() {
        for (verdict, expected) in [
            (ObservationVerdict::Fail, StageStatus::Failed),
            (ObservationVerdict::Unclear, StageStatus::Blocked),
        ] {
            let (run, stage, departure) = start_stage("mouse-test");
            let ready = observation(
                &run,
                "mouse-test",
                7,
                InventoryAvailability::Available,
                true,
            );
            let (run, transition) = unwrap_record(
                prepare_lifecycle_checkpoint(run, &stage, &departure.progress, verdict, &ready)
                    .expect("terminal observation records"),
            );
            assert_eq!(transition.progress.status, expected);
            assert!(!transition.hardware_write_executed);
            assert!(run.active.is_none());
        }
    }

    #[test]
    fn failed_resume_keeps_only_a_fresh_suspend_retry_checkpoint() {
        let (run, stage, departure) = start_stage("keyboard-test");
        let (run, device_return) = advance_to_device_return(run, &stage, &departure.progress);
        let ready = observation(
            &run,
            "keyboard-test",
            7,
            InventoryAvailability::Available,
            true,
        );
        let (run, authorization) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &device_return.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("device return reaches suspend authorization"),
        );
        let (suspended, _) = unwrap_suspend(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &authorization.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("suspend checkpoint prepares"),
        );
        let baseline = suspended
            .active
            .as_ref()
            .and_then(|active| active.suspend_stamp.clone())
            .expect("suspend stamp exists");
        let resume_stamp = SuspendStamp {
            boot_id_sha256: baseline.boot_id_sha256,
            suspend_gap_ms: baseline.suspend_gap_ms + MINIMUM_SUSPEND_GAP_MS,
        };
        let (run, resume) = reconcile_system_resume(suspended, &ready, &resume_stamp)
            .expect("resume reconciles")
            .expect("resume checkpoint exists");
        let (run, failed) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &resume.progress,
                ObservationVerdict::Fail,
                &ready,
            )
            .expect("physical failure records"),
        );

        assert_eq!(failed.progress.status, StageStatus::Failed);
        assert_eq!(
            failed
                .progress
                .checkpoint
                .as_ref()
                .expect("retry checkpoint exists")
                .checkpoint_id,
            "system-suspend-authorization"
        );
        assert!(
            failed
                .progress
                .result
                .as_ref()
                .expect("failure remains recorded")
                .evidence_refs
                .iter()
                .any(|value| value == SYSTEM_RESUME_RETRY_EVIDENCE)
        );
        assert_eq!(
            run.active.as_ref().expect("retry remains active").phase,
            LifecyclePhase::SuspendAuthorization
        );
        assert!(
            run.active
                .as_ref()
                .expect("retry remains active")
                .suspend_stamp
                .is_none()
        );

        let (_, pending) = unwrap_suspend(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &failed.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("explicit retry prepares one new suspend"),
        );
        assert_eq!(pending.progress.status, StageStatus::Running);
    }

    #[test]
    fn legacy_terminal_resume_failure_upgrades_to_the_same_retry_contract() {
        let (mut run, stage, _) = start_stage("keyboard-test");
        run.active = None;
        let mut progress = finished(
            StageStatus::Failed,
            "A watched lifecycle result did not match the required physical state.",
            vec![
                "system-suspend:authorized".to_owned(),
                "system-suspend-gap-ms:9496".to_owned(),
            ],
        )
        .progress;

        assert!(
            recover_retryable_resume_failure(&mut run, &stage.stage_id, &mut progress)
                .expect("legacy failure upgrades")
        );
        assert_eq!(progress.status, StageStatus::Failed);
        assert_eq!(
            progress
                .checkpoint
                .as_ref()
                .expect("retry checkpoint exists")
                .checkpoint_id,
            "system-suspend-authorization"
        );
        assert_eq!(
            run.active.as_ref().expect("retry is retained").phase,
            LifecyclePhase::SuspendAuthorization
        );
        run.validate().expect("upgraded retry remains valid");
    }

    #[test]
    fn keyboard_suspend_is_separately_authorized_and_same_boot_bound() {
        let (run, stage, departure) = start_stage("keyboard-test");
        let (run, device_return) = advance_to_device_return(run, &stage, &departure.progress);
        let ready = observation(
            &run,
            "keyboard-test",
            7,
            InventoryAvailability::Available,
            true,
        );
        let (run, authorization) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &device_return.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("device return reaches suspend authorization"),
        );
        assert_eq!(
            authorization
                .progress
                .checkpoint
                .as_ref()
                .expect("authorization checkpoint")
                .checkpoint_id,
            "system-suspend-authorization"
        );
        assert!(
            authorization
                .progress
                .checkpoint
                .as_ref()
                .expect("authorization checkpoint")
                .observations
                .is_empty()
        );

        let (suspended, pending) = unwrap_suspend(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &authorization.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("suspend checkpoint prepares"),
        );
        assert_eq!(
            pending
                .progress
                .checkpoint
                .as_ref()
                .expect("pending checkpoint")
                .checkpoint_id,
            "system-resume-pending"
        );
        let baseline = suspended
            .active
            .as_ref()
            .and_then(|active| active.suspend_stamp.clone())
            .expect("suspend stamp exists");

        let reboot_stamp = SuspendStamp {
            boot_id_sha256: digest_hex(b"different-boot"),
            suspend_gap_ms: baseline.suspend_gap_ms + MINIMUM_SUSPEND_GAP_MS,
        };
        let (blocked_run, blocked) =
            reconcile_system_resume(suspended.clone(), &ready, &reboot_stamp)
                .expect("reboot is a recorded block")
                .expect("reboot yields a transition");
        assert_eq!(blocked.progress.status, StageStatus::Blocked);
        assert!(blocked_run.active.is_none());

        let resume_stamp = SuspendStamp {
            boot_id_sha256: baseline.boot_id_sha256,
            suspend_gap_ms: baseline.suspend_gap_ms + MINIMUM_SUSPEND_GAP_MS,
        };
        let (run, resume) = reconcile_system_resume(suspended, &ready, &resume_stamp)
            .expect("resume reconciles")
            .expect("measured gap yields a checkpoint");
        assert_eq!(
            resume
                .progress
                .checkpoint
                .as_ref()
                .expect("resume checkpoint")
                .checkpoint_id,
            "system-resume-observation"
        );
        let (run, completed) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &resume.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("resume observation records"),
        );
        assert_eq!(completed.progress.status, StageStatus::Passed);
        assert!(run.active.is_none());
    }

    #[test]
    fn retained_campaign_keeps_the_expected_identity_visible_while_absent() {
        let (mut run, _, _) = start_stage("mouse-test");
        run.active.as_mut().expect("stage is active").phase = LifecyclePhase::ReceiverReturn;
        let mut view = fixture_view("rust-qualified-pair.json", true);
        view.receivers.clear();
        view.plans.clear();
        view.companion.state = CompanionState::NoReceiver;
        overlay_lifecycle_run(&mut view, &run);
        assert_eq!(view.companion.state, CompanionState::LifecycleTransition);
        assert_eq!(view.receivers.len(), 1);
        assert_eq!(view.plans.len(), 2);
        assert!(
            view.receivers[0]
                .devices
                .iter()
                .all(|device| device.presence == PresenceView::Unavailable)
        );
    }

    #[test]
    fn single_controller_campaign_never_prompts_about_siblings() {
        let view = fixture_view("rust-qualified-mouse.json", true);
        let stage = lifecycle_stage(&view, "mouse-test");
        let run = capture_lifecycle_run(&view, &stage).expect("campaign captures");
        let ready = observation(
            &run,
            "mouse-test",
            7,
            InventoryAvailability::Available,
            true,
        );
        let (run, departure) = start_lifecycle_stage(run, &stage, &ready).expect("stage starts");
        assert!(
            departure
                .progress
                .checkpoint
                .as_ref()
                .expect("departure checkpoint")
                .observations
                .iter()
                .all(|prompt| prompt.id != "siblings")
        );
        let (run, device_return) = advance_to_device_return(run, &stage, &departure.progress);
        assert!(
            device_return
                .progress
                .checkpoint
                .as_ref()
                .expect("return checkpoint")
                .observations
                .iter()
                .all(|prompt| prompt.id != "siblings")
        );
        let ready = observation(
            &run,
            "mouse-test",
            run.receiver.generation_id,
            InventoryAvailability::Available,
            true,
        );
        let (run, receiver_departure) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &device_return.progress,
                ObservationVerdict::Pass,
                &ready,
            )
            .expect("device return records"),
        );
        let absent = absent_receiver(&run);
        let (run, receiver_return) = unwrap_record(
            prepare_lifecycle_checkpoint(
                run,
                &stage,
                &receiver_departure.progress,
                ObservationVerdict::Pass,
                &absent,
            )
            .expect("receiver departure records"),
        );
        assert!(
            receiver_return
                .progress
                .checkpoint
                .as_ref()
                .expect("receiver return checkpoint")
                .observations
                .iter()
                .all(|prompt| prompt.id != "siblings")
        );
        run.validate().expect("single-device run remains valid");
    }

    #[test]
    fn unrelated_receivers_do_not_invalidate_a_bound_campaign() {
        let mut view = fixture_view("rust-qualified-pair.json", true);
        let stage = lifecycle_stage(&view, "mouse-test");
        let mut run = capture_lifecycle_run(&view, &stage).expect("campaign captures");
        let mut unrelated = view.receivers[0].clone();
        unrelated.receiver_id = "receiver-unrelated".to_owned();
        view.receivers.insert(0, unrelated.clone());
        assert!(identity_transition_matches(&view, &run));

        run.active = Some(LifecycleStageState {
            stage_id: stage.stage_id,
            device_id: "mouse-test".to_owned(),
            device_kind: "mouse".to_owned(),
            phase: LifecyclePhase::ReceiverReturn,
            baseline_generation_id: run.receiver.generation_id,
            evidence_refs: Vec::new(),
            suspend_stamp: None,
        });
        view.receivers = vec![unrelated];
        assert!(identity_transition_matches(&view, &run));
    }

    #[test]
    fn malformed_saved_runs_cannot_cross_bind_a_stage_or_generation() {
        let (mut run, _, _) = start_stage("mouse-test");
        run.active.as_mut().expect("stage is active").device_id = "keyboard-test".to_owned();
        assert!(run.validate().is_err());

        let (mut run, _, _) = start_stage("mouse-test");
        run.generation_history = vec![7, 7];
        assert!(run.validate().is_err());
    }
}
