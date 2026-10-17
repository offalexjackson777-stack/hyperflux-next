// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod activation;
mod component;
mod dkms_migration;
mod probe;

pub use activation::{DriverActivationController, DriverActivationError, DriverActivationReceipt};
pub use component::{FullComponentSnapshot, FullComponentState};
pub use dkms_migration::{
    ImageTool, MigrationError, MigrationHost, MigrationPlan, MigrationReceipt, MigrationRoots,
    RealMigrationHost, apply_migration, plan_migration, render_migration_preview,
};
pub use probe::{
    BridgeHealth, BridgeIntegration, CommandOutput, CommandRunner, IntegrationProjection,
    KernelModuleIdentitySnapshot, OpenRazerAccessState, OpenRazerDriverProbe,
    OpenRazerDriverSnapshot, OpenRazerDriverState, OpenRazerPublishedDevice,
    OpenRazerRuntimeSnapshot, OpenRazerRuntimeState, ProbeError, RealCommandRunner,
    RealSystemProbe, ServiceState, SystemController, SystemProbe, SystemSnapshot,
    write_private_file,
};
