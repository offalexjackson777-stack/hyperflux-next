// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use std::collections::VecDeque;
use std::os::unix::fs::symlink;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
struct FakeRunner {
    outputs: Mutex<VecDeque<Result<CommandOutput, ProbeError>>>,
    calls: Mutex<Vec<(String, Vec<String>)>>,
    timeouts: Mutex<Vec<Duration>>,
}

impl FakeRunner {
    fn new(outputs: Vec<Result<CommandOutput, ProbeError>>) -> Self {
        Self {
            outputs: Mutex::new(outputs.into()),
            calls: Mutex::new(Vec::new()),
            timeouts: Mutex::new(Vec::new()),
        }
    }

    fn calls(&self) -> Vec<(String, Vec<String>)> {
        self.calls.lock().expect("fake call lock").clone()
    }

    fn timeouts(&self) -> Vec<Duration> {
        self.timeouts.lock().expect("fake timeout lock").clone()
    }
}

impl CommandRunner for FakeRunner {
    fn run(
        &self,
        program: &str,
        arguments: &[&str],
        timeout: Duration,
    ) -> Result<CommandOutput, ProbeError> {
        self.calls.lock().expect("fake call lock").push((
            program.to_owned(),
            arguments.iter().map(|value| (*value).to_owned()).collect(),
        ));
        self.timeouts
            .lock()
            .expect("fake timeout lock")
            .push(timeout);
        self.outputs
            .lock()
            .expect("fake command lock")
            .pop_front()
            .expect("fake command output")
    }
}

#[derive(Debug)]
struct ActivationRunner {
    calls: Mutex<Vec<(String, Vec<String>)>>,
    installed_identities: Mutex<VecDeque<String>>,
    sys_module_root: PathBuf,
    hid_drivers_root: PathBuf,
    loaded_identity_on_load: String,
}

impl ActivationRunner {
    fn new(
        installed_identities: &[&str],
        sys_module_root: PathBuf,
        hid_drivers_root: PathBuf,
        loaded_identity_on_load: &str,
    ) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            installed_identities: Mutex::new(
                installed_identities
                    .iter()
                    .map(|value| (*value).to_owned())
                    .collect(),
            ),
            sys_module_root,
            hid_drivers_root,
            loaded_identity_on_load: loaded_identity_on_load.to_owned(),
        }
    }

    fn calls(&self) -> Vec<(String, Vec<String>)> {
        self.calls.lock().expect("activation call lock").clone()
    }

    fn next_installed_identity(&self) -> String {
        let mut identities = self
            .installed_identities
            .lock()
            .expect("installed identity lock");
        if identities.len() > 1 {
            identities.pop_front().expect("queued installed identity")
        } else {
            identities
                .front()
                .expect("stable installed identity")
                .clone()
        }
    }

    fn remove_loaded_module(&self) {
        let module = KERNEL_MODULE_NAME.replace('-', "_");
        let _ = fs::remove_dir_all(self.sys_module_root.join(&module));
        let _ = fs::remove_dir_all(self.hid_drivers_root.join(KERNEL_MODULE_NAME));
    }

    fn create_loaded_module(&self) {
        write_loaded_module_fixture(
            &self.sys_module_root,
            &self.hid_drivers_root,
            &self.loaded_identity_on_load,
            "0",
        );
    }
}

impl CommandRunner for ActivationRunner {
    fn run(
        &self,
        program: &str,
        arguments: &[&str],
        _timeout: Duration,
    ) -> Result<CommandOutput, ProbeError> {
        self.calls.lock().expect("activation call lock").push((
            program.to_owned(),
            arguments.iter().map(|value| (*value).to_owned()).collect(),
        ));
        match (program, arguments) {
            ("udevadm", ["settle", "--timeout=5"]) => Ok(CommandOutput {
                success: true,
                stdout: String::new(),
            }),
            ("modinfo", ["-F", "srcversion", module]) if *module == KERNEL_MODULE_NAME => {
                Ok(CommandOutput {
                    success: true,
                    stdout: self.next_installed_identity(),
                })
            }
            ("modprobe", ["-r", module]) if *module == KERNEL_MODULE_NAME => {
                self.remove_loaded_module();
                Ok(CommandOutput {
                    success: true,
                    stdout: String::new(),
                })
            }
            ("modprobe", [module]) if *module == KERNEL_MODULE_NAME => {
                self.create_loaded_module();
                Ok(CommandOutput {
                    success: true,
                    stdout: String::new(),
                })
            }
            _ => Err(ProbeError::CommandUnavailable),
        }
    }
}

fn temporary_activation_roots(prefix: &str) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let root = std::env::temp_dir().join(format!("hfx-{prefix}-{unique}"));
    let sys_module_root = root.join("sys-module");
    let hid_drivers_root = root.join("hid-drivers");
    let usb_devices_root = root.join("usb-devices");
    fs::create_dir_all(&sys_module_root).expect("module root");
    fs::create_dir_all(&hid_drivers_root).expect("driver root");
    fs::create_dir_all(&usb_devices_root).expect("USB root");
    (root, sys_module_root, hid_drivers_root, usb_devices_root)
}

fn write_loaded_module_fixture(
    sys_module_root: &Path,
    hid_drivers_root: &Path,
    identity: &str,
    refcount: &str,
) {
    let module = KERNEL_MODULE_NAME.replace('-', "_");
    let module_path = sys_module_root.join(&module);
    let driver_path = hid_drivers_root.join(KERNEL_MODULE_NAME);
    fs::create_dir_all(module_path.join("holders")).expect("module holders");
    fs::create_dir_all(driver_path).expect("HID driver root");
    fs::write(module_path.join("srcversion"), identity).expect("module identity");
    fs::write(module_path.join("refcnt"), refcount).expect("module refcount");
}

fn activation_probe<R: CommandRunner>(
    runner: R,
    sys_module_root: PathBuf,
    hid_drivers_root: PathBuf,
    usb_devices_root: PathBuf,
) -> RealSystemProbe<R> {
    RealSystemProbe {
        runner,
        sys_module_root,
        hid_drivers_root,
        hid_devices_root: PathBuf::from("/missing-hid-devices"),
        dkms_root: PathBuf::from("/missing-dkms"),
        usb_devices_root,
        openrazer_daemon_path: PathBuf::from("/missing-openrazer"),
        full_component_descriptor_path: PathBuf::from("/missing-full-component.json"),
        socket_path: PathBuf::from("/missing.sock"),
        timeout: Duration::from_millis(100),
    }
}
fn openrazer_access_with_owner_result(
    owner_result: Result<CommandOutput, ProbeError>,
) -> (OpenRazerAccessState, Vec<(String, Vec<String>)>) {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let daemon = std::env::temp_dir().join(format!("hfx-openrazer-owner-{unique}"));
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
                stdout: "1000 946".to_owned(),
            }),
            owner_result,
        ]),
        PathBuf::from("/missing"),
        PathBuf::from("/missing-usb"),
        daemon.clone(),
        PathBuf::from("/missing.sock"),
        Duration::from_millis(10),
    );
    let state = probe.openrazer_access();
    let calls = probe.runner.calls();
    fs::remove_file(daemon).expect("remove daemon fixture");
    (state, calls)
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
fn integration_projection_fails_closed_without_the_bridge_socket() {
    let probe = RealSystemProbe::with_paths(
        FakeRunner::new(Vec::new()),
        PathBuf::from("/missing"),
        PathBuf::from("/missing.sock"),
        Duration::from_millis(10),
    );
    assert_eq!(
        probe.integration_projection().err(),
        Some(ProbeError::BridgeUnavailable)
    );
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
fn focused_openrazer_driver_probe_invokes_only_bounded_modinfo_reads() {
    let (root, sys_module_root, _, _) = temporary_activation_roots("openrazer-driver-metadata");
    let loaded_keyboard = sys_module_root.join("razerkbd");
    fs::create_dir(&loaded_keyboard).expect("loaded keyboard module fixture");
    fs::write(loaded_keyboard.join("srcversion"), "KBD\n")
        .expect("loaded keyboard identity fixture");
    let success = |stdout: &str| {
        Ok(CommandOutput {
            success: true,
            stdout: stdout.to_owned(),
        })
    };
    let probe = RealSystemProbe::with_paths(
        FakeRunner::new(vec![
            success("ACCESSORY"),
            success("KBD"),
            success("razer-feature-report-v1"),
            success("KRAKEN"),
            success("MOUSE"),
            success("razer-feature-report-v1"),
        ]),
        sys_module_root,
        root.join("unused-bridge.sock"),
        Duration::from_millis(STATUS_TIMEOUT_MS),
    );

    let snapshot = probe.openrazer_driver_snapshot();

    assert_eq!(snapshot.state(), OpenRazerDriverState::Ready);
    let keyboard = snapshot
        .modules
        .iter()
        .find(|module| module.module_name == "razerkbd")
        .expect("keyboard module snapshot");
    assert!(keyboard.loaded);
    assert_eq!(keyboard.loaded_identity.as_deref(), Some("KBD"));
    assert_eq!(
        probe.runner.calls(),
        vec![
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    "srcversion".to_owned(),
                    "razeraccessory".to_owned()
                ],
            ),
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    "srcversion".to_owned(),
                    "razerkbd".to_owned()
                ],
            ),
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    OPENRAZER_NATIVE_CAPABILITY_FIELD.to_owned(),
                    "razerkbd".to_owned(),
                ],
            ),
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    "srcversion".to_owned(),
                    "razerkraken".to_owned()
                ],
            ),
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    "srcversion".to_owned(),
                    "razermouse".to_owned()
                ],
            ),
            (
                "modinfo".to_owned(),
                vec![
                    "-F".to_owned(),
                    OPENRAZER_NATIVE_CAPABILITY_FIELD.to_owned(),
                    "razermouse".to_owned(),
                ],
            ),
        ]
    );
    assert_eq!(
        probe.runner.timeouts(),
        vec![Duration::from_millis(STATUS_TIMEOUT_MS); 6]
    );
    fs::remove_dir_all(root).expect("probe cleanup");
}

#[test]
fn openrazer_version_parser_ignores_distribution_package_releases() {
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
fn openrazer_runtime_records_versions_without_using_them_as_a_gate() {
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
    assert_eq!(verified.state, OpenRazerRuntimeState::Detected);
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
    assert_eq!(future.state, OpenRazerRuntimeState::Detected);
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
fn stock_openrazer_daemon_requires_exact_bounded_dbus_owner_query() {
    let (state, calls) = openrazer_access_with_owner_result(Ok(CommandOutput {
        success: true,
        stdout: "b true".to_owned(),
    }));
    assert_eq!(state, OpenRazerAccessState::Ready);
    assert_eq!(calls[0].1, ["group", "openrazer"]);
    assert_eq!(calls[1].1, ["-u"]);
    assert_eq!(calls[2].1, ["-G"]);
    assert_eq!(calls[3].0, "busctl");
    assert_eq!(
        calls[3].1,
        [
            "--user",
            "--no-pager",
            "--no-legend",
            "call",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            "s",
            "org.razer",
        ]
    );
}

#[test]
fn absent_stock_openrazer_bus_owner_is_unavailable() {
    let (state, _) = openrazer_access_with_owner_result(Ok(CommandOutput {
        success: true,
        stdout: "b false".to_owned(),
    }));
    assert_eq!(state, OpenRazerAccessState::DbusUnavailable);
}

#[test]
fn malformed_or_failed_stock_openrazer_owner_probes_fail_closed() {
    for malformed in [
        "",
        "true",
        "b TRUE",
        "b 1",
        "b true extra",
        "b true\nb false",
    ] {
        let (state, _) = openrazer_access_with_owner_result(Ok(CommandOutput {
            success: true,
            stdout: malformed.to_owned(),
        }));
        assert_eq!(state, OpenRazerAccessState::Unverified, "{malformed:?}");
    }

    let (timed_out, _) = openrazer_access_with_owner_result(Err(ProbeError::CommandTimedOut));
    assert_eq!(timed_out, OpenRazerAccessState::Unverified);

    let (unsuccessful, _) = openrazer_access_with_owner_result(Ok(CommandOutput {
        success: false,
        stdout: "b true".to_owned(),
    }));
    assert_eq!(unsuccessful, OpenRazerAccessState::Unverified);
}

#[test]
fn root_doctor_does_not_claim_a_desktop_openrazer_bus() {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let daemon = std::env::temp_dir().join(format!("hfx-openrazer-root-{unique}"));
    fs::write(&daemon, "fixture").expect("write daemon fixture");
    let probe = RealSystemProbe::with_all_paths(
        FakeRunner::new(vec![
            Ok(CommandOutput {
                success: true,
                stdout: "openrazer:x:946:".to_owned(),
            }),
            Ok(CommandOutput {
                success: true,
                stdout: "0".to_owned(),
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
        OpenRazerAccessState::NotApplicable
    );
    assert_eq!(probe.runner.calls().len(), 2);
    fs::remove_file(daemon).expect("remove daemon fixture");
}

#[test]
fn real_command_runner_preserves_the_global_output_bound() {
    let oversized = "x".repeat(MAX_COMMAND_OUTPUT_BYTES + 1);
    assert_eq!(
        RealCommandRunner.run("printf", &["%s", &oversized], Duration::from_secs(1)),
        Err(ProbeError::CommandOutputInvalid)
    );
}

#[test]
fn typed_bus_owner_replies_are_exact() {
    assert_eq!(parse_bus_name_has_owner("b true"), Some(true));
    assert_eq!(parse_bus_name_has_owner("b false"), Some(false));
    assert_eq!(parse_bus_name_has_owner("org.razer 1234 openrazer"), None);
}

#[test]
fn stock_openrazer_publication_replies_are_bounded_and_exact() {
    assert_eq!(parse_openrazer_serials("as 0"), Some(Vec::new()));
    assert_eq!(
        parse_openrazer_serials("as 2 \"MOUSE_0001\" \"UNKNOWN_15320296_0000\""),
        Some(vec![
            "MOUSE_0001".to_owned(),
            "UNKNOWN_15320296_0000".to_owned(),
        ])
    );
    assert_eq!(
        parse_openrazer_vid_pid("ai 2 5426 662"),
        Some(OpenRazerPublishedDevice {
            vendor_id: 0x1532,
            product_id: 0x0296,
        })
    );
    for malformed in [
        "",
        "as 1 MOUSE_0001",
        "as 1 \"bad/path\"",
        "as 1 \"duplicate\" \"extra\"",
        "as 2 \"duplicate\" \"duplicate\"",
        "as 65",
    ] {
        assert_eq!(parse_openrazer_serials(malformed), None, "{malformed:?}");
    }
    for malformed in [
        "",
        "ai 1 5426",
        "ai 2 0 662",
        "ai 2 5426 0",
        "ai 2 5426 65536",
        "ai 2 5426 662 extra",
    ] {
        assert_eq!(parse_openrazer_vid_pid(malformed), None, "{malformed:?}");
    }
}

#[test]
fn stock_openrazer_publication_uses_only_public_no_autostart_calls() {
    let probe = RealSystemProbe::with_all_paths(
        FakeRunner::new(vec![
            Ok(CommandOutput {
                success: true,
                stdout: "as 2 \"MOUSE_0001\" \"KEYBOARD_0001\"".to_owned(),
            }),
            Ok(CommandOutput {
                success: true,
                stdout: "ai 2 5426 205".to_owned(),
            }),
            Ok(CommandOutput {
                success: true,
                stdout: "ai 2 5426 662".to_owned(),
            }),
        ]),
        PathBuf::from("/missing"),
        PathBuf::from("/missing-usb"),
        PathBuf::from("/missing-openrazer"),
        PathBuf::from("/missing.sock"),
        Duration::from_millis(10),
    );

    assert_eq!(
        probe
            .openrazer_published_devices()
            .expect("bounded stock inventory"),
        vec![
            OpenRazerPublishedDevice {
                vendor_id: 0x1532,
                product_id: 0x00cd,
            },
            OpenRazerPublishedDevice {
                vendor_id: 0x1532,
                product_id: 0x0296,
            },
        ]
    );
    let calls = probe.runner.calls();
    assert_eq!(calls.len(), 3);
    assert_eq!(
        calls[0],
        (
            "busctl".to_owned(),
            vec![
                "--user".to_owned(),
                "--no-pager".to_owned(),
                "--no-legend".to_owned(),
                "--auto-start=no".to_owned(),
                "call".to_owned(),
                "org.razer".to_owned(),
                "/org/razer".to_owned(),
                "razer.devices".to_owned(),
                "getDevices".to_owned(),
            ],
        )
    );
    assert_eq!(calls[1].1[6], "/org/razer/device/MOUSE_0001");
    assert_eq!(calls[1].1[7], "razer.device.misc");
    assert_eq!(calls[1].1[8], "getVidPid");
    assert_eq!(calls[2].1[6], "/org/razer/device/KEYBOARD_0001");
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

#[test]
fn disconnected_activation_waits_for_refs_then_replaces_the_core_module_once() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("activation");
    write_loaded_module_fixture(&sys_root, &hid_root, "OLD", "1");
    let refcount = sys_root
        .join(KERNEL_MODULE_NAME.replace('-', "_"))
        .join("refcnt");
    let release = thread::spawn(move || {
        thread::sleep(Duration::from_millis(60));
        fs::write(refcount, "0").expect("release module reference");
    });
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    let receipt = probe
        .activate_disconnected_driver()
        .expect("settled replacement succeeds");
    release.join().expect("reference release thread");

    assert_eq!(receipt.installed_module_identity, "NEW");
    assert_eq!(receipt.loaded_module_identity, "NEW");
    let calls = probe.runner.calls();
    assert_eq!(
        calls
            .iter()
            .filter(|(program, _)| program == "modprobe")
            .cloned()
            .collect::<Vec<_>>(),
        vec![
            (
                "modprobe".to_owned(),
                vec!["-r".to_owned(), KERNEL_MODULE_NAME.to_owned()]
            ),
            ("modprobe".to_owned(), vec![KERNEL_MODULE_NAME.to_owned()]),
        ]
    );
    assert_eq!(
        calls
            .iter()
            .filter(|(program, _)| program == "udevadm")
            .count(),
        3
    );
    assert!(!calls.iter().any(|(_, arguments)| {
        arguments
            .iter()
            .any(|argument| argument == "razerkbd" || argument == "razermouse")
    }));
    fs::remove_dir_all(root).expect("activation fixture cleanup");
}

#[test]
fn matching_loaded_driver_is_verified_without_module_mutation() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("idempotent");
    write_loaded_module_fixture(&sys_root, &hid_root, "NEW", "0");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    let receipt = probe
        .activate_disconnected_driver()
        .expect("matching module verifies");

    assert_eq!(receipt.loaded_module_identity, "NEW");
    assert!(
        !probe
            .runner
            .calls()
            .iter()
            .any(|(program, _)| program == "modprobe")
    );
    fs::remove_dir_all(root).expect("idempotent fixture cleanup");
}

#[test]
fn post_load_identity_mismatch_fails_after_one_bounded_replacement() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("mismatch");
    write_loaded_module_fixture(&sys_root, &hid_root, "OLD", "0");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "WRONG");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::IdentityMismatch)
    );
    assert_eq!(
        probe
            .runner
            .calls()
            .iter()
            .filter(|(program, _)| program == "modprobe")
            .count(),
        2
    );
    fs::remove_dir_all(root).expect("mismatch fixture cleanup");
}

#[test]
fn loaded_module_with_missing_holders_fails_closed() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("holders");
    let module = KERNEL_MODULE_NAME.replace('-', "_");
    let module_path = sys_root.join(&module);
    fs::create_dir_all(&module_path).expect("module fixture");
    fs::create_dir_all(hid_root.join(KERNEL_MODULE_NAME)).expect("driver fixture");
    fs::write(module_path.join("srcversion"), "OLD").expect("identity fixture");
    fs::write(module_path.join("refcnt"), "0").expect("refcount fixture");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::ModuleReferencesUnavailable)
    );
    assert!(
        !probe
            .runner
            .calls()
            .iter()
            .any(|(program, _)| program == "modprobe")
    );
    fs::remove_dir_all(root).expect("holders fixture cleanup");
}

#[test]
fn a_bound_hid_device_prevents_module_unload() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("binding");
    write_loaded_module_fixture(&sys_root, &hid_root, "OLD", "0");
    let target = root.join("bound-device");
    fs::create_dir(&target).expect("bound target");
    symlink(
        target,
        hid_root
            .join(KERNEL_MODULE_NAME)
            .join("0003:1532:00CF.0001"),
    )
    .expect("bound device symlink");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert!(!probe.module_references_gone().expect("references read"));
    fs::remove_dir_all(root).expect("binding fixture cleanup");
}

#[test]
fn destructive_receiver_absence_proof_fails_on_inventory_overflow() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("usb-overflow");
    for index in 0..=MAX_USB_DEVICE_ENTRIES {
        fs::create_dir(usb_root.join(format!("entry-{index}"))).expect("USB entry");
    }
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::ReceiverInventoryUnavailable)
    );
    assert!(
        !probe
            .runner
            .calls()
            .iter()
            .any(|(program, _)| program == "modprobe")
    );
    fs::remove_dir_all(root).expect("USB overflow fixture cleanup");
}

#[test]
fn destructive_receiver_absence_proof_rejects_malformed_identity() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("usb-malformed");
    let device = usb_root.join("3-4");
    fs::create_dir(&device).expect("USB device");
    fs::write(device.join("idVendor"), "zzzz\n").expect("malformed vendor");
    fs::write(device.join("idProduct"), "00cf\n").expect("product");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::ReceiverInventoryUnavailable)
    );
    assert!(
        !probe
            .runner
            .calls()
            .iter()
            .any(|(program, _)| program == "modprobe")
    );
    fs::remove_dir_all(root).expect("malformed USB fixture cleanup");
}

#[test]
fn connected_supported_receiver_blocks_before_module_mutation() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("receiver-present");
    let receiver = usb_root.join("3-4");
    fs::create_dir(&receiver).expect("receiver fixture");
    fs::write(receiver.join("idVendor"), "1532\n").expect("receiver vendor");
    fs::write(receiver.join("idProduct"), "00cf\n").expect("receiver product");
    let runner = ActivationRunner::new(&["NEW"], sys_root.clone(), hid_root.clone(), "NEW");
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::ReceiverPresent)
    );
    assert!(
        !probe
            .runner
            .calls()
            .iter()
            .any(|(program, _)| program == "modprobe")
    );
    fs::remove_dir_all(root).expect("receiver fixture cleanup");
}

#[test]
fn failed_udev_settle_stops_before_inventory_or_module_mutation() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("udev-failure");
    let runner = FakeRunner::new(vec![Ok(CommandOutput {
        success: false,
        stdout: String::new(),
    })]);
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::UdevSettleFailed)
    );
    assert_eq!(
        probe.runner.calls(),
        vec![(
            "udevadm".to_owned(),
            vec!["settle".to_owned(), "--timeout=5".to_owned()]
        )]
    );
    fs::remove_dir_all(root).expect("udev fixture cleanup");
}

#[test]
fn concurrent_installed_module_change_is_identity_unstable() {
    let (root, sys_root, hid_root, usb_root) = temporary_activation_roots("identity-change");
    write_loaded_module_fixture(&sys_root, &hid_root, "OLD", "0");
    let runner = ActivationRunner::new(
        &["NEW", "NEW", "OTHER"],
        sys_root.clone(),
        hid_root.clone(),
        "NEW",
    );
    let probe = activation_probe(runner, sys_root, hid_root, usb_root);

    assert_eq!(
        probe.activate_disconnected_driver(),
        Err(DriverActivationError::IdentityUnstable)
    );
    fs::remove_dir_all(root).expect("identity change fixture cleanup");
}

fn driver_inspection_probe(prefix: &str) -> (PathBuf, RealSystemProbe<FakeRunner>) {
    let (root, sys_module_root, hid_drivers_root, usb_devices_root) =
        temporary_activation_roots(prefix);
    let mut probe = activation_probe(
        FakeRunner::new(Vec::new()),
        sys_module_root,
        hid_drivers_root,
        usb_devices_root,
    );
    probe.hid_devices_root = root.join("hid-devices");
    probe.dkms_root = root.join("dkms");
    fs::create_dir_all(&probe.hid_devices_root).expect("HID device root");
    fs::create_dir_all(&probe.dkms_root).expect("DKMS root");
    (root, probe)
}

fn hid_interface(
    probe: &RealSystemProbe<FakeRunner>,
    root: &Path,
    name: &str,
    driver: Option<&str>,
) {
    let device = probe.hid_devices_root.join(name);
    fs::create_dir_all(&device).expect("HID device");
    if let Some(driver) = driver {
        let target = root.join("bus-drivers").join(driver);
        fs::create_dir_all(&target).expect("driver directory");
        symlink(&target, device.join("driver")).expect("driver link");
    }
}

#[test]
fn receiver_interface_drivers_report_the_actual_binding_in_stable_order() {
    let (root, probe) = driver_inspection_probe("receiver-binding");
    hid_interface(&probe, &root, "0003:1532:00CF.000A", None);
    hid_interface(
        &probe,
        &root,
        "0003:1532:00CF.0009",
        Some(KERNEL_MODULE_NAME),
    );
    hid_interface(
        &probe,
        &root,
        "0003:1532:00CF.0008",
        Some("hid-razer-hyperflux-v2"),
    );
    hid_interface(&probe, &root, "0003:046D:C52B.0001", Some("hid-generic"));
    hid_interface(&probe, &root, "0005:1532:00CF.0002", Some("hid-generic"));

    assert_eq!(
        probe.receiver_interface_drivers(),
        vec![
            Some("hid-razer-hyperflux-v2".to_owned()),
            Some(KERNEL_MODULE_NAME.to_owned()),
            None,
        ]
    );
    fs::remove_dir_all(root).expect("cleanup");
}

#[test]
fn missing_hid_device_root_reports_no_receiver_interfaces() {
    let (root, mut probe) = driver_inspection_probe("receiver-binding-missing");
    probe.hid_devices_root = root.join("absent");
    assert!(probe.receiver_interface_drivers().is_empty());
    fs::remove_dir_all(root).expect("cleanup");
}

#[test]
fn only_exact_retired_driver_registrations_are_reported() {
    let (root, probe) = driver_inspection_probe("retired-dkms");
    fs::create_dir_all(probe.dkms_root.join("hid-razer-hyperflux-v2").join("0.1.1"))
        .expect("retired registration");
    fs::create_dir_all(probe.dkms_root.join("hid-razer-hyperflux-v2").join("9.9.9"))
        .expect("unknown version");
    fs::create_dir_all(probe.dkms_root.join(KERNEL_MODULE_NAME).join("2.0.0"))
        .expect("released registration");

    assert_eq!(
        probe.retired_driver_registrations(),
        vec!["hid-razer-hyperflux-v2/0.1.1".to_owned()]
    );
    fs::remove_dir_all(root).expect("cleanup");
}

#[test]
fn hid_device_names_yield_only_usb_bus_identities() {
    assert_eq!(
        hid_device_usb_id("0003:1532:00CF.0008"),
        Some((0x1532, 0x00cf))
    );
    assert_eq!(hid_device_usb_id("0005:1532:00CF.0008"), None);
    assert_eq!(hid_device_usb_id("0003:1532:00CF"), None);
    assert_eq!(hid_device_usb_id("0003:ZZZZ:00CF.0008"), None);
    assert_eq!(hid_device_usb_id("not-a-device"), None);
}

#[test]
fn default_probe_reads_the_real_system_locations() {
    let probe = RealSystemProbe::default();
    assert_eq!(probe.sys_module_root, Path::new("/sys/module"));
    assert_eq!(probe.hid_drivers_root, Path::new(HID_DRIVERS_ROOT));
    assert_eq!(probe.hid_devices_root, Path::new(HID_DEVICES_ROOT));
    assert_eq!(probe.dkms_root, Path::new(DKMS_ROOT));
    assert_eq!(probe.usb_devices_root, Path::new(USB_DEVICES_ROOT));
    assert_eq!(
        probe.openrazer_daemon_path,
        Path::new(OPENRAZER_NATIVE_DAEMON_EXECUTABLE_PATH)
    );
    assert_eq!(
        probe.full_component_descriptor_path,
        Path::new(FULL_COMPONENT_DESCRIPTOR_PATH)
    );
    assert_eq!(probe.socket_path, Path::new(BRIDGE_SOCKET_PATH));
    assert_eq!(probe.timeout, Duration::from_millis(STATUS_TIMEOUT_MS));
}
