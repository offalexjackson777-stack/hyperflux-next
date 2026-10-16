// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod assessment;
mod configuration;
mod qualification;
mod support;

pub use assessment::{
    Assessment, AssessmentFinding, AssessmentState, ComponentAssessment, ComponentId,
    ComponentState, DriverState, assess_system, render_doctor_text, render_status_text,
};
pub use configuration::{
    ConfigMigrationError, ConfigMigrationOutcome, ConfigMigrationPlan,
    OperationalConfigurationOutcome, QualificationConfigurationOutcome, confirm_configuration,
    enable_operational_configuration, enable_qualification_configuration, migrate_configuration,
    restore_qualification_configuration,
};
pub use hfx_device_qualification::{
    ActionConfirmation, BatteryAvailability, BatteryView, CapabilityAccess, CapabilityView,
    CompanionAction, CompanionState, CompanionView, DeviceView, EvidenceArtifactState,
    EvidenceView, FirmwareCollection, NativeObservationError, NativeObservationPrivacy,
    NativeRouteObservation, ObservationChoice, ObservationPrompt, PresenceView, ProfileBindingView,
    QUALIFICATION_API_VERSION, QUALIFICATION_ENDPOINT, QUALIFICATION_REPORT_PRODUCER,
    QUALIFICATION_SCHEMA, QualificationCollection, QualificationGroup, QualificationPlan,
    QualificationServerConfig, QualificationServerError, QualificationStage,
    QualificationSystemView, QualificationView, ReceiverLifecycleView, ReceiverView,
    ReportBridgeRequest, ReportFinding, ReportFindingSeverity, RiskLevel, RunnerAvailability,
    RunnerCapabilities, StageKind, StageResult, StageStatus, SupportView, VerdictState,
    build_native_route_observations, build_qualification_report, build_qualification_view,
    default_native_observation_directory, qualification_generated_at, serve_qualification_console,
    write_native_route_observations,
};
pub use hfx_system_probe::{
    BridgeHealth, BridgeIntegration, CommandOutput, CommandRunner, DriverActivationController,
    DriverActivationError, DriverActivationReceipt, FullComponentSnapshot, FullComponentState,
    IntegrationProjection, KernelModuleIdentitySnapshot, OpenRazerAccessState,
    OpenRazerDriverProbe, OpenRazerDriverSnapshot, OpenRazerDriverState, OpenRazerPublishedDevice,
    OpenRazerRuntimeSnapshot, OpenRazerRuntimeState, ProbeError, RealCommandRunner,
    RealSystemProbe, ServiceState, SystemController, SystemProbe, SystemSnapshot,
};
pub use qualification::{
    QualificationReportError, collect_qualification_report, qualification_report_json,
    render_qualification_text, write_qualification_report,
};
pub use support::{
    SupportBundle, SupportBundleError, SupportBundlePreview, SupportOutputDeclaration,
    SupportSideEffectDeclaration, build_support_bundle, preview_support_bundle,
    suggested_support_name, write_support_bundle,
};
