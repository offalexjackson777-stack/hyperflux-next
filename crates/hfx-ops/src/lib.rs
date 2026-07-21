// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod activation;
mod assessment;
mod configuration;
mod probe;
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
    ConfigMigrationError, ConfigMigrationOutcome, ConfigMigrationPlan, confirm_configuration,
    migrate_configuration,
};
pub use probe::{
    BridgeHealth, CommandOutput, CommandRunner, ProbeError, RealCommandRunner, RealSystemProbe,
    ServiceState, SystemController, SystemProbe, SystemSnapshot,
};
pub use support::{
    SupportBundle, SupportBundleError, SupportBundlePreview, SupportOutputDeclaration,
    SupportSideEffectDeclaration, build_support_bundle, preview_support_bundle,
    suggested_support_name, write_support_bundle,
};
