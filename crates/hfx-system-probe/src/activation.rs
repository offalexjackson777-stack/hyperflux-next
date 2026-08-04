// SPDX-License-Identifier: GPL-2.0-only

use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DriverActivationReceipt {
    pub installed_module_identity: String,
    pub loaded_module_identity: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DriverActivationError {
    ReceiverPresent,
    ReceiverInventoryUnavailable,
    UdevSettleFailed,
    InstalledIdentityUnavailable,
    LoadedIdentityUnavailable,
    ModuleReferencesUnavailable,
    ModuleBusy,
    ModuleUnloadFailed,
    ModuleUnloadIncomplete,
    ModuleLoadFailed,
    IdentityMismatch,
    IdentityUnstable,
}

impl fmt::Display for DriverActivationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::ReceiverPresent => {
                "the supported receiver is still connected; disconnect it before driver activation"
            }
            Self::ReceiverInventoryUnavailable => "receiver presence could not be verified safely",
            Self::UdevSettleFailed => "udev did not settle within the activation bound",
            Self::InstalledIdentityUnavailable => {
                "the installed kernel module identity is unavailable"
            }
            Self::LoadedIdentityUnavailable => "the loaded kernel module identity is unavailable",
            Self::ModuleReferencesUnavailable => {
                "kernel module references could not be verified safely"
            }
            Self::ModuleBusy => "the old kernel module still has active references",
            Self::ModuleUnloadFailed => "the old kernel module could not be unloaded",
            Self::ModuleUnloadIncomplete => {
                "the old kernel module remained present after bounded unload settling"
            }
            Self::ModuleLoadFailed => "the newly installed kernel module could not be loaded",
            Self::IdentityMismatch => {
                "the loaded kernel module does not match the installed module"
            }
            Self::IdentityUnstable => {
                "the installed or loaded kernel module identity changed during verification"
            }
        })
    }
}

impl std::error::Error for DriverActivationError {}

pub trait DriverActivationController {
    /// Replaces an unbound older module and proves the installed module is active.
    ///
    /// # Errors
    ///
    /// Returns a typed fail-closed error for receiver presence, outstanding
    /// references, bounded settling, module replacement, or identity proof.
    fn activate_disconnected_driver(
        &self,
    ) -> Result<DriverActivationReceipt, DriverActivationError>;
}
