// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    DriverActivationController, DriverActivationReceipt, OpenRazerDriverProbe,
    OpenRazerDriverSnapshot, OpenRazerDriverState, OperationalConfigurationOutcome,
    RealSystemProbe, enable_operational_configuration,
};
use hfx_runtime::BRIDGE_CONFIGURATION_FILE_PATH;
use hfx_system_probe::{
    MigrationHost, MigrationRoots, RealMigrationHost, SystemProbe, apply_migration, plan_migration,
    render_migration_preview,
};
use std::env;
use std::fmt::Write as _;
use std::path::Path;

fn usage() -> &'static str {
    "Usage: hyperflux-next-activate <activate-disconnected-driver|check-openrazer-driver|migrate-retired-dkms [--apply]>\n"
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

/// Previews, or with `--apply` performs, the migration away from retired
/// `HyperFlux` DKMS drivers that can take the receiver after a kernel update.
fn migrate_retired_dkms(probe: &RealSystemProbe, apply: bool) -> Result<(), String> {
    let mut output = String::new();
    let result = migrate_retired_dkms_at(
        &MigrationRoots::default(),
        probe.snapshot().receiver_interface_drivers,
        apply.then_some(rustix::process::geteuid().as_raw() == 0),
        &RealMigrationHost,
        &mut output,
    );
    print!("{output}");
    result
}

/// Plans the migration and, when `apply_as_root` is set, applies it.
///
/// `apply_as_root` is `None` for a preview, otherwise whether the caller is
/// root. The preview, and any completion notice, is appended to `output`
/// even when applying fails.
fn migrate_retired_dkms_at(
    roots: &MigrationRoots,
    interface_drivers: Vec<Option<String>>,
    apply_as_root: Option<bool>,
    host: &impl MigrationHost,
    output: &mut String,
) -> Result<(), String> {
    let plan = plan_migration(roots, interface_drivers);
    output.push_str(&render_migration_preview(&plan));
    let Some(root) = apply_as_root else {
        return Ok(());
    };
    if !root {
        return Err("applying the migration must run as root".to_owned());
    }
    let record = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "the system clock is before 1970".to_owned())?
        .as_secs()
        .to_string();
    let receipt = apply_migration(&plan, roots, host, &record)
        .map_err(|error| format!("migration stopped: {error}"))?;
    if !receipt.removed_registrations.is_empty() || !receipt.moved_source_trees.is_empty() {
        let _ = writeln!(
            output,
            "Migration complete. Record: {}. Reboot normally, then run hyperfluxctl doctor.",
            receipt.record_directory.display()
        );
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Action {
    ActivateDisconnectedDriver,
    CheckOpenRazerDriver,
    MigrateRetiredDkms { apply: bool },
}

fn parse_action(mut arguments: impl Iterator<Item = String>) -> Result<Action, String> {
    let command = arguments.next().ok_or_else(|| usage().to_owned())?;
    let flag = arguments.next();
    if arguments.next().is_some() {
        return Err(usage().to_owned());
    }
    match (command.as_str(), flag.as_deref()) {
        ("activate-disconnected-driver", None) => Ok(Action::ActivateDisconnectedDriver),
        ("check-openrazer-driver", None) => Ok(Action::CheckOpenRazerDriver),
        ("migrate-retired-dkms", None) => Ok(Action::MigrateRetiredDkms { apply: false }),
        ("migrate-retired-dkms", Some("--apply")) => Ok(Action::MigrateRetiredDkms { apply: true }),
        _ => Err(usage().to_owned()),
    }
}

fn run() -> Result<(), String> {
    let action = parse_action(env::args().skip(1))?;
    let probe = RealSystemProbe::default();
    match action {
        Action::ActivateDisconnectedDriver => activate_disconnected_driver(&probe),
        Action::CheckOpenRazerDriver => check_openrazer_driver(&probe),
        Action::MigrateRetiredDkms { apply } => migrate_retired_dkms(&probe, apply),
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

    fn arguments(values: &[&str]) -> impl Iterator<Item = String> {
        values
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>()
            .into_iter()
    }

    #[test]
    fn arguments_select_exactly_one_action() {
        assert_eq!(
            parse_action(arguments(&["activate-disconnected-driver"])),
            Ok(Action::ActivateDisconnectedDriver)
        );
        assert_eq!(
            parse_action(arguments(&["check-openrazer-driver"])),
            Ok(Action::CheckOpenRazerDriver)
        );
        assert_eq!(
            parse_action(arguments(&["migrate-retired-dkms"])),
            Ok(Action::MigrateRetiredDkms { apply: false })
        );
        assert_eq!(
            parse_action(arguments(&["migrate-retired-dkms", "--apply"])),
            Ok(Action::MigrateRetiredDkms { apply: true })
        );
        for rejected in [
            &[][..],
            &["unknown"][..],
            &["check-openrazer-driver", "--apply"][..],
            &["migrate-retired-dkms", "--force"][..],
            &["migrate-retired-dkms", "--apply", "extra"][..],
        ] {
            assert_eq!(
                parse_action(arguments(rejected)),
                Err(usage().to_owned()),
                "{rejected:?}"
            );
        }
    }

    struct MigrationFixture {
        root: PathBuf,
        roots: MigrationRoots,
    }

    impl Drop for MigrationFixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn migration_fixture(prefix: &str, retired: bool) -> MigrationFixture {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let root = std::env::temp_dir().join(format!("hfx-activate-{prefix}-{unique}"));
        let roots = MigrationRoots {
            dkms: root.join("dkms"),
            usr_src: root.join("usr-src"),
            kernel_modules: root.join("modules"),
            boot: root.join("boot"),
            dracut_hook_script: root.join("dracut-install"),
            mkinitcpio_presets: root.join("mkinitcpio.d"),
            records: root.join("records"),
        };
        for directory in [
            &roots.dkms,
            &roots.usr_src,
            &roots.kernel_modules,
            &roots.boot,
        ] {
            fs::create_dir_all(directory).expect("fixture directory");
        }
        fs::write(&roots.dracut_hook_script, "#!/bin/sh\n").expect("hook script");
        fs::write(roots.boot.join("initramfs-linux.img"), "image").expect("boot image");
        if retired {
            let (name, version) = hfx_runtime::RETIRED_KERNEL_DRIVERS[0];
            fs::create_dir_all(roots.dkms.join(name).join(version)).expect("registration");
            fs::create_dir_all(roots.usr_src.join(format!("{name}-{version}")))
                .expect("source tree");
        }
        MigrationFixture { root, roots }
    }

    struct CleanImageHost;

    impl MigrationHost for CleanImageHost {
        fn run(
            &self,
            program: &Path,
            _arguments: &[&str],
            _stdin: Option<&str>,
        ) -> Result<String, hfx_system_probe::MigrationError> {
            Ok(if program == Path::new("lsinitrd") {
                "usr/lib/modules/6.18/kernel/drivers/hid/hid-hyperflux-next.ko.zst\n".to_owned()
            } else {
                String::new()
            })
        }
    }

    #[test]
    fn migration_preview_changes_nothing_and_apply_requires_root() {
        let fixture = migration_fixture("preview", true);
        let mut preview = String::new();
        migrate_retired_dkms_at(
            &fixture.roots,
            Vec::new(),
            None,
            &CleanImageHost,
            &mut preview,
        )
        .expect("preview");
        assert!(preview.contains("Would deregister DKMS"), "{preview}");
        assert!(!fixture.roots.records.exists());

        let mut refused = String::new();
        let error = migrate_retired_dkms_at(
            &fixture.roots,
            Vec::new(),
            Some(false),
            &CleanImageHost,
            &mut refused,
        )
        .expect_err("non-root apply");
        assert!(error.contains("must run as root"), "{error}");
        assert!(refused.contains("Would deregister DKMS"), "{refused}");
        assert!(!fixture.roots.records.exists());
    }

    #[test]
    fn applied_migration_reports_its_record_only_when_it_changed_something() {
        let retired = migration_fixture("apply", true);
        let mut output = String::new();
        migrate_retired_dkms_at(
            &retired.roots,
            Vec::new(),
            Some(true),
            &CleanImageHost,
            &mut output,
        )
        .expect("migration");
        assert!(output.contains("Migration complete. Record: "), "{output}");

        let healthy = migration_fixture("healthy", false);
        let mut quiet = String::new();
        migrate_retired_dkms_at(
            &healthy.roots,
            Vec::new(),
            Some(true),
            &CleanImageHost,
            &mut quiet,
        )
        .expect("no-op");
        assert!(quiet.contains("Nothing to do."), "{quiet}");
        assert!(!quiet.contains("Migration complete"), "{quiet}");
    }

    #[test]
    fn stopped_migration_keeps_the_preview_and_names_the_failure() {
        struct FailingHost;
        impl MigrationHost for FailingHost {
            fn run(
                &self,
                _program: &Path,
                _arguments: &[&str],
                _stdin: Option<&str>,
            ) -> Result<String, hfx_system_probe::MigrationError> {
                Err(hfx_system_probe::MigrationError("dkms failed".to_owned()))
            }
        }
        let fixture = migration_fixture("stopped", true);
        let mut output = String::new();
        let error = migrate_retired_dkms_at(
            &fixture.roots,
            Vec::new(),
            Some(true),
            &FailingHost,
            &mut output,
        )
        .expect_err("stops");
        assert_eq!(error, "migration stopped: dkms failed");
        assert!(output.contains("Would deregister DKMS"), "{output}");
    }
}
