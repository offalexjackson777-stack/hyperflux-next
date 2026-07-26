// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ClientId, ClientName, EventBatchLimit, ProtocolFeatureId, ProtocolVersion, SequenceNumber,
};
use hfx_integration_model::{
    OPENRAZER_NATIVE_CAPABILITY_FIELD, OPENRAZER_NATIVE_CAPABILITY_MODULES,
    OPENRAZER_NATIVE_CAPABILITY_VALUE, OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH,
    OPENRAZER_NATIVE_DRIVER_MODULES, OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION,
    OPENRAZER_NATIVE_VERSION_ARGUMENT,
};
use hfx_profiles::receiver_profile_by_usb_id;
use hfx_protocol::{
    BridgeSnapshot, CURRENT_PROTOCOL_VERSION, DiagnosticSnapshot, EventBatch, EventCursor,
    IntegrationView, NativeRouteInventory,
};
use hfx_runtime::{
    BRIDGE_SERVICE_UNIT, BRIDGE_SOCKET_PATH, EVENT_CAPACITY, KERNEL_MODULE_NAME, PRODUCT_VERSION,
    STATUS_TIMEOUT_MS,
};
use hfx_sdk::{EventSubscription, HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
use serde::{Deserialize, Serialize};
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
const USB_DEVICES_ROOT: &str = "/sys/bus/usb/devices";
const OPENRAZER_GROUP: &str = "openrazer";
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SystemSnapshot {
    pub package_version: String,
    pub installed_module_identity: Option<String>,
    pub loaded_module_identity: Option<String>,
    pub openrazer_driver: OpenRazerDriverSnapshot,
    pub openrazer_runtime: OpenRazerRuntimeSnapshot,
    pub openrazer_access: OpenRazerAccessState,
    pub service_state: ServiceState,
    pub bridge: Option<BridgeHealth>,
    pub legacy_v2_stack_detected: bool,
    pub supported_receiver_present: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpenRazerAccessState {
    NotInstalled,
    Ready,
    GroupUnavailable,
    SessionMembershipRequired,
    Unverified,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpenRazerRuntimeState {
    NotInstalled,
    Verified,
    ReverificationRequired,
    Unverified,
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
    usb_devices_root: PathBuf,
    openrazer_daemon_path: PathBuf,
    socket_path: PathBuf,
    timeout: Duration,
}

impl Default for RealSystemProbe<RealCommandRunner> {
    fn default() -> Self {
        Self {
            runner: RealCommandRunner,
            sys_module_root: PathBuf::from("/sys/module"),
            usb_devices_root: PathBuf::from(USB_DEVICES_ROOT),
            openrazer_daemon_path: PathBuf::from(OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH),
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
            usb_devices_root,
            openrazer_daemon_path,
            socket_path,
            timeout,
        }
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
                state: OpenRazerRuntimeState::Unverified,
                observed_upstream_version: None,
            };
        };
        let Ok(output) =
            self.runner
                .run(program, &[OPENRAZER_NATIVE_VERSION_ARGUMENT], self.timeout)
        else {
            return OpenRazerRuntimeSnapshot {
                state: OpenRazerRuntimeState::Unverified,
                observed_upstream_version: None,
            };
        };
        let observed = output
            .success
            .then(|| parse_openrazer_upstream_version(&output.stdout))
            .flatten();
        let state = match observed.as_deref() {
            Some(OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION) => OpenRazerRuntimeState::Verified,
            Some(_) => OpenRazerRuntimeState::ReverificationRequired,
            None => OpenRazerRuntimeState::Unverified,
        };
        OpenRazerRuntimeSnapshot {
            state,
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
            return OpenRazerAccessState::Ready;
        }
        let Ok(group_ids) = self.runner.run("id", &["-G"], self.timeout) else {
            return OpenRazerAccessState::Unverified;
        };
        if !user_id.success || !group_ids.success {
            return OpenRazerAccessState::Unverified;
        }
        if group_ids
            .stdout
            .split_ascii_whitespace()
            .filter_map(|value| value.parse::<u32>().ok())
            .any(|value| value == group_id)
        {
            OpenRazerAccessState::Ready
        } else {
            OpenRazerAccessState::SessionMembershipRequired
        }
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

    fn supported_receiver_present(&self) -> bool {
        let Ok(entries) = fs::read_dir(&self.usb_devices_root) else {
            return false;
        };
        entries
            .filter_map(Result::ok)
            .take(MAX_USB_DEVICE_ENTRIES)
            .any(|entry| {
                let path = entry.path();
                let Some(vendor_id) = read_usb_hex_id(&path.join("idVendor")) else {
                    return false;
                };
                let Some(product_id) = read_usb_hex_id(&path.join("idProduct")) else {
                    return false;
                };
                receiver_profile_by_usb_id(vendor_id, product_id).is_some()
            })
    }

    pub(crate) fn bridge_client(
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
    pub(crate) fn request_system_suspend(&self) -> Result<(), ProbeError> {
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

impl<R: CommandRunner> SystemProbe for RealSystemProbe<R> {
    fn snapshot(&self) -> SystemSnapshot {
        let service_state = self.service_state();
        let bridge = if service_state == ServiceState::Active {
            self.bridge_health().ok()
        } else {
            None
        };
        SystemSnapshot {
            package_version: PRODUCT_VERSION.to_owned(),
            installed_module_identity: self.installed_module_identity(),
            loaded_module_identity: self.loaded_module_identity(),
            openrazer_driver: self.openrazer_driver(),
            openrazer_runtime: self.openrazer_runtime(),
            openrazer_access: self.openrazer_access(),
            service_state,
            bridge,
            legacy_v2_stack_detected: self.legacy_v2_stack_detected(),
            supported_receiver_present: self.supported_receiver_present(),
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

fn read_usb_hex_id(path: &Path) -> Option<u16> {
    let value = fs::read_to_string(path).ok()?;
    let value = value.trim();
    if value.len() != 4 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    u16::from_str_radix(value, 16).ok()
}

pub(crate) fn write_private_file(path: &Path, value: &[u8]) -> io::Result<()> {
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
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Debug)]
    struct FakeRunner {
        outputs: Mutex<VecDeque<Result<CommandOutput, ProbeError>>>,
        calls: Mutex<Vec<(String, Vec<String>)>>,
    }

    impl FakeRunner {
        fn new(outputs: Vec<Result<CommandOutput, ProbeError>>) -> Self {
            Self {
                outputs: Mutex::new(outputs.into()),
                calls: Mutex::new(Vec::new()),
            }
        }

        fn calls(&self) -> Vec<(String, Vec<String>)> {
            self.calls.lock().expect("fake call lock").clone()
        }
    }

    impl CommandRunner for FakeRunner {
        fn run(
            &self,
            program: &str,
            arguments: &[&str],
            _timeout: Duration,
        ) -> Result<CommandOutput, ProbeError> {
            self.calls.lock().expect("fake call lock").push((
                program.to_owned(),
                arguments.iter().map(|value| (*value).to_owned()).collect(),
            ));
            self.outputs
                .lock()
                .expect("fake command lock")
                .pop_front()
                .expect("fake command output")
        }
    }

    #[test]
    fn operational_clients_negotiate_the_features_they_call() {
        let version = ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION).expect("version");
        let doctor = sdk_client_config(
            "hyperfluxctl",
            "HyperFlux Doctor",
            version,
            &[STRUCTURED_DIAGNOSTICS_FEATURE],
        )
        .expect("doctor config");
        let qualification = sdk_client_config(
            "hyperflux-qualification",
            "HyperFlux Qualification Console",
            version,
            &[INTEGRATION_VIEW_FEATURE, EVENT_SUBSCRIPTIONS_FEATURE],
        )
        .expect("qualification config");

        assert_eq!(
            doctor
                .required_features
                .iter()
                .map(ProtocolFeatureId::as_str)
                .collect::<Vec<_>>(),
            [STRUCTURED_DIAGNOSTICS_FEATURE]
        );
        assert_eq!(
            qualification
                .required_features
                .iter()
                .map(ProtocolFeatureId::as_str)
                .collect::<Vec<_>>(),
            [INTEGRATION_VIEW_FEATURE, EVENT_SUBSCRIPTIONS_FEATURE]
        );
    }

    #[test]
    fn invalid_service_output_becomes_unavailable() {
        let probe = RealSystemProbe::with_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: true,
                stdout: "surprise".to_owned(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );
        assert_eq!(probe.service_state(), ServiceState::Unavailable);
    }

    #[test]
    fn identities_are_ascii_and_bounded() {
        assert_eq!(bounded_identity("ABC123"), Some("ABC123".to_owned()));
        assert_eq!(bounded_identity(""), None);
        assert_eq!(bounded_identity("not an identity"), None);
        assert_eq!(bounded_identity(&"a".repeat(65)), None);
    }

    #[test]
    fn openrazer_driver_state_distinguishes_unloaded_stale_and_missing_modules() {
        let ready = OpenRazerDriverSnapshot {
            modules: vec![KernelModuleIdentitySnapshot {
                module_name: "razerkbd".to_owned(),
                installed_identity: Some("new".to_owned()),
                loaded: false,
                loaded_identity: None,
                native_transport_capability: Some(OPENRAZER_NATIVE_CAPABILITY_VALUE.to_owned()),
            }],
        };
        assert_eq!(ready.state(), OpenRazerDriverState::Ready);

        let mut pending = ready.clone();
        pending.modules[0].loaded = true;
        pending.modules[0].loaded_identity = Some("old".to_owned());
        assert_eq!(pending.state(), OpenRazerDriverState::ActivationPending);

        let mut unavailable = ready;
        unavailable.modules[0].installed_identity = None;
        assert_eq!(unavailable.state(), OpenRazerDriverState::Unavailable);
        assert_eq!(
            unavailable
                .blocking_module()
                .map(|module| module.module_name.as_str()),
            Some("razerkbd")
        );

        let mut missing_capability = pending;
        missing_capability.modules[0].loaded = false;
        missing_capability.modules[0].loaded_identity = None;
        missing_capability.modules[0].native_transport_capability = None;
        assert_eq!(
            missing_capability.state(),
            OpenRazerDriverState::CapabilityUnavailable
        );
    }

    #[test]
    fn openrazer_source_line_ignores_package_release_but_requires_reverification_for_new_sources() {
        for value in [
            "openrazer-daemon 3.12.4",
            "openrazer-daemon 3.12.4-3",
            "openrazer-daemon 3.12.4-4",
            "openrazer-daemon 3.12.4+distribution.4",
        ] {
            assert_eq!(
                parse_openrazer_upstream_version(value).as_deref(),
                Some("3.12.4")
            );
        }
        assert_eq!(
            parse_openrazer_upstream_version("openrazer-daemon 3.12.5-1").as_deref(),
            Some("3.12.5")
        );
        assert_eq!(
            parse_openrazer_upstream_version("openrazer-daemon 3.13-1").as_deref(),
            Some("3.13")
        );
        assert_eq!(parse_openrazer_upstream_version("OpenRazer 3.12.4"), None);
        assert_eq!(
            parse_openrazer_upstream_version("openrazer-daemon 3.12.4rc1"),
            None
        );
    }

    #[test]
    fn openrazer_runtime_classifies_verified_and_future_source_lines_separately() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let daemon = std::env::temp_dir().join(format!("hfx-openrazer-runtime-{unique}"));
        fs::write(&daemon, "fixture").expect("write daemon fixture");

        let verified = RealSystemProbe::with_all_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: true,
                stdout: "openrazer-daemon 3.12.4-4".to_owned(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing-usb"),
            daemon.clone(),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        )
        .openrazer_runtime();
        assert_eq!(verified.state, OpenRazerRuntimeState::Verified);
        assert_eq!(
            verified.observed_upstream_version.as_deref(),
            Some("3.12.4")
        );

        let future = RealSystemProbe::with_all_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: true,
                stdout: "openrazer-daemon 3.13.0-1".to_owned(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing-usb"),
            daemon.clone(),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        )
        .openrazer_runtime();
        assert_eq!(future.state, OpenRazerRuntimeState::ReverificationRequired);
        assert_eq!(future.observed_upstream_version.as_deref(), Some("3.13.0"));
        fs::remove_file(daemon).expect("remove daemon fixture");
    }

    #[test]
    fn openrazer_group_id_requires_the_exact_group_record() {
        assert_eq!(parse_group_id("openrazer:x:946:"), Some(946));
        assert_eq!(parse_group_id("other:x:946:"), None);
        assert_eq!(parse_group_id("openrazer:x:not-a-number:"), None);
        assert_eq!(parse_group_id("openrazer:x:946::extra"), None);
    }

    #[test]
    fn installed_openrazer_daemon_requires_active_session_membership() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let daemon = std::env::temp_dir().join(format!("hfx-openrazer-{unique}"));
        fs::write(&daemon, "fixture").expect("write daemon fixture");
        let probe = RealSystemProbe::with_all_paths(
            FakeRunner::new(vec![
                Ok(CommandOutput {
                    success: true,
                    stdout: "openrazer:x:946:".to_owned(),
                }),
                Ok(CommandOutput {
                    success: true,
                    stdout: "1000".to_owned(),
                }),
                Ok(CommandOutput {
                    success: true,
                    stdout: "1000 998".to_owned(),
                }),
            ]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing-usb"),
            daemon.clone(),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );

        assert_eq!(
            probe.openrazer_access(),
            OpenRazerAccessState::SessionMembershipRequired
        );
        assert_eq!(
            probe.runner.calls(),
            vec![
                (
                    "getent".to_owned(),
                    vec!["group".to_owned(), "openrazer".to_owned()]
                ),
                ("id".to_owned(), vec!["-u".to_owned()]),
                ("id".to_owned(), vec!["-G".to_owned()]),
            ]
        );
        fs::remove_file(daemon).expect("remove daemon fixture");
    }

    #[test]
    fn absent_openrazer_daemon_does_not_require_group_access() {
        let probe = RealSystemProbe::with_all_paths(
            FakeRunner::new(Vec::new()),
            PathBuf::from("/missing"),
            PathBuf::from("/missing-usb"),
            PathBuf::from("/missing-openrazer-daemon"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );

        assert_eq!(probe.openrazer_access(), OpenRazerAccessState::NotInstalled);
        assert!(probe.runner.calls().is_empty());
    }

    #[test]
    fn legacy_v2_service_is_detected_without_claiming_next_is_ready() {
        let probe = RealSystemProbe::with_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: true,
                stdout: "loaded".to_owned(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );
        assert!(probe.legacy_v2_stack_detected());
    }

    #[test]
    fn absent_legacy_v2_service_is_not_detected() {
        let probe = RealSystemProbe::with_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: false,
                stdout: "not-found".to_owned(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );
        assert!(!probe.legacy_v2_stack_detected());
    }

    #[test]
    fn suspend_request_is_one_bounded_shell_free_systemctl_call() {
        let probe = RealSystemProbe::with_paths(
            FakeRunner::new(vec![Ok(CommandOutput {
                success: true,
                stdout: String::new(),
            })]),
            PathBuf::from("/missing"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );
        probe
            .request_system_suspend()
            .expect("suspend request succeeds");
        assert_eq!(
            probe.runner.calls(),
            vec![(
                "systemctl".to_owned(),
                vec!["--no-block".to_owned(), "suspend".to_owned()]
            )]
        );
    }

    #[test]
    fn qualified_receiver_is_discovered_from_generated_usb_profiles() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let usb_root = std::env::temp_dir().join(format!("hfx-usb-{unique}"));
        let receiver = usb_root.join("3-4");
        fs::create_dir_all(&receiver).expect("create receiver fixture");
        fs::write(receiver.join("idVendor"), "1532\n").expect("write vendor");
        fs::write(receiver.join("idProduct"), "00cf\n").expect("write product");
        let probe = RealSystemProbe::with_all_paths(
            FakeRunner::new(Vec::new()),
            PathBuf::from("/missing"),
            usb_root.clone(),
            PathBuf::from("/missing-openrazer-daemon"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );

        assert!(probe.supported_receiver_present());
        fs::remove_dir_all(usb_root).expect("remove receiver fixture");
    }

    #[test]
    fn unknown_usb_device_is_not_treated_as_a_supported_receiver() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let usb_root = std::env::temp_dir().join(format!("hfx-usb-{unique}"));
        let device = usb_root.join("1-1");
        fs::create_dir_all(&device).expect("create USB fixture");
        fs::write(device.join("idVendor"), "ffff\n").expect("write vendor");
        fs::write(device.join("idProduct"), "ffff\n").expect("write product");
        let probe = RealSystemProbe::with_all_paths(
            FakeRunner::new(Vec::new()),
            PathBuf::from("/missing"),
            usb_root.clone(),
            PathBuf::from("/missing-openrazer-daemon"),
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );

        assert!(!probe.supported_receiver_present());
        fs::remove_dir_all(usb_root).expect("remove USB fixture");
    }
}
