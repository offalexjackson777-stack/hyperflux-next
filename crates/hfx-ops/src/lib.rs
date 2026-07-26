// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod activation;
mod assessment;
mod configuration;
mod native_onboarding;
mod probe;
mod qualification;
mod qualification_event_evidence;
mod qualification_lifecycle;
mod qualification_migration;
mod qualification_runner;
mod qualification_server;
mod qualification_state;
mod support;

pub use activation::{
    ActivationAction, ActivationDecision, ActivationError, UpdateIntent, decide_post_update,
    load_update_intent, record_update_intent, remove_update_intent,
};
pub use assessment::{
    Assessment, AssessmentFinding, AssessmentState, DriverState, assess_system, render_doctor_text,
    render_status_text,
};
pub use configuration::{
    ConfigMigrationError, ConfigMigrationOutcome, ConfigMigrationPlan,
    QualificationConfigurationOutcome, confirm_configuration, enable_qualification_configuration,
    migrate_configuration, restore_qualification_configuration,
};
pub use native_onboarding::{
    NativeObservationError, NativeObservationPrivacy, NativeRouteObservation,
    build_native_route_observations, default_native_observation_directory,
    write_native_route_observations,
};
pub use probe::{
    BridgeHealth, BridgeIntegration, CommandOutput, CommandRunner, KernelModuleIdentitySnapshot,
    OpenRazerAccessState, OpenRazerDriverSnapshot, OpenRazerDriverState, ProbeError,
    RealCommandRunner, RealSystemProbe, ServiceState, SystemController, SystemProbe,
    SystemSnapshot,
};
pub use qualification::{
    ActionConfirmation, BatteryAvailability, BatteryView, CapabilityAccess, CapabilityView,
    CompanionAction, CompanionState, CompanionView, DeviceView, EvidenceArtifactState,
    EvidenceView, ObservationChoice, ObservationPrompt, PresenceView, ProfileBindingView,
    QUALIFICATION_API_VERSION, QUALIFICATION_ENDPOINT, QUALIFICATION_SCHEMA, QualificationGroup,
    QualificationPlan, QualificationStage, QualificationSystemView, QualificationView,
    ReceiverLifecycleView, ReceiverView, RiskLevel, RunnerAvailability, RunnerCapabilities,
    StageKind, StageResult, StageStatus, SupportView, VerdictState, build_qualification_view,
    qualification_generated_at,
};
pub use qualification_server::{
    QualificationServerConfig, QualificationServerError, serve_qualification_console,
};
pub use support::{
    SupportBundle, SupportBundleError, SupportBundlePreview, SupportOutputDeclaration,
    SupportSideEffectDeclaration, build_support_bundle, preview_support_bundle,
    suggested_support_name, write_support_bundle,
};
