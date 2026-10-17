// SPDX-License-Identifier: GPL-2.0-only

use crate::activation::{
    DriverActivationController, DriverActivationError, DriverActivationReceipt,
};
use crate::component::{FullComponentSnapshot, probe_full_component};
use hfx_domain::{
    ClientId, ClientName, EventBatchLimit, ProtocolFeatureId, ProtocolVersion, SequenceNumber,
};
use hfx_integration_model::{
    OPENRAZER_NATIVE_CAPABILITY_FIELD, OPENRAZER_NATIVE_CAPABILITY_MODULES,
    OPENRAZER_NATIVE_CAPABILITY_VALUE, OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH,
    OPENRAZER_NATIVE_DRIVER_MODULES, OPENRAZER_NATIVE_KEY_EVENT_LINKS,
    OPENRAZER_NATIVE_VERSION_ARGUMENT,
};
use hfx_profiles::receiver_profile_by_usb_id;
use hfx_protocol::{
    BridgeSnapshot, CURRENT_PROTOCOL_VERSION, DiagnosticSnapshot, EventBatch, EventCursor,
    IntegrationView, NativeRouteInventory,
};
use hfx_runtime::{
    BRIDGE_SERVICE_UNIT, BRIDGE_SOCKET_PATH, EVENT_CAPACITY, FULL_COMPONENT_DESCRIPTOR_PATH,
    KERNEL_MODULE_NAME, PRODUCT_VERSION, RETIRED_KERNEL_DRIVERS, STATUS_TIMEOUT_MS,
};
use hfx_sdk::{EventSubscription, HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::fmt;
use std::fs;
use std::io;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const MAX_COMMAND_OUTPUT_BYTES: usize = 4_096;
const MAX_USB_DEVICE_ENTRIES: usize = 256;
const MAX_MODULE_HOLDERS: usize = 64;
const MAX_DRIVER_BINDINGS: usize = 256;
const USB_DEVICES_ROOT: &str = "/sys/bus/usb/devices";
const HID_DEVICES_ROOT: &str = "/sys/bus/hid/devices";
const DKMS_ROOT: &str = "/var/lib/dkms";
const INPUT_BY_ID_ROOT: &str = "/dev/input/by-id";
const HID_BUS_USB: &str = "0003";
const OPENRAZER_GROUP: &str = "openrazer";
const OPENRAZER_SESSION_BUS_NAME: &str = "org.razer";
const OPENRAZER_ROOT_OBJECT_PATH: &str = "/org/razer";
const OPENRAZER_DEVICES_INTERFACE: &str = "razer.devices";
const OPENRAZER_GET_DEVICES_METHOD: &str = "getDevices";
const OPENRAZER_DEVICE_OBJECT_PREFIX: &str = "/org/razer/device/";
const OPENRAZER_DEVICE_MISC_INTERFACE: &str = "razer.device.misc";
const OPENRAZER_GET_VID_PID_METHOD: &str = "getVidPid";
const MAX_OPENRAZER_PUBLISHED_DEVICES: usize = 64;
const MAX_OPENRAZER_SERIAL_BYTES: usize = 128;
const DBUS_SERVICE_NAME: &str = "org.freedesktop.DBus";
const DBUS_OBJECT_PATH: &str = "/org/freedesktop/DBus";
const DBUS_INTERFACE_NAME: &str = "org.freedesktop.DBus";
const DBUS_NAME_HAS_OWNER_METHOD: &str = "NameHasOwner";
const HID_DRIVERS_ROOT: &str = "/sys/bus/hid/drivers";
const DRIVER_ACTIVATION_SETTLE_SECONDS: &str = "5";
const DRIVER_ACTIVATION_COMMAND_TIMEOUT: Duration = Duration::from_secs(6);
const DRIVER_IDENTITY_STABILITY_DELAY: Duration = Duration::from_millis(50);
const DRIVER_REFERENCE_SETTLE_TIMEOUT: Duration = Duration::from_secs(2);
const DRIVER_REFERENCE_POLL_INTERVAL: Duration = Duration::from_millis(25);
const EVENT_SUBSCRIPTIONS_FEATURE: &str = "event-subscriptions";
const INTEGRATION_VIEW_FEATURE: &str = "integration-view-projection";
const STRUCTURED_DIAGNOSTICS_FEATURE: &str = "structured-diagnostics";
const NATIVE_RECEIVER_TUNNEL_FEATURE: &str = "native-receiver-tunnel";
const LEGACY_V2_BRIDGE_SERVICE_UNIT: &str = "hyperflux-bridge.service";
const LEGACY_V2_KERNEL_MODULE_NAME: &str = "hid-razer-hyperflux-v2";
static QUALIFICATION_CLIENT_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ServiceState {
    Active,
    Activating,
    Failed,
    Inactive,
    Stopping,
    Unavailable,
}

impl ServiceState {
    #[must_use]
    pub const fn finding_value(self, bridge_present: bool) -> &'static str {
        match self {
            Self::Active if !bridge_present => "active-unready",
            Self::Active => "active-unready",
            Self::Activating => "activating",
            Self::Failed => "failed",
            Self::Inactive => "inactive",
            Self::Stopping => "stopping",
            Self::Unavailable => "unavailable",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProbeError {
    CommandUnavailable,
    CommandTimedOut,
    CommandOutputInvalid,
    BridgeUnavailable,
}

impl fmt::Display for ProbeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::CommandUnavailable => "a required local status command is unavailable",
            Self::CommandTimedOut => "a bounded local status command timed out",
            Self::CommandOutputInvalid => "a local status command returned invalid output",
            Self::BridgeUnavailable => "the local bridge diagnostic endpoint is unavailable",
        })
    }
}

impl std::error::Error for ProbeError {}

pub trait CommandRunner {
    /// Runs one bounded local command without invoking a shell.
    ///
    /// # Errors
    ///
    /// Returns a sanitized command failure without command output or private paths.
    fn run(
        &self,
        program: &str,
        arguments: &[&str],
        timeout: Duration,
    ) -> Result<CommandOutput, ProbeError>;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct RealCommandRunner;

impl CommandRunner for RealCommandRunner {
    fn run(
        &self,
        program: &str,
        arguments: &[&str],
        timeout: Duration,
    ) -> Result<CommandOutput, ProbeError> {
        let mut child = Command::new(program)
            .args(arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| ProbeError::CommandUnavailable)?;
        let started = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if started.elapsed() < timeout => {
                    thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(ProbeError::CommandTimedOut);
                }
                Err(_) => return Err(ProbeError::CommandUnavailable),
            }
        }
        let output = child
            .wait_with_output()
            .map_err(|_| ProbeError::CommandUnavailable)?;
        if output.stdout.len() > MAX_COMMAND_OUTPUT_BYTES {
            return Err(ProbeError::CommandOutputInvalid);
        }
        let stdout = String::from_utf8(output.stdout)
            .map_err(|_| ProbeError::CommandOutputInvalid)?
            .trim()
            .to_owned();
        Ok(CommandOutput {
            success: output.status.success(),
            stdout,
        })
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeHealth {
    pub snapshot: BridgeSnapshot,
    pub diagnostics: DiagnosticSnapshot,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeIntegration {
    pub protocol_version: ProtocolVersion,
    pub view: IntegrationView,
    pub events: EventBatch,
}

/// The bridge's integration projection read without an event subscription.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IntegrationProjection {
    pub protocol_version: ProtocolVersion,
    pub view: IntegrationView,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SystemSnapshot {
    pub package_version: String,
    pub full_component: FullComponentSnapshot,
    pub installed_module_identity: Option<String>,
    pub loaded_module_identity: Option<String>,
    pub openrazer_driver: OpenRazerDriverSnapshot,
    pub openrazer_runtime: OpenRazerRuntimeSnapshot,
    pub openrazer_access: OpenRazerAccessState,
    pub openrazer_published_devices: Option<Vec<OpenRazerPublishedDevice>>,
    pub service_state: ServiceState,
    pub bridge: Option<BridgeHealth>,
    pub legacy_v2_stack_detected: bool,
    pub supported_receiver_present: bool,
    /// Kernel driver bound to each attached qualified receiver HID interface,
    /// in stable interface order; `None` means the interface is unbound.
    pub receiver_interface_drivers: Vec<Option<String>>,
    /// Registered DKMS trees of retired `HyperFlux` drivers, as `name/version`.
    pub retired_driver_registrations: Vec<String>,
    /// `/dev/input/by-id` key-event links the Full udev rules add for an
    /// attached receiver that do not resolve, so `OpenRazer` cannot read the
    /// keyboard's key presses; sorted and empty without the Full component.
    pub missing_openrazer_key_event_links: Vec<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OpenRazerPublishedDevice {
    pub vendor_id: u16,
    pub product_id: u16,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpenRazerAccessState {
    NotInstalled,
    NotApplicable,
    Ready,
    GroupUnavailable,
    SessionMembershipRequired,
    DbusUnavailable,
    Unverified,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpenRazerRuntimeState {
    NotInstalled,
    Detected,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpenRazerRuntimeSnapshot {
    pub state: OpenRazerRuntimeState,
    pub observed_upstream_version: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KernelModuleIdentitySnapshot {
    pub module_name: String,
    pub installed_identity: Option<String>,
    pub loaded: bool,
    pub loaded_identity: Option<String>,
    pub native_transport_capability: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpenRazerDriverState {
    Ready,
    ActivationPending,
    CapabilityUnavailable,
    Unavailable,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpenRazerDriverSnapshot {
    pub modules: Vec<KernelModuleIdentitySnapshot>,
}

impl OpenRazerDriverSnapshot {
    #[must_use]
    pub fn state(&self) -> OpenRazerDriverState {
        if self
            .modules
            .iter()
            .any(|module| module.installed_identity.is_none())
        {
            return OpenRazerDriverState::Unavailable;
        }
        if self.modules.iter().any(|module| {
            OPENRAZER_NATIVE_CAPABILITY_MODULES.contains(&module.module_name.as_str())
                && module.native_transport_capability.as_deref()
                    != Some(OPENRAZER_NATIVE_CAPABILITY_VALUE)
        }) {
            return OpenRazerDriverState::CapabilityUnavailable;
        }
        if self.modules.iter().any(|module| {
            module.loaded
                && module.loaded_identity.as_deref() != module.installed_identity.as_deref()
        }) {
            return OpenRazerDriverState::ActivationPending;
        }
        OpenRazerDriverState::Ready
    }

    #[must_use]
    pub fn blocking_module(&self) -> Option<&KernelModuleIdentitySnapshot> {
        self.modules
            .iter()
            .find(|module| module.installed_identity.is_none())
            .or_else(|| {
                self.modules.iter().find(|module| {
                    OPENRAZER_NATIVE_CAPABILITY_MODULES.contains(&module.module_name.as_str())
                        && module.native_transport_capability.as_deref()
                            != Some(OPENRAZER_NATIVE_CAPABILITY_VALUE)
                })
            })
            .or_else(|| {
                self.modules.iter().find(|module| {
                    module.loaded
                        && module.loaded_identity.as_deref() != module.installed_identity.as_deref()
                })
            })
    }
}

pub trait SystemProbe {
    fn snapshot(&self) -> SystemSnapshot;
}

pub trait OpenRazerDriverProbe {
    fn openrazer_driver_snapshot(&self) -> OpenRazerDriverSnapshot;
}

pub trait SystemController {
    /// Enables and starts the conservative read-only service after a fresh install.
    ///
    /// # Errors
    ///
    /// Returns a bounded local-control failure.
    fn enable_bridge(&self) -> Result<(), ProbeError>;

    /// Restarts the compatible bridge service without changing kernel or hardware state.
    ///
    /// # Errors
    ///
    /// Returns a bounded local-control failure.
    fn restart_bridge(&self) -> Result<(), ProbeError>;

    /// Stops the bridge before package replacement.
    ///
    /// # Errors
    ///
    /// Returns a bounded local-control failure.
    fn stop_bridge(&self) -> Result<(), ProbeError>;
}

#[derive(Clone, Debug)]
pub struct RealSystemProbe<R = RealCommandRunner> {
    runner: R,
    sys_module_root: PathBuf,
    hid_drivers_root: PathBuf,
    hid_devices_root: PathBuf,
    dkms_root: PathBuf,
    usb_devices_root: PathBuf,
    input_by_id_root: PathBuf,
    openrazer_daemon_path: PathBuf,
    full_component_descriptor_path: PathBuf,
    socket_path: PathBuf,
    timeout: Duration,
}

impl Default for RealSystemProbe<RealCommandRunner> {
    fn default() -> Self {
        Self {
            runner: RealCommandRunner,
            sys_module_root: PathBuf::from("/sys/module"),
            hid_drivers_root: PathBuf::from(HID_DRIVERS_ROOT),
            hid_devices_root: PathBuf::from(HID_DEVICES_ROOT),
            dkms_root: PathBuf::from(DKMS_ROOT),
            usb_devices_root: PathBuf::from(USB_DEVICES_ROOT),
            input_by_id_root: PathBuf::from(INPUT_BY_ID_ROOT),
            openrazer_daemon_path: PathBuf::from(OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH),
            full_component_descriptor_path: PathBuf::from(FULL_COMPONENT_DESCRIPTOR_PATH),
            socket_path: PathBuf::from(BRIDGE_SOCKET_PATH),
            timeout: Duration::from_millis(STATUS_TIMEOUT_MS),
        }
    }
}

impl<R: CommandRunner> RealSystemProbe<R> {
    #[must_use]
    pub fn with_paths(
        runner: R,
        sys_module_root: PathBuf,
        socket_path: PathBuf,
        timeout: Duration,
    ) -> Self {
        Self::with_all_paths(
            runner,
            sys_module_root,
            PathBuf::from(USB_DEVICES_ROOT),
            PathBuf::from(OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH),
            socket_path,
            timeout,
        )
    }

    fn with_all_paths(
        runner: R,
        sys_module_root: PathBuf,
        usb_devices_root: PathBuf,
        openrazer_daemon_path: PathBuf,
        socket_path: PathBuf,
        timeout: Duration,
    ) -> Self {
        Self {
            runner,
            sys_module_root,
            hid_drivers_root: PathBuf::from(HID_DRIVERS_ROOT),
            hid_devices_root: PathBuf::from(HID_DEVICES_ROOT),
            dkms_root: PathBuf::from(DKMS_ROOT),
            usb_devices_root,
            input_by_id_root: PathBuf::from(INPUT_BY_ID_ROOT),
            openrazer_daemon_path,
            full_component_descriptor_path: PathBuf::from(FULL_COMPONENT_DESCRIPTOR_PATH),
            socket_path,
            timeout,
        }
    }

    #[must_use]
    pub fn with_full_component_descriptor_path(mut self, path: PathBuf) -> Self {
        self.full_component_descriptor_path = path;
        self
    }

    fn installed_module_identity(&self) -> Option<String> {
        self.installed_identity(KERNEL_MODULE_NAME)
    }

    fn installed_identity(&self, module: &str) -> Option<String> {
        self.runner
            .run("modinfo", &["-F", "srcversion", module], self.timeout)
            .ok()
            .filter(|output| output.success)
            .and_then(|output| bounded_identity(&output.stdout))
    }

    fn installed_native_capability(&self, module: &str) -> Option<String> {
        if !OPENRAZER_NATIVE_CAPABILITY_MODULES.contains(&module) {
            return None;
        }
        self.runner
            .run(
                "modinfo",
                &["-F", OPENRAZER_NATIVE_CAPABILITY_FIELD, module],
                self.timeout,
            )
            .ok()
            .filter(|output| output.success)
            .and_then(|output| bounded_identity(&output.stdout))
    }

    fn loaded_module_identity(&self) -> Option<String> {
        self.loaded_identity(KERNEL_MODULE_NAME)
    }

    fn loaded_identity(&self, module: &str) -> Option<String> {
        let module = module.replace('-', "_");
        let path = self.sys_module_root.join(module).join("srcversion");
        fs::read_to_string(path)
            .ok()
            .and_then(|value| bounded_identity(value.trim()))
    }

    fn checked_supported_receiver_present(&self) -> Result<bool, DriverActivationError> {
        let entries = fs::read_dir(&self.usb_devices_root)
            .map_err(|_| DriverActivationError::ReceiverInventoryUnavailable)?;
        for (index, entry) in entries.enumerate() {
            if index >= MAX_USB_DEVICE_ENTRIES {
                return Err(DriverActivationError::ReceiverInventoryUnavailable);
            }
            let path = entry
                .map_err(|_| DriverActivationError::ReceiverInventoryUnavailable)?
                .path();
            let vendor_path = path.join("idVendor");
            let product_path = path.join("idProduct");
            let vendor_exists = vendor_path
                .try_exists()
                .map_err(|_| DriverActivationError::ReceiverInventoryUnavailable)?;
            let product_exists = product_path
                .try_exists()
                .map_err(|_| DriverActivationError::ReceiverInventoryUnavailable)?;
            if !vendor_exists && !product_exists {
                continue;
            }
            if !vendor_exists || !product_exists {
                return Err(DriverActivationError::ReceiverInventoryUnavailable);
            }
            let vendor_id = read_usb_hex_id(&vendor_path)
                .ok_or(DriverActivationError::ReceiverInventoryUnavailable)?;
            let product_id = read_usb_hex_id(&product_path)
                .ok_or(DriverActivationError::ReceiverInventoryUnavailable)?;
            if receiver_profile_by_usb_id(vendor_id, product_id).is_some() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn settle_udev(&self) -> Result<(), DriverActivationError> {
        let timeout = format!("--timeout={DRIVER_ACTIVATION_SETTLE_SECONDS}");
        let output = self
            .runner
            .run(
                "udevadm",
                &["settle", timeout.as_str()],
                DRIVER_ACTIVATION_COMMAND_TIMEOUT,
            )
            .map_err(|_| DriverActivationError::UdevSettleFailed)?;
        if output.success {
            Ok(())
        } else {
            Err(DriverActivationError::UdevSettleFailed)
        }
    }

    fn bounded_symlink_count(
        path: &Path,
        maximum: usize,
        missing_is_empty: bool,
    ) -> Result<usize, DriverActivationError> {
        let entries = match fs::read_dir(path) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound && missing_is_empty => {
                return Ok(0);
            }
            Err(_) => return Err(DriverActivationError::ModuleReferencesUnavailable),
        };
        let mut count = 0_usize;
        for entry in entries {
            let entry = entry.map_err(|_| DriverActivationError::ModuleReferencesUnavailable)?;
            if entry.file_name() == "module" {
                continue;
            }
            let metadata = fs::symlink_metadata(entry.path())
                .map_err(|_| DriverActivationError::ModuleReferencesUnavailable)?;
            if metadata.file_type().is_symlink() {
                count = count
                    .checked_add(1)
                    .ok_or(DriverActivationError::ModuleReferencesUnavailable)?;
                if count > maximum {
                    return Err(DriverActivationError::ModuleReferencesUnavailable);
                }
            }
        }
        Ok(count)
    }

    fn module_references_gone(&self) -> Result<bool, DriverActivationError> {
        let module_name = KERNEL_MODULE_NAME.replace('-', "_");
        let module_path = self.sys_module_root.join(&module_name);
        if !module_path.is_dir() {
            return Ok(true);
        }
        let refcount = fs::read_to_string(module_path.join("refcnt"))
            .map_err(|_| DriverActivationError::ModuleReferencesUnavailable)?
            .trim()
            .parse::<usize>()
            .map_err(|_| DriverActivationError::ModuleReferencesUnavailable)?;
        let holders =
            Self::bounded_symlink_count(&module_path.join("holders"), MAX_MODULE_HOLDERS, false)?;
        let bindings = Self::bounded_symlink_count(
            &self.hid_drivers_root.join(KERNEL_MODULE_NAME),
            MAX_DRIVER_BINDINGS,
            true,
        )?;
        Ok(refcount == 0 && holders == 0 && bindings == 0)
    }

    fn wait_for_module_references_gone(&self) -> Result<(), DriverActivationError> {
        let started = Instant::now();
        loop {
            if self.checked_supported_receiver_present()? {
                return Err(DriverActivationError::ReceiverPresent);
            }
            if self.module_references_gone()? {
                return Ok(());
            }
            if started.elapsed() >= DRIVER_REFERENCE_SETTLE_TIMEOUT {
                return Err(DriverActivationError::ModuleBusy);
            }
            thread::sleep(DRIVER_REFERENCE_POLL_INTERVAL);
        }
    }

    fn run_modprobe(&self, arguments: &[&str]) -> Result<(), DriverActivationError> {
        let output = self
            .runner
            .run("modprobe", arguments, DRIVER_ACTIVATION_COMMAND_TIMEOUT)
            .map_err(|_| {
                if arguments.first() == Some(&"-r") {
                    DriverActivationError::ModuleUnloadFailed
                } else {
                    DriverActivationError::ModuleLoadFailed
                }
            })?;
        if output.success {
            Ok(())
        } else if arguments.first() == Some(&"-r") {
            Err(DriverActivationError::ModuleUnloadFailed)
        } else {
            Err(DriverActivationError::ModuleLoadFailed)
        }
    }

    fn verify_activated_driver(
        &self,
        expected_installed: &str,
    ) -> Result<DriverActivationReceipt, DriverActivationError> {
        let first_installed = self
            .installed_module_identity()
            .ok_or(DriverActivationError::InstalledIdentityUnavailable)?;
        if first_installed != expected_installed {
            return Err(DriverActivationError::IdentityUnstable);
        }
        let first_loaded = self
            .loaded_module_identity()
            .ok_or(DriverActivationError::LoadedIdentityUnavailable)?;
        if first_loaded != first_installed {
            return Err(DriverActivationError::IdentityMismatch);
        }
        thread::sleep(DRIVER_IDENTITY_STABILITY_DELAY);
        let second_installed = self
            .installed_module_identity()
            .ok_or(DriverActivationError::InstalledIdentityUnavailable)?;
        let second_loaded = self
            .loaded_module_identity()
            .ok_or(DriverActivationError::LoadedIdentityUnavailable)?;
        if second_installed != first_installed || second_loaded != first_loaded {
            return Err(DriverActivationError::IdentityUnstable);
        }
        Ok(DriverActivationReceipt {
            installed_module_identity: second_installed,
            loaded_module_identity: second_loaded,
        })
    }

    fn openrazer_driver(&self) -> OpenRazerDriverSnapshot {
        OpenRazerDriverSnapshot {
            modules: OPENRAZER_NATIVE_DRIVER_MODULES
                .iter()
                .map(|module| {
                    let loaded_path = self.sys_module_root.join(module.replace('-', "_"));
                    KernelModuleIdentitySnapshot {
                        module_name: (*module).to_owned(),
                        installed_identity: self.installed_identity(module),
                        loaded: loaded_path.is_dir(),
                        loaded_identity: self.loaded_identity(module),
                        native_transport_capability: self.installed_native_capability(module),
                    }
                })
                .collect(),
        }
    }

    fn openrazer_runtime(&self) -> OpenRazerRuntimeSnapshot {
        if !self.openrazer_daemon_path.is_file() {
            return OpenRazerRuntimeSnapshot {
                state: OpenRazerRuntimeState::NotInstalled,
                observed_upstream_version: None,
            };
        }
        let Some(program) = self.openrazer_daemon_path.to_str() else {
            return OpenRazerRuntimeSnapshot {
                state: OpenRazerRuntimeState::Detected,
                observed_upstream_version: None,
            };
        };
        let observed = self
            .runner
            .run(program, &[OPENRAZER_NATIVE_VERSION_ARGUMENT], self.timeout)
            .ok()
            .filter(|output| output.success)
            .and_then(|output| parse_openrazer_upstream_version(&output.stdout));
        OpenRazerRuntimeSnapshot {
            state: OpenRazerRuntimeState::Detected,
            observed_upstream_version: observed,
        }
    }

    fn openrazer_access(&self) -> OpenRazerAccessState {
        if !self.openrazer_daemon_path.is_file() {
            return OpenRazerAccessState::NotInstalled;
        }
        let Ok(group) = self
            .runner
            .run("getent", &["group", OPENRAZER_GROUP], self.timeout)
        else {
            return OpenRazerAccessState::Unverified;
        };
        let Some(group_id) = group
            .success
            .then(|| parse_group_id(&group.stdout))
            .flatten()
        else {
            return OpenRazerAccessState::GroupUnavailable;
        };
        let Ok(user_id) = self.runner.run("id", &["-u"], self.timeout) else {
            return OpenRazerAccessState::Unverified;
        };
        if user_id.success && user_id.stdout.parse::<u32>() == Ok(0) {
            return OpenRazerAccessState::NotApplicable;
        }
        let Ok(group_ids) = self.runner.run("id", &["-G"], self.timeout) else {
            return OpenRazerAccessState::Unverified;
        };
        if !user_id.success || !group_ids.success {
            return OpenRazerAccessState::Unverified;
        }
        if !group_ids
            .stdout
            .split_ascii_whitespace()
            .filter_map(|value| value.parse::<u32>().ok())
            .any(|value| value == group_id)
        {
            return OpenRazerAccessState::SessionMembershipRequired;
        }
        let Ok(owner) = self.runner.run(
            "busctl",
            &[
                "--user",
                "--no-pager",
                "--no-legend",
                "call",
                DBUS_SERVICE_NAME,
                DBUS_OBJECT_PATH,
                DBUS_INTERFACE_NAME,
                DBUS_NAME_HAS_OWNER_METHOD,
                "s",
                OPENRAZER_SESSION_BUS_NAME,
            ],
            self.timeout,
        ) else {
            return OpenRazerAccessState::Unverified;
        };
        if !owner.success {
            return OpenRazerAccessState::Unverified;
        }
        match parse_bus_name_has_owner(&owner.stdout) {
            Some(true) => OpenRazerAccessState::Ready,
            Some(false) => OpenRazerAccessState::DbusUnavailable,
            None => OpenRazerAccessState::Unverified,
        }
    }

    fn openrazer_published_devices(&self) -> Result<Vec<OpenRazerPublishedDevice>, ProbeError> {
        let inventory = self.openrazer_dbus_call(
            OPENRAZER_ROOT_OBJECT_PATH,
            OPENRAZER_DEVICES_INTERFACE,
            OPENRAZER_GET_DEVICES_METHOD,
        )?;
        let serials = parse_openrazer_serials(&inventory.stdout)
            .filter(|_| inventory.success)
            .ok_or(ProbeError::CommandOutputInvalid)?;
        let mut devices = Vec::with_capacity(serials.len());
        for serial in serials {
            let object_path = format!("{OPENRAZER_DEVICE_OBJECT_PREFIX}{serial}");
            let identity = self.openrazer_dbus_call(
                &object_path,
                OPENRAZER_DEVICE_MISC_INTERFACE,
                OPENRAZER_GET_VID_PID_METHOD,
            )?;
            let device = parse_openrazer_vid_pid(&identity.stdout)
                .filter(|_| identity.success)
                .ok_or(ProbeError::CommandOutputInvalid)?;
            devices.push(device);
        }
        devices.sort_unstable();
        Ok(devices)
    }

    fn openrazer_dbus_call(
        &self,
        object_path: &str,
        interface: &str,
        method: &str,
    ) -> Result<CommandOutput, ProbeError> {
        self.runner.run(
            "busctl",
            &[
                "--user",
                "--no-pager",
                "--no-legend",
                "--auto-start=no",
                "call",
                OPENRAZER_SESSION_BUS_NAME,
                object_path,
                interface,
                method,
            ],
            self.timeout,
        )
    }

    fn service_state(&self) -> ServiceState {
        let Ok(output) = self.runner.run(
            "systemctl",
            &[
                "show",
                "--property=ActiveState",
                "--value",
                BRIDGE_SERVICE_UNIT,
            ],
            self.timeout,
        ) else {
            return ServiceState::Unavailable;
        };
        if !output.success {
            return ServiceState::Unavailable;
        }
        match output.stdout.as_str() {
            "active" => ServiceState::Active,
            "activating" => ServiceState::Activating,
            "failed" => ServiceState::Failed,
            "inactive" => ServiceState::Inactive,
            "deactivating" => ServiceState::Stopping,
            _ => ServiceState::Unavailable,
        }
    }

    fn legacy_v2_stack_detected(&self) -> bool {
        let legacy_module = LEGACY_V2_KERNEL_MODULE_NAME.replace('-', "_");
        if self.sys_module_root.join(legacy_module).is_dir() {
            return true;
        }
        let Ok(output) = self.runner.run(
            "systemctl",
            &[
                "show",
                "--property=LoadState",
                "--value",
                LEGACY_V2_BRIDGE_SERVICE_UNIT,
            ],
            self.timeout,
        ) else {
            return false;
        };
        output.success && output.stdout == "loaded"
    }

    /// Reads which kernel driver actually owns each qualified receiver HID
    /// interface. Module presence alone does not prove the released driver is
    /// in use: a retired driver advertising the same modalias can bind first.
    fn receiver_interface_drivers(&self) -> Vec<Option<String>> {
        let Ok(entries) = fs::read_dir(&self.hid_devices_root) else {
            return Vec::new();
        };
        let mut interfaces = entries
            .filter_map(Result::ok)
            .take(MAX_DRIVER_BINDINGS)
            .filter_map(|entry| {
                let name = entry.file_name().into_string().ok()?;
                let (vendor_id, product_id) = hid_device_usb_id(&name)?;
                receiver_profile_by_usb_id(vendor_id, product_id)?;
                let driver = fs::read_link(entry.path().join("driver"))
                    .ok()
                    .and_then(|target| {
                        target
                            .file_name()
                            .and_then(|value| value.to_str())
                            .and_then(bounded_identity)
                    });
                Some((name, driver))
            })
            .collect::<Vec<_>>();
        interfaces.sort();
        interfaces.into_iter().map(|(_, driver)| driver).collect()
    }

    /// Lists DKMS registrations of known retired `HyperFlux` drivers. A kernel
    /// update rebuilds every registered module, which can revive them.
    fn retired_driver_registrations(&self) -> Vec<String> {
        RETIRED_KERNEL_DRIVERS
            .iter()
            .filter(|(name, version)| self.dkms_root.join(name).join(version).is_dir())
            .map(|(name, version)| format!("{name}/{version}"))
            .collect()
    }

    fn supported_receiver_present(&self) -> bool {
        self.attached_usb_ids()
            .iter()
            .any(|&(vendor_id, product_id)| {
                receiver_profile_by_usb_id(vendor_id, product_id).is_some()
            })
    }

    fn attached_usb_ids(&self) -> BTreeSet<(u16, u16)> {
        let Ok(entries) = fs::read_dir(&self.usb_devices_root) else {
            return BTreeSet::new();
        };
        entries
            .filter_map(Result::ok)
            .take(MAX_USB_DEVICE_ENTRIES)
            .filter_map(|entry| {
                let path = entry.path();
                let vendor_id = read_usb_hex_id(&path.join("idVendor"))?;
                let product_id = read_usb_hex_id(&path.join("idProduct"))?;
                Some((vendor_id, product_id))
            })
            .collect()
    }

    fn missing_openrazer_key_event_links(&self) -> Vec<String> {
        let attached = self.attached_usb_ids();
        let mut missing = OPENRAZER_NATIVE_KEY_EVENT_LINKS
            .iter()
            .filter(|(vendor_id, product_id, _)| attached.contains(&(*vendor_id, *product_id)))
            .filter(|(_, _, link_name)| {
                fs::metadata(self.input_by_id_root.join(link_name)).is_err()
            })
            .map(|(_, _, link_name)| format!("{INPUT_BY_ID_ROOT}/{link_name}"))
            .collect::<Vec<_>>();
        missing.sort();
        missing.dedup();
        missing
    }

    #[doc(hidden)]
    pub fn bridge_client(
        &self,
        client_id: &str,
        client_name: &str,
        required_features: &[&str],
    ) -> Result<HyperFluxClient<UnixStream, KernelRequestIdentitySource>, ProbeError> {
        let stream =
            UnixStream::connect(&self.socket_path).map_err(|_| ProbeError::BridgeUnavailable)?;
        stream
            .set_read_timeout(Some(self.timeout))
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        stream
            .set_write_timeout(Some(self.timeout))
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        let version = ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION)
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        let config = sdk_client_config(client_id, client_name, version, required_features)?;
        HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(|_| ProbeError::BridgeUnavailable)
    }

    fn bridge_health(&self) -> Result<BridgeHealth, ProbeError> {
        let mut client = self.bridge_client(
            "hyperfluxctl",
            "HyperFlux Doctor",
            &[STRUCTURED_DIAGNOSTICS_FEATURE],
        )?;
        let snapshot = client
            .snapshot()
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        let diagnostics = client
            .diagnostics()
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        Ok(BridgeHealth {
            snapshot,
            diagnostics,
        })
    }

    /// Returns the bridge's canonical application projection for the local
    /// qualification console.
    ///
    /// # Errors
    ///
    /// Returns a bounded bridge-unavailable error when the local socket cannot
    /// be reached, negotiated, or queried.
    pub fn qualification_integration(&self) -> Result<BridgeIntegration, ProbeError> {
        let client_id = qualification_client_id();
        let mut client = self.bridge_client(
            &client_id,
            "HyperFlux Qualification Console",
            &[INTEGRATION_VIEW_FEATURE, EVENT_SUBSCRIPTIONS_FEATURE],
        )?;
        let protocol_version = client.server_hello().selected_version;
        let view = client
            .integration_view()
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        let events = retained_events(&mut client)?;
        Ok(BridgeIntegration {
            protocol_version,
            view,
            events,
        })
    }

    /// Returns the bridge's integration projection for the installed
    /// `hyperfluxctl qualification` report with one read-only
    /// `integration-view` request: no event subscription, no active device
    /// query, and no write.
    ///
    /// # Errors
    ///
    /// Returns a bounded bridge-unavailable error when the local socket cannot
    /// be reached, negotiated, or queried.
    pub fn integration_projection(&self) -> Result<IntegrationProjection, ProbeError> {
        let mut client = self.bridge_client(
            "hyperfluxctl",
            "HyperFlux Qualification Report",
            &[INTEGRATION_VIEW_FEATURE],
        )?;
        let protocol_version = client.server_hello().selected_version;
        let view = client
            .integration_view()
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        Ok(IntegrationProjection {
            protocol_version,
            view,
        })
    }

    /// Reads the privacy-neutral native route inventory exposed by protocol v7.
    ///
    /// # Errors
    ///
    /// Returns a bounded bridge-unavailable error when the local socket cannot
    /// negotiate or return the complete inventory.
    pub fn native_route_inventory(&self) -> Result<NativeRouteInventory, ProbeError> {
        let client_id = qualification_client_id();
        let mut client = self.bridge_client(
            &client_id,
            "HyperFlux Native Onboarding",
            &[NATIVE_RECEIVER_TUNNEL_FEATURE],
        )?;
        client
            .native_enumerate()
            .map_err(|_| ProbeError::BridgeUnavailable)
    }

    /// Requests exactly one asynchronous system suspend without invoking a
    /// shell. The qualification coordinator persists its resume checkpoint
    /// before calling this method.
    #[doc(hidden)]
    pub fn request_system_suspend(&self) -> Result<(), ProbeError> {
        let output = self
            .runner
            .run("systemctl", &["--no-block", "suspend"], self.timeout)?;
        if output.success {
            Ok(())
        } else {
            Err(ProbeError::CommandUnavailable)
        }
    }
}

fn sdk_client_config(
    client_id: &str,
    client_name: &str,
    version: ProtocolVersion,
    required_features: &[&str],
) -> Result<SdkClientConfig, ProbeError> {
    Ok(SdkClientConfig {
        client_id: ClientId::try_from(client_id).map_err(|_| ProbeError::BridgeUnavailable)?,
        client_name: ClientName::try_from(client_name)
            .map_err(|_| ProbeError::BridgeUnavailable)?,
        minimum_version: version,
        maximum_version: version,
        required_features: required_features
            .iter()
            .map(|feature| {
                ProtocolFeatureId::try_from(*feature).map_err(|_| ProbeError::BridgeUnavailable)
            })
            .collect::<Result<Vec<_>, _>>()?,
        optional_features: Vec::new(),
    })
}

impl<R: CommandRunner> OpenRazerDriverProbe for RealSystemProbe<R> {
    fn openrazer_driver_snapshot(&self) -> OpenRazerDriverSnapshot {
        self.openrazer_driver()
    }
}

impl<R: CommandRunner> SystemProbe for RealSystemProbe<R> {
    fn snapshot(&self) -> SystemSnapshot {
        let service_state = self.service_state();
        let bridge = if service_state == ServiceState::Active {
            self.bridge_health().ok()
        } else {
            None
        };
        let full_component =
            probe_full_component(&self.full_component_descriptor_path, PRODUCT_VERSION);
        let (openrazer_driver, openrazer_runtime, mut openrazer_access) =
            if full_component.is_ready() {
                (
                    self.openrazer_driver_snapshot(),
                    self.openrazer_runtime(),
                    self.openrazer_access(),
                )
            } else {
                (
                    OpenRazerDriverSnapshot {
                        modules: Vec::new(),
                    },
                    OpenRazerRuntimeSnapshot {
                        state: OpenRazerRuntimeState::NotInstalled,
                        observed_upstream_version: None,
                    },
                    OpenRazerAccessState::NotApplicable,
                )
            };
        let openrazer_published_devices =
            if full_component.is_ready() && openrazer_access == OpenRazerAccessState::Ready {
                if let Ok(devices) = self.openrazer_published_devices() {
                    Some(devices)
                } else {
                    openrazer_access = OpenRazerAccessState::Unverified;
                    None
                }
            } else {
                None
            };
        let missing_openrazer_key_event_links = if full_component.is_ready() {
            self.missing_openrazer_key_event_links()
        } else {
            Vec::new()
        };
        SystemSnapshot {
            package_version: PRODUCT_VERSION.to_owned(),
            full_component,
            installed_module_identity: self.installed_module_identity(),
            loaded_module_identity: self.loaded_module_identity(),
            openrazer_driver,
            openrazer_runtime,
            openrazer_access,
            openrazer_published_devices,
            service_state,
            bridge,
            legacy_v2_stack_detected: self.legacy_v2_stack_detected(),
            supported_receiver_present: self.supported_receiver_present(),
            receiver_interface_drivers: self.receiver_interface_drivers(),
            retired_driver_registrations: self.retired_driver_registrations(),
            missing_openrazer_key_event_links,
        }
    }
}

impl<R: CommandRunner> SystemController for RealSystemProbe<R> {
    fn enable_bridge(&self) -> Result<(), ProbeError> {
        let output = self.runner.run(
            "systemctl",
            &["enable", "--now", BRIDGE_SERVICE_UNIT],
            self.timeout,
        )?;
        if output.success {
            Ok(())
        } else {
            Err(ProbeError::CommandUnavailable)
        }
    }

    fn restart_bridge(&self) -> Result<(), ProbeError> {
        let output =
            self.runner
                .run("systemctl", &["restart", BRIDGE_SERVICE_UNIT], self.timeout)?;
        if output.success {
            Ok(())
        } else {
            Err(ProbeError::CommandUnavailable)
        }
    }

    fn stop_bridge(&self) -> Result<(), ProbeError> {
        let output = self
            .runner
            .run("systemctl", &["stop", BRIDGE_SERVICE_UNIT], self.timeout)?;
        if output.success {
            Ok(())
        } else {
            Err(ProbeError::CommandUnavailable)
        }
    }
}

impl<R: CommandRunner> DriverActivationController for RealSystemProbe<R> {
    fn activate_disconnected_driver(
        &self,
    ) -> Result<DriverActivationReceipt, DriverActivationError> {
        self.settle_udev()?;
        if self.checked_supported_receiver_present()? {
            return Err(DriverActivationError::ReceiverPresent);
        }
        let installed_before = self
            .installed_module_identity()
            .ok_or(DriverActivationError::InstalledIdentityUnavailable)?;
        let module_path = self
            .sys_module_root
            .join(KERNEL_MODULE_NAME.replace('-', "_"));

        if module_path.is_dir() {
            let loaded_before = self
                .loaded_module_identity()
                .ok_or(DriverActivationError::LoadedIdentityUnavailable)?;
            if loaded_before == installed_before {
                return self.verify_activated_driver(&installed_before);
            }
            self.wait_for_module_references_gone()?;
            if self.checked_supported_receiver_present()? {
                return Err(DriverActivationError::ReceiverPresent);
            }
            self.run_modprobe(&["-r", KERNEL_MODULE_NAME])?;
            self.settle_udev()?;
            if module_path.is_dir() {
                return Err(DriverActivationError::ModuleUnloadIncomplete);
            }
        }

        if self.checked_supported_receiver_present()? {
            return Err(DriverActivationError::ReceiverPresent);
        }
        let installed_after_unload = self
            .installed_module_identity()
            .ok_or(DriverActivationError::InstalledIdentityUnavailable)?;
        if installed_after_unload != installed_before {
            return Err(DriverActivationError::IdentityUnstable);
        }

        self.run_modprobe(&[KERNEL_MODULE_NAME])?;
        self.settle_udev()?;
        if self.checked_supported_receiver_present()? {
            return Err(DriverActivationError::ReceiverPresent);
        }
        self.verify_activated_driver(&installed_before)
    }
}

fn qualification_client_id() -> String {
    let sequence = QUALIFICATION_CLIENT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("hyperflux-qualification-{}-{sequence}", std::process::id())
}

fn retained_events(
    client: &mut HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
) -> Result<EventBatch, ProbeError> {
    let max_events = EventBatchLimit::try_from(EventBatchLimit::MAX)
        .map_err(|_| ProbeError::BridgeUnavailable)?;
    let mut batch = client
        .subscribe(EventSubscription {
            subscription_id: None,
            expected_cursor: None,
            max_events,
        })
        .map_err(|_| ProbeError::BridgeUnavailable)?;
    if batch.cursor_gap {
        let replay_cursor = EventCursor {
            stream_id: batch.next_cursor.stream_id.clone(),
            stream_epoch: batch.next_cursor.stream_epoch,
            projection_revision: batch.next_cursor.projection_revision,
            sequence: SequenceNumber::try_from(batch.oldest_available.get().saturating_sub(1))
                .map_err(|_| ProbeError::BridgeUnavailable)?,
        };
        batch = client
            .subscribe(EventSubscription {
                subscription_id: Some(batch.subscription_id),
                expected_cursor: Some(replay_cursor),
                max_events,
            })
            .map_err(|_| ProbeError::BridgeUnavailable)?;
    }
    if batch.cursor_gap {
        return Err(ProbeError::BridgeUnavailable);
    }
    let subscription_id = batch.subscription_id.clone();
    let oldest_available = batch.oldest_available;
    let dropped_events = batch.dropped_events;
    let mut events = std::mem::take(&mut batch.events);
    while batch.has_more {
        let previous_cursor = batch.next_cursor.clone();
        batch = client
            .subscribe(EventSubscription {
                subscription_id: Some(subscription_id.clone()),
                expected_cursor: Some(previous_cursor),
                max_events,
            })
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        if batch.cursor_gap
            || batch.subscription_id != subscription_id
            || events.len().saturating_add(batch.events.len()) > usize::from(EVENT_CAPACITY)
        {
            return Err(ProbeError::BridgeUnavailable);
        }
        events.append(&mut batch.events);
    }
    Ok(EventBatch {
        subscription_id,
        next_cursor: batch.next_cursor,
        events,
        oldest_available,
        latest_available: batch.latest_available,
        dropped_events,
        cursor_gap: false,
        has_more: false,
    })
}

fn bounded_identity(value: &str) -> Option<String> {
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    {
        None
    } else {
        Some(value.to_owned())
    }
}

fn parse_openrazer_upstream_version(value: &str) -> Option<String> {
    let version = value.strip_prefix("openrazer-daemon ")?;
    if version.is_empty()
        || version.len() > 64
        || version.bytes().any(|byte| {
            !(byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'-' | b'~'))
        })
    {
        return None;
    }
    let suffix = version.find(['-', '+', '~']).unwrap_or(version.len());
    let upstream = &version[..suffix];
    let components = upstream.split('.').collect::<Vec<_>>();
    if !(2..=4).contains(&components.len())
        || components.iter().any(|component| {
            component.is_empty() || !component.bytes().all(|byte| byte.is_ascii_digit())
        })
    {
        return None;
    }
    Some(upstream.to_owned())
}

fn parse_group_id(value: &str) -> Option<u32> {
    let mut fields = value.split(':');
    if fields.next()? != OPENRAZER_GROUP {
        return None;
    }
    let _password = fields.next()?;
    let group_id = fields.next()?.parse::<u32>().ok()?;
    let _members = fields.next()?;
    fields.next().is_none().then_some(group_id)
}

fn parse_bus_name_has_owner(output: &str) -> Option<bool> {
    match output {
        "b true" => Some(true),
        "b false" => Some(false),
        _ => None,
    }
}

fn parse_openrazer_serials(output: &str) -> Option<Vec<String>> {
    let mut fields = output.split_ascii_whitespace();
    if fields.next()? != "as" {
        return None;
    }
    let count = fields.next()?.parse::<usize>().ok()?;
    if count > MAX_OPENRAZER_PUBLISHED_DEVICES {
        return None;
    }
    let mut seen = BTreeSet::new();
    let mut serials = Vec::with_capacity(count);
    for _ in 0..count {
        let quoted = fields.next()?;
        let serial = quoted.strip_prefix('"')?.strip_suffix('"')?;
        if serial.is_empty()
            || serial.len() > MAX_OPENRAZER_SERIAL_BYTES
            || !serial
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            || !seen.insert(serial.to_owned())
        {
            return None;
        }
        serials.push(serial.to_owned());
    }
    fields.next().is_none().then_some(serials)
}

fn parse_openrazer_vid_pid(output: &str) -> Option<OpenRazerPublishedDevice> {
    let mut fields = output.split_ascii_whitespace();
    if fields.next()? != "ai" || fields.next()? != "2" {
        return None;
    }
    let vendor_id = fields.next()?.parse::<u16>().ok()?;
    let product_id = fields.next()?.parse::<u16>().ok()?;
    if vendor_id == 0 || product_id == 0 || fields.next().is_some() {
        return None;
    }
    Some(OpenRazerPublishedDevice {
        vendor_id,
        product_id,
    })
}

/// Parses a HID device name such as `0003:1532:00CF.0008` (bus, vendor,
/// product, instance) and returns the USB identity for USB-bus devices only.
fn hid_device_usb_id(name: &str) -> Option<(u16, u16)> {
    let mut parts = name.splitn(3, ':');
    if parts.next()? != HID_BUS_USB {
        return None;
    }
    let vendor_id = u16::from_str_radix(parts.next()?, 16).ok()?;
    let (product, _instance) = parts.next()?.split_once('.')?;
    let product_id = u16::from_str_radix(product, 16).ok()?;
    Some((vendor_id, product_id))
}

fn read_usb_hex_id(path: &Path) -> Option<u16> {
    let value = fs::read_to_string(path).ok()?;
    let value = value.trim();
    if value.len() != 4 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    u16::from_str_radix(value, 16).ok()
}

#[doc(hidden)]
pub fn write_private_file(path: &Path, value: &[u8]) -> io::Result<()> {
    use std::fs::OpenOptions;
    use std::io::Write as _;
    use std::os::unix::fs::OpenOptionsExt as _;

    let mut options = OpenOptions::new();
    options.write(true).create_new(true).mode(0o600);
    let mut file = options.open(path)?;
    file.write_all(value)?;
    file.sync_all()
}

#[cfg(test)]
mod tests;
