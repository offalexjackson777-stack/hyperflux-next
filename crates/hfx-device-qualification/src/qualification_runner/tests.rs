// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_domain::{GenerationId, LedCount, LogicalDeviceId, ProfileDigest, ProfileId, ReceiverId};
use std::collections::VecDeque;

struct FakeTransport {
    commands: Vec<LightingCommand>,
    transactions: Vec<TransactionId>,
    receipts: VecDeque<Result<DeliveryReceipt, QualificationRunnerError>>,
}

impl FakeTransport {
    fn successful() -> Self {
        Self {
            commands: Vec::new(),
            transactions: Vec::new(),
            receipts: (1..=8)
                .map(|index| {
                    Ok(DeliveryReceipt {
                        evidence_ref: format!("transaction:{index}"),
                        hardware_write_executed: true,
                    })
                })
                .collect(),
        }
    }
}

impl LightingTransport for FakeTransport {
    fn deliver(
        &mut self,
        _target: &BoundTarget,
        command: LightingCommand,
        transaction_id: &TransactionId,
    ) -> Result<DeliveryReceipt, QualificationRunnerError> {
        self.commands.push(command);
        self.transactions.push(transaction_id.clone());
        self.receipts.pop_front().expect("fake receipt exists")
    }
}

fn binding(id: &str, digest: char) -> hfx_protocol::ProfileBindingView {
    hfx_protocol::ProfileBindingView {
        profile_id: ProfileId::try_from(id).expect("profile id"),
        profile_digest: ProfileDigest::try_from(digest.to_string().repeat(64))
            .expect("profile digest"),
    }
}

fn target(rows: u16, columns: u16) -> BoundTarget {
    BoundTarget {
        receiver_id: ReceiverId::try_from("receiver-test").expect("receiver id"),
        generation_id: GenerationId::try_from(7_u64).expect("generation"),
        device_id: LogicalDeviceId::try_from("device-test").expect("device id"),
        receiver_profile: binding("receiver.test", 'a'),
        device_profile: binding("child.test", 'b'),
        application_slots: LedCount::try_from(rows * columns).expect("slots"),
        rows: LedCount::try_from(rows).expect("rows"),
        columns: LedCount::try_from(columns).expect("columns"),
    }
}

fn stage(id: &str) -> QualificationStage {
    QualificationStage {
        stage_id: id.to_owned(),
        title: "Test".to_owned(),
        description: "Test".to_owned(),
        kind: crate::StageKind::WatchedObservation,
        risk: crate::RiskLevel::LightingWrite,
        status: StageStatus::Ready,
        capabilities: Vec::new(),
        instructions: Vec::new(),
        observations: Vec::new(),
        action_id: Some(format!("run:{id}")),
        result: None,
    }
}

fn execute_prepared(
    transport: &mut FakeTransport,
    action: PreparedLightingAction,
    sequence: usize,
) -> RunnerTransition {
    match action {
        PreparedLightingAction::Record(transition) => transition,
        PreparedLightingAction::Write(prepared) => execute_lighting_write(
            transport,
            &prepared,
            &TransactionId::try_from(format!("transaction-{sequence}")).expect("transaction id"),
        )
        .expect("fake transaction succeeds"),
    }
}

#[test]
fn lighting_sequence_never_repeats_a_completed_request() {
    let target = target(1, 13);
    let stage = stage("mouse-00cd-lighting");
    let mut transport = FakeTransport::successful();
    let first = execute_prepared(
        &mut transport,
        prepare_lighting_stage_for_target(target.clone(), &stage).expect("blue step prepares"),
        1,
    );
    assert_eq!(transport.commands, vec![LightingCommand::StaticBlue]);
    assert_eq!(
        first
            .progress
            .checkpoint
            .as_ref()
            .expect("checkpoint")
            .checkpoint_id,
        "lighting-blue"
    );

    let second = execute_prepared(
        &mut transport,
        prepare_lighting_checkpoint_for_target(
            target.clone(),
            &stage,
            &first.progress,
            ObservationVerdict::Pass,
        )
        .expect("off step prepares"),
        2,
    );
    assert_eq!(
        transport.commands,
        vec![LightingCommand::StaticBlue, LightingCommand::Off]
    );

    let third = execute_prepared(
        &mut transport,
        prepare_lighting_checkpoint_for_target(
            target.clone(),
            &stage,
            &second.progress,
            ObservationVerdict::Pass,
        )
        .expect("restore step prepares"),
        3,
    );
    let done = execute_prepared(
        &mut transport,
        prepare_lighting_checkpoint_for_target(
            target,
            &stage,
            &third.progress,
            ObservationVerdict::Pass,
        )
        .expect("final observation prepares"),
        4,
    );
    assert_eq!(
        transport.commands,
        vec![
            LightingCommand::StaticBlue,
            LightingCommand::Off,
            LightingCommand::StaticBlue
        ]
    );
    assert_eq!(
        transport
            .transactions
            .iter()
            .map(TransactionId::as_str)
            .collect::<Vec<_>>(),
        vec!["transaction-1", "transaction-2", "transaction-3"]
    );
    assert_eq!(done.progress.status, StageStatus::Passed);
    assert_eq!(
        done.progress
            .result
            .as_ref()
            .expect("result")
            .evidence_refs
            .len(),
        3
    );
}

#[test]
fn failed_observation_performs_one_cleanup_without_repeating_the_test_frame() {
    let target = target(6, 17);
    let stage = stage("keyboard-0296-map");
    let mut transport = FakeTransport::successful();
    let first = execute_prepared(
        &mut transport,
        prepare_lighting_stage_for_target(target.clone(), &stage).expect("pattern step prepares"),
        1,
    );
    let done = execute_prepared(
        &mut transport,
        prepare_lighting_checkpoint_for_target(
            target,
            &stage,
            &first.progress,
            ObservationVerdict::Fail,
        )
        .expect("cleanup prepares"),
        2,
    );
    assert_eq!(
        transport.commands,
        vec![LightingCommand::MapPattern, LightingCommand::StaticBlue]
    );
    assert_eq!(done.progress.status, StageStatus::Failed);
}

#[test]
fn map_pattern_uses_rows_for_matrices_and_slots_for_linear_devices() {
    let matrix = target(6, 17);
    let matrix_colors = LightingCommand::MapPattern.colors(&matrix);
    assert_eq!(matrix_colors[0], matrix_colors[16]);
    assert_ne!(matrix_colors[0], matrix_colors[17]);

    let linear = target(1, 13);
    let linear_colors = LightingCommand::MapPattern.colors(&linear);
    assert_ne!(linear_colors[0], linear_colors[1]);
    assert_eq!(linear_colors[0], linear_colors[8]);
}

#[test]
fn uncertain_submission_forbids_retry_without_claiming_confirmed_execution() {
    let error = QualificationRunnerError::after_submission("uncertain", false);
    assert!(error.retry_forbidden());
    assert!(!error.hardware_write_executed());
    let prewrite = QualificationRunnerError::before_write("safe to retry");
    assert!(!prewrite.retry_forbidden());
    assert!(!prewrite.hardware_write_executed());
}
