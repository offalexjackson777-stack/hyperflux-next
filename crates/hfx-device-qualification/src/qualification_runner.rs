// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::{StageCheckpoint, StageProgress};
use crate::{
    ObservationChoice, ObservationPrompt, QualificationStage, QualificationView, RealSystemProbe,
    StageResult, StageStatus, qualification_generated_at,
};
use hfx_domain::{
    ColorChannel, ControllerAvailability, FrameIndex, LeaseDurationMs, MonotonicMs,
    SideEffectCertainty, StableLightingMode, TransactionClass, TransactionId, TransactionState,
};
use hfx_protocol::{
    ControllerView, DeviceProfileBinding, LeaseGrant, LeaseResult, LightingFrame, ResourceKey,
    RgbColor, StableLightingIntent, TransactionResult, TransactionTerminal,
};
use hfx_sdk::{KernelRequestIdentitySource, RequestIdentitySource, TransactionSubmission};
use rustix::time::{ClockId, clock_gettime};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::thread;
use std::time::{Duration, Instant};

const LEASE_DURATION_MS: u32 = 15_000;
const TRANSACTION_DEADLINE_MS: u64 = 5_000;
const TRANSACTION_WAIT: Duration = Duration::from_secs(6);
const TRANSACTION_POLL: Duration = Duration::from_millis(20);
const LIVE_LIGHTING_FEATURES: &[&str] = &[
    "atomic-transactions",
    "integration-view-projection",
    "ownership-leases",
    "profile-bound-transactions",
    "semantic-stable-lighting",
];
const TRANSACTION_RECOVERY_FEATURES: &[&str] = &["atomic-transactions"];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ObservationVerdict {
    Pass,
    Fail,
    Unclear,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum LightingCommand {
    MapPattern,
    StaticBlue,
    Off,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum LightingStep {
    MapPattern,
    MapCleanup,
    LightingBlue,
    LightingOff,
    LightingRestore,
    FailedCleanup,
    UnclearCleanup,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BoundTarget {
    receiver_id: hfx_domain::ReceiverId,
    generation_id: hfx_domain::GenerationId,
    device_id: hfx_domain::LogicalDeviceId,
    receiver_profile: hfx_protocol::ProfileBindingView,
    device_profile: hfx_protocol::ProfileBindingView,
    application_slots: hfx_domain::LedCount,
    rows: hfx_domain::LedCount,
    columns: hfx_domain::LedCount,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DeliveryReceipt {
    evidence_ref: String,
    hardware_write_executed: bool,
}

trait LightingTransport {
    fn deliver(
        &mut self,
        target: &BoundTarget,
        command: LightingCommand,
        transaction_id: &TransactionId,
    ) -> Result<DeliveryReceipt, QualificationRunnerError>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RunnerTransition {
    pub progress: StageProgress,
    pub hardware_write_executed: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PendingLightingTransaction {
    pub stage_id: String,
    pub transaction_id: TransactionId,
    pub step: LightingStep,
    pub prior_progress: Option<StageProgress>,
}

pub(crate) enum PreparedLightingAction {
    Record(RunnerTransition),
    Write(PreparedLightingWrite),
}

pub(crate) struct PreparedLightingWrite {
    target: BoundTarget,
    step: LightingStep,
    prior_progress: Option<StageProgress>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QualificationRunnerError {
    message: String,
    hardware_write_executed: bool,
    retry_forbidden: bool,
}

impl QualificationRunnerError {
    fn before_write(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            hardware_write_executed: false,
            retry_forbidden: false,
        }
    }

    fn after_submission(message: impl Into<String>, hardware_write_executed: bool) -> Self {
        Self {
            message: message.into(),
            hardware_write_executed,
            retry_forbidden: true,
        }
    }

    #[must_use]
    pub(crate) fn hardware_write_executed(&self) -> bool {
        self.hardware_write_executed
    }

    #[must_use]
    pub(crate) fn retry_forbidden(&self) -> bool {
        self.retry_forbidden
    }
}

impl fmt::Display for QualificationRunnerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for QualificationRunnerError {}

pub(crate) struct LiveLightingTransport<'a> {
    probe: &'a RealSystemProbe,
}

impl<'a> LiveLightingTransport<'a> {
    pub(crate) const fn new(probe: &'a RealSystemProbe) -> Self {
        Self { probe }
    }
}

impl LightingTransport for LiveLightingTransport<'_> {
    fn deliver(
        &mut self,
        target: &BoundTarget,
        command: LightingCommand,
        transaction_id: &TransactionId,
    ) -> Result<DeliveryReceipt, QualificationRunnerError> {
        let mut client = self
            .probe
            .bridge_client(
                "hyperflux-qualification-runner",
                "HyperFlux Qualification Runner",
                LIVE_LIGHTING_FEATURES,
            )
            .map_err(|_| {
                QualificationRunnerError::before_write(
                    "The local bridge could not open a qualification writer session.",
                )
            })?;
        let controller = current_controller(&mut client, target)?;
        let lease = acquire_qualification_lease(&mut client, &controller.resource)?;
        let submission = lighting_submission(
            target,
            command,
            &controller.resource,
            &lease,
            transaction_id,
        )?;
        let submitted = client.submit_transaction(submission).map_err(|_| {
            QualificationRunnerError::after_submission(
                "The bridge did not return a transaction outcome; the request was not repeated.",
                false,
            )
        });
        let result = match submitted {
            Ok(result) => wait_for_terminal(&mut client, transaction_id, result),
            Err(error) => Err(error),
        };
        let released = matches!(
            client.release_lease(lease.lease_id),
            Ok(LeaseResult::Granted(_))
        );
        let terminal = result?;
        if !released {
            return Err(QualificationRunnerError::after_submission(
                "The lighting request completed, but its lease release was not confirmed; wait for lease expiry before retrying.",
                terminal.live_write_executed,
            ));
        }
        validate_terminal(&terminal)?;
        Ok(DeliveryReceipt {
            evidence_ref: format!("bridge-transaction:{transaction_id}"),
            hardware_write_executed: true,
        })
    }
}

fn current_controller<S, I>(
    client: &mut hfx_sdk::HyperFluxClient<S, I>,
    target: &BoundTarget,
) -> Result<ControllerView, QualificationRunnerError>
where
    S: std::io::Read + std::io::Write,
    I: RequestIdentitySource,
{
    let integration = client.integration_view().map_err(|_| {
        QualificationRunnerError::before_write(
            "The bridge could not refresh the generation-bound controller view.",
        )
    })?;
    let controller = exact_controller(&integration, target)?.clone();
    if controller.availability != ControllerAvailability::Ready {
        return Err(QualificationRunnerError::before_write(
            "The selected controller is not ready for a bounded lighting step.",
        ));
    }
    Ok(controller)
}

fn acquire_qualification_lease<S, I>(
    client: &mut hfx_sdk::HyperFluxClient<S, I>,
    resource: &ResourceKey,
) -> Result<LeaseGrant, QualificationRunnerError>
where
    S: std::io::Read + std::io::Write,
    I: RequestIdentitySource,
{
    let duration = LeaseDurationMs::try_from(LEASE_DURATION_MS).map_err(|_| {
        QualificationRunnerError::before_write(
            "The qualification lease duration is outside the protocol contract.",
        )
    })?;
    match client.acquire_lease(vec![resource.clone()], duration) {
        Ok(LeaseResult::Granted(grant)) => Ok(grant),
        Ok(LeaseResult::Conflict(_)) => Err(QualificationRunnerError::before_write(
            "Another application owns this controller. Close its lighting session and retry.",
        )),
        Ok(LeaseResult::Rejected(_)) | Err(_) => Err(QualificationRunnerError::before_write(
            "The bridge rejected the qualification lighting lease before any write.",
        )),
    }
}

fn lighting_submission(
    target: &BoundTarget,
    command: LightingCommand,
    resource: &ResourceKey,
    lease: &LeaseGrant,
    transaction_id: &TransactionId,
) -> Result<TransactionSubmission, QualificationRunnerError> {
    let stable_intents = command.stable_mode().map_or_else(Vec::new, |mode| {
        vec![StableLightingIntent {
            device_id: target.device_id.clone(),
            mode,
        }]
    });
    Ok(TransactionSubmission {
        transaction_id: transaction_id.clone(),
        lease_id: lease.lease_id.clone(),
        receiver_id: target.receiver_id.clone(),
        generation_id: target.generation_id,
        receiver_profile_id: target.receiver_profile.profile_id.clone(),
        receiver_profile_digest: target.receiver_profile.profile_digest.clone(),
        device_profiles: vec![DeviceProfileBinding {
            device_id: target.device_id.clone(),
            profile_id: target.device_profile.profile_id.clone(),
            profile_digest: target.device_profile.profile_digest.clone(),
            application_slot_count: target.application_slots,
        }],
        transaction_class: command.transaction_class(),
        stable_intents,
        deadline_ms: transaction_deadline()?,
        resources: vec![resource.clone()],
        frames: vec![LightingFrame {
            device_id: target.device_id.clone(),
            frame_index: FrameIndex::try_from(0_u32).map_err(|_| {
                QualificationRunnerError::before_write(
                    "The qualification frame index is outside the protocol contract.",
                )
            })?,
            colors: command.colors(target),
        }],
    })
}

fn validate_terminal(terminal: &TransactionTerminal) -> Result<(), QualificationRunnerError> {
    if terminal.state == TransactionState::Succeeded
        && terminal.declared_frames.get() == 1
        && terminal.delivered_frames.get() == 1
        && terminal.side_effect_certainty == SideEffectCertainty::Committed
        && terminal.live_write_executed
    {
        return Ok(());
    }
    Err(QualificationRunnerError::after_submission(
        "The bridge did not terminally confirm one complete qualification frame; the request was not repeated.",
        terminal.live_write_executed,
    ))
}

pub(crate) fn prepare_lighting_stage(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
) -> Result<PreparedLightingAction, QualificationRunnerError> {
    let target = bound_target(probe, view, stage)?;
    prepare_lighting_stage_for_target(target, stage)
}

pub(crate) fn prepare_lighting_checkpoint(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
    current: &StageProgress,
    verdict: ObservationVerdict,
) -> Result<PreparedLightingAction, QualificationRunnerError> {
    let target = bound_target(probe, view, stage)?;
    prepare_lighting_checkpoint_for_target(target, stage, current, verdict)
}

fn prepare_lighting_stage_for_target(
    target: BoundTarget,
    stage: &QualificationStage,
) -> Result<PreparedLightingAction, QualificationRunnerError> {
    let step = if stage.stage_id.ends_with("-map") {
        LightingStep::MapPattern
    } else if stage.stage_id.ends_with("-lighting") {
        LightingStep::LightingBlue
    } else {
        return Err(QualificationRunnerError::before_write(
            "This supervised stage is not a recognized lighting qualification step.",
        ));
    };
    Ok(PreparedLightingAction::Write(PreparedLightingWrite {
        target,
        step,
        prior_progress: None,
    }))
}

fn prepare_lighting_checkpoint_for_target(
    target: BoundTarget,
    stage: &QualificationStage,
    current: &StageProgress,
    verdict: ObservationVerdict,
) -> Result<PreparedLightingAction, QualificationRunnerError> {
    let checkpoint = current.checkpoint.as_ref().ok_or_else(|| {
        QualificationRunnerError::before_write(
            "The supervised stage has no resumable checkpoint; no request was sent.",
        )
    })?;
    let step = match verdict {
        ObservationVerdict::Fail => LightingStep::FailedCleanup,
        ObservationVerdict::Unclear => LightingStep::UnclearCleanup,
        ObservationVerdict::Pass => match checkpoint.checkpoint_id.as_str() {
            "map-pattern" if stage.stage_id.ends_with("-map") => LightingStep::MapCleanup,
            "lighting-blue" if stage.stage_id.ends_with("-lighting") => LightingStep::LightingOff,
            "lighting-off" if stage.stage_id.ends_with("-lighting") => {
                LightingStep::LightingRestore
            }
            "lighting-restored" if stage.stage_id.ends_with("-lighting") => {
                return Ok(PreparedLightingAction::Record(finished(
                    StageStatus::Passed,
                    "Static blue, semantic Off, complete black, and final blue restoration were confirmed.",
                    checkpoint.evidence_refs.clone(),
                    false,
                )));
            }
            _ => {
                return Err(QualificationRunnerError::before_write(
                    "The checkpoint does not belong to the current profile-bound stage.",
                ));
            }
        },
    };
    Ok(PreparedLightingAction::Write(PreparedLightingWrite {
        target,
        step,
        prior_progress: Some(current.clone()),
    }))
}

impl PreparedLightingWrite {
    #[must_use]
    pub(crate) fn pending(
        &self,
        stage_id: &str,
        transaction_id: TransactionId,
    ) -> PendingLightingTransaction {
        PendingLightingTransaction {
            stage_id: stage_id.to_owned(),
            transaction_id,
            step: self.step,
            prior_progress: self.prior_progress.clone(),
        }
    }

    pub(crate) fn execute(
        &self,
        probe: &RealSystemProbe,
        transaction_id: &TransactionId,
    ) -> Result<RunnerTransition, QualificationRunnerError> {
        let mut transport = LiveLightingTransport::new(probe);
        execute_lighting_write(&mut transport, self, transaction_id)
    }
}

pub(crate) fn reconcile_pending_lighting(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
    pending: &PendingLightingTransaction,
) -> Result<Option<RunnerTransition>, QualificationRunnerError> {
    if stage.stage_id != pending.stage_id || !pending.step.belongs_to(stage) {
        return Err(QualificationRunnerError::before_write(
            "The saved transaction does not belong to the current profile-bound stage.",
        ));
    }
    let target = bound_target(probe, view, stage)?;
    let mut client = probe
        .bridge_client(
            "hyperflux-qualification-runner",
            "HyperFlux Qualification Recovery",
            TRANSACTION_RECOVERY_FEATURES,
        )
        .map_err(|_| {
            QualificationRunnerError::before_write(
                "The bridge is unavailable while reconciling the saved transaction.",
            )
        })?;
    let outcome = client
        .transaction_outcome(pending.transaction_id.clone())
        .map_err(|_| {
            QualificationRunnerError::after_submission(
                "The saved transaction outcome cannot be queried yet; it was not repeated.",
                false,
            )
        })?;
    match outcome {
        TransactionResult::Progress(_) => Ok(None),
        TransactionResult::Terminal(terminal) => {
            if let Err(error) = validate_terminal(&terminal) {
                return Ok(Some(blocked_pending(
                    pending,
                    &error.to_string(),
                    terminal.live_write_executed,
                )));
            }
            let prepared = PreparedLightingWrite {
                target,
                step: pending.step,
                prior_progress: pending.prior_progress.clone(),
            };
            complete_lighting_step(
                &prepared.target,
                prepared.step,
                prepared.prior_progress.as_ref(),
                DeliveryReceipt {
                    evidence_ref: format!("bridge-transaction:{}", pending.transaction_id),
                    hardware_write_executed: true,
                },
            )
            .map(Some)
        }
        TransactionResult::Unavailable(_) => Ok(Some(blocked_pending(
            pending,
            "The saved transaction outcome is no longer retained; the request was not repeated.",
            false,
        ))),
    }
}

fn blocked_pending(
    pending: &PendingLightingTransaction,
    summary: &str,
    hardware_write_executed: bool,
) -> RunnerTransition {
    let mut evidence = pending
        .prior_progress
        .as_ref()
        .and_then(|progress| progress.checkpoint.as_ref())
        .map_or_else(Vec::new, |checkpoint| checkpoint.evidence_refs.clone());
    evidence.push(format!("bridge-transaction:{}", pending.transaction_id));
    finished(
        StageStatus::Blocked,
        summary,
        evidence,
        hardware_write_executed,
    )
}

fn execute_lighting_write<T: LightingTransport>(
    transport: &mut T,
    prepared: &PreparedLightingWrite,
    transaction_id: &TransactionId,
) -> Result<RunnerTransition, QualificationRunnerError> {
    let receipt = transport.deliver(&prepared.target, prepared.step.command(), transaction_id)?;
    complete_lighting_step(
        &prepared.target,
        prepared.step,
        prepared.prior_progress.as_ref(),
        receipt,
    )
}

fn complete_lighting_step(
    target: &BoundTarget,
    step: LightingStep,
    prior: Option<&StageProgress>,
    receipt: DeliveryReceipt,
) -> Result<RunnerTransition, QualificationRunnerError> {
    let hardware_write_executed = receipt.hardware_write_executed;
    let prior_evidence = || -> Result<Vec<String>, QualificationRunnerError> {
        prior
            .and_then(|progress| progress.checkpoint.as_ref())
            .map(|checkpoint| checkpoint.evidence_refs.clone())
            .ok_or_else(|| {
                QualificationRunnerError::after_submission(
                    "The completed transaction has no compatible saved checkpoint; it was not repeated.",
                    hardware_write_executed,
                )
            })
    };
    let mut evidence = if matches!(step, LightingStep::MapPattern | LightingStep::LightingBlue) {
        Vec::new()
    } else {
        prior_evidence()?
    };
    evidence.push(receipt.evidence_ref);
    Ok(match step {
        LightingStep::MapPattern => {
            awaiting(map_checkpoint(target, evidence), hardware_write_executed)
        }
        LightingStep::MapCleanup => finished(
            StageStatus::Passed,
            "The application layout pattern and bounded blue cleanup were confirmed.",
            evidence,
            hardware_write_executed,
        ),
        LightingStep::LightingBlue => awaiting(blue_checkpoint(evidence), hardware_write_executed),
        LightingStep::LightingOff => awaiting(off_checkpoint(evidence), hardware_write_executed),
        LightingStep::LightingRestore => {
            awaiting(restored_checkpoint(evidence), hardware_write_executed)
        }
        LightingStep::FailedCleanup => finished(
            StageStatus::Failed,
            "The watched lighting result did not match every requested checkpoint.",
            evidence,
            hardware_write_executed,
        ),
        LightingStep::UnclearCleanup => finished(
            StageStatus::Blocked,
            "The watched lighting result was inconclusive.",
            evidence,
            hardware_write_executed,
        ),
    })
}

fn awaiting(checkpoint: StageCheckpoint, hardware_write_executed: bool) -> RunnerTransition {
    RunnerTransition {
        progress: StageProgress {
            status: StageStatus::AwaitingObservation,
            result: None,
            checkpoint: Some(checkpoint),
        },
        hardware_write_executed,
    }
}

fn finished(
    status: StageStatus,
    summary: &str,
    evidence_refs: Vec<String>,
    hardware_write_executed: bool,
) -> RunnerTransition {
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
        hardware_write_executed,
    }
}

fn bound_target(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
) -> Result<BoundTarget, QualificationRunnerError> {
    let plan = view
        .plans
        .iter()
        .find(|plan| {
            plan.groups
                .iter()
                .flat_map(|group| &group.stages)
                .any(|candidate| candidate.stage_id == stage.stage_id)
        })
        .ok_or_else(|| {
            QualificationRunnerError::before_write(
                "The selected stage no longer belongs to a current qualification plan.",
            )
        })?;
    let receiver = view
        .receivers
        .iter()
        .find(|receiver| {
            receiver.receiver_id == plan.receiver_id
                && receiver.generation_id == plan.generation_id
                && receiver
                    .devices
                    .iter()
                    .any(|device| device.device_id == plan.device_id)
        })
        .ok_or_else(|| {
            QualificationRunnerError::before_write(
                "The selected controller no longer belongs to a current receiver generation.",
            )
        })?;
    let integration = probe.qualification_integration().map_err(|_| {
        QualificationRunnerError::before_write(
            "The bridge could not refresh the selected controller before the write.",
        )
    })?;
    let current_receiver = integration
        .view
        .receivers
        .iter()
        .find(|candidate| {
            candidate.receiver_id.as_str() == receiver.receiver_id
                && candidate.generation_id.get() == receiver.generation_id
        })
        .ok_or_else(|| {
            QualificationRunnerError::before_write(
                "The receiver generation changed before the qualification write.",
            )
        })?;
    let controller = current_receiver
        .controllers
        .iter()
        .find(|controller| controller.device_id.as_str() == plan.device_id)
        .ok_or_else(|| {
            QualificationRunnerError::before_write(
                "The selected controller has no current writable profile-bound route.",
            )
        })?;
    if plan.profile_binding.as_ref().is_none_or(|binding| {
        binding.id != controller.device_profile.profile_id.as_str()
            || binding.digest != controller.device_profile.profile_digest.as_str()
    }) {
        return Err(QualificationRunnerError::before_write(
            "The selected controller profile changed before the qualification write.",
        ));
    }
    Ok(BoundTarget::from(controller))
}

fn exact_controller<'a>(
    integration: &'a hfx_protocol::IntegrationView,
    target: &BoundTarget,
) -> Result<&'a ControllerView, QualificationRunnerError> {
    integration
        .receivers
        .iter()
        .find(|receiver| {
            receiver.receiver_id == target.receiver_id
                && receiver.generation_id == target.generation_id
        })
        .and_then(|receiver| {
            receiver.controllers.iter().find(|controller| {
                controller.device_id == target.device_id
                    && controller.receiver_profile == target.receiver_profile
                    && controller.device_profile == target.device_profile
                    && controller.lighting.application_slot_count == target.application_slots
                    && controller.lighting.rows == target.rows
                    && controller.lighting.columns == target.columns
            })
        })
        .ok_or_else(|| {
            QualificationRunnerError::before_write(
                "The controller authority changed before lease acquisition.",
            )
        })
}

impl From<&ControllerView> for BoundTarget {
    fn from(controller: &ControllerView) -> Self {
        Self {
            receiver_id: controller.receiver_id.clone(),
            generation_id: controller.generation_id,
            device_id: controller.device_id.clone(),
            receiver_profile: controller.receiver_profile.clone(),
            device_profile: controller.device_profile.clone(),
            application_slots: controller.lighting.application_slot_count,
            rows: controller.lighting.rows,
            columns: controller.lighting.columns,
        }
    }
}

impl LightingCommand {
    const fn transaction_class(self) -> TransactionClass {
        match self {
            Self::MapPattern => TransactionClass::EffectFrame,
            Self::StaticBlue | Self::Off => TransactionClass::StaticLighting,
        }
    }

    const fn stable_mode(self) -> Option<StableLightingMode> {
        match self {
            Self::MapPattern => None,
            Self::StaticBlue => Some(StableLightingMode::Static),
            Self::Off => Some(StableLightingMode::Off),
        }
    }

    fn colors(self, target: &BoundTarget) -> Vec<RgbColor> {
        let count = usize::from(target.application_slots.get());
        match self {
            Self::MapPattern => map_colors(target, count),
            Self::StaticBlue => vec![rgb(0, 0, 255); count],
            Self::Off => vec![rgb(0, 0, 0); count],
        }
    }
}

impl LightingStep {
    const fn command(self) -> LightingCommand {
        match self {
            Self::MapPattern => LightingCommand::MapPattern,
            Self::MapCleanup
            | Self::LightingBlue
            | Self::LightingRestore
            | Self::FailedCleanup
            | Self::UnclearCleanup => LightingCommand::StaticBlue,
            Self::LightingOff => LightingCommand::Off,
        }
    }

    fn belongs_to(self, stage: &QualificationStage) -> bool {
        match self {
            Self::MapPattern | Self::MapCleanup => stage.stage_id.ends_with("-map"),
            Self::LightingBlue | Self::LightingOff | Self::LightingRestore => {
                stage.stage_id.ends_with("-lighting")
            }
            Self::FailedCleanup | Self::UnclearCleanup => {
                stage.stage_id.ends_with("-map") || stage.stage_id.ends_with("-lighting")
            }
        }
    }
}

fn map_colors(target: &BoundTarget, count: usize) -> Vec<RgbColor> {
    const PALETTE: [(u8, u8, u8); 8] = [
        (255, 0, 0),
        (255, 128, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 255, 255),
        (0, 0, 255),
        (128, 0, 255),
        (255, 0, 255),
    ];
    let columns = usize::from(target.columns.get());
    let rows = usize::from(target.rows.get());
    (0..count)
        .map(|index| {
            let palette_index = if rows > 1 {
                (index / columns) % PALETTE.len()
            } else {
                index % PALETTE.len()
            };
            let (red, green, blue) = PALETTE[palette_index];
            rgb(red, green, blue)
        })
        .collect()
}

fn rgb(red: u8, green: u8, blue: u8) -> RgbColor {
    RgbColor {
        red: ColorChannel::try_from(red).expect("every u8 is a canonical color channel"),
        green: ColorChannel::try_from(green).expect("every u8 is a canonical color channel"),
        blue: ColorChannel::try_from(blue).expect("every u8 is a canonical color channel"),
    }
}

pub(crate) fn new_transaction_id() -> Result<TransactionId, QualificationRunnerError> {
    let mut identities = KernelRequestIdentitySource;
    let request = identities.next_request_id().map_err(|_| {
        QualificationRunnerError::before_write(
            "Operating-system entropy is unavailable for a unique transaction identity.",
        )
    })?;
    TransactionId::try_from(format!("qualification-{}", request.as_str())).map_err(|_| {
        QualificationRunnerError::before_write(
            "The generated qualification transaction identity is invalid.",
        )
    })
}

fn transaction_deadline() -> Result<MonotonicMs, QualificationRunnerError> {
    let stamp = clock_gettime(ClockId::Monotonic);
    let seconds = u64::try_from(stamp.tv_sec).map_err(|_| {
        QualificationRunnerError::before_write("The system monotonic clock is unavailable.")
    })?;
    let nanos = u64::try_from(stamp.tv_nsec).map_err(|_| {
        QualificationRunnerError::before_write("The system monotonic clock is unavailable.")
    })?;
    let now = seconds
        .checked_mul(1_000)
        .and_then(|value| value.checked_add(nanos / 1_000_000))
        .ok_or_else(|| {
            QualificationRunnerError::before_write("The system monotonic clock overflowed.")
        })?;
    MonotonicMs::try_from(now.saturating_add(TRANSACTION_DEADLINE_MS)).map_err(|_| {
        QualificationRunnerError::before_write("The qualification transaction deadline is invalid.")
    })
}

fn wait_for_terminal<S, I>(
    client: &mut hfx_sdk::HyperFluxClient<S, I>,
    transaction_id: &TransactionId,
    mut result: TransactionResult,
) -> Result<hfx_protocol::TransactionTerminal, QualificationRunnerError>
where
    S: std::io::Read + std::io::Write,
    I: RequestIdentitySource,
{
    let started = Instant::now();
    loop {
        match result {
            TransactionResult::Terminal(terminal) => return Ok(terminal),
            TransactionResult::Unavailable(_) => {
                return Err(QualificationRunnerError::after_submission(
                    "The submitted transaction is unavailable; the request was not repeated.",
                    true,
                ));
            }
            TransactionResult::Progress(_) if started.elapsed() < TRANSACTION_WAIT => {
                thread::sleep(TRANSACTION_POLL);
                result = client
                    .transaction_outcome(transaction_id.clone())
                    .map_err(|_| {
                        QualificationRunnerError::after_submission(
                            "The transaction outcome could not be refreshed; the request was not repeated.",
                            true,
                        )
                    })?;
            }
            TransactionResult::Progress(_) => {
                return Err(QualificationRunnerError::after_submission(
                    "The transaction did not become terminal before its bounded wait expired; the request was not repeated.",
                    true,
                ));
            }
        }
    }
}

fn map_checkpoint(target: &BoundTarget, evidence_refs: Vec<String>) -> StageCheckpoint {
    let pattern = if target.rows.get() > 1 {
        "Confirm that each physical row follows the displayed repeating row palette from top to bottom."
    } else {
        "Confirm that the complete physical LED path follows the repeating red, orange, yellow, green, cyan, blue, violet, and magenta palette in application order."
    };
    StageCheckpoint {
        checkpoint_id: "map-pattern".to_owned(),
        instructions: vec![
            pattern.to_owned(),
            "Exercise the device's ordinary controls once; do not repeat the lighting action."
                .to_owned(),
        ],
        observations: vec![
            visual_observation("Does the physical layout match the requested application pattern?"),
            controls_observation(),
        ],
        evidence_refs,
    }
}

fn blue_checkpoint(evidence_refs: Vec<String>) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "lighting-blue".to_owned(),
        instructions: vec![
            "Confirm that every light on only the selected controller is solid blue.".to_owned(),
            "Exercise the device's ordinary controls once.".to_owned(),
        ],
        observations: vec![
            visual_observation("Is the selected controller completely blue?"),
            controls_observation(),
        ],
        evidence_refs,
    }
}

fn off_checkpoint(evidence_refs: Vec<String>) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "lighting-off".to_owned(),
        instructions: vec![
            "Confirm that every light on the selected controller is unlit while sibling controllers remain unchanged."
                .to_owned(),
            "Exercise the device's ordinary controls once.".to_owned(),
        ],
        observations: vec![
            visual_observation("Is only the selected controller completely unlit?"),
            controls_observation(),
        ],
        evidence_refs,
    }
}

fn restored_checkpoint(evidence_refs: Vec<String>) -> StageCheckpoint {
    StageCheckpoint {
        checkpoint_id: "lighting-restored".to_owned(),
        instructions: vec![
            "Confirm that the selected controller returned to complete solid blue and sibling controllers remain unchanged."
                .to_owned(),
            "Exercise the device's ordinary controls once.".to_owned(),
        ],
        observations: vec![
            visual_observation("Did complete solid blue return on only the selected controller?"),
            controls_observation(),
        ],
        evidence_refs,
    }
}

fn visual_observation(prompt: &str) -> ObservationPrompt {
    ObservationPrompt {
        id: "visual".to_owned(),
        prompt: prompt.to_owned(),
        choices: choices("Matches", "Does not match"),
    }
}

fn controls_observation() -> ObservationPrompt {
    ObservationPrompt {
        id: "controls".to_owned(),
        prompt: "Do the device's ordinary semantic controls still work correctly?".to_owned(),
        choices: choices("Controls work", "A control changed"),
    }
}

fn choices(pass: &str, fail: &str) -> Vec<ObservationChoice> {
    vec![
        ObservationChoice {
            id: "pass".to_owned(),
            label: pass.to_owned(),
            outcome: "pass".to_owned(),
        },
        ObservationChoice {
            id: "fail".to_owned(),
            label: fail.to_owned(),
            outcome: "fail".to_owned(),
        },
        ObservationChoice {
            id: "unclear".to_owned(),
            label: "Unclear".to_owned(),
            outcome: "unclear".to_owned(),
        },
    ]
}

#[cfg(test)]
mod tests;
