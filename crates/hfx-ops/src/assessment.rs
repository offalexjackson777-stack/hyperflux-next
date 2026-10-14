// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    FullComponentState, OpenRazerAccessState, OpenRazerDriverState, OpenRazerPublishedDevice,
    OpenRazerRuntimeState, ServiceState, SystemSnapshot,
};
use hfx_domain::{RouteKind, RouteState};
use hfx_errors::{ErrorCode, error_by_code, remediation_by_id};
use hfx_integration_model::{OPENRAZER_NATIVE_CAPABILITY_FIELD, SourceValidatedDeviceCatalog};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::str::FromStr;

pub const ASSESSMENT_SCHEMA: &str = "hyperflux-system-assessment-v2";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum AssessmentState {
    Ready,
    NeedsAttention,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum DriverState {
    InstalledIdle,
    Active,
    ActivationPending,
    Unavailable,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ComponentId {
    Core,
    Full,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ComponentState {
    Ready,
    OptionalNotInstalled,
    NeedsAttention,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentAssessment {
    pub component: ComponentId,
    pub state: ComponentState,
    pub required: bool,
    pub installed: bool,
    pub version: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AssessmentFinding {
    pub code: String,
    pub severity: String,
    pub technical_cause: String,
    pub user_explanation: String,
    pub safe_action: String,
    pub verification: String,
    pub documentation: String,
    pub details: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Assessment {
    pub schema: String,
    pub state: AssessmentState,
    pub package_version: String,
    pub components: Vec<ComponentAssessment>,
    pub driver: DriverState,
    pub bridge_ready: bool,
    /// Whether the bridge process answered at all. Not part of the JSON contract:
    /// `bridge_ready` keeps its published meaning (bridge and child routes ready).
    #[serde(skip)]
    pub bridge_responding: bool,
    pub receiver_generations: usize,
    pub qualified_generations: usize,
    pub working: Vec<String>,
    pub findings: Vec<AssessmentFinding>,
    pub next_action: String,
}

fn local_finding(code: ErrorCode, details: BTreeMap<String, String>) -> AssessmentFinding {
    let descriptor = error_by_code(code);
    let remediation = remediation_by_id(descriptor.remediation_id);
    AssessmentFinding {
        code: code.to_string(),
        severity: descriptor.severity.to_string(),
        technical_cause: descriptor.technical_cause.to_owned(),
        user_explanation: descriptor.user_explanation.to_owned(),
        safe_action: remediation.safe_action.to_owned(),
        verification: remediation.verification.to_owned(),
        documentation: descriptor.docs_path.to_owned(),
        details,
    }
}

fn bridge_findings(snapshot: &SystemSnapshot) -> Vec<AssessmentFinding> {
    let Some(bridge) = &snapshot.bridge else {
        return Vec::new();
    };
    bridge
        .diagnostics
        .findings
        .iter()
        .map(|finding| {
            let verification = ErrorCode::from_str(finding.finding_id.as_str())
                .ok()
                .map(error_by_code)
                .map_or(
                    "Run Doctor again and confirm the finding is absent.",
                    |descriptor| remediation_by_id(descriptor.remediation_id).verification,
                );
            AssessmentFinding {
                code: finding.finding_id.to_string(),
                severity: finding.severity.to_string(),
                technical_cause: finding.cause.to_string(),
                user_explanation: finding.explanation.to_string(),
                safe_action: finding.safe_action.to_string(),
                verification: verification.to_owned(),
                documentation: finding.documentation.to_string(),
                details: BTreeMap::new(),
            }
        })
        .collect()
}

fn classify_driver(snapshot: &SystemSnapshot, working: &mut Vec<String>) -> DriverState {
    match (
        snapshot.installed_module_identity.as_deref(),
        snapshot.loaded_module_identity.as_deref(),
    ) {
        (Some(installed), Some(loaded)) if installed == loaded => {
            working.push("the installed driver is active".to_owned());
            DriverState::Active
        }
        (Some(_), Some(_)) => DriverState::ActivationPending,
        (Some(_), None) if snapshot.supported_receiver_present => DriverState::ActivationPending,
        (Some(_), None) => {
            working.push("the driver is installed and no receiver is currently bound".to_owned());
            DriverState::InstalledIdle
        }
        (None, _) => DriverState::Unavailable,
    }
}

fn classify_openrazer_driver(
    snapshot: &SystemSnapshot,
    working: &mut Vec<String>,
    findings: &mut Vec<AssessmentFinding>,
) -> OpenRazerDriverState {
    let state = snapshot.openrazer_driver.state();
    if state == OpenRazerDriverState::Ready {
        working.push("the OpenRazer native-route driver is ready".to_owned());
        return state;
    }
    if let Some(module) = snapshot.openrazer_driver.blocking_module() {
        if state == OpenRazerDriverState::CapabilityUnavailable {
            findings.push(local_finding(
                ErrorCode::HfxIntegration004,
                BTreeMap::from([
                    (
                        "capability".to_owned(),
                        OPENRAZER_NATIVE_CAPABILITY_FIELD.to_owned(),
                    ),
                    ("module".to_owned(), module.module_name.clone()),
                ]),
            ));
            return state;
        }
        findings.push(local_finding(
            ErrorCode::HfxKernel002,
            BTreeMap::from([
                (
                    "installed_abi".to_owned(),
                    module
                        .installed_identity
                        .clone()
                        .unwrap_or_else(|| "not-installed".to_owned()),
                ),
                (
                    "loaded_abi".to_owned(),
                    if module.loaded {
                        module
                            .loaded_identity
                            .clone()
                            .unwrap_or_else(|| "identity-unavailable".to_owned())
                    } else {
                        "not-loaded".to_owned()
                    },
                ),
                ("module".to_owned(), module.module_name.clone()),
            ]),
        ));
    }
    state
}

fn classify_openrazer_runtime(
    snapshot: &SystemSnapshot,
    working: &mut Vec<String>,
    _findings: &mut Vec<AssessmentFinding>,
) {
    match snapshot.openrazer_runtime.state {
        OpenRazerRuntimeState::Detected => {
            let observed = snapshot
                .openrazer_runtime
                .observed_upstream_version
                .as_deref()
                .unwrap_or("version unavailable");
            working.push(format!(
                "OpenRazer userspace is installed ({observed}; version is diagnostic only)"
            ));
        }
        OpenRazerRuntimeState::NotInstalled => {}
    }
}

fn classify_openrazer_access(
    snapshot: &SystemSnapshot,
    working: &mut Vec<String>,
    findings: &mut Vec<AssessmentFinding>,
) {
    match snapshot.openrazer_access {
        OpenRazerAccessState::Ready => {
            let expected = expected_openrazer_published_devices(snapshot);
            let published = snapshot.openrazer_published_devices.as_deref();
            match (expected, published) {
                (Ok(expected), Some(published)) => {
                    let missing = missing_openrazer_published_devices(&expected, published);
                    if missing.is_empty() {
                        working.push("OpenRazer application access is ready".to_owned());
                    } else {
                        findings.push(local_finding(
                            ErrorCode::HfxIntegration003,
                            BTreeMap::from([
                                (
                                    "access_state".to_owned(),
                                    "device-publication-incomplete".to_owned(),
                                ),
                                (
                                    "expected_device_count".to_owned(),
                                    expected.len().to_string(),
                                ),
                                (
                                    "missing_devices".to_owned(),
                                    render_openrazer_device_identities(&missing),
                                ),
                                (
                                    "published_device_count".to_owned(),
                                    published.len().to_string(),
                                ),
                            ]),
                        ));
                    }
                }
                _ => findings.push(local_finding(
                    ErrorCode::HfxIntegration003,
                    BTreeMap::from([("access_state".to_owned(), "access-unverified".to_owned())]),
                )),
            }
        }
        OpenRazerAccessState::GroupUnavailable => findings.push(local_finding(
            ErrorCode::HfxIntegration002,
            BTreeMap::from([("access_state".to_owned(), "group-unavailable".to_owned())]),
        )),
        state @ (OpenRazerAccessState::SessionMembershipRequired
        | OpenRazerAccessState::DbusUnavailable
        | OpenRazerAccessState::Unverified) => findings.push(local_finding(
            ErrorCode::HfxIntegration003,
            BTreeMap::from([(
                "access_state".to_owned(),
                match state {
                    OpenRazerAccessState::SessionMembershipRequired => {
                        "session-membership-required"
                    }
                    OpenRazerAccessState::DbusUnavailable => "dbus-unavailable",
                    OpenRazerAccessState::Unverified => "access-unverified",
                    _ => unreachable!("matched OpenRazer access state"),
                }
                .to_owned(),
            )]),
        )),
        OpenRazerAccessState::NotInstalled | OpenRazerAccessState::NotApplicable => {}
    }
}

fn expected_openrazer_published_devices(
    snapshot: &SystemSnapshot,
) -> Result<Vec<OpenRazerPublishedDevice>, ()> {
    let catalog = SourceValidatedDeviceCatalog::load().map_err(|_| ())?;
    let mut expected = Vec::new();
    if let Some(bridge) = &snapshot.bridge {
        for receiver in &bridge.snapshot.receivers {
            if receiver.profile_id.is_none() || receiver.profile_digest.is_none() {
                continue;
            }
            for device in &receiver.devices {
                if device.profile_id.is_none()
                    || device.profile_digest.is_none()
                    || !device.endpoints.iter().any(|endpoint| {
                        endpoint.route_kind == RouteKind::HyperfluxWireless
                            && endpoint.route_state == RouteState::Available
                    })
                {
                    continue;
                }
                let Some(source) = catalog.devices().iter().find(|source| {
                    source.product_id == device.product_id
                        && source.device_kind == device.device_kind
                        && source.openrazer.is_some()
                }) else {
                    continue;
                };
                expected.push(OpenRazerPublishedDevice {
                    vendor_id: source.vendor_id.get(),
                    product_id: source.product_id.get(),
                });
            }
        }
    }
    expected.sort_unstable();
    Ok(expected)
}

fn missing_openrazer_published_devices(
    expected: &[OpenRazerPublishedDevice],
    published: &[OpenRazerPublishedDevice],
) -> Vec<OpenRazerPublishedDevice> {
    let mut remaining = published.to_vec();
    let mut missing = Vec::new();
    for expected_device in expected {
        if let Some(index) = remaining
            .iter()
            .position(|published_device| published_device == expected_device)
        {
            remaining.remove(index);
        } else {
            missing.push(*expected_device);
        }
    }
    missing
}

fn render_openrazer_device_identities(devices: &[OpenRazerPublishedDevice]) -> String {
    devices
        .iter()
        .map(|device| format!("{:04x}:{:04x}", device.vendor_id, device.product_id))
        .collect::<Vec<_>>()
        .join(",")
}

fn child_routes_ready(snapshot: &SystemSnapshot) -> bool {
    if !snapshot.supported_receiver_present {
        return true;
    }
    snapshot.bridge.as_ref().is_some_and(|bridge| {
        !bridge.snapshot.receivers.is_empty()
            && bridge.snapshot.receivers.iter().all(|receiver| {
                receiver.profile_id.is_some()
                    && receiver.profile_digest.is_some()
                    && !receiver.devices.is_empty()
                    && receiver.devices.iter().any(|device| {
                        device.profile_id.is_some()
                            && device.profile_digest.is_some()
                            && device.endpoints.iter().any(|endpoint| {
                                endpoint.route_kind == RouteKind::HyperfluxWireless
                                    && endpoint.route_state == RouteState::Available
                            })
                    })
            })
    })
}

/// Names the layer that is actually not ready. A responding bridge is never
/// reported as a service failure: restarting it cannot create receiver or
/// child-route evidence (Discussion #19).
fn readiness_finding(snapshot: &SystemSnapshot, bridge_responding: bool) -> AssessmentFinding {
    let receivers = snapshot
        .bridge
        .as_ref()
        .filter(|_| bridge_responding)
        .map(|bridge| &bridge.snapshot.receivers);
    let Some(receivers) = receivers else {
        return local_finding(
            ErrorCode::HfxService001,
            BTreeMap::from([(
                "service_state".to_owned(),
                snapshot
                    .service_state
                    .finding_value(snapshot.bridge.is_some())
                    .to_owned(),
            )]),
        );
    };
    let receiver_state = if receivers.is_empty() {
        Some("not-registered")
    } else if receivers
        .iter()
        .any(|receiver| receiver.profile_id.is_none() || receiver.profile_digest.is_none())
    {
        Some("unqualified")
    } else {
        None
    };
    if let Some(receiver_state) = receiver_state {
        return local_finding(
            ErrorCode::HfxService002,
            BTreeMap::from([("receiver_state".to_owned(), receiver_state.to_owned())]),
        );
    }
    let route_state = if receivers.iter().any(|receiver| receiver.devices.is_empty()) {
        "no-paired-device"
    } else {
        "not-live"
    };
    local_finding(
        ErrorCode::HfxService003,
        BTreeMap::from([("route_state".to_owned(), route_state.to_owned())]),
    )
}

fn assess_full_component(
    snapshot: &SystemSnapshot,
    working: &mut Vec<String>,
    findings: &mut Vec<AssessmentFinding>,
) -> usize {
    let finding_start = findings.len();
    match snapshot.full_component.state {
        FullComponentState::NotInstalled => {
            working.push("the optional Full integration component is not installed".to_owned());
        }
        FullComponentState::Ready => {
            working.push("the Full integration component is installed".to_owned());
            classify_openrazer_driver(snapshot, working, findings);
            classify_openrazer_runtime(snapshot, working, findings);
            classify_openrazer_access(snapshot, working, findings);
        }
        state => findings.push(local_finding(
            ErrorCode::HfxIntegration006,
            BTreeMap::from([(
                "component_state".to_owned(),
                state.finding_value().to_owned(),
            )]),
        )),
    }
    findings.len() - finding_start
}

fn receiver_generation_counts(snapshot: &SystemSnapshot) -> (usize, usize) {
    snapshot.bridge.as_ref().map_or((0, 0), |bridge| {
        let receivers = bridge.snapshot.receivers.len();
        let qualified = bridge
            .snapshot
            .receivers
            .iter()
            .filter(|receiver| receiver.profile_id.is_some() && receiver.profile_digest.is_some())
            .count();
        (receivers, qualified)
    })
}

fn component_assessments(
    snapshot: &SystemSnapshot,
    core_finding_count: usize,
    full_finding_count: usize,
) -> Vec<ComponentAssessment> {
    vec![
        ComponentAssessment {
            component: ComponentId::Core,
            state: if core_finding_count == 0 {
                ComponentState::Ready
            } else {
                ComponentState::NeedsAttention
            },
            required: true,
            installed: true,
            version: Some(snapshot.package_version.clone()),
        },
        ComponentAssessment {
            component: ComponentId::Full,
            state: match snapshot.full_component.state {
                FullComponentState::NotInstalled => ComponentState::OptionalNotInstalled,
                _ if full_finding_count == 0 => ComponentState::Ready,
                _ => ComponentState::NeedsAttention,
            },
            required: false,
            installed: snapshot.full_component.state != FullComponentState::NotInstalled,
            version: snapshot.full_component.product_version.clone(),
        },
    ]
}

#[must_use]
pub fn assess_system(snapshot: &SystemSnapshot) -> Assessment {
    let mut working = vec!["package metadata is available".to_owned()];
    let installed = snapshot.installed_module_identity.as_deref();
    let loaded = snapshot.loaded_module_identity.as_deref();
    let driver = classify_driver(snapshot, &mut working);

    let mut findings = Vec::new();
    if matches!(
        driver,
        DriverState::ActivationPending | DriverState::Unavailable
    ) {
        findings.push(local_finding(
            ErrorCode::HfxKernel001,
            BTreeMap::from([
                (
                    "installed_abi".to_owned(),
                    installed.unwrap_or("not-installed").to_owned(),
                ),
                (
                    "loaded_abi".to_owned(),
                    loaded
                        .unwrap_or(if snapshot.supported_receiver_present {
                            "receiver-unbound"
                        } else {
                            "not-loaded"
                        })
                        .to_owned(),
                ),
            ]),
        ));
    }

    let bridge_responding =
        snapshot.service_state == ServiceState::Active && snapshot.bridge.is_some();
    if bridge_responding {
        working.push("the bridge diagnostic endpoint is responding".to_owned());
        findings.extend(bridge_findings(snapshot));
    }
    let bridge_ready = bridge_responding && child_routes_ready(snapshot);
    if !bridge_ready
        && !matches!(
            driver,
            DriverState::ActivationPending | DriverState::Unavailable
        )
    {
        findings.push(readiness_finding(snapshot, bridge_responding));
    }

    let core_finding_count = findings.len();
    let full_finding_count = assess_full_component(snapshot, &mut working, &mut findings);

    let (receiver_generations, qualified_generations) = receiver_generation_counts(snapshot);
    if bridge_responding {
        working.push(format!(
            "{receiver_generations} receiver generation(s) are registered"
        ));
        if qualified_generations > 0 {
            working.push(format!(
                "{qualified_generations} receiver generation(s) are profile-qualified"
            ));
        }
    }

    let state = if findings.is_empty() {
        AssessmentState::Ready
    } else {
        AssessmentState::NeedsAttention
    };
    let next_action = findings.first().map_or_else(
        || "no action required".to_owned(),
        |finding| finding.safe_action.clone(),
    );
    let components = component_assessments(snapshot, core_finding_count, full_finding_count);
    Assessment {
        schema: ASSESSMENT_SCHEMA.to_owned(),
        state,
        package_version: snapshot.package_version.clone(),
        components,
        driver,
        bridge_ready,
        bridge_responding,
        receiver_generations,
        qualified_generations,
        working,
        findings,
        next_action,
    }
}

#[must_use]
pub fn render_doctor_text(assessment: &Assessment, explain: bool) -> String {
    if assessment.state == AssessmentState::Ready {
        let optional = assessment
            .components
            .iter()
            .find(|component| component.component == ComponentId::Full)
            .filter(|component| component.state == ComponentState::OptionalNotInstalled)
            .map_or("", |_| "Optional Full integration is not installed.\n");
        return format!(
            "HyperFlux Next is ready.\nDriver active or idle | Bridge running | {} receiver generation(s)\n{optional}No action required.\n",
            assessment.receiver_generations,
        );
    }
    let mut output = String::from("HyperFlux Next needs attention.\n");
    if let Some(primary) = assessment.findings.first() {
        let _ = writeln!(
            output,
            "Primary issue: [{}] {}",
            primary.code, primary.user_explanation
        );
        let _ = writeln!(output, "Why: {}", primary.technical_cause);
        let _ = writeln!(output, "Do: {}", primary.safe_action);
        let _ = writeln!(output, "Verify: {}", primary.verification);
    }
    if assessment.findings.len() > 1 {
        let _ = writeln!(
            output,
            "More: {} additional finding(s); run hyperfluxctl doctor --explain.",
            assessment.findings.len() - 1
        );
    }
    if explain {
        for finding in assessment.findings.iter().skip(1) {
            let _ = writeln!(
                output,
                "\n[{}] {}\nWhy: {}\nDo: {}\nVerify: {}",
                finding.code,
                finding.user_explanation,
                finding.technical_cause,
                finding.safe_action,
                finding.verification
            );
        }
    }
    output.push_str("Support: hyperfluxctl support-bundle --preview if the problem remains.\n");
    output
}

#[must_use]
pub fn render_status_text(assessment: &Assessment) -> String {
    let driver = match assessment.driver {
        DriverState::InstalledIdle => "installed (no receiver bound)",
        DriverState::Active => "active",
        DriverState::ActivationPending => "activation pending",
        DriverState::Unavailable => "unavailable",
    };
    let bridge = if assessment.bridge_responding {
        "running"
    } else {
        "unavailable"
    };
    let optional = assessment
        .components
        .iter()
        .find(|component| component.component == ComponentId::Full)
        .filter(|component| component.state == ComponentState::OptionalNotInstalled)
        .map_or("", |_| "Full: optional (not installed)\n");
    format!(
        "HyperFlux Next status\nDriver: {driver}\nBridge: {bridge}\nReceivers: {} generation(s), {} qualified\n{optional}Next: {}\n",
        assessment.receiver_generations, assessment.qualified_generations, assessment.next_action
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        BridgeHealth, KernelModuleIdentitySnapshot, OpenRazerDriverSnapshot, ServiceState,
        SystemSnapshot,
    };
    use hfx_domain::{
        ActivityState, ConnectionMode, ContactState, DeviceKind, EvidenceConfidence,
        FreshnessState, GenerationId, PairingState, PowerState, PresenceState, ProductId,
        ProfileDigest, ProfileId, ProjectionRevision, QueueCapacity, ReceiverId,
        ReceiverLifecycleState, RestoreState, SequenceNumber, SleepState, StreamEpoch, StreamId,
        SupportLevel, TelemetryAvailability,
    };
    use hfx_protocol::{
        BatteryObservation, BridgeSnapshot, DiagnosticSnapshot, EndpointSnapshot, EventCursor,
        LogicalDeviceSnapshot, ReceiverSnapshot,
    };

    fn empty_bridge() -> BridgeHealth {
        BridgeHealth {
            snapshot: BridgeSnapshot {
                cursor: EventCursor {
                    stream_id: StreamId::try_from("ops-test").expect("stream"),
                    stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch"),
                    projection_revision: ProjectionRevision::try_from(1_u32).expect("revision"),
                    sequence: SequenceNumber::try_from(0_u64).expect("sequence"),
                },
                receivers: Vec::new(),
            },
            diagnostics: DiagnosticSnapshot {
                sequence: SequenceNumber::try_from(0_u64).expect("sequence"),
                findings: Vec::new(),
                event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity"),
                transaction_queue_capacity: QueueCapacity::try_from(64).expect("capacity"),
            },
        }
    }

    fn bridge_with_child(route_state: RouteState) -> BridgeHealth {
        let mut bridge = empty_bridge();
        bridge.snapshot.receivers.push(ReceiverSnapshot {
            receiver_id: ReceiverId::try_from("receiver-1").expect("receiver"),
            generation_id: GenerationId::try_from(1_u64).expect("generation"),
            profile_id: Some(
                ProfileId::try_from("receiver.razer.hyperflux-v2.1532-00cf")
                    .expect("receiver profile"),
            ),
            profile_digest: Some(ProfileDigest::try_from("a".repeat(64)).expect("digest")),
            lifecycle: ReceiverLifecycleState::Active,
            devices: vec![LogicalDeviceSnapshot {
                device_id: "paired-mouse-00cd".try_into().expect("device"),
                device_kind: DeviceKind::Mouse,
                product_id: ProductId::try_from(0x00cd_u16).expect("product"),
                profile_id: Some(
                    ProfileId::try_from("child.razer.basilisk-v3-pro-35k.00cd")
                        .expect("child profile"),
                ),
                profile_digest: Some(ProfileDigest::try_from("b".repeat(64)).expect("digest")),
                pairing: PairingState::Paired,
                presence: PresenceState::Available,
                support_level: SupportLevel::LightingQualified,
                endpoints: vec![EndpointSnapshot {
                    endpoint_id: "hyperflux-pointer".try_into().expect("endpoint"),
                    route_kind: RouteKind::HyperfluxWireless,
                    route_state,
                    connection_mode: ConnectionMode::Hyperflux24ghz,
                    power_state: PowerState::Unknown,
                    sleep_state: SleepState::Unknown,
                    activity_state: ActivityState::Unknown,
                    contact_state: ContactState::Unknown,
                    freshness: FreshnessState::Fresh,
                    confidence: EvidenceConfidence::Observed,
                    evidence_claim_id: None,
                    observed_at_ms: None,
                    return_epoch: None,
                }],
                battery: BatteryObservation {
                    availability: TelemetryAvailability::Unknown,
                    percentage: None,
                    freshness: FreshnessState::Unknown,
                    confidence: EvidenceConfidence::Observed,
                    observed_at_ms: None,
                },
                capabilities: Vec::new(),
            }],
            ownership: Vec::new(),
            stable_restore_enabled: false,
            restore_state: RestoreState::Idle,
        });
        bridge
    }

    fn bridge_with_sibling(route_state: RouteState, qualified: bool) -> BridgeHealth {
        let mut bridge = bridge_with_child(RouteState::Available);
        let receiver = &mut bridge.snapshot.receivers[0];
        let mut sibling = receiver.devices[0].clone();
        sibling.device_id = "paired-keyboard-0296".try_into().expect("device");
        sibling.device_kind = DeviceKind::Keyboard;
        sibling.product_id = ProductId::try_from(0x0296_u16).expect("product");
        sibling.profile_id = qualified.then(|| {
            ProfileId::try_from("child.razer.deathstalker-v2-pro-tkl.0296").expect("child profile")
        });
        sibling.profile_digest =
            qualified.then(|| ProfileDigest::try_from("c".repeat(64)).expect("digest"));
        sibling.presence = match route_state {
            RouteState::Available => PresenceState::Available,
            RouteState::Unavailable => PresenceState::Unavailable,
            _ => PresenceState::Unknown,
        };
        sibling.support_level = if qualified {
            SupportLevel::LightingQualified
        } else {
            SupportLevel::Identified
        };
        sibling.endpoints[0].endpoint_id = "hyperflux-keyboard".try_into().expect("endpoint");
        sibling.endpoints[0].route_state = route_state;
        receiver.devices.push(sibling);
        bridge
    }

    fn snapshot() -> SystemSnapshot {
        SystemSnapshot {
            package_version: "2.0.0".to_owned(),
            full_component: crate::FullComponentSnapshot::ready("2.0.0"),
            installed_module_identity: Some("installed".to_owned()),
            loaded_module_identity: Some("installed".to_owned()),
            openrazer_driver: OpenRazerDriverSnapshot {
                modules: vec![KernelModuleIdentitySnapshot {
                    module_name: "razerkbd".to_owned(),
                    installed_identity: Some("installed".to_owned()),
                    loaded: false,
                    loaded_identity: None,
                    native_transport_capability: Some("razer-feature-report-v1".to_owned()),
                }],
            },
            openrazer_runtime: crate::OpenRazerRuntimeSnapshot {
                state: crate::OpenRazerRuntimeState::Detected,
                observed_upstream_version: Some("3.12.4".to_owned()),
            },
            openrazer_access: OpenRazerAccessState::Ready,
            openrazer_published_devices: Some(vec![
                OpenRazerPublishedDevice {
                    vendor_id: 0x1532,
                    product_id: 0x00cd,
                },
                OpenRazerPublishedDevice {
                    vendor_id: 0x1532,
                    product_id: 0x0296,
                },
            ]),
            service_state: ServiceState::Active,
            bridge: Some(empty_bridge()),
            legacy_v2_stack_detected: false,
            supported_receiver_present: false,
        }
    }

    #[test]
    fn ready_system_has_no_action() {
        let assessment = assess_system(&snapshot());
        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.findings.is_empty());
        assert_eq!(assessment.next_action, "no action required");
        assert_eq!(assessment.components[1].state, ComponentState::Ready);
    }

    #[test]
    fn healthy_core_does_not_require_optional_full_integrations() {
        let mut value = snapshot();
        value.full_component = crate::FullComponentSnapshot::not_installed();
        value.openrazer_driver.modules[0].installed_identity = None;
        value.openrazer_access = OpenRazerAccessState::DbusUnavailable;
        let assessment = assess_system(&value);
        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.findings.is_empty());
        assert_eq!(
            assessment.components[1].state,
            ComponentState::OptionalNotInstalled
        );
        assert_eq!(
            render_doctor_text(&assessment, false),
            "HyperFlux Next is ready.\nDriver active or idle | Bridge running | 0 receiver generation(s)\nOptional Full integration is not installed.\nNo action required.\n"
        );
    }

    #[test]
    fn invalid_full_descriptor_fails_closed_without_masking_core_health() {
        let mut value = snapshot();
        value.full_component = crate::FullComponentSnapshot {
            state: FullComponentState::DescriptorInvalid,
            product_version: None,
        };
        let assessment = assess_system(&value);
        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.components[0].state, ComponentState::Ready);
        assert_eq!(
            assessment.components[1].state,
            ComponentState::NeedsAttention
        );
        assert_eq!(assessment.findings[0].code, "HFX-INTEGRATION-006");
        assert_eq!(
            assessment.findings[0].details["component_state"],
            "descriptor-invalid"
        );
    }

    #[test]
    fn attached_receiver_without_children_is_not_ready() {
        let mut value = snapshot();
        value.supported_receiver_present = true;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert!(!assessment.bridge_ready);
        assert!(assessment.bridge_responding);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-002");
        assert_eq!(
            assessment.findings[0].details["receiver_state"],
            "not-registered"
        );
    }

    #[test]
    fn attached_receiver_requires_an_available_qualified_child_route() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_child(RouteState::Unknown));
        assert_eq!(assess_system(&value).state, AssessmentState::NeedsAttention);

        value.bridge = Some(bridge_with_child(RouteState::Available));
        let assessment = assess_system(&value);
        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.bridge_ready);
        assert!(assessment.findings.is_empty());
    }

    #[test]
    fn available_qualified_child_is_ready_with_unqualified_sibling() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_sibling(RouteState::Unknown, false));

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.bridge_ready);
        assert!(assessment.findings.is_empty());
    }

    #[test]
    fn available_qualified_child_is_ready_with_unavailable_sibling() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_sibling(RouteState::Unavailable, true));

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.bridge_ready);
        assert!(assessment.findings.is_empty());
    }

    #[test]
    fn stock_openrazer_access_requires_every_available_source_validated_child() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_sibling(RouteState::Available, true));
        value.openrazer_published_devices = Some(vec![OpenRazerPublishedDevice {
            vendor_id: 0x1532,
            product_id: 0x00cd,
        }]);

        let incomplete = assess_system(&value);

        assert!(
            incomplete.bridge_ready,
            "Core route health remains independent"
        );
        assert_eq!(incomplete.state, AssessmentState::NeedsAttention);
        assert_eq!(incomplete.findings.len(), 1);
        assert_eq!(incomplete.findings[0].code, "HFX-INTEGRATION-003");
        assert_eq!(
            incomplete.findings[0].details["access_state"],
            "device-publication-incomplete"
        );
        assert_eq!(
            incomplete.findings[0].details["missing_devices"],
            "1532:0296"
        );

        value
            .openrazer_published_devices
            .as_mut()
            .expect("published inventory")
            .push(OpenRazerPublishedDevice {
                vendor_id: 0x1532,
                product_id: 0x0296,
            });
        let complete = assess_system(&value);
        assert_eq!(complete.state, AssessmentState::Ready);
        assert!(complete.findings.is_empty());
    }

    #[test]
    fn zero_qualified_available_child_routes_is_not_ready() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_sibling(RouteState::Unavailable, false));
        value.bridge.as_mut().expect("bridge").snapshot.receivers[0].devices[0].endpoints[0]
            .route_state = RouteState::Unavailable;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert!(!assessment.bridge_ready);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-003");
        assert_eq!(assessment.findings[0].details["route_state"], "not-live");
    }

    #[test]
    fn paired_mouse_without_a_live_route_is_not_a_bridge_failure() {
        // Discussion #19: bridge running, receiver qualified, mouse paired but
        // not live. Restarting the bridge cannot help, so it must not be advised.
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = Some(bridge_with_child(RouteState::Unavailable));

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert!(!assessment.bridge_ready);
        assert!(assessment.bridge_responding);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-003");
        assert_eq!(assessment.findings[0].details["route_state"], "not-live");
        assert!(
            !assessment.next_action.contains("restart"),
            "a responding bridge must not be restarted: {}",
            assessment.next_action
        );
        let status = render_status_text(&assessment);
        assert!(status.contains("Bridge: running\n"), "{status}");
        assert!(
            status.contains("Next: Move or click the paired device."),
            "{status}"
        );
    }

    #[test]
    fn qualified_receiver_without_paired_devices_names_the_route_state() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        let mut bridge = bridge_with_child(RouteState::Available);
        bridge.snapshot.receivers[0].devices.clear();
        value.bridge = Some(bridge);

        let assessment = assess_system(&value);

        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-003");
        assert_eq!(
            assessment.findings[0].details["route_state"],
            "no-paired-device"
        );
    }

    #[test]
    fn unqualified_receiver_is_named_without_blaming_the_bridge() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        let mut bridge = bridge_with_child(RouteState::Available);
        bridge.snapshot.receivers[0].profile_id = None;
        value.bridge = Some(bridge);

        let assessment = assess_system(&value);

        assert!(assessment.bridge_responding);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-002");
        assert_eq!(
            assessment.findings[0].details["receiver_state"],
            "unqualified"
        );
    }

    #[test]
    fn unreachable_bridge_still_reports_the_service_and_unavailable_status() {
        let mut value = snapshot();
        value.supported_receiver_present = true;
        value.bridge = None;

        let assessment = assess_system(&value);

        assert!(!assessment.bridge_responding);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-001");
        assert!(render_status_text(&assessment).contains("Bridge: unavailable\n"));
    }

    #[test]
    fn kernel_activation_suppresses_secondary_service_noise() {
        let mut value = snapshot();
        value.loaded_module_identity = Some("old".to_owned());
        value.service_state = ServiceState::Inactive;
        value.bridge = None;
        let assessment = assess_system(&value);
        assert_eq!(assessment.driver, DriverState::ActivationPending);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-KERNEL-001");
    }

    #[test]
    fn attached_supported_receiver_requires_driver_activation_before_service_start() {
        let mut value = snapshot();
        value.loaded_module_identity = None;
        value.supported_receiver_present = true;
        value.service_state = ServiceState::Inactive;
        value.bridge = None;

        let assessment = assess_system(&value);

        assert_eq!(assessment.driver, DriverState::ActivationPending);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-KERNEL-001");
        assert_eq!(
            assessment.findings[0].details["loaded_abi"],
            "receiver-unbound"
        );
    }

    #[test]
    fn active_but_unreachable_service_is_named_truthfully() {
        let mut value = snapshot();
        value.bridge = None;
        let assessment = assess_system(&value);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-001");
        assert_eq!(
            assessment.findings[0].details["service_state"],
            "active-unready"
        );
    }

    #[test]
    fn stale_openrazer_module_and_inactive_core_are_reported_independently() {
        let mut value = snapshot();
        value.openrazer_driver.modules[0].loaded = true;
        value.openrazer_driver.modules[0].loaded_identity = Some("old".to_owned());
        value.service_state = ServiceState::Inactive;
        value.bridge = None;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.findings.len(), 2);
        assert_eq!(assessment.findings[0].code, "HFX-SERVICE-001");
        assert_eq!(assessment.findings[1].code, "HFX-KERNEL-002");
        assert_eq!(assessment.findings[1].details["module"], "razerkbd");
    }

    #[test]
    fn optional_openrazer_absence_does_not_block_the_core_bridge() {
        let mut value = snapshot();
        value.openrazer_runtime = crate::OpenRazerRuntimeSnapshot {
            state: crate::OpenRazerRuntimeState::NotInstalled,
            observed_upstream_version: None,
        };
        value.openrazer_access = OpenRazerAccessState::NotInstalled;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.findings.is_empty());
    }

    #[test]
    fn missing_native_module_capability_is_reported_independently() {
        let mut value = snapshot();
        value.openrazer_driver.modules[0].native_transport_capability = None;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-INTEGRATION-004");
        assert_eq!(assessment.findings[0].details["module"], "razerkbd");
        assert_eq!(
            assessment.findings[0].details["capability"],
            "hyperflux_native_transport"
        );
    }

    #[test]
    fn future_openrazer_userspace_version_is_diagnostic_only() {
        let mut value = snapshot();
        value.openrazer_runtime = crate::OpenRazerRuntimeSnapshot {
            state: crate::OpenRazerRuntimeState::Detected,
            observed_upstream_version: Some("3.13.0".to_owned()),
        };

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::Ready);
        assert!(assessment.findings.is_empty());
        assert!(
            assessment
                .working
                .iter()
                .any(|message| message.contains("3.13.0") && message.contains("diagnostic only"))
        );
    }

    #[test]
    fn installed_openrazer_names_missing_session_membership_exactly() {
        let mut value = snapshot();
        value.openrazer_access = OpenRazerAccessState::SessionMembershipRequired;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-INTEGRATION-003");
        assert_eq!(
            assessment.findings[0].details["access_state"],
            "session-membership-required"
        );
    }

    #[test]
    fn stock_openrazer_bus_failures_are_named_without_claiming_daemon_ownership() {
        for (state, detail) in [
            (OpenRazerAccessState::DbusUnavailable, "dbus-unavailable"),
            (OpenRazerAccessState::Unverified, "access-unverified"),
        ] {
            let mut value = snapshot();
            value.openrazer_access = state;
            let assessment = assess_system(&value);
            assert_eq!(assessment.findings.len(), 1);
            assert_eq!(assessment.findings[0].code, "HFX-INTEGRATION-003");
            assert_eq!(assessment.findings[0].details["access_state"], detail);
        }

        let mut root = snapshot();
        root.openrazer_access = OpenRazerAccessState::NotApplicable;
        let assessment = assess_system(&root);
        assert!(assessment.findings.is_empty());
    }
}
