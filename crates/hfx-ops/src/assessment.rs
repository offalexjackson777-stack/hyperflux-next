// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    OpenRazerAccessState, OpenRazerDriverState, OpenRazerRuntimeState, ServiceState, SystemSnapshot,
};
use hfx_errors::{ErrorCode, error_by_code, remediation_by_id};
use hfx_integration_model::{
    OPENRAZER_NATIVE_CAPABILITY_FIELD, OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::str::FromStr;

pub const ASSESSMENT_SCHEMA: &str = "hyperflux-system-assessment-v1";

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
    pub driver: DriverState,
    pub bridge_ready: bool,
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
    findings: &mut Vec<AssessmentFinding>,
) {
    match snapshot.openrazer_runtime.state {
        OpenRazerRuntimeState::Verified => {
            working.push("the OpenRazer runtime source line is verified".to_owned());
        }
        OpenRazerRuntimeState::ReverificationRequired | OpenRazerRuntimeState::Unverified => {
            findings.push(local_finding(
                ErrorCode::HfxIntegration005,
                BTreeMap::from([
                    (
                        "observed_version".to_owned(),
                        snapshot
                            .openrazer_runtime
                            .observed_upstream_version
                            .clone()
                            .unwrap_or_else(|| "unavailable".to_owned()),
                    ),
                    (
                        "runtime_state".to_owned(),
                        if snapshot.openrazer_runtime.state
                            == OpenRazerRuntimeState::ReverificationRequired
                        {
                            "rebase-reverification-required"
                        } else {
                            "version-unverified"
                        }
                        .to_owned(),
                    ),
                    (
                        "verified_version".to_owned(),
                        OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION.to_owned(),
                    ),
                ]),
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
            working.push("OpenRazer application access is ready".to_owned());
        }
        OpenRazerAccessState::GroupUnavailable => findings.push(local_finding(
            ErrorCode::HfxIntegration002,
            BTreeMap::from([("access_state".to_owned(), "group-unavailable".to_owned())]),
        )),
        OpenRazerAccessState::SessionMembershipRequired => findings.push(local_finding(
            ErrorCode::HfxIntegration003,
            BTreeMap::from([(
                "access_state".to_owned(),
                "session-membership-required".to_owned(),
            )]),
        )),
        OpenRazerAccessState::NotInstalled | OpenRazerAccessState::Unverified => {}
    }
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

    let openrazer_driver = classify_openrazer_driver(snapshot, &mut working, &mut findings);

    let bridge_ready = snapshot.service_state == ServiceState::Active && snapshot.bridge.is_some();
    if bridge_ready {
        working.push("the bridge diagnostic endpoint is responding".to_owned());
        findings.extend(bridge_findings(snapshot));
    } else if !matches!(
        driver,
        DriverState::ActivationPending | DriverState::Unavailable
    ) && openrazer_driver == OpenRazerDriverState::Ready
    {
        findings.push(local_finding(
            ErrorCode::HfxService001,
            BTreeMap::from([(
                "service_state".to_owned(),
                snapshot
                    .service_state
                    .finding_value(snapshot.bridge.is_some())
                    .to_owned(),
            )]),
        ));
    }

    classify_openrazer_runtime(snapshot, &mut working, &mut findings);
    classify_openrazer_access(snapshot, &mut working, &mut findings);

    let (receiver_generations, qualified_generations) =
        snapshot.bridge.as_ref().map_or((0, 0), |bridge| {
            let receivers = bridge.snapshot.receivers.len();
            let qualified = bridge
                .snapshot
                .receivers
                .iter()
                .filter(|receiver| {
                    receiver.profile_id.is_some() && receiver.profile_digest.is_some()
                })
                .count();
            (receivers, qualified)
        });
    if bridge_ready {
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
    Assessment {
        schema: ASSESSMENT_SCHEMA.to_owned(),
        state,
        package_version: snapshot.package_version.clone(),
        driver,
        bridge_ready,
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
        return format!(
            "HyperFlux Next is ready.\nDriver active or idle | Bridge running | {} receiver generation(s)\nNo action required.\n",
            assessment.receiver_generations
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
    let bridge = if assessment.bridge_ready {
        "running"
    } else {
        "unavailable"
    };
    format!(
        "HyperFlux Next status\nDriver: {driver}\nBridge: {bridge}\nReceivers: {} generation(s), {} qualified\nNext: {}\n",
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
    use hfx_domain::{ProjectionRevision, QueueCapacity, SequenceNumber, StreamEpoch, StreamId};
    use hfx_protocol::{BridgeSnapshot, DiagnosticSnapshot, EventCursor};

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

    fn snapshot() -> SystemSnapshot {
        SystemSnapshot {
            package_version: "0.0.0-dev.1".to_owned(),
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
                state: crate::OpenRazerRuntimeState::Verified,
                observed_upstream_version: Some("3.12.4".to_owned()),
            },
            openrazer_access: OpenRazerAccessState::Ready,
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
    fn stale_openrazer_module_blocks_ready_without_service_noise() {
        let mut value = snapshot();
        value.openrazer_driver.modules[0].loaded = true;
        value.openrazer_driver.modules[0].loaded_identity = Some("old".to_owned());
        value.service_state = ServiceState::Inactive;
        value.bridge = None;

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-KERNEL-002");
        assert_eq!(assessment.findings[0].details["module"], "razerkbd");
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
    fn missing_native_module_capability_is_distinct_from_a_version_change() {
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
    fn future_openrazer_source_line_requires_rebase_without_claiming_capability_loss() {
        let mut value = snapshot();
        value.openrazer_runtime = crate::OpenRazerRuntimeSnapshot {
            state: crate::OpenRazerRuntimeState::ReverificationRequired,
            observed_upstream_version: Some("3.13.0".to_owned()),
        };

        let assessment = assess_system(&value);

        assert_eq!(assessment.state, AssessmentState::NeedsAttention);
        assert_eq!(assessment.findings.len(), 1);
        assert_eq!(assessment.findings[0].code, "HFX-INTEGRATION-005");
        assert_eq!(
            assessment.findings[0].details["runtime_state"],
            "rebase-reverification-required"
        );
        assert_eq!(assessment.findings[0].details["observed_version"], "3.13.0");
        assert_eq!(assessment.findings[0].details["verified_version"], "3.12.4");
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
}
