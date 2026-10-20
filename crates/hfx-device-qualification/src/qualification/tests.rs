// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_domain::{
    BatteryPercent, DroppedEventCount, EvidenceConfidence, FreshnessState, GenerationId,
    InventoryAvailability, PairingState, PresenceState, ProductId, ProjectionRevision,
    ProtocolVersion, ReceiverId, RestoreState, SequenceNumber, StreamEpoch, SubscriptionId,
    SupportLevel, TelemetryAvailability,
};
use hfx_protocol::{
    BatteryObservation, DeviceInventoryView, EventBatch, EventCursor, IntegrationView,
};
use std::path::PathBuf;

fn catalog() -> RuntimeProfileCatalog {
    RuntimeProfileCatalog::load().expect("generated profile catalog is valid")
}

fn integration() -> BridgeIntegration {
    let catalog = catalog();
    let receiver_profile = catalog
        .iter()
        .find(|profile| profile.profile_id.as_str().starts_with("receiver."))
        .expect("receiver profile exists");
    let child_profile = catalog
        .child(ProductId::try_from(0x00cd_u16).expect("PID is valid"))
        .expect("mouse profile exists");
    let cursor = EventCursor {
        stream_id: hfx_domain::StreamId::try_from("qualification-stream")
            .expect("stream id is valid"),
        stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
        projection_revision: ProjectionRevision::try_from(1_u32).expect("projection is valid"),
        sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
    };
    BridgeIntegration {
        protocol_version: ProtocolVersion::try_from(5_u16).expect("protocol is valid"),
        view: IntegrationView {
            cursor: cursor.clone(),
            receivers: vec![IntegrationReceiverView {
                receiver_id: ReceiverId::try_from("receiver-test").expect("id is valid"),
                generation_id: GenerationId::try_from(7_u64).expect("generation is valid"),
                profile: Some(hfx_protocol::ProfileBindingView {
                    profile_id: receiver_profile.profile_id.clone(),
                    profile_digest: receiver_profile.runtime_digest.clone(),
                }),
                model_name: None,
                lifecycle: ReceiverLifecycleState::Active,
                stable_restore_enabled: false,
                restore_state: RestoreState::Idle,
                inventory: vec![DeviceInventoryView {
                    device_id: hfx_domain::LogicalDeviceId::try_from("mouse-test")
                        .expect("device id is valid"),
                    device_kind: DeviceKind::Mouse,
                    product_id: ProductId::try_from(0x00cd_u16).expect("PID is valid"),
                    profile: Some(hfx_protocol::ProfileBindingView {
                        profile_id: child_profile.profile_id.clone(),
                        profile_digest: child_profile.runtime_digest.clone(),
                    }),
                    model_name: None,
                    pairing: PairingState::Paired,
                    presence: PresenceState::Available,
                    availability: InventoryAvailability::Available,
                    support_level: SupportLevel::ProductionQualified,
                    endpoints: Vec::new(),
                    battery: BatteryObservation {
                        availability: TelemetryAvailability::Reported,
                        percentage: Some(
                            BatteryPercent::try_from(80_u8).expect("battery is valid"),
                        ),
                        charge: None,
                        freshness: FreshnessState::Fresh,
                        confidence: EvidenceConfidence::Observed,
                        observed_at_ms: None,
                    },
                    capabilities: child_profile
                        .capabilities
                        .iter()
                        .map(|capability| capability.id.clone())
                        .collect(),
                }],
                controllers: Vec::new(),
            }],
        },
        events: EventBatch {
            subscription_id: SubscriptionId::try_from("qualification-subscription")
                .expect("subscription is valid"),
            next_cursor: cursor,
            events: Vec::new(),
            oldest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            latest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            dropped_events: DroppedEventCount::try_from(0_u64).expect("drop count is valid"),
            cursor_gap: false,
            has_more: false,
        },
    }
}

fn paired_integration() -> BridgeIntegration {
    let catalog = catalog();
    let keyboard_profile = catalog
        .child(ProductId::try_from(0x0296_u16).expect("PID is valid"))
        .expect("keyboard profile exists");
    let mut integration = integration();
    integration.view.receivers[0]
        .inventory
        .push(DeviceInventoryView {
            device_id: hfx_domain::LogicalDeviceId::try_from("keyboard-test")
                .expect("device id is valid"),
            device_kind: DeviceKind::Keyboard,
            product_id: ProductId::try_from(0x0296_u16).expect("PID is valid"),
            profile: Some(hfx_protocol::ProfileBindingView {
                profile_id: keyboard_profile.profile_id.clone(),
                profile_digest: keyboard_profile.runtime_digest.clone(),
            }),
            model_name: None,
            pairing: PairingState::Paired,
            presence: PresenceState::Available,
            availability: InventoryAvailability::Available,
            support_level: SupportLevel::ProductionQualified,
            endpoints: Vec::new(),
            battery: BatteryObservation {
                availability: TelemetryAvailability::Reported,
                percentage: Some(BatteryPercent::try_from(67_u8).expect("battery is valid")),
                charge: None,
                freshness: FreshnessState::Fresh,
                confidence: EvidenceConfidence::Observed,
                observed_at_ms: None,
            },
            capabilities: keyboard_profile
                .capabilities
                .iter()
                .map(|capability| capability.id.clone())
                .collect(),
        });
    integration
}

fn system() -> SystemSnapshot {
    SystemSnapshot {
        package_version: PRODUCT_VERSION.to_owned(),
        full_component: crate::FullComponentSnapshot::ready(PRODUCT_VERSION),
        installed_module_identity: Some("same".to_owned()),
        loaded_module_identity: Some("same".to_owned()),
        openrazer_driver: crate::OpenRazerDriverSnapshot {
            modules: vec![crate::KernelModuleIdentitySnapshot {
                module_name: "razerkbd".to_owned(),
                installed_identity: Some("same".to_owned()),
                loaded: false,
                loaded_identity: None,
                native_transport_capability: Some("razer-feature-report-v1".to_owned()),
            }],
        },
        openrazer_runtime: crate::OpenRazerRuntimeSnapshot {
            state: crate::OpenRazerRuntimeState::NotInstalled,
            observed_upstream_version: None,
        },
        openrazer_access: crate::OpenRazerAccessState::NotInstalled,
        openrazer_published_devices: None,
        service_state: ServiceState::Active,
        bridge: None,
        legacy_v2_stack_detected: false,
        supported_receiver_present: false,
        receiver_interface_drivers: Vec::new(),
        retired_driver_registrations: Vec::new(),
        missing_openrazer_key_event_links: Vec::new(),
        broken_openrazer_event_links: Vec::new(),
    }
}

fn contract_fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../apps/device-qualification/tests/fixtures/rust-qualified-mouse.json")
}

fn paired_contract_fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../apps/device-qualification/tests/fixtures/rust-qualified-pair.json")
}

fn stage_by_suffix<'a>(view: &'a QualificationView, suffix: &str) -> &'a QualificationStage {
    view.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with(suffix))
        .expect("expected qualification stage exists")
}

fn action_for_stage<'a>(
    view: &'a QualificationView,
    stage: &QualificationStage,
) -> &'a CompanionAction {
    let action_id = stage.action_id.as_deref().expect("stage has an action id");
    view.actions
        .iter()
        .find(|action| action.id == action_id)
        .expect("expected qualification action exists")
}

#[test]
fn projection_uses_exact_profile_identity_and_no_simulation() {
    let integration = integration();
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.companion.state, CompanionState::Ready);
    assert!(!view.companion.simulation);
    assert_eq!(view.receivers.len(), 1);
    assert_eq!(
        view.receivers[0].model_name.as_deref(),
        Some("Razer HyperFlux V2 Receiver")
    );
    assert_eq!(view.receivers[0].devices[0].product_id, 0x00cd);
    assert_eq!(
        view.receivers[0].devices[0].model_name.as_deref(),
        Some("Razer Basilisk V3 Pro 35K")
    );
    assert_eq!(
        view.receivers[0].devices[0]
            .profile
            .as_ref()
            .expect("profile exists")
            .id,
        "child.razer.basilisk-v3-pro-35k.00cd"
    );
    assert_eq!(view.plans.len(), 1);
    assert!(
        view.actions
            .iter()
            .all(|action| action.risk == RiskLevel::ReadOnly)
    );
}

#[test]
fn bridge_reported_model_names_take_precedence_over_the_catalog() {
    let mut integration = integration();
    let receiver = &mut integration.view.receivers[0];
    receiver.model_name =
        Some(hfx_domain::ModelName::try_from("Bridge Receiver").expect("name is valid"));
    receiver.inventory[0].model_name =
        Some(hfx_domain::ModelName::try_from("Bridge Mouse").expect("name is valid"));
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities::default(),
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(
        view.receivers[0].model_name.as_deref(),
        Some("Bridge Receiver")
    );
    assert_eq!(
        view.receivers[0].devices[0].model_name.as_deref(),
        Some("Bridge Mouse")
    );
}

#[test]
fn generation_ids_use_exact_decimal_strings_across_the_browser_boundary() {
    let mut integration = integration();
    integration.view.receivers[0].generation_id =
        GenerationId::try_from(u64::MAX).expect("maximum generation is valid");
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );

    let encoded = serde_json::to_value(&view).expect("view serializes");
    assert_eq!(
        encoded["receivers"][0]["generation_id"],
        serde_json::Value::String(u64::MAX.to_string())
    );
    assert_eq!(
        encoded["plans"][0]["generation_id"],
        serde_json::Value::String(u64::MAX.to_string())
    );
    let decoded: QualificationView = serde_json::from_value(encoded).expect("view deserializes");
    assert_eq!(decoded.receivers[0].generation_id, u64::MAX);
    assert_eq!(decoded.plans[0].generation_id, u64::MAX);
}

#[test]
fn rust_contract_fixture_matches_the_current_projection() {
    let integration = integration();
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let rendered = serde_json::to_string_pretty(&view).expect("view serializes") + "\n";
    let fixture = contract_fixture_path();
    if std::env::var_os("HFX_UPDATE_QUALIFICATION_FIXTURE").is_some() {
        std::fs::write(&fixture, &rendered).expect("fixture updates");
    }
    let expected = std::fs::read_to_string(&fixture).expect(
        "qualification fixture exists; set HFX_UPDATE_QUALIFICATION_FIXTURE=1 to create it",
    );
    assert_eq!(expected, rendered, "qualification API fixture is stale");
}

#[test]
fn rust_paired_contract_fixture_matches_the_current_projection() {
    let integration = paired_integration();
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let rendered = serde_json::to_string_pretty(&view).expect("view serializes") + "\n";
    let fixture = paired_contract_fixture_path();
    if std::env::var_os("HFX_UPDATE_QUALIFICATION_FIXTURE").is_some() {
        std::fs::write(&fixture, &rendered).expect("fixture updates");
    }
    let expected = std::fs::read_to_string(&fixture).expect(
        "paired qualification fixture exists; set HFX_UPDATE_QUALIFICATION_FIXTURE=1 to create it",
    );
    assert_eq!(
        expected, rendered,
        "paired qualification API fixture is stale"
    );
    assert_eq!(view.receivers[0].devices.len(), 2);
    assert_eq!(view.plans.len(), 2);
}

#[test]
fn incompatible_loaded_module_blocks_bridge_claims() {
    let mut system = system();
    system.loaded_module_identity = Some("old".to_owned());
    let view = build_qualification_view(
        &system,
        Some(&integration()),
        &catalog(),
        RunnerCapabilities::default(),
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.companion.state, CompanionState::DriverPending);
}

#[test]
fn legacy_v2_installation_is_not_reported_as_a_broken_next_bridge() {
    let mut system = system();
    system.service_state = ServiceState::Unavailable;
    system.bridge = None;
    system.legacy_v2_stack_detected = true;
    let view = build_qualification_view(
        &system,
        None,
        &catalog(),
        RunnerCapabilities::default(),
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.companion.state, CompanionState::LegacyV2Detected);
}

#[test]
fn a_ready_next_installation_takes_precedence_over_legacy_detection() {
    let mut system = system();
    system.legacy_v2_stack_detected = true;
    let view = build_qualification_view(
        &system,
        Some(&integration()),
        &catalog(),
        RunnerCapabilities::default(),
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.companion.state, CompanionState::Ready);
}

#[test]
fn stage_progress_is_ordered_and_evidence_is_generation_local() {
    let integration = integration();
    let mut view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            device_lifecycle: RunnerAvailability::Available,
            system_lifecycle: RunnerAvailability::Available,
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let stage_ids = view.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .map(|stage| stage.stage_id.clone())
        .collect::<Vec<_>>();
    assert!(stage_ids.len() >= 4);

    apply_stage_results(&mut view, &BTreeMap::new(), None);
    let statuses = view.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .map(|stage| stage.status)
        .collect::<Vec<_>>();
    assert_eq!(statuses[0], StageStatus::Ready);
    assert!(
        statuses[1..]
            .iter()
            .all(|status| *status == StageStatus::Locked)
    );
    assert_eq!(
        view.actions.iter().filter(|action| action.enabled).count(),
        1
    );

    let first_result = StageResult {
        summary: "Profile binding verified.".to_owned(),
        completed_at: "2026-07-22T12:01:00Z".to_owned(),
        evidence_refs: vec!["local-read-only:identity".to_owned()],
    };
    let mut results = BTreeMap::from([(
        stage_ids[0].clone(),
        StageProgress {
            status: StageStatus::Passed,
            result: Some(first_result.clone()),
            checkpoint: None,
        },
    )]);
    apply_stage_results(&mut view, &results, Some("run-generation-7"));
    assert_eq!(view.plans[0].verdict, VerdictState::InProgress);
    assert_eq!(
        view.plans[0].evidence.artifact_state,
        EvidenceArtifactState::Collecting
    );
    assert_eq!(
        view.plans[0].evidence.completed_claims,
        vec![stage_ids[0].clone()]
    );
    assert_eq!(
        view.plans[0].evidence.run_id.as_deref(),
        Some("run-generation-7")
    );
    assert_eq!(
        view.actions.iter().filter(|action| action.enabled).count(),
        1
    );

    for stage_id in &stage_ids[1..] {
        results.insert(
            stage_id.clone(),
            StageProgress {
                status: StageStatus::Passed,
                result: Some(first_result.clone()),
                checkpoint: None,
            },
        );
    }
    apply_stage_results(&mut view, &results, Some("run-generation-7"));
    assert_eq!(view.plans[0].verdict, VerdictState::EvidenceReady);
    assert_eq!(
        view.plans[0].evidence.artifact_state,
        EvidenceArtifactState::Ready
    );
    assert_eq!(view.plans[0].evidence.completed_claims, stage_ids);
    assert!(view.plans[0].evidence.missing_claims.is_empty());
    assert!(view.actions.iter().all(|action| !action.enabled));
    assert!(!view.companion.hardware_write_executed);
}

#[test]
fn lighting_authorization_precedes_checkpoint_observations() {
    let integration = integration();
    let mut view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let stages = view.plans[0]
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .map(|stage| stage.stage_id.clone())
        .collect::<Vec<_>>();
    let completed = StageResult {
        summary: "Read-only prerequisite passed.".to_owned(),
        completed_at: "2026-07-22T12:01:00Z".to_owned(),
        evidence_refs: vec!["local-read-only:test".to_owned()],
    };
    let mut results = BTreeMap::from([
        (
            stages[0].clone(),
            StageProgress {
                status: StageStatus::Passed,
                result: Some(completed.clone()),
                checkpoint: None,
            },
        ),
        (
            stages[1].clone(),
            StageProgress {
                status: StageStatus::Passed,
                result: Some(completed),
                checkpoint: None,
            },
        ),
    ]);
    apply_stage_results(&mut view, &results, Some("run-generation-7"));
    let map = stage_by_suffix(&view, "-map");
    assert_eq!(map.status, StageStatus::Ready);
    assert!(map.observations.is_empty());
    let action = action_for_stage(&view, map);
    assert!(action.enabled);
    assert!(
        action
            .confirmation
            .as_ref()
            .expect("lighting authorization")
            .summary
            .contains("cleanup")
    );

    results.insert(
        map.stage_id.clone(),
        StageProgress {
            status: StageStatus::AwaitingObservation,
            result: None,
            checkpoint: Some(StageCheckpoint {
                checkpoint_id: "map-pattern".to_owned(),
                instructions: vec!["Inspect the pattern.".to_owned()],
                observations: vec![outcome_observation()],
                evidence_refs: vec!["bridge-transaction:test".to_owned()],
            }),
        },
    );
    apply_stage_results(&mut view, &results, Some("run-generation-7"));
    let map = stage_by_suffix(&view, "-map");
    assert_eq!(map.status, StageStatus::AwaitingObservation);
    assert_eq!(map.observations.len(), 1);
    let action = action_for_stage(&view, map);
    assert_eq!(action.label, "Record watched result");
    assert!(action.enabled);
    assert!(action.confirmation.is_none());
    let lighting = stage_by_suffix(&view, "-lighting");
    assert_eq!(lighting.status, StageStatus::Locked);
    let lighting_action = action_for_stage(&view, lighting);
    assert!(!lighting_action.enabled);
}

#[test]
fn measured_resume_failure_exposes_only_the_exact_suspend_retry() {
    let integration = paired_integration();
    let mut view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            device_lifecycle: RunnerAvailability::Available,
            system_lifecycle: RunnerAvailability::Available,
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let completed = StageResult {
        summary: "Prerequisite passed.".to_owned(),
        completed_at: "2026-07-23T11:40:00Z".to_owned(),
        evidence_refs: vec!["local-read-only:test".to_owned()],
    };
    let mut results = view
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .filter(|stage| !stage.stage_id.ends_with("-lifecycle"))
        .map(|stage| {
            (
                stage.stage_id.clone(),
                StageProgress {
                    status: StageStatus::Passed,
                    result: Some(completed.clone()),
                    checkpoint: None,
                },
            )
        })
        .collect::<BTreeMap<_, _>>();
    let lifecycle_id = view
        .plans
        .iter()
        .find(|plan| plan.device_id.contains("keyboard"))
        .expect("keyboard plan exists")
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-lifecycle"))
        .expect("lifecycle stage exists")
        .stage_id
        .clone();
    results.insert(
        lifecycle_id.clone(),
        StageProgress {
            status: StageStatus::Failed,
            result: Some(StageResult {
                summary: "The watched resume result failed.".to_owned(),
                completed_at: "2026-07-23T11:50:45Z".to_owned(),
                evidence_refs: vec![SYSTEM_RESUME_RETRY_EVIDENCE.to_owned()],
            }),
            checkpoint: None,
        },
    );
    apply_stage_results(&mut view, &results, Some("run-generation-7"));

    let lifecycle = view
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id == lifecycle_id)
        .expect("keyboard lifecycle remains visible");
    let action = action_for_stage(&view, lifecycle);
    assert!(action.enabled);
    assert_eq!(action.label, "Retry supervised resume");
    assert_eq!(
        action
            .confirmation
            .as_ref()
            .expect("retry requires authorization")
            .phrase,
        "authorize one supervised system suspend"
    );
    let mouse_lifecycle = view
        .plans
        .iter()
        .find(|plan| plan.device_id.contains("mouse"))
        .expect("mouse plan exists")
        .groups
        .iter()
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id.ends_with("-lifecycle"))
        .expect("mouse lifecycle exists");
    assert_eq!(mouse_lifecycle.status, StageStatus::Locked);
    assert!(!action_for_stage(&view, mouse_lifecycle).enabled);
    assert_eq!(
        view.actions.iter().filter(|action| action.enabled).count(),
        1
    );
}

#[test]
fn identical_models_on_two_receivers_have_distinct_stage_and_action_ids() {
    let mut integration = integration();
    let mut second = integration.view.receivers[0].clone();
    second.receiver_id = ReceiverId::try_from("receiver-second").expect("id is valid");
    second.generation_id = GenerationId::try_from(11_u64).expect("generation is valid");
    integration.view.receivers.push(second);
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            ..RunnerCapabilities::default()
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.plans.len(), 2);
    let plan_bindings = view
        .plans
        .iter()
        .map(|plan| {
            (
                plan.receiver_id.as_str(),
                plan.generation_id,
                plan.device_id.as_str(),
            )
        })
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(plan_bindings.len(), 2);
    let stage_ids = view
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .map(|stage| stage.stage_id.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let stage_count = view
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .map(|group| group.stages.len())
        .sum::<usize>();
    assert_eq!(stage_ids.len(), stage_count);
    let action_ids = view
        .actions
        .iter()
        .map(|action| action.id.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(action_ids.len(), view.actions.len());
}

#[test]
fn unknown_device_never_receives_a_qualification_plan() {
    let mut integration = integration();
    let device = &mut integration.view.receivers[0].inventory[0];
    device.profile = None;
    device.support_level = SupportLevel::Candidate;
    device.capabilities.clear();
    let view = build_qualification_view(
        &system(),
        Some(&integration),
        &catalog(),
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            device_lifecycle: RunnerAvailability::Available,
            system_lifecycle: RunnerAvailability::Available,
        },
        1,
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.receivers[0].devices[0].support, SupportView::Unknown);
    assert!(view.plans.is_empty());
    assert!(view.actions.is_empty());
    assert!(!view.companion.hardware_write_executed);
}

#[test]
fn utc_timestamp_formatter_handles_epoch_and_leap_day() {
    assert_eq!(unix_seconds_to_rfc3339(0), "1970-01-01T00:00:00Z");
    assert_eq!(
        unix_seconds_to_rfc3339(1_582_934_400),
        "2020-02-29T00:00:00Z"
    );
}

fn report_finding(
    id: &str,
    severity: hfx_domain::ErrorSeverity,
    privacy: hfx_domain::PrivacyClass,
) -> hfx_protocol::DiagnosticFinding {
    hfx_protocol::DiagnosticFinding {
        finding_id: hfx_domain::FindingId::try_from(id).expect("finding id is valid"),
        severity,
        cause: hfx_domain::HumanMessage::try_from("cause names /dev/hidraw7 on host-name")
            .expect("cause is valid"),
        explanation: hfx_domain::HumanMessage::try_from("explanation for user-name")
            .expect("explanation is valid"),
        safe_action: hfx_domain::HumanMessage::try_from("run hyperfluxctl doctor")
            .expect("action is valid"),
        privacy,
        documentation: hfx_domain::DocumentationPath::try_from("docs/user/troubleshooting.md")
            .expect("documentation path is valid"),
    }
}

fn reporting_system() -> SystemSnapshot {
    use hfx_domain::{ErrorSeverity, PrivacyClass};
    let mut system = system();
    system.bridge = Some(crate::BridgeHealth {
        snapshot: hfx_protocol::BridgeSnapshot {
            cursor: integration().view.cursor,
            receivers: Vec::new(),
        },
        diagnostics: hfx_protocol::DiagnosticSnapshot {
            sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            findings: vec![
                report_finding(
                    "HFX-SERVICE-002",
                    ErrorSeverity::Warning,
                    PrivacyClass::Public,
                ),
                report_finding(
                    "HFX-KERNEL-001",
                    ErrorSeverity::Error,
                    PrivacyClass::Private,
                ),
                report_finding(
                    "HFX-TRANSPORT-001",
                    ErrorSeverity::Error,
                    PrivacyClass::PublicSummary,
                ),
                report_finding("HFX-UNKNOWN-999", ErrorSeverity::Info, PrivacyClass::Public),
                report_finding(
                    "finding on host-name",
                    ErrorSeverity::Info,
                    PrivacyClass::Public,
                ),
            ],
            event_buffer_capacity: hfx_domain::QueueCapacity::try_from(256_u16)
                .expect("capacity is valid"),
            transaction_queue_capacity: hfx_domain::QueueCapacity::try_from(32_u16)
                .expect("capacity is valid"),
        },
    });
    system
}

fn report_projection() -> IntegrationView {
    paired_integration().view
}

fn report_fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../apps/device-qualification/tests/fixtures/rust-qualified-report.json")
}

fn protocol_v7() -> ProtocolVersion {
    ProtocolVersion::try_from(7_u16).expect("protocol is valid")
}

#[test]
fn rust_report_fixture_matches_the_current_report() {
    let projection = report_projection();
    let view = crate::build_qualification_report(
        &reporting_system(),
        Some((protocol_v7(), &projection)),
        &catalog(),
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let rendered = serde_json::to_string_pretty(&view).expect("report serializes") + "\n";
    let fixture = report_fixture_path();
    if std::env::var_os("HFX_UPDATE_QUALIFICATION_FIXTURE").is_some() {
        std::fs::write(&fixture, &rendered).expect("fixture updates");
    }
    let expected = std::fs::read_to_string(&fixture).expect(
        "qualification report fixture exists; set HFX_UPDATE_QUALIFICATION_FIXTURE=1 to create it",
    );
    assert_eq!(expected, rendered, "qualification report fixture is stale");
}

#[test]
fn report_replaces_stable_identifiers_and_keeps_only_catalog_findings() {
    let projection = report_projection();
    let view = crate::build_qualification_report(
        &reporting_system(),
        Some((protocol_v7(), &projection)),
        &catalog(),
        "2026-07-22T12:00:00Z".to_owned(),
    );
    let rendered = serde_json::to_string(&view).expect("report serializes");
    for private in [
        "receiver-test",
        "mouse-test",
        "keyboard-test",
        "hidraw",
        "host-name",
        "user-name",
        "docs/user/troubleshooting.md",
        "HFX-KERNEL-001",
        "HFX-UNKNOWN-999",
    ] {
        assert!(!rendered.contains(private), "report leaked {private}");
    }
    assert_eq!(view.companion.state, CompanionState::Ready);
    assert_eq!(view.receivers[0].receiver_id, "receiver-1");
    assert_eq!(view.receivers[0].generation_id, 1);
    assert!(
        !rendered.contains("\"7\""),
        "report kept the driver generation"
    );
    assert_eq!(view.receivers[0].devices.len(), 2);
    assert!(view.actions.is_empty());
    let collection = view.collection.expect("report declares its collection");
    assert_eq!(collection.producer, crate::QUALIFICATION_REPORT_PRODUCER);
    assert_eq!(
        collection.bridge_requests,
        [
            crate::ReportBridgeRequest::Snapshot,
            crate::ReportBridgeRequest::Diagnostics,
            crate::ReportBridgeRequest::IntegrationView,
        ]
    );
    assert!(!collection.active_device_query_executed);
    assert_eq!(collection.firmware, crate::FirmwareCollection::NotCollected);
    assert_eq!(
        collection
            .diagnostic_findings
            .iter()
            .map(|finding| finding.finding_id.as_str())
            .collect::<Vec<_>>(),
        ["HFX-SERVICE-002", "HFX-TRANSPORT-001"]
    );
}

#[test]
fn report_without_a_bridge_declares_no_bridge_requests() {
    let view = crate::build_qualification_report(
        &system(),
        None,
        &catalog(),
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.companion.state, CompanionState::BridgeUnavailable);
    assert!(view.receivers.is_empty());
    let collection = view.collection.expect("report declares its collection");
    assert!(collection.bridge_requests.is_empty());
    assert!(collection.diagnostic_findings.is_empty());
}

#[test]
fn report_bounds_receivers_and_devices_to_the_console_contract() {
    let mut projection = report_projection();
    let device = projection.receivers[0].inventory[0].clone();
    projection.receivers[0].inventory = vec![device; 20];
    let receiver = projection.receivers[0].clone();
    projection.receivers = vec![receiver; 10];
    let view = crate::build_qualification_report(
        &reporting_system(),
        Some((protocol_v7(), &projection)),
        &catalog(),
        "2026-07-22T12:00:00Z".to_owned(),
    );
    assert_eq!(view.receivers.len(), 8);
    assert!(
        view.receivers
            .iter()
            .all(|receiver| receiver.devices.len() == 16)
    );
    assert_eq!(view.plans.len(), 32);
    let receiver_ids = view
        .receivers
        .iter()
        .map(|receiver| receiver.receiver_id.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(receiver_ids.len(), 8);
}
