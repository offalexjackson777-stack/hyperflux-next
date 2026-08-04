// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_domain::{
    DroppedEventCount, EventKind, GenerationId, LogicalDeviceId, ProjectionRevision, ReceiverId,
    SequenceNumber, StreamEpoch, StreamId, SubscriptionId, TransactionId,
};
use hfx_protocol::{BridgeEvent, EventCursor};
use std::path::PathBuf;

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../apps/device-qualification/tests/fixtures")
        .join(name)
}

fn fixture_view(name: &str, prerequisites_passed: bool) -> QualificationView {
    let mut view: QualificationView =
        serde_json::from_slice(&fs::read(fixture_path(name)).expect("qualification fixture reads"))
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
    let receiver_id = ReceiverId::try_from(run.receiver.receiver_id.as_str()).expect("receiver");
    let baseline = GenerationId::try_from(run.receiver.generation_id).expect("generation");
    let renewed = GenerationId::try_from(generation_id).expect("generation");
    let mut sequence = 0_u64;
    let mut events = Vec::new();
    let mut push =
        |kind: EventKind, generation: GenerationId, device_id: Option<&str>, transaction: bool| {
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
        prepare_lifecycle_checkpoint(run, stage, progress, ObservationVerdict::Pass, &departure)
            .expect("departure records"),
    )
}

#[test]
fn unavailable_event_anchor_restarts_only_the_active_lifecycle_stage() {
    let (run, _, _) = start_stage("keyboard-test");
    let device_id = run
        .active
        .as_ref()
        .expect("stage is active")
        .device_id
        .clone();
    let events = test_events(&run, &device_id, run.receiver.generation_id);

    let mut current = run.clone();
    assert_eq!(
        reset_unavailable_event_anchor(&mut current, Some(&events)),
        None
    );
    assert!(current.active.is_some());

    let mut replaced_stream = events;
    replaced_stream.next_cursor.stream_id =
        StreamId::try_from("replacement-lifecycle-stream").expect("stream id");
    let stage_id = run
        .active
        .as_ref()
        .expect("stage is active")
        .stage_id
        .clone();
    let mut stale = run;
    assert_eq!(
        reset_unavailable_event_anchor(&mut stale, Some(&replaced_stream)),
        Some(stage_id)
    );
    assert!(stale.active.is_none());
    stale.validate().expect("remaining campaign stays valid");
}

#[test]
fn campaign_requires_every_non_lifecycle_prerequisite() {
    let view = fixture_view("rust-qualified-pair.json", false);
    let stage = lifecycle_stage(&view, "mouse-test");
    let error = capture_lifecycle_run(&view, &stage).expect_err("prerequisites must gate");
    assert!(error.to_string().contains("identity and lighting"));
}

#[test]
fn retained_route_departure_requires_the_exact_profile_policy() {
    let (mut run, stage, departure) = start_stage("mouse-test");
    let mouse_profile = profile_by_id("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("qualified mouse profile exists");
    let expected = run
        .receiver
        .devices
        .iter_mut()
        .find(|device| device.device_id == "mouse-test")
        .expect("mouse exists");
    expected.profile = Some(ProfileBindingView {
        id: mouse_profile.id.to_owned(),
        digest: mouse_profile.runtime_sha256.to_owned(),
    });
    let retained_prompt = device_departure_checkpoint(&run);
    assert!(
        retained_prompt
            .instructions
            .iter()
            .any(|value| value.contains("receiver route may remain listed"))
    );
    assert_eq!(
        retained_prompt.observations[0].prompt,
        "Is the selected controller physically powered off and unlit?"
    );
    let retained = observation(
        &run,
        "mouse-test",
        run.receiver.generation_id,
        InventoryAvailability::Available,
        true,
    );
    let (accepted, transition) = unwrap_record(
        prepare_lifecycle_checkpoint(
            run,
            &stage,
            &departure.progress,
            ObservationVerdict::Pass,
            &retained,
        )
        .expect("watched retained-route departure records"),
    );
    let evidence = &accepted
        .active
        .as_ref()
        .expect("stage remains active")
        .evidence_refs;
    assert!(
        evidence
            .iter()
            .any(|value| value == "device-departure:watched-physical-retained-route")
    );
    assert!(
        evidence
            .last()
            .is_some_and(|value| value.starts_with("bridge-cursor-sha256:"))
    );
    assert_eq!(
        transition
            .progress
            .checkpoint
            .as_ref()
            .expect("return checkpoint")
            .checkpoint_id,
        "device-return"
    );

    let (mut run, stage, departure) = start_stage("mouse-test");
    let keyboard_profile = profile_by_id("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("qualified keyboard profile exists");
    let expected = run
        .receiver
        .devices
        .iter_mut()
        .find(|device| device.device_id == "mouse-test")
        .expect("mouse exists");
    expected.profile = Some(ProfileBindingView {
        id: keyboard_profile.id.to_owned(),
        digest: keyboard_profile.runtime_sha256.to_owned(),
    });
    let retained = observation(
        &run,
        "mouse-test",
        run.receiver.generation_id,
        InventoryAvailability::Available,
        true,
    );
    let Err(error) = prepare_lifecycle_checkpoint(
        run,
        &stage,
        &departure.progress,
        ObservationVerdict::Pass,
        &retained,
    ) else {
        panic!("a profile without retained-route policy stays strict");
    };
    assert!(error.to_string().contains("has not departed cleanly"));
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
    let (blocked_run, blocked) = reconcile_system_resume(suspended.clone(), &ready, &reboot_stamp)
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
