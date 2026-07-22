// SPDX-License-Identifier: GPL-2.0-only

use crate::{BridgeIntegration, ServiceState, SystemSnapshot};
use hfx_domain::{
    DeviceKind, FreshnessState, InventoryAvailability, ReceiverLifecycleState, SupportLevel,
    TelemetryAvailability,
};
use hfx_profiles::{PROFILE_SOURCE_SHA256, RuntimeProfile, RuntimeProfileCatalog};
use hfx_protocol::{BatteryObservation, DeviceInventoryView, IntegrationReceiverView};
use hfx_runtime::PRODUCT_VERSION;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

pub const QUALIFICATION_API_VERSION: u16 = 1;
pub const QUALIFICATION_SCHEMA: &str = "hyperflux-local-qualification-v1";
pub const QUALIFICATION_ENDPOINT: &str = "http://127.0.0.1:47821";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CompanionState {
    Ready,
    DriverPending,
    LegacyV2Detected,
    BridgeUnavailable,
    NoReceiver,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum PresenceView {
    Active,
    Sleeping,
    Unavailable,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SupportView {
    Unknown,
    Identified,
    ProfileQualified,
    ProductionQualified,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum VerdictState {
    NotRun,
    InProgress,
    Blocked,
    Failed,
    EvidenceReady,
    Accepted,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum StageStatus {
    Locked,
    Ready,
    Running,
    AwaitingObservation,
    Passed,
    Failed,
    Blocked,
    Skipped,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum RiskLevel {
    ReadOnly,
    LightingWrite,
    DeviceLifecycle,
    SystemLifecycle,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum StageKind {
    Automatic,
    WatchedObservation,
    Lifecycle,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum EvidenceArtifactState {
    None,
    Collecting,
    Ready,
    Reviewed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CapabilityAccess {
    Read,
    Write,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum BatteryAvailability {
    Reported,
    Stale,
    Unavailable,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReceiverLifecycleView {
    Active,
    Recovering,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileBindingView {
    pub id: String,
    pub digest: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilityView {
    pub id: String,
    pub access: CapabilityAccess,
    pub support_level: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BatteryView {
    pub availability: BatteryAvailability,
    pub percentage: Option<u8>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceView {
    pub device_id: String,
    pub kind: String,
    pub model_name: Option<String>,
    pub vendor_id: Option<u16>,
    pub product_id: u16,
    pub profile: Option<ProfileBindingView>,
    pub presence: PresenceView,
    pub support: SupportView,
    pub battery: BatteryView,
    pub capabilities: Vec<CapabilityView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiverView {
    pub receiver_id: String,
    pub generation_id: u64,
    pub model_name: Option<String>,
    pub vendor_id: Option<u16>,
    pub product_id: Option<u16>,
    pub profile: Option<ProfileBindingView>,
    pub lifecycle: ReceiverLifecycleView,
    pub devices: Vec<DeviceView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationChoice {
    pub id: String,
    pub label: String,
    pub outcome: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationPrompt {
    pub id: String,
    pub prompt: String,
    pub choices: Vec<ObservationChoice>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ActionConfirmation {
    pub phrase: String,
    pub summary: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompanionAction {
    pub id: String,
    pub label: String,
    pub method: String,
    pub href: String,
    pub enabled: bool,
    pub risk: RiskLevel,
    pub confirmation: Option<ActionConfirmation>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StageResult {
    pub summary: String,
    pub completed_at: String,
    pub evidence_refs: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct StageProgress {
    pub status: StageStatus,
    pub result: StageResult,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationStage {
    pub stage_id: String,
    pub title: String,
    pub description: String,
    pub kind: StageKind,
    pub risk: RiskLevel,
    pub status: StageStatus,
    pub capabilities: Vec<String>,
    pub instructions: Vec<String>,
    pub observations: Vec<ObservationPrompt>,
    pub action_id: Option<String>,
    pub result: Option<StageResult>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationGroup {
    pub group_id: String,
    pub title: String,
    pub description: String,
    pub stages: Vec<QualificationStage>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceView {
    pub run_id: Option<String>,
    pub artifact_state: EvidenceArtifactState,
    pub completed_claims: Vec<String>,
    pub missing_claims: Vec<String>,
    pub export_action_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationPlan {
    pub plan_id: String,
    pub device_id: String,
    pub profile_binding: Option<ProfileBindingView>,
    pub verdict: VerdictState,
    pub summary: String,
    pub groups: Vec<QualificationGroup>,
    pub evidence: EvidenceView,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompanionView {
    pub state: CompanionState,
    pub version: String,
    pub bridge_protocol: Option<u16>,
    pub endpoint: String,
    pub simulation: bool,
    pub network_upload_executed: bool,
    pub hardware_write_executed: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationSystemView {
    pub driver_version: Option<String>,
    pub bridge_version: Option<String>,
    pub profile_catalog_digest: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationView {
    pub schema: String,
    pub api_version: u16,
    pub view_revision: u64,
    pub generated_at: String,
    pub companion: CompanionView,
    pub system: QualificationSystemView,
    pub receivers: Vec<ReceiverView>,
    pub plans: Vec<QualificationPlan>,
    pub actions: Vec<CompanionAction>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum RunnerAvailability {
    #[default]
    Unavailable,
    Available,
}

impl RunnerAvailability {
    const fn is_available(self) -> bool {
        matches!(self, Self::Available)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct RunnerCapabilities {
    pub read_only_actions: RunnerAvailability,
    pub supervised_lighting: RunnerAvailability,
    pub device_lifecycle: RunnerAvailability,
    pub system_lifecycle: RunnerAvailability,
}

/// Returns one UTC RFC 3339 timestamp without adding a wall-clock dependency
/// to the installed operations binary.
#[must_use]
pub fn qualification_generated_at() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs());
    unix_seconds_to_rfc3339(seconds)
}

/// Builds one truthful, bounded console view from the system probe and the
/// bridge's viewer-specific integration projection.
#[must_use]
pub fn build_qualification_view(
    system: &SystemSnapshot,
    integration: Option<&BridgeIntegration>,
    catalog: &RuntimeProfileCatalog,
    runner: RunnerCapabilities,
    view_revision: u64,
    generated_at: String,
) -> QualificationView {
    let state = companion_state(system, integration);
    let mut receivers = Vec::new();
    let mut plans = Vec::new();
    let mut actions = Vec::new();
    if let Some(integration) = integration {
        for receiver in &integration.view.receivers {
            receivers.push(receiver_view(receiver, catalog));
            for device in &receiver.inventory {
                if let Some(plan) = qualification_plan(receiver, device, catalog, runner) {
                    actions.extend(actions_for_plan(&plan));
                    plans.push(plan);
                }
            }
        }
    }
    QualificationView {
        schema: QUALIFICATION_SCHEMA.to_owned(),
        api_version: QUALIFICATION_API_VERSION,
        view_revision,
        generated_at,
        companion: CompanionView {
            state,
            version: PRODUCT_VERSION.to_owned(),
            bridge_protocol: integration.map(|value| value.protocol_version.get()),
            endpoint: QUALIFICATION_ENDPOINT.to_owned(),
            simulation: false,
            network_upload_executed: false,
            hardware_write_executed: false,
        },
        system: QualificationSystemView {
            driver_version: system
                .loaded_module_identity
                .as_ref()
                .map(|_| system.package_version.clone()),
            bridge_version: integration.map(|_| system.package_version.clone()),
            profile_catalog_digest: Some(PROFILE_SOURCE_SHA256.to_owned()),
        },
        receivers,
        plans,
        actions,
    }
}

fn companion_state(
    system: &SystemSnapshot,
    integration: Option<&BridgeIntegration>,
) -> CompanionState {
    if matches!(
        (&system.installed_module_identity, &system.loaded_module_identity),
        (Some(installed), Some(loaded)) if installed != loaded
    ) {
        return CompanionState::DriverPending;
    }
    if system.service_state == ServiceState::Active && integration.is_some() {
        return if integration.is_some_and(|value| value.view.receivers.is_empty()) {
            CompanionState::NoReceiver
        } else {
            CompanionState::Ready
        };
    }
    if system.legacy_v2_stack_detected {
        return CompanionState::LegacyV2Detected;
    }
    if system.service_state != ServiceState::Active || integration.is_none() {
        return CompanionState::BridgeUnavailable;
    }
    CompanionState::BridgeUnavailable
}

fn receiver_view(
    receiver: &IntegrationReceiverView,
    catalog: &RuntimeProfileCatalog,
) -> ReceiverView {
    let profile = receiver
        .profile
        .as_ref()
        .and_then(|binding| catalog.profile(&binding.profile_id));
    ReceiverView {
        receiver_id: receiver.receiver_id.as_str().to_owned(),
        generation_id: receiver.generation_id.get(),
        model_name: receiver
            .model_name
            .as_ref()
            .map(|value| value.as_str().to_owned())
            .or_else(|| profile.map(|value| value.model_name.to_owned())),
        vendor_id: profile
            .and_then(|value| value.vendor_id)
            .map(hfx_domain::VendorId::get),
        product_id: profile
            .and_then(|value| value.product_id)
            .map(hfx_domain::ProductId::get),
        profile: receiver.profile.as_ref().map(profile_binding),
        lifecycle: match receiver.lifecycle {
            ReceiverLifecycleState::Active => ReceiverLifecycleView::Active,
            ReceiverLifecycleState::Suspended
            | ReceiverLifecycleState::PartiallySuspended
            | ReceiverLifecycleState::Disconnecting => ReceiverLifecycleView::Recovering,
            ReceiverLifecycleState::Unknown => ReceiverLifecycleView::Unavailable,
        },
        devices: receiver
            .inventory
            .iter()
            .map(|device| device_view(device, catalog))
            .collect(),
    }
}

fn device_view(device: &DeviceInventoryView, catalog: &RuntimeProfileCatalog) -> DeviceView {
    let profile = device
        .profile
        .as_ref()
        .and_then(|binding| catalog.profile(&binding.profile_id));
    let capabilities = profile.map_or_else(Vec::new, |profile| {
        profile
            .capabilities
            .iter()
            .filter(|capability| device.capabilities.binary_search(&capability.id).is_ok())
            .map(|capability| CapabilityView {
                id: capability.id.as_str().to_owned(),
                access: if capability.writable {
                    CapabilityAccess::Write
                } else {
                    CapabilityAccess::Read
                },
                support_level: capability.support_level.as_str().to_owned(),
            })
            .collect()
    });
    DeviceView {
        device_id: device.device_id.as_str().to_owned(),
        kind: match device.device_kind {
            DeviceKind::Mouse => "mouse",
            DeviceKind::Keyboard => "keyboard",
            _ => "other",
        }
        .to_owned(),
        model_name: device
            .model_name
            .as_ref()
            .map(|value| value.as_str().to_owned())
            .or_else(|| profile.map(|value| value.model_name.to_owned())),
        vendor_id: profile
            .and_then(|value| value.vendor_id)
            .map(hfx_domain::VendorId::get),
        product_id: device.product_id.get(),
        profile: device.profile.as_ref().map(profile_binding),
        presence: match device.availability {
            InventoryAvailability::Available => PresenceView::Active,
            InventoryAvailability::Sleeping => PresenceView::Sleeping,
            InventoryAvailability::Unavailable
            | InventoryAvailability::Unpaired
            | InventoryAvailability::ReceiverUnavailable => PresenceView::Unavailable,
            InventoryAvailability::Unknown | InventoryAvailability::PairingUnknown => {
                PresenceView::Unknown
            }
        },
        support: support_view(device),
        battery: battery_view(&device.battery),
        capabilities,
    }
}

fn support_view(device: &DeviceInventoryView) -> SupportView {
    if device.profile.is_some() {
        if device.support_level == SupportLevel::ProductionQualified {
            SupportView::ProductionQualified
        } else {
            SupportView::ProfileQualified
        }
    } else if device.support_level > SupportLevel::Candidate {
        SupportView::Identified
    } else {
        SupportView::Unknown
    }
}

fn battery_view(battery: &BatteryObservation) -> BatteryView {
    let availability = match (battery.availability, battery.freshness) {
        (TelemetryAvailability::Reported, FreshnessState::Stale) => BatteryAvailability::Stale,
        (TelemetryAvailability::Reported, _) => BatteryAvailability::Reported,
        (TelemetryAvailability::Unavailable, _) => BatteryAvailability::Unavailable,
        (TelemetryAvailability::Unknown, _) => BatteryAvailability::Unknown,
    };
    BatteryView {
        availability,
        percentage: battery.percentage.map(hfx_domain::BatteryPercent::get),
    }
}

fn profile_binding(binding: &hfx_protocol::ProfileBindingView) -> ProfileBindingView {
    ProfileBindingView {
        id: binding.profile_id.as_str().to_owned(),
        digest: binding.profile_digest.as_str().to_owned(),
    }
}

fn qualification_plan(
    receiver: &IntegrationReceiverView,
    device: &DeviceInventoryView,
    catalog: &RuntimeProfileCatalog,
    runner: RunnerCapabilities,
) -> Option<QualificationPlan> {
    let binding = device.profile.as_ref()?;
    let profile = catalog.profile(&binding.profile_id)?;
    let slug = format!(
        "{}-{:04x}",
        match device.device_kind {
            DeviceKind::Mouse => "mouse",
            DeviceKind::Keyboard => "keyboard",
            _ => "device",
        },
        device.product_id.get()
    );
    let mut groups = vec![identity_group(&slug, profile, runner.read_only_actions)];
    if let Some(group) = lighting_group(&slug, device, profile, runner.supervised_lighting) {
        groups.push(group);
    }
    groups.push(lifecycle_group(&slug, device, runner));
    let missing_claims = groups
        .iter()
        .flat_map(|group| group.stages.iter().map(|stage| stage.stage_id.clone()))
        .collect::<Vec<_>>();
    let count = missing_claims.len();
    Some(QualificationPlan {
        plan_id: format!(
            "plan:{}:{}:{}",
            receiver.receiver_id, receiver.generation_id, binding.profile_id
        ),
        device_id: device.device_id.as_str().to_owned(),
        profile_binding: Some(profile_binding(binding)),
        verdict: VerdictState::NotRun,
        summary: format!("{count} evidence checks for this exact profile and receiver generation."),
        groups,
        evidence: EvidenceView {
            run_id: None,
            artifact_state: EvidenceArtifactState::None,
            completed_claims: Vec::new(),
            missing_claims,
            export_action_id: None,
        },
    })
}

fn identity_group(
    slug: &str,
    profile: &RuntimeProfile,
    runner: RunnerAvailability,
) -> QualificationGroup {
    let identity_id = format!("{slug}-identity");
    let telemetry_id = format!("{slug}-telemetry");
    let available = runner.is_available();
    let mut stages = vec![QualificationStage {
        stage_id: identity_id.clone(),
        title: "Profile binding".to_owned(),
        description:
            "Verify the observed product ID, immutable profile digest, route, and generation."
                .to_owned(),
        kind: StageKind::Automatic,
        risk: RiskLevel::ReadOnly,
        status: if available {
            StageStatus::Ready
        } else {
            StageStatus::Blocked
        },
        capabilities: matching_capabilities(
            profile,
            &[
                "identity.model",
                "identity.paired-product-id",
                "route.hyperflux-wireless",
            ],
        ),
        instructions: Vec::new(),
        observations: Vec::new(),
        action_id: available.then(|| action_id(&identity_id)),
        result: None,
    }];
    if has_any(
        profile,
        &[
            "presence.passive-evidence",
            "telemetry.battery-percent",
            "telemetry.connection-evidence",
            "telemetry.mouse-contact",
        ],
    ) {
        stages.push(QualificationStage {
            stage_id: telemetry_id.clone(),
            title: "Presence and telemetry".to_owned(),
            description: "Exercise the device and verify fresh passive observations without changing hardware state."
                .to_owned(),
            kind: StageKind::WatchedObservation,
            risk: RiskLevel::ReadOnly,
            status: if available {
                StageStatus::Locked
            } else {
                StageStatus::Blocked
            },
            capabilities: matching_capabilities(
                profile,
                &[
                    "presence.passive-evidence",
                    "telemetry.battery-percent",
                    "telemetry.connection-evidence",
                    "telemetry.mouse-contact",
                ],
            ),
            instructions: vec![
                "Exercise the device and its ordinary controls.".to_owned(),
                "Confirm that presence or battery evidence updates without a lighting write."
                    .to_owned(),
            ],
            observations: vec![controls_observation()],
            action_id: available.then(|| action_id(&telemetry_id)),
            result: None,
        });
    }
    QualificationGroup {
        group_id: "identity".to_owned(),
        title: "Identity and telemetry".to_owned(),
        description:
            "Read-only facts are bound to this receiver generation and exact profile digest."
                .to_owned(),
        stages,
    }
}

fn lighting_group(
    slug: &str,
    device: &DeviceInventoryView,
    profile: &RuntimeProfile,
    runner: RunnerAvailability,
) -> Option<QualificationGroup> {
    let has_map = has_any(profile, &["lighting.per-key", "lighting.per-led"]);
    let lighting_capabilities = [
        "lighting.brightness",
        "lighting.complete-black",
        "lighting.off",
        "lighting.software-effect-frames",
        "lighting.static",
    ];
    let has_lighting = has_any(profile, &lighting_capabilities);
    if !has_map && !has_lighting {
        return None;
    }
    let mut stages = Vec::new();
    if has_map {
        stages.push(blocked_or_locked_stage(
            &format!("{slug}-map"),
            if device.device_kind == DeviceKind::Keyboard {
                "Complete key map"
            } else {
                "Complete LED map"
            },
            "Verify every application slot against the same physical light using bounded sentinel frames.",
            matching_capabilities(
                profile,
                &["lighting.direct-frame", "lighting.per-key", "lighting.per-led"],
            ),
            runner.is_available(),
            RiskLevel::LightingWrite,
        ));
    }
    if has_lighting {
        stages.push(blocked_or_locked_stage(
            &format!("{slug}-lighting"),
            "Lighting behavior",
            "Verify complete black, Off, Static, brightness endpoints, and streamed application frames claimed by this profile.",
            matching_capabilities(profile, &lighting_capabilities),
            runner.is_available(),
            RiskLevel::LightingWrite,
        ));
    }
    Some(QualificationGroup {
        group_id: "lighting".to_owned(),
        title: "Lighting transport".to_owned(),
        description: "Only profile-bound writes may exercise the receiver transport.".to_owned(),
        stages,
    })
}

fn lifecycle_group(
    slug: &str,
    device: &DeviceInventoryView,
    runner: RunnerCapabilities,
) -> QualificationGroup {
    let lifecycle_enabled = runner.device_lifecycle.is_available()
        && (device.device_kind != DeviceKind::Keyboard || runner.system_lifecycle.is_available());
    QualificationGroup {
        group_id: "lifecycle".to_owned(),
        title: "Lifecycle continuity".to_owned(),
        description:
            "New generations must invalidate stale authority and restore only verified state."
                .to_owned(),
        stages: vec![blocked_or_locked_stage(
            &format!("{slug}-lifecycle"),
            "Sleep, return, reconnect, and resume",
            "Verify controller return, sibling isolation, receiver renewal, and system resume where applicable.",
            vec!["receiver.generation".to_owned()],
            lifecycle_enabled,
            if device.device_kind == DeviceKind::Keyboard {
                RiskLevel::SystemLifecycle
            } else {
                RiskLevel::DeviceLifecycle
            },
        )],
    }
}

fn blocked_or_locked_stage(
    stage_id: &str,
    title: &str,
    description: &str,
    capabilities: Vec<String>,
    runner_enabled: bool,
    risk: RiskLevel,
) -> QualificationStage {
    QualificationStage {
        stage_id: stage_id.to_owned(),
        title: title.to_owned(),
        description: description.to_owned(),
        kind: if matches!(risk, RiskLevel::LightingWrite) {
            StageKind::WatchedObservation
        } else {
            StageKind::Lifecycle
        },
        risk,
        status: if runner_enabled {
            StageStatus::Locked
        } else {
            StageStatus::Blocked
        },
        capabilities,
        instructions: if runner_enabled {
            vec![
                "Follow each on-screen checkpoint; do not repeat a physical step until requested."
                    .to_owned(),
            ]
        } else {
            vec!["The installed build does not include this supervised runner yet; no write was attempted.".to_owned()]
        },
        observations: vec![outcome_observation()],
        action_id: runner_enabled.then(|| action_id(stage_id)),
        result: None,
    }
}

fn actions_for_plan(plan: &QualificationPlan) -> Vec<CompanionAction> {
    plan.groups
        .iter()
        .flat_map(|group| &group.stages)
        .filter_map(|stage| {
            let id = stage.action_id.clone()?;
            let confirmation = match stage.risk {
                RiskLevel::ReadOnly => None,
                RiskLevel::LightingWrite => Some(ActionConfirmation {
                    phrase: "authorize profile-bound lighting test".to_owned(),
                    summary: "This stage may send bounded RGB frames only to the selected profile and generation."
                        .to_owned(),
                }),
                RiskLevel::DeviceLifecycle => Some(ActionConfirmation {
                    phrase: "authorize watched device lifecycle".to_owned(),
                    summary: "This stage waits for separately prompted device power or receiver actions."
                        .to_owned(),
                }),
                RiskLevel::SystemLifecycle => Some(ActionConfirmation {
                    phrase: "authorize watched system lifecycle".to_owned(),
                    summary: "A system suspend requires a later, separate confirmation at its checkpoint."
                        .to_owned(),
                }),
            };
            Some(CompanionAction {
                id: id.clone(),
                label: match stage.kind {
                    StageKind::Automatic => "Run read-only check",
                    StageKind::WatchedObservation if stage.risk == RiskLevel::ReadOnly => {
                        "Record observation"
                    }
                    StageKind::WatchedObservation => "Begin supervised test",
                    StageKind::Lifecycle => "Begin watched lifecycle",
                }
                .to_owned(),
                method: "POST".to_owned(),
                href: format!("/v1/qualification/actions/{}", percent_encode(&id)),
                enabled: stage.status == StageStatus::Ready,
                risk: stage.risk,
                confirmation,
            })
        })
        .collect()
}

pub(crate) fn apply_stage_results(
    view: &mut QualificationView,
    results: &BTreeMap<String, StageProgress>,
    run_id: Option<&str>,
) {
    for plan in &mut view.plans {
        let mut completed = Vec::new();
        let mut first_pending_seen = false;
        for stage in plan
            .groups
            .iter_mut()
            .flat_map(|group| group.stages.iter_mut())
        {
            if let Some(progress) = results.get(&stage.stage_id) {
                stage.status = progress.status;
                stage.result = Some(progress.result.clone());
                if progress.status == StageStatus::Passed {
                    completed.push(stage.stage_id.clone());
                }
                continue;
            }
            if stage.status == StageStatus::Blocked {
                continue;
            }
            if first_pending_seen {
                stage.status = StageStatus::Locked;
            } else {
                stage.status = StageStatus::Ready;
                first_pending_seen = true;
            }
        }
        let missing = plan
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .filter(|stage| stage.status != StageStatus::Passed)
            .map(|stage| stage.stage_id.clone())
            .collect::<Vec<_>>();
        let runnable_missing = plan
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .any(|stage| !matches!(stage.status, StageStatus::Passed | StageStatus::Blocked));
        let blocked_missing = plan
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .any(|stage| stage.status == StageStatus::Blocked);
        let failed = plan
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .any(|stage| stage.status == StageStatus::Failed);
        let recorded_block = results.iter().any(|(stage_id, progress)| {
            progress.status == StageStatus::Blocked
                && plan
                    .groups
                    .iter()
                    .flat_map(|group| &group.stages)
                    .any(|stage| stage.stage_id == *stage_id)
        });
        plan.verdict = if failed {
            VerdictState::Failed
        } else if recorded_block {
            VerdictState::Blocked
        } else if missing.is_empty() {
            VerdictState::EvidenceReady
        } else if !completed.is_empty() {
            if runnable_missing {
                VerdictState::InProgress
            } else if blocked_missing {
                VerdictState::Blocked
            } else {
                VerdictState::InProgress
            }
        } else {
            VerdictState::NotRun
        };
        plan.evidence = EvidenceView {
            run_id: run_id.map(str::to_owned),
            artifact_state: if missing.is_empty() {
                EvidenceArtifactState::Ready
            } else if completed.is_empty() {
                EvidenceArtifactState::None
            } else {
                EvidenceArtifactState::Collecting
            },
            completed_claims: completed,
            missing_claims: missing,
            export_action_id: None,
        };
    }
    for action in &mut view.actions {
        action.enabled = view.plans.iter().any(|plan| {
            plan.groups
                .iter()
                .flat_map(|group| &group.stages)
                .any(|stage| {
                    stage.action_id.as_deref() == Some(action.id.as_str())
                        && stage.status == StageStatus::Ready
                })
        });
    }
}

fn action_id(stage_id: &str) -> String {
    format!("run:{stage_id}")
}

fn has_any(profile: &RuntimeProfile, ids: &[&str]) -> bool {
    ids.iter().any(|id| {
        profile
            .capabilities
            .iter()
            .any(|capability| capability.id.as_str() == *id)
    })
}

fn matching_capabilities(profile: &RuntimeProfile, ids: &[&str]) -> Vec<String> {
    ids.iter()
        .filter(|id| {
            profile
                .capabilities
                .iter()
                .any(|capability| capability.id.as_str() == **id)
        })
        .map(|id| (*id).to_owned())
        .collect()
}

fn controls_observation() -> ObservationPrompt {
    ObservationPrompt {
        id: "controls".to_owned(),
        prompt: "Do the device's ordinary semantic controls still work correctly?".to_owned(),
        choices: outcome_choices("Controls work", "A control changed"),
    }
}

fn outcome_observation() -> ObservationPrompt {
    ObservationPrompt {
        id: "result".to_owned(),
        prompt: "Did the physical result match every requested checkpoint?".to_owned(),
        choices: outcome_choices("Every checkpoint matched", "A checkpoint failed"),
    }
}

fn outcome_choices(pass: &str, fail: &str) -> Vec<ObservationChoice> {
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

fn percent_encode(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            let _ = write!(encoded, "{byte:02X}");
        }
    }
    encoded
}

fn unix_seconds_to_rfc3339(seconds: u64) -> String {
    let days = i64::try_from(seconds / 86_400).unwrap_or(i64::MAX);
    let seconds_of_day = seconds % 86_400;
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;
    let shifted = days.saturating_add(719_468);
    let era = if shifted >= 0 {
        shifted
    } else {
        shifted.saturating_sub(146_096)
    } / 146_097;
    let day_of_era = shifted.saturating_sub(era.saturating_mul(146_097));
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era.saturating_add(era.saturating_mul(400));
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_parameter = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_parameter + 2) / 5 + 1;
    let month = month_parameter + if month_parameter < 10 { 3 } else { -9 };
    if month <= 2 {
        year += 1;
    }
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        BatteryPercent, EvidenceConfidence, FreshnessState, GenerationId, InventoryAvailability,
        PairingState, PresenceState, ProductId, ProjectionRevision, ProtocolVersion, ReceiverId,
        RestoreState, SequenceNumber, StreamEpoch, SupportLevel, TelemetryAvailability,
    };
    use hfx_protocol::{BatteryObservation, DeviceInventoryView, EventCursor, IntegrationView};
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
        BridgeIntegration {
            protocol_version: ProtocolVersion::try_from(5_u16).expect("protocol is valid"),
            view: IntegrationView {
                cursor: EventCursor {
                    stream_id: hfx_domain::StreamId::try_from("qualification-stream")
                        .expect("stream id is valid"),
                    stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
                    projection_revision: ProjectionRevision::try_from(1_u32)
                        .expect("projection is valid"),
                    sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
                },
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
            installed_module_identity: Some("same".to_owned()),
            loaded_module_identity: Some("same".to_owned()),
            service_state: ServiceState::Active,
            bridge: None,
            legacy_v2_stack_detected: false,
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
                result: first_result.clone(),
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
                    result: first_result.clone(),
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
}
