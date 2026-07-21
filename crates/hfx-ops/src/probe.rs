// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{ClientId, ClientName, ProtocolFeatureId, ProtocolVersion};
use hfx_protocol::{BridgeSnapshot, CURRENT_PROTOCOL_VERSION, DiagnosticSnapshot};
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SystemSnapshot {
    pub package_version: String,
    pub installed_module_identity: Option<String>,
    pub loaded_module_identity: Option<String>,
    pub service_state: ServiceState,
    pub bridge: Option<BridgeHealth>,
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
    socket_path: PathBuf,
    timeout: Duration,
}

impl Default for RealSystemProbe<RealCommandRunner> {
    fn default() -> Self {
        Self {
            runner: RealCommandRunner,
            sys_module_root: PathBuf::from("/sys/module"),
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
        Self {
            runner,
            sys_module_root,
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

    fn bridge_health(&self) -> Result<BridgeHealth, ProbeError> {
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
        let config = SdkClientConfig {
            client_id: ClientId::try_from("hyperfluxctl")
                .map_err(|_| ProbeError::BridgeUnavailable)?,
            client_name: ClientName::try_from("HyperFlux Doctor")
                .map_err(|_| ProbeError::BridgeUnavailable)?,
            minimum_version: version,
            maximum_version: version,
            required_features: vec![
                ProtocolFeatureId::try_from("integration-view-projection")
                    .map_err(|_| ProbeError::BridgeUnavailable)?,
            ],
            optional_features: Vec::new(),
        };
        let mut client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(|_| ProbeError::BridgeUnavailable)?;
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

    #[derive(Debug)]
    struct FakeRunner {
        outputs: Mutex<VecDeque<Result<CommandOutput, ProbeError>>>,
    }

    impl FakeRunner {
        fn new(outputs: Vec<Result<CommandOutput, ProbeError>>) -> Self {
            Self {
                outputs: Mutex::new(outputs.into()),
            }
        }
    }

    impl CommandRunner for FakeRunner {
        fn run(
            &self,
            _program: &str,
            _arguments: &[&str],
            _timeout: Duration,
        ) -> Result<CommandOutput, ProbeError> {
            self.outputs
                .lock()
                .expect("fake command lock")
                .pop_front()
                .expect("fake command output")
        }
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
}
