// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod native_onboarding;
mod qualification;
mod qualification_event_evidence;
mod qualification_lifecycle;
mod qualification_migration;
mod qualification_runner;
mod qualification_server;
mod qualification_state;

pub(crate) use hfx_system_probe::{
    BridgeIntegration, RealSystemProbe, ServiceState, SystemProbe, SystemSnapshot,
};
#[cfg(test)]
pub(crate) use hfx_system_probe::{
    FullComponentSnapshot, KernelModuleIdentitySnapshot, OpenRazerAccessState,
    OpenRazerDriverSnapshot, OpenRazerRuntimeSnapshot, OpenRazerRuntimeState,
};

pub use native_onboarding::{
    NativeObservationError, NativeObservationPrivacy, NativeRouteObservation,
    build_native_route_observations, default_native_observation_directory,
    write_native_route_observations,
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
