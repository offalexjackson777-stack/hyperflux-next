// SPDX-License-Identifier: GPL-2.0-only

use super::*;

fn qualification_fixture(name: &str) -> QualificationView {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../apps/device-qualification/tests/fixtures")
        .join(name);
    serde_json::from_slice(&fs::read(fixture).expect("fixture reads")).expect("fixture parses")
}

fn pending_transaction() -> crate::qualification_runner::PendingLightingTransaction {
    crate::qualification_runner::PendingLightingTransaction {
        stage_id: "mouse-test-lighting".to_owned(),
        transaction_id: hfx_domain::TransactionId::try_from("qualification-test")
            .expect("transaction id"),
        step: crate::qualification_runner::LightingStep::LightingBlue,
        prior_progress: None,
    }
}

#[test]
fn parser_accepts_only_bounded_http11_shapes() {
    let request =
        parse_request("GET /v1/qualification/view HTTP/1.1\r\nHost: 127.0.0.1:47821\r\n\r\n")
            .expect("request is valid");
    assert_eq!(request.method, "GET");
    assert_eq!(request.path, "/v1/qualification/view");
    let post = parse_request(
            "POST /v1/qualification/actions/run HTTP/1.1\r\nHost: 127.0.0.1:47821\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n",
        )
        .expect("post headers are valid");
    assert_eq!(post.content_length, 2);
    assert!(parse_request("GET / HTTP/1.0\r\nHost: localhost\r\n\r\n").is_err());
}

#[test]
fn local_authority_checks_reject_remote_origins() {
    assert!(host_is_local(Some("127.0.0.1:47821"), 47_821));
    assert!(host_is_local(Some("localhost:47821"), 47_821));
    assert!(!host_is_local(Some("example.com"), 47_821));
    assert!(origin_is_local(None, 47_821));
    assert!(!origin_is_local(Some("https://example.com"), 47_821));
}

#[test]
fn asset_resolution_stays_beneath_the_canonical_root() {
    let root = std::env::temp_dir().join(format!(
        "hyperflux-qualification-assets-{}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("assets")).expect("asset directory creates");
    fs::write(root.join("index.html"), "index").expect("index writes");
    fs::write(root.join("assets/app.js"), "app").expect("asset writes");
    let canonical = canonical_asset_root(&root).expect("root is valid");
    assert_eq!(
        resolve_asset(&canonical, "/assets/app.js"),
        Some(canonical.join("assets/app.js"))
    );
    assert_eq!(resolve_asset(&canonical, "/../secret"), None);
    fs::remove_dir_all(&root).expect("test root removes");
}

#[test]
fn failed_final_commit_keeps_the_pending_transaction_for_reconciliation() {
    let parent = std::env::temp_dir().join(format!(
        "hyperflux-qualification-invalid-state-parent-{}",
        std::process::id()
    ));
    let _ = fs::remove_file(&parent);
    fs::write(&parent, "not a directory").expect("blocking parent writes");
    let pending = pending_transaction();
    let mut state = ServerState {
        pending_transaction: Some(pending.clone()),
        ..ServerState::default()
    };
    assert!(commit_completed_state(&parent.join("session.json"), &mut state).is_err());
    assert_eq!(state.pending_transaction, Some(pending));
    fs::remove_file(parent).expect("blocking parent removes");
}

#[test]
fn failed_unsent_restore_keeps_the_pending_gate_in_memory() {
    let pending = pending_transaction();
    let mut state = ServerState {
        pending_transaction: Some(pending.clone()),
        results: BTreeMap::from([(
            pending.stage_id.clone(),
            StageProgress {
                status: StageStatus::Running,
                result: None,
                checkpoint: None,
            },
        )]),
        ..ServerState::default()
    };
    restore_prior_progress(&mut state, &pending.stage_id, None);
    restore_pending_progress(&mut state, pending.clone());
    assert_eq!(state.pending_transaction, Some(pending.clone()));
    assert_eq!(
        state.results[&pending.stage_id].status,
        StageStatus::Running
    );
}

#[test]
fn identity_change_invalidates_claims_but_preserves_uncertain_side_effects() {
    let pending = pending_transaction();
    let mut state = ServerState {
        identity_fingerprint: Some([1_u8; 32]),
        run_id: Some("run-old-generation".to_owned()),
        results: BTreeMap::from([(
            "mouse-test-profile".to_owned(),
            StageProgress {
                status: StageStatus::Passed,
                result: None,
                checkpoint: None,
            },
        )]),
        pending_transaction: Some(pending.clone()),
        hardware_write_executed: true,
        ..ServerState::default()
    };
    assert!(refresh_identity_binding(
        &mut state,
        [2_u8; 32],
        &qualification_fixture("rust-qualified-mouse.json"),
    ));
    assert_eq!(state.results.len(), 1);
    assert_eq!(
        state.results["mouse-test-lighting"].status,
        StageStatus::Running
    );
    assert!(state.run_id.is_none());
    assert_eq!(state.pending_transaction, Some(pending));
    assert!(state.hardware_write_executed);
}

#[test]
fn identity_fingerprint_ignores_inventory_order() {
    let mut view = qualification_fixture("rust-qualified-pair.json");
    let mut second = view.receivers[0].clone();
    second.receiver_id = "receiver-second".to_owned();
    second.generation_id = 11;
    view.receivers.push(second);
    let expected = identity_fingerprint(&view).expect("fingerprint computes");
    view.receivers.reverse();
    for receiver in &mut view.receivers {
        receiver.devices.reverse();
    }
    assert_eq!(
        identity_fingerprint(&view).expect("reordered fingerprint computes"),
        expected
    );
}

#[test]
fn lifecycle_binding_accepts_only_its_expected_profile_bound_generation() {
    let mut view = qualification_fixture("rust-qualified-pair.json");
    for group in view.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
        for stage in &mut group.stages {
            stage.status = if group.group_id == "lifecycle" {
                StageStatus::Ready
            } else {
                StageStatus::Passed
            };
        }
    }
    let lifecycle_stage = view.plans[0]
        .groups
        .iter()
        .find(|group| group.group_id == "lifecycle")
        .expect("lifecycle group")
        .stages[0]
        .clone();
    let mut run = capture_lifecycle_run(&view, &lifecycle_stage).expect("run captures");
    run.active = Some(crate::qualification_lifecycle::LifecycleStageState {
        stage_id: lifecycle_stage.stage_id.clone(),
        device_id: view.plans[0].device_id.clone(),
        device_kind: "mouse".to_owned(),
        phase: crate::qualification_lifecycle::LifecyclePhase::ReceiverReturn,
        baseline_generation_id: 7,
        evidence_refs: vec!["receiver-generation:7".to_owned()],
        suspend_stamp: None,
    });
    run.validate().expect("run is valid");
    let mut session = ServerState {
        identity_fingerprint: Some(identity_fingerprint(&view).expect("fingerprint")),
        run_id: Some("run-generation-7".to_owned()),
        results: BTreeMap::from([(
            lifecycle_stage.stage_id.clone(),
            StageProgress {
                status: StageStatus::AwaitingObservation,
                result: None,
                checkpoint: None,
            },
        )]),
        lifecycle_run: Some(run),
        ..ServerState::default()
    };

    view.receivers[0].generation_id = 8;
    let renewed = identity_fingerprint(&view).expect("renewed fingerprint");
    assert!(refresh_identity_binding(&mut session, renewed, &view));
    assert!(session.lifecycle_run.is_some());
    assert!(!session.results.is_empty());

    view.receivers[0].devices[0]
        .profile
        .as_mut()
        .expect("profile exists")
        .digest = "0".repeat(64);
    let foreign = identity_fingerprint(&view).expect("foreign fingerprint");
    assert!(refresh_identity_binding(&mut session, foreign, &view));
    assert!(session.lifecycle_run.is_none());
    assert!(session.results.is_empty());
    assert!(session.run_id.is_none());
}

#[test]
fn server_upgrades_a_measured_terminal_resume_failure_once() {
    let mut view = qualification_fixture("rust-qualified-pair.json");
    for group in view.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
        for stage in &mut group.stages {
            stage.status = if group.group_id == "lifecycle" {
                StageStatus::Ready
            } else {
                StageStatus::Passed
            };
        }
    }
    let stage = view
        .plans
        .iter()
        .find(|plan| plan.device_id == "keyboard-test")
        .expect("keyboard plan exists")
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-lifecycle"))
        .expect("keyboard lifecycle exists")
        .clone();
    let mut run = capture_lifecycle_run(&view, &stage).expect("run captures");
    run.active = None;
    let mut session = ServerState {
        results: BTreeMap::from([(
            stage.stage_id.clone(),
            StageProgress {
                status: StageStatus::Failed,
                result: Some(StageResult {
                    summary: "A watched lifecycle result did not match.".to_owned(),
                    completed_at: "2026-07-23T11:50:45Z".to_owned(),
                    evidence_refs: vec![
                        "system-suspend:authorized".to_owned(),
                        "system-suspend-gap-ms:9496".to_owned(),
                    ],
                }),
                checkpoint: None,
            },
        )]),
        lifecycle_run: Some(run),
        ..ServerState::default()
    };

    assert!(recover_retryable_resume_state(&mut session).expect("state upgrades"));
    assert!(!recover_retryable_resume_state(&mut session).expect("upgrade is idempotent"));
    let progress = &session.results[&stage.stage_id];
    assert_eq!(progress.status, StageStatus::Failed);
    assert_eq!(
        progress
            .checkpoint
            .as_ref()
            .expect("retry checkpoint exists")
            .checkpoint_id,
        "system-suspend-authorization"
    );
}

#[test]
fn transient_telemetry_gap_remains_retryable() {
    let mut view = qualification_fixture("rust-qualified-pair.json");
    let stage = view
        .plans
        .iter()
        .find(|plan| plan.device_id == "keyboard-test")
        .expect("keyboard plan exists")
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-telemetry"))
        .expect("telemetry stage exists")
        .clone();
    let keyboard = view
        .receivers
        .iter_mut()
        .flat_map(|receiver| &mut receiver.devices)
        .find(|device| device.device_id == "keyboard-test")
        .expect("keyboard device exists");
    keyboard.battery.availability = BatteryAvailability::Unknown;
    keyboard.battery.percentage = None;
    let input = ActionInput {
        view_revision: 1,
        confirmation: None,
        observations: BTreeMap::from([("controls".to_owned(), "pass".to_owned())]),
    };
    let mut session = ServerState::default();

    let failure = record_read_only_result(&view, &stage, &input, &mut session)
        .expect_err("missing telemetry remains retryable");
    assert!(matches!(failure.status, HttpStatus::Conflict));
    assert!(!failure.state_changed);
    assert!(failure.message.contains("refresh, and retry"));
    assert!(session.results.is_empty());

    let keyboard = view
        .receivers
        .iter_mut()
        .flat_map(|receiver| &mut receiver.devices)
        .find(|device| device.device_id == "keyboard-test")
        .expect("keyboard device exists");
    keyboard.battery.availability = BatteryAvailability::Reported;
    keyboard.battery.percentage = Some(20);
    assert!(record_read_only_result(&view, &stage, &input, &mut session).is_ok());
    assert_eq!(session.results[&stage.stage_id].status, StageStatus::Passed);
}

#[test]
fn malformed_read_only_observation_does_not_poison_the_run() {
    let view = qualification_fixture("rust-qualified-pair.json");
    let stage = view
        .plans
        .iter()
        .find(|plan| plan.device_id == "keyboard-test")
        .expect("keyboard plan exists")
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-telemetry"))
        .expect("telemetry stage exists")
        .clone();
    let input = ActionInput {
        view_revision: 1,
        confirmation: None,
        observations: BTreeMap::new(),
    };
    let mut session = ServerState::default();

    let failure = record_read_only_result(&view, &stage, &input, &mut session)
        .expect_err("missing answer is rejected");
    assert!(matches!(failure.status, HttpStatus::BadRequest));
    assert!(!failure.state_changed);
    assert!(session.results.is_empty());
}

#[test]
fn watched_answers_are_exact_and_reduce_to_one_verdict() {
    let stage = QualificationStage {
        stage_id: "mouse-test-lighting".to_owned(),
        title: "Lighting".to_owned(),
        description: "Lighting".to_owned(),
        kind: crate::StageKind::WatchedObservation,
        risk: RiskLevel::LightingWrite,
        status: StageStatus::AwaitingObservation,
        capabilities: Vec::new(),
        instructions: Vec::new(),
        observations: vec![crate::ObservationPrompt {
            id: "visual".to_owned(),
            prompt: "Does it match?".to_owned(),
            choices: vec![
                crate::ObservationChoice {
                    id: "pass".to_owned(),
                    label: "Matches".to_owned(),
                    outcome: "pass".to_owned(),
                },
                crate::ObservationChoice {
                    id: "fail".to_owned(),
                    label: "Does not match".to_owned(),
                    outcome: "fail".to_owned(),
                },
                crate::ObservationChoice {
                    id: "unclear".to_owned(),
                    label: "Unclear".to_owned(),
                    outcome: "unclear".to_owned(),
                },
            ],
        }],
        action_id: Some("run:mouse-test-lighting".to_owned()),
        result: None,
    };
    let input = |answer: &str| ActionInput {
        view_revision: 1,
        confirmation: None,
        observations: BTreeMap::from([("visual".to_owned(), answer.to_owned())]),
    };
    assert_eq!(
        observation_verdict(&stage, &input("pass")),
        Ok(ObservationVerdict::Pass)
    );
    assert_eq!(
        observation_verdict(&stage, &input("fail")),
        Ok(ObservationVerdict::Fail)
    );
    assert_eq!(
        observation_verdict(&stage, &input("unclear")),
        Ok(ObservationVerdict::Unclear)
    );
    assert!(observation_verdict(&stage, &input("typo")).is_err());
    assert!(
        observation_verdict(
            &stage,
            &ActionInput {
                view_revision: 1,
                confirmation: None,
                observations: BTreeMap::new(),
            }
        )
        .is_err()
    );
}
