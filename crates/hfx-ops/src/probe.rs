// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{ClientId, ClientName, ProtocolFeatureId, ProtocolVersion};
use hfx_profiles::receiver_profile_by_usb_id;
use hfx_protocol::{BridgeSnapshot, CURRENT_PROTOCOL_VERSION, DiagnosticSnapshot, IntegrationView};
use hfx_runtime::{
    BRIDGE_SERVICE_UNIT, BRIDGE_SOCKET_PATH, KERNEL_MODULE_NAME, PRODUCT_VERSION, STATUS_TIMEOUT_MS,
};
use hfx_sdk::{HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs;
use std::io;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_COMMAND_OUTPUT_BYTES: usize = 4_096;
const MAX_USB_DEVICE_ENTRIES: usize = 256;
const USB_DEVICES_ROOT: &str = "/sys/bus/usb/devices";
const INTEGRATION_VIEW_FEATURE: &str = "integration-view-projection";
const STRUCTURED_DIAGNOSTICS_FEATURE: &str = "structured-diagnostics";
const LEGACY_V2_BRIDGE_SERVICE_UNIT: &str = "hyperflux-bridge.service";
const LEGACY_V2_KERNEL_MODULE_NAME: &str = "hid-razer-hyperflux-v2";

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
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SystemSnapshot {
    pub package_version: String,
    pub installed_module_identity: Option<String>,
    pub loaded_module_identity: Option<String>,
    pub service_state: ServiceState,
    pub bridge: Option<BridgeHealth>,
    pub legacy_v2_stack_detected: bool,
    pub supported_receiver_present: bool,
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
    socket_path: PathBuf,
    timeout: Duration,
}

impl Default for RealSystemProbe<RealCommandRunner> {
    fn default() -> Self {
        Self {
            runner: RealCommandRunner,
            sys_module_root: PathBuf::from("/sys/module"),
            usb_devices_root: PathBuf::from(USB_DEVICES_ROOT),
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
            socket_path,
            timeout,
        )
    }

    fn with_all_paths(
        runner: R,
        sys_module_root: PathBuf,
        usb_devices_root: PathBuf,
        socket_path: PathBuf,
        timeout: Duration,
    ) -> Self {
        Self {
            runner,
            sys_module_root,
            usb_devices_root,
            socket_path,
            timeout,
        }
    }

    fn installed_module_identity(&self) -> Option<String> {
        self.runner
            .run(
                "modinfo",
                &["-F", "srcversion", KERNEL_MODULE_NAME],
                self.timeout,
            )
            .ok()
            .filter(|output| output.success)
            .and_then(|output| bounded_identity(&output.stdout))
    }

    fn loaded_module_identity(&self) -> Option<String> {
        let module = KERNEL_MODULE_NAME.replace('-', "_");
        let path = self.sys_module_root.join(module).join("srcversion");
        fs::read_to_string(path)
            .ok()
            .and_then(|value| bounded_identity(value.trim()))
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
        let mut client = self.bridge_client(
            "hyperflux-qualification",
            "HyperFlux Qualification Console",
            &[INTEGRATION_VIEW_FEATURE],
        )?;
        let protocol_version = client.server_hello().selected_version;
        let view = client
            .integration_view()
            .map_err(|_| ProbeError::BridgeUnavailable)?;
        Ok(BridgeIntegration {
            protocol_version,
            view,
        })
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
            &[INTEGRATION_VIEW_FEATURE],
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
            [INTEGRATION_VIEW_FEATURE]
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
            PathBuf::from("/missing.sock"),
            Duration::from_millis(10),
        );

        assert!(!probe.supported_receiver_present());
        fs::remove_dir_all(usb_root).expect("remove USB fixture");
    }
}
