// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use std::cell::RefCell;
use std::time::{SystemTime, UNIX_EPOCH};

const RETIRED: &str = "hid-razer-hyperflux-v2";
const RELEASED_ALIAS: &str = "alias hid:b0003g*v00001532p000000CF hid_hyperflux_next\n";
const RETIRED_ALIAS: &str = "alias hid:b0003g*v00001532p000000CF hid_razer_hyperflux_v2\n";
const CLEAN_LISTING: &str =
    "usr/lib/modules/6.18.52-1-lts/kernel/drivers/hid/hid-hyperflux-next.ko.zst\n";

struct Fixture {
    root: PathBuf,
    roots: MigrationRoots,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn fixture(prefix: &str) -> Fixture {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let root = std::env::temp_dir().join(format!("hfx-migration-{prefix}-{unique}"));
    let roots = MigrationRoots {
        dkms: root.join("dkms"),
        usr_src: root.join("usr-src"),
        kernel_modules: root.join("modules"),
        boot: root.join("boot"),
        dracut_hook_script: root.join("hooks").join("dracut-install"),
        mkinitcpio_presets: root.join("mkinitcpio.d"),
        records: root.join("records"),
    };
    for directory in [&roots.dkms, &roots.usr_src, &roots.boot, &roots.records] {
        fs::create_dir_all(directory).expect("fixture directory");
    }
    let kernel = roots.kernel_modules.join("6.18.52-1-lts");
    fs::create_dir_all(&kernel).expect("kernel modules");
    fs::write(kernel.join("modules.alias"), RELEASED_ALIAS).expect("alias table");
    fs::create_dir_all(roots.dracut_hook_script.parent().expect("hook directory"))
        .expect("hook directory");
    fs::write(&roots.dracut_hook_script, "#!/bin/sh\n").expect("hook script");
    for image in [
        "initramfs-linux.img",
        "initramfs-linux-fallback.img",
        "intel-ucode.img",
    ] {
        fs::write(roots.boot.join(image), "image").expect("boot image");
    }
    Fixture { root, roots }
}

fn add_retired(fixture: &Fixture, version: &str) {
    fs::create_dir_all(fixture.roots.dkms.join(RETIRED).join(version)).expect("registration");
    let tree = fixture.roots.usr_src.join(format!("{RETIRED}-{version}"));
    fs::create_dir_all(&tree).expect("source tree");
    fs::write(tree.join("dkms.conf"), "PACKAGE_NAME=retired\n").expect("dkms.conf");
    let alias = fixture
        .roots
        .kernel_modules
        .join("6.18.52-1-lts")
        .join("modules.alias");
    fs::write(alias, format!("{RELEASED_ALIAS}{RETIRED_ALIAS}")).expect("alias table");
}

type Call = (String, Vec<String>, Option<String>);

struct ScriptedHost {
    listing: String,
    fail_program: Option<&'static str>,
    calls: RefCell<Vec<Call>>,
}

impl ScriptedHost {
    fn new(listing: &str) -> Self {
        Self {
            listing: listing.to_owned(),
            fail_program: None,
            calls: RefCell::new(Vec::new()),
        }
    }

    fn programs(&self) -> Vec<String> {
        self.calls
            .borrow()
            .iter()
            .map(|call| call.0.clone())
            .collect()
    }
}

impl MigrationHost for ScriptedHost {
    fn run(
        &self,
        program: &Path,
        arguments: &[&str],
        stdin: Option<&str>,
    ) -> Result<String, MigrationError> {
        let name = program
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_owned();
        self.calls.borrow_mut().push((
            name.clone(),
            arguments.iter().map(|value| (*value).to_owned()).collect(),
            stdin.map(str::to_owned),
        ));
        if self.fail_program == Some(name.as_str()) {
            return Err(MigrationError(format!("{name} failed")));
        }
        Ok(if matches!(name.as_str(), "lsinitrd" | "lsinitcpio") {
            self.listing.clone()
        } else {
            String::new()
        })
    }
}

fn released() -> Vec<Option<String>> {
    vec![Some(KERNEL_MODULE_NAME.to_owned()); 2]
}

#[test]
fn healthy_system_needs_nothing_and_runs_nothing() {
    let fixture = fixture("healthy");
    let plan = plan_migration(&fixture.roots, released());
    assert!(!plan.is_needed());
    assert_eq!(plan.refusal, None);
    assert!(render_migration_preview(&plan).contains("Nothing to do."));

    let host = ScriptedHost::new(CLEAN_LISTING);
    let receipt = apply_migration(&plan, &fixture.roots, &host, "record").expect("no-op");
    assert!(receipt.removed_registrations.is_empty());
    assert!(host.programs().is_empty());
}

#[test]
fn plan_lists_exact_retired_state_and_ignores_unknown_versions() {
    let fixture = fixture("plan");
    add_retired(&fixture, "0.1.1");
    fs::create_dir_all(fixture.roots.dkms.join(RETIRED).join("9.9.9")).expect("unknown version");

    let plan = plan_migration(&fixture.roots, released());

    assert_eq!(
        plan.registrations,
        vec![(RETIRED.to_owned(), "0.1.1".to_owned())]
    );
    assert_eq!(
        plan.source_trees,
        vec![fixture.roots.usr_src.join(format!("{RETIRED}-0.1.1"))]
    );
    assert_eq!(
        plan.alias_owners,
        vec![
            "hid_hyperflux_next".to_owned(),
            "hid_razer_hyperflux_v2".to_owned()
        ]
    );
    assert_eq!(plan.image_tool, Some(ImageTool::DracutHook));
    assert_eq!(
        plan.images,
        vec![
            fixture.roots.boot.join("initramfs-linux-fallback.img"),
            fixture.roots.boot.join("initramfs-linux.img"),
        ],
        "microcode images are not boot images to inspect"
    );
    assert_eq!(plan.refusal, None);
    let preview = render_migration_preview(&plan);
    assert!(preview.contains("Would deregister DKMS hid-razer-hyperflux-v2/0.1.1"));
    assert!(preview.contains("--apply"));
}

#[test]
fn unknown_module_claiming_the_receiver_refuses_without_running_anything() {
    let fixture = fixture("unknown-alias");
    add_retired(&fixture, "0.1.0");
    let alias = fixture
        .roots
        .kernel_modules
        .join("6.18.52-1-lts")
        .join("modules.alias");
    fs::write(
        alias,
        format!("{RELEASED_ALIAS}alias hid:b0003g*v00001532p000000CF hid_mystery\n"),
    )
    .expect("alias table");

    let plan = plan_migration(&fixture.roots, released());
    let reason = plan.refusal.clone().expect("refused");
    assert!(reason.contains("hid_mystery"), "{reason}");

    let host = ScriptedHost::new(CLEAN_LISTING);
    assert!(apply_migration(&plan, &fixture.roots, &host, "record").is_err());
    assert!(host.programs().is_empty());
    assert!(
        fixture
            .roots
            .usr_src
            .join(format!("{RETIRED}-0.1.0"))
            .is_dir()
    );
}

#[test]
fn unknown_bound_driver_refuses_but_generic_hid_does_not() {
    let fixture = fixture("bound");
    add_retired(&fixture, "0.1.2");
    let refused = plan_migration(&fixture.roots, vec![Some("hid-mystery".to_owned())]);
    assert!(refused.refusal.expect("refused").contains("hid-mystery"));

    let generic = plan_migration(&fixture.roots, vec![Some("hid-generic".to_owned()), None]);
    assert_eq!(generic.refusal, None);
    let retired = plan_migration(&fixture.roots, vec![Some(RETIRED.to_owned())]);
    assert_eq!(retired.refusal, None);
}

#[test]
fn missing_or_ambiguous_image_tooling_refuses() {
    let fixture = fixture("tools");
    add_retired(&fixture, "0.1.1");
    fs::remove_file(&fixture.roots.dracut_hook_script).expect("remove hook");
    let none = plan_migration(&fixture.roots, released());
    assert!(
        none.refusal
            .expect("refused")
            .contains("no supported boot-image tool")
    );

    fs::write(&fixture.roots.dracut_hook_script, "#!/bin/sh\n").expect("hook");
    fs::create_dir_all(&fixture.roots.mkinitcpio_presets).expect("presets");
    fs::write(fixture.roots.mkinitcpio_presets.join("linux.preset"), "").expect("preset");
    let both = plan_migration(&fixture.roots, released());
    assert!(
        both.refusal
            .expect("refused")
            .contains("both dracut and mkinitcpio")
    );
}

#[test]
fn dracut_migration_deregisters_moves_rebuilds_and_inspects_in_order() {
    let fixture = fixture("apply-dracut");
    add_retired(&fixture, "0.1.1");
    let plan = plan_migration(&fixture.roots, released());
    let host = ScriptedHost::new(CLEAN_LISTING);

    let receipt = apply_migration(&plan, &fixture.roots, &host, "record").expect("migration");

    assert_eq!(
        host.programs(),
        vec!["dkms", "dkms", "dracut-install", "lsinitrd", "lsinitrd"]
    );
    let calls = host.calls.borrow();
    assert_eq!(calls[0].1, vec!["status"]);
    assert_eq!(
        calls[1].1,
        vec!["remove", "hid-razer-hyperflux-v2/0.1.1", "--all"]
    );
    assert!(
        calls[2]
            .2
            .as_deref()
            .is_some_and(|stdin| stdin.ends_with("/dkms.conf\n")),
        "the dracut hook must rebuild every kernel"
    );
    drop(calls);
    assert_eq!(
        receipt.removed_registrations,
        vec!["hid-razer-hyperflux-v2/0.1.1"]
    );
    assert_eq!(receipt.inspected_images.len(), 2);
    let moved = fixture
        .roots
        .records
        .join("record")
        .join("src")
        .join(format!("{RETIRED}-0.1.1"));
    assert!(
        moved.join("dkms.conf").is_file(),
        "source is moved, never deleted"
    );
    assert!(
        !fixture
            .roots
            .usr_src
            .join(format!("{RETIRED}-0.1.1"))
            .exists()
    );
    for record in [
        "dkms-status-before.txt",
        "receiver-alias-owners.txt",
        "boot-images-before.txt",
    ] {
        assert!(
            fixture.roots.records.join("record").join(record).is_file(),
            "{record}"
        );
    }
}

#[test]
fn mkinitcpio_migration_uses_its_own_rebuild_and_inspector() {
    let fixture = fixture("apply-mkinitcpio");
    add_retired(&fixture, "0.1.0");
    fs::remove_file(&fixture.roots.dracut_hook_script).expect("remove hook");
    fs::create_dir_all(&fixture.roots.mkinitcpio_presets).expect("presets");
    fs::write(fixture.roots.mkinitcpio_presets.join("linux.preset"), "").expect("preset");
    let plan = plan_migration(&fixture.roots, released());
    let host = ScriptedHost::new(CLEAN_LISTING);

    apply_migration(&plan, &fixture.roots, &host, "record").expect("migration");

    assert_eq!(
        host.programs(),
        vec!["dkms", "dkms", "mkinitcpio", "lsinitcpio", "lsinitcpio"]
    );
    assert_eq!(host.calls.borrow()[2].1, vec!["-P"]);
}

#[test]
fn image_inspection_must_positively_prove_the_retired_driver_is_gone() {
    for (listing, expected) in [
        ("", "could not be positively inspected"),
        (
            "usr/lib/modules/6.18/kernel/drivers/hid/hid-razer-hyperflux-v2.ko.zst\n",
            "still contains a retired HyperFlux driver",
        ),
        (
            "usr/lib/modules/6.18/kernel/drivers/hid/hid_razer_hyperflux_v2.ko\n",
            "still contains a retired HyperFlux driver",
        ),
    ] {
        let fixture = fixture("inspect");
        add_retired(&fixture, "0.1.1");
        let plan = plan_migration(&fixture.roots, released());
        let host = ScriptedHost::new(listing);
        let error = apply_migration(&plan, &fixture.roots, &host, "record").expect_err("stops");
        assert!(error.0.contains(expected), "{error}");
    }

    let fixture = fixture("inspector-fails");
    add_retired(&fixture, "0.1.1");
    let plan = plan_migration(&fixture.roots, released());
    let mut host = ScriptedHost::new(CLEAN_LISTING);
    host.fail_program = Some("lsinitrd");
    assert!(apply_migration(&plan, &fixture.roots, &host, "record").is_err());
}

#[test]
fn default_roots_are_the_real_system_locations() {
    let roots = MigrationRoots::default();
    assert_eq!(roots.dkms, Path::new("/var/lib/dkms"));
    assert_eq!(roots.usr_src, Path::new("/usr/src"));
    assert_eq!(roots.kernel_modules, Path::new("/usr/lib/modules"));
    assert_eq!(roots.boot, Path::new("/boot"));
    assert_eq!(
        roots.dracut_hook_script,
        Path::new("/usr/share/libalpm/scripts/dracut-install")
    );
    assert_eq!(roots.mkinitcpio_presets, Path::new("/etc/mkinitcpio.d"));
    assert_eq!(
        roots.records,
        Path::new("/var/lib/hyperflux-next/migrations")
    );
    assert_eq!(MigrationError("stopped".to_owned()).to_string(), "stopped");
}

#[test]
fn preview_names_unbound_interfaces_refusals_and_missing_tooling() {
    let fixture = fixture("preview-refused");
    add_retired(&fixture, "0.1.1");
    fs::remove_file(&fixture.roots.dracut_hook_script).expect("remove hook");
    let plan = plan_migration(&fixture.roots, vec![None]);
    let preview = render_migration_preview(&plan);
    assert!(
        preview.contains("Receiver interfaces: unbound"),
        "{preview}"
    );
    assert!(
        preview.contains("No boot images would be rebuilt."),
        "{preview}"
    );
    assert!(
        preview.contains("Cannot apply: no supported boot-image tool"),
        "{preview}"
    );
    assert!(!preview.contains("--apply"), "{preview}");

    let detached = plan_migration(&fixture.roots, Vec::new());
    assert!(render_migration_preview(&detached).contains("Receiver interfaces: none attached"));
}

#[test]
fn real_host_runs_programs_without_a_shell_and_bounds_failures() {
    let host = RealMigrationHost;
    assert_eq!(
        host.run(Path::new("cat"), &[], Some("piped input"))
            .expect("cat echoes its input"),
        "piped input"
    );
    assert_eq!(
        host.run(Path::new("printf"), &["%s", "argument"], None)
            .expect("printf runs"),
        "argument"
    );

    let failed = host
        .run(Path::new("false"), &[], None)
        .expect_err("false fails");
    assert!(failed.0.contains("false failed with"), "{failed}");

    let missing = host
        .run(Path::new("/nonexistent/hfx-migration-tool"), &[], None)
        .expect_err("missing program");
    assert!(missing.0.contains("cannot start"), "{missing}");

    let bound = (MAX_COMMAND_OUTPUT_BYTES + 1).to_string();
    let oversized = host
        .run(
            Path::new("head"),
            &["-c", bound.as_str(), "/dev/zero"],
            None,
        )
        .expect_err("oversized output");
    assert!(oversized.0.contains("exceeded its bound"), "{oversized}");
}
