// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    DriverActivationController, DriverActivationReceipt, OpenRazerDriverProbe,
    OpenRazerDriverSnapshot, OpenRazerDriverState, OperationalConfigurationOutcome,
    RealSystemProbe, enable_operational_configuration,
};
use hfx_runtime::BRIDGE_CONFIGURATION_FILE_PATH;
use std::env;
use std::path::Path;

fn usage() -> &'static str {
    "Usage: hyperflux-next-activate <activate-disconnected-driver|check-openrazer-driver>\n"
}

fn ensure_openrazer_driver(snapshot: &OpenRazerDriverSnapshot) -> Result<(), String> {
    match snapshot.state() {
        OpenRazerDriverState::Ready => Ok(()),
        OpenRazerDriverState::ActivationPending => Err(
            "an older OpenRazer route module is active; reboot before starting the native relay"
                .to_owned(),
        ),
        OpenRazerDriverState::CapabilityUnavailable => {
            let module = snapshot
                .blocking_module()
                .map_or("unknown", |value| value.module_name.as_str());
            Err(format!(
                "the installed OpenRazer route module {module} lacks the HyperFlux native transport capability"
            ))
        }
        OpenRazerDriverState::Unavailable => {
            let module = snapshot
                .blocking_module()
                .map_or("unknown", |value| value.module_name.as_str());
            Err(format!(
                "the installed OpenRazer route module {module} is unavailable"
            ))
        }
    }
}

fn activate_disconnected_driver_at(
    probe: &impl DriverActivationController,
) -> Result<DriverActivationReceipt, String> {
    probe
        .activate_disconnected_driver()
        .map_err(|error| format!("driver activation failed: {error}"))
}

fn activate_operationally_at(
    probe: &impl DriverActivationController,
    configuration_path: &Path,
) -> Result<(DriverActivationReceipt, OperationalConfigurationOutcome), String> {
    let receipt = activate_disconnected_driver_at(probe)?;
    let outcome = enable_operational_configuration(configuration_path)
        .map_err(|error| format!("operational configuration failed: {error}"))?;
    Ok((receipt, outcome))
}

fn activate_disconnected_driver(probe: &RealSystemProbe) -> Result<(), String> {
    if rustix::process::geteuid().as_raw() != 0 {
        return Err("disconnected driver activation must run as root".to_owned());
    }
    let (receipt, _) = activate_operationally_at(probe, Path::new(BRIDGE_CONFIGURATION_FILE_PATH))?;
    println!(
        "HyperFlux Next activated the kernel module (installed {}, loaded {}).",
        receipt.installed_module_identity, receipt.loaded_module_identity
    );
    Ok(())
}

fn check_openrazer_driver(probe: &impl OpenRazerDriverProbe) -> Result<(), String> {
    ensure_openrazer_driver(&probe.openrazer_driver_snapshot())
}

fn run() -> Result<(), String> {
    let mut arguments = env::args().skip(1);
    let command = arguments.next().ok_or_else(|| usage().to_owned())?;
    if arguments.next().is_some() {
        return Err(usage().to_owned());
    }
    let probe = RealSystemProbe::default();
    match command.as_str() {
        "activate-disconnected-driver" => activate_disconnected_driver(&probe),
        "check-openrazer-driver" => check_openrazer_driver(&probe),
        _ => Err(usage().to_owned()),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("hyperflux-next-activate: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_ops::DriverActivationError;
    use hfx_runtime::BRIDGE_CLIENT_GROUP;
    use std::cell::Cell;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct DriverMetadataProbe {
        calls: Cell<usize>,
        snapshot: OpenRazerDriverSnapshot,
    }

    impl OpenRazerDriverProbe for DriverMetadataProbe {
        fn openrazer_driver_snapshot(&self) -> OpenRazerDriverSnapshot {
            self.calls.set(self.calls.get() + 1);
            self.snapshot.clone()
        }
    }

    fn ready_openrazer_driver_snapshot() -> OpenRazerDriverSnapshot {
        OpenRazerDriverSnapshot {
            modules: ["razeraccessory", "razerkbd", "razerkraken", "razermouse"]
                .into_iter()
                .map(|module| hfx_ops::KernelModuleIdentitySnapshot {
                    module_name: module.to_owned(),
                    installed_identity: Some(format!("{module}-identity")),
                    loaded: false,
                    loaded_identity: None,
                    native_transport_capability: matches!(module, "razerkbd" | "razermouse")
                        .then(|| "razer-feature-report-v1".to_owned()),
                })
                .collect(),
        }
    }

    struct ActivationProbe {
        calls: Cell<usize>,
        result: Result<DriverActivationReceipt, DriverActivationError>,
    }

    fn temporary_configuration(mode: &str, restoration: bool) -> (PathBuf, PathBuf) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let directory = std::env::temp_dir().join(format!("hfx-activation-{unique}"));
        fs::create_dir(&directory).expect("temporary directory");
        let path = directory.join("bridge.json");
        fs::write(
            &path,
            format!(
                "{{\n  \"$schema\": \"/usr/share/hyperflux-next/schemas/bridge-config.schema.json\",\n  \"schema\": \"hyperflux-bridge-config-v1\",\n  \"mode\": \"{mode}\",\n  \"restoration\": {{\"enabled\": {restoration}}},\n  \"socket\": {{\"group\": \"{BRIDGE_CLIENT_GROUP}\", \"mode\": \"0660\"}}\n}}\n"
            ),
        )
        .expect("configuration writes");
        (directory, path)
    }

    impl DriverActivationController for ActivationProbe {
        fn activate_disconnected_driver(
            &self,
        ) -> Result<DriverActivationReceipt, DriverActivationError> {
            self.calls.set(self.calls.get() + 1);
            self.result.clone()
        }
    }

    #[test]
    fn explicit_activation_reports_the_proven_module_identity() {
        let probe = ActivationProbe {
            calls: Cell::new(0),
            result: Ok(DriverActivationReceipt {
                installed_module_identity: "NEW".to_owned(),
                loaded_module_identity: "NEW".to_owned(),
            }),
        };

        let receipt = activate_disconnected_driver_at(&probe).expect("activation succeeds");

        assert_eq!(receipt.installed_module_identity, "NEW");
        assert_eq!(receipt.loaded_module_identity, "NEW");
        assert_eq!(probe.calls.get(), 1);
    }

    #[test]
    fn explicit_activation_enables_operation_without_enabling_restoration() {
        let probe = ActivationProbe {
            calls: Cell::new(0),
            result: Ok(DriverActivationReceipt {
                installed_module_identity: "NEW".to_owned(),
                loaded_module_identity: "NEW".to_owned(),
            }),
        };
        let (directory, path) = temporary_configuration("read-only", false);

        let (_, outcome) = activate_operationally_at(&probe, &path).expect("activation succeeds");
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(&path).expect("configuration reads"))
                .expect("configuration parses");

        assert_eq!(outcome, OperationalConfigurationOutcome::Enabled);
        assert_eq!(value["mode"], "qualified-live");
        assert_eq!(value["restoration"]["enabled"], false);
        assert_eq!(probe.calls.get(), 1);
        fs::remove_dir_all(directory).expect("cleanup");
    }

    #[test]
    fn failed_driver_activation_does_not_change_configuration() {
        let probe = ActivationProbe {
            calls: Cell::new(0),
            result: Err(DriverActivationError::ReceiverPresent),
        };
        let (directory, path) = temporary_configuration("read-only", false);
        let before = fs::read(&path).expect("configuration reads");

        let error = activate_operationally_at(&probe, &path)
            .expect_err("connected receiver must block activation");

        assert!(error.contains("receiver is still connected"));
        assert_eq!(fs::read(&path).expect("configuration remains"), before);
        fs::remove_dir_all(directory).expect("cleanup");
    }

    #[test]
    fn explicit_activation_preserves_the_fail_closed_reason() {
        let probe = ActivationProbe {
            calls: Cell::new(0),
            result: Err(DriverActivationError::ReceiverPresent),
        };

        let error = activate_disconnected_driver_at(&probe)
            .expect_err("connected receiver must block activation");

        assert!(error.contains("receiver is still connected"));
        assert_eq!(probe.calls.get(), 1);
    }

    #[test]
    fn openrazer_driver_check_uses_only_the_focused_metadata_probe() {
        let probe = DriverMetadataProbe {
            calls: Cell::new(0),
            snapshot: ready_openrazer_driver_snapshot(),
        };

        check_openrazer_driver(&probe).expect("focused driver metadata is ready");

        assert_eq!(probe.calls.get(), 1);
    }

    #[test]
    fn openrazer_driver_check_fails_closed_for_each_required_metadata_state() {
        let mut unavailable = ready_openrazer_driver_snapshot();
        unavailable.modules[0].installed_identity = None;
        let mut capability_missing = ready_openrazer_driver_snapshot();
        capability_missing.modules[1].native_transport_capability = None;
        let mut stale = ready_openrazer_driver_snapshot();
        stale.modules[1].loaded = true;
        stale.modules[1].loaded_identity = Some("older-identity".to_owned());

        for (snapshot, expected) in [
            (unavailable, "razeraccessory is unavailable"),
            (capability_missing, "razerkbd lacks"),
            (stale, "older OpenRazer route module is active"),
        ] {
            let probe = DriverMetadataProbe {
                calls: Cell::new(0),
                snapshot,
            };

            let error = check_openrazer_driver(&probe).expect_err("metadata must fail closed");

            assert!(error.contains(expected), "unexpected error: {error}");
            assert_eq!(probe.calls.get(), 1);
        }
    }
}
