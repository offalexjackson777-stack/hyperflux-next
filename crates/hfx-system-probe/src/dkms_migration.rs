// SPDX-License-Identifier: GPL-2.0-only

//! Previewable, fail-closed migration away from retired `HyperFlux` DKMS drivers.
//!
//! A retired pre-release driver that stays registered with DKMS is rebuilt by
//! every kernel update and can bind the receiver before the released driver.
//! Planning only reads state. Applying removes exactly the known retired
//! registrations, moves (never deletes) their source trees into a migration
//! record, rebuilds boot images with the distribution's own tooling, and
//! positively inspects every image. Anything it cannot identify stops it.

use hfx_domain::ProfileKind;
use hfx_profiles::PROFILES;
use hfx_runtime::{KERNEL_MODULE_NAME, RETIRED_KERNEL_DRIVERS};
use std::fmt;
use std::fmt::Write as _;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_COMMAND_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_mins(10);
const GENERIC_HID_DRIVER: &str = "hid-generic";

/// Filesystem locations the migration reads and writes; overridable for tests.
#[derive(Clone, Debug)]
pub struct MigrationRoots {
    pub dkms: PathBuf,
    pub usr_src: PathBuf,
    pub kernel_modules: PathBuf,
    pub boot: PathBuf,
    pub dracut_hook_script: PathBuf,
    pub mkinitcpio_presets: PathBuf,
    pub records: PathBuf,
}

impl Default for MigrationRoots {
    fn default() -> Self {
        Self {
            dkms: PathBuf::from("/var/lib/dkms"),
            usr_src: PathBuf::from("/usr/src"),
            kernel_modules: PathBuf::from("/usr/lib/modules"),
            boot: PathBuf::from("/boot"),
            dracut_hook_script: PathBuf::from("/usr/share/libalpm/scripts/dracut-install"),
            mkinitcpio_presets: PathBuf::from("/etc/mkinitcpio.d"),
            records: PathBuf::from("/var/lib/hyperflux-next/migrations"),
        }
    }
}

/// The boot-image tooling this system actually uses.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ImageTool {
    /// The distribution's dracut pacman hook script.
    DracutHook,
    /// mkinitcpio with its presets.
    Mkinitcpio,
}

impl ImageTool {
    const fn inspector(self) -> &'static str {
        match self {
            Self::DracutHook => "lsinitrd",
            Self::Mkinitcpio => "lsinitcpio",
        }
    }
}

/// Everything the migration would do, gathered without changing anything.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationPlan {
    /// Exact retired `(name, version)` DKMS registrations present.
    pub registrations: Vec<(String, String)>,
    /// Retired source trees under `/usr/src` to move into the record.
    pub source_trees: Vec<PathBuf>,
    /// Driver bound to each qualified receiver interface (`None` = unbound).
    pub interface_drivers: Vec<Option<String>>,
    /// Distinct kernel modules that advertise a qualified receiver modalias.
    pub alias_owners: Vec<String>,
    /// Boot-image tooling, when exactly one supported tool is in use.
    pub image_tool: Option<ImageTool>,
    /// Boot images to rebuild and inspect.
    pub images: Vec<PathBuf>,
    /// Why applying must not proceed, if it must not.
    pub refusal: Option<String>,
}

impl MigrationPlan {
    /// Whether any retired registration or source tree remains.
    #[must_use]
    pub fn is_needed(&self) -> bool {
        !self.registrations.is_empty() || !self.source_trees.is_empty()
    }
}

/// A bounded failure that stops the migration.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationError(pub String);

impl fmt::Display for MigrationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for MigrationError {}

/// What a completed migration changed, for the record and for the user.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationReceipt {
    pub record_directory: PathBuf,
    pub removed_registrations: Vec<String>,
    pub moved_source_trees: Vec<PathBuf>,
    pub inspected_images: Vec<PathBuf>,
}

/// Runs migration commands; the real host executes programs without a shell.
pub trait MigrationHost {
    /// Runs one program and returns its standard output.
    ///
    /// # Errors
    ///
    /// Returns a bounded failure when the program cannot start, exits
    /// unsuccessfully, exceeds its output bound, or times out.
    fn run(
        &self,
        program: &Path,
        arguments: &[&str],
        stdin: Option<&str>,
    ) -> Result<String, MigrationError>;
}

/// Executes migration commands on the real system.
#[derive(Clone, Copy, Debug, Default)]
pub struct RealMigrationHost;

impl MigrationHost for RealMigrationHost {
    fn run(
        &self,
        program: &Path,
        arguments: &[&str],
        stdin: Option<&str>,
    ) -> Result<String, MigrationError> {
        let label = program.display().to_string();
        let mut child = Command::new(program)
            .args(arguments)
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| MigrationError(format!("cannot start {label}: {error}")))?;
        if let (Some(input), Some(mut pipe)) = (stdin, child.stdin.take()) {
            pipe.write_all(input.as_bytes())
                .map_err(|error| MigrationError(format!("cannot write to {label}: {error}")))?;
        }
        let mut stdout = child
            .stdout
            .take()
            .ok_or_else(|| MigrationError(format!("{label} has no output pipe")))?;
        let reader = thread::spawn(move || {
            let mut bytes = Vec::new();
            let mut limited = (&mut stdout).take(MAX_COMMAND_OUTPUT_BYTES as u64 + 1);
            limited.read_to_end(&mut bytes).map(|_| bytes)
        });
        let deadline = Instant::now() + COMMAND_TIMEOUT;
        let status = loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|error| MigrationError(format!("cannot wait for {label}: {error}")))?
            {
                break status;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(MigrationError(format!("{label} timed out")));
            }
            thread::sleep(Duration::from_millis(50));
        };
        let bytes = reader
            .join()
            .map_err(|_| MigrationError(format!("{label} output reader failed")))?
            .map_err(|error| MigrationError(format!("cannot read {label} output: {error}")))?;
        if bytes.len() > MAX_COMMAND_OUTPUT_BYTES {
            return Err(MigrationError(format!("{label} output exceeded its bound")));
        }
        if !status.success() {
            return Err(MigrationError(format!("{label} failed with {status}")));
        }
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }
}

fn module_key(name: &str) -> String {
    name.replace('-', "_")
}

fn is_retired(name: &str) -> bool {
    let key = module_key(name);
    RETIRED_KERNEL_DRIVERS
        .iter()
        .any(|(retired, _)| module_key(retired) == key)
}

fn is_released(name: &str) -> bool {
    module_key(name) == module_key(KERNEL_MODULE_NAME)
}

/// Modalias fragments (`v0000VVVVp0000PPPP`) of every qualified receiver.
fn receiver_modalias_fragments() -> Vec<String> {
    PROFILES
        .iter()
        .filter(|profile| profile.profile_kind == ProfileKind::Receiver)
        .filter_map(|profile| Some((profile.vendor_id?, profile.product_id?)))
        .map(|(vendor, product)| format!("v0000{vendor:04X}p0000{product:04X}"))
        .collect()
}

fn alias_owners(kernel_modules: &Path) -> Vec<String> {
    let fragments = receiver_modalias_fragments();
    let mut owners = fs::read_dir(kernel_modules)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| fs::read_to_string(entry.path().join("modules.alias")).ok())
        .flat_map(|aliases| {
            aliases
                .lines()
                .filter(|line| fragments.iter().any(|fragment| line.contains(fragment)))
                .filter_map(|line| line.split_whitespace().last().map(module_key))
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    owners.sort();
    owners.dedup();
    owners
}

fn image_tool(roots: &MigrationRoots) -> Result<ImageTool, String> {
    let dracut = roots.dracut_hook_script.is_file();
    let mkinitcpio = fs::read_dir(&roots.mkinitcpio_presets)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .any(|entry| {
            entry
                .path()
                .extension()
                .is_some_and(|value| value == "preset")
        });
    match (dracut, mkinitcpio) {
        (true, false) => Ok(ImageTool::DracutHook),
        (false, true) => Ok(ImageTool::Mkinitcpio),
        (true, true) => Err(
            "both dracut and mkinitcpio are configured; rebuild boot images manually".to_owned(),
        ),
        (false, false) => Err(
            "no supported boot-image tool (dracut hook or mkinitcpio presets) was found".to_owned(),
        ),
    }
}

fn boot_images(boot: &Path) -> Vec<PathBuf> {
    let mut images = fs::read_dir(boot)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("initramfs-") && !name.contains("ucode"))
                && path
                    .extension()
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("img"))
        })
        .collect::<Vec<_>>();
    images.sort();
    images
}

/// Gathers the migration plan without changing anything.
#[must_use]
pub fn plan_migration(
    roots: &MigrationRoots,
    interface_drivers: Vec<Option<String>>,
) -> MigrationPlan {
    let registrations = RETIRED_KERNEL_DRIVERS
        .iter()
        .filter(|(name, version)| roots.dkms.join(name).join(version).is_dir())
        .map(|(name, version)| ((*name).to_owned(), (*version).to_owned()))
        .collect::<Vec<_>>();
    let source_trees = RETIRED_KERNEL_DRIVERS
        .iter()
        .map(|(name, version)| roots.usr_src.join(format!("{name}-{version}")))
        .filter(|path| path.is_dir())
        .collect::<Vec<_>>();
    let alias_owners = alias_owners(&roots.kernel_modules);
    let tool = image_tool(roots);
    let unknown_alias = alias_owners
        .iter()
        .find(|owner| !is_released(owner) && !is_retired(owner));
    let unknown_bound = interface_drivers.iter().flatten().find(|driver| {
        !is_released(driver) && !is_retired(driver) && driver.as_str() != GENERIC_HID_DRIVER
    });
    let refusal = if let Some(owner) = unknown_alias {
        Some(format!(
            "the unknown module {owner} also claims the receiver; it is left untouched"
        ))
    } else if let Some(driver) = unknown_bound {
        Some(format!(
            "the unknown driver {driver} owns a receiver interface; it is left untouched"
        ))
    } else {
        tool.as_ref().err().cloned()
    };
    MigrationPlan {
        registrations,
        source_trees,
        interface_drivers,
        alias_owners,
        image_tool: tool.ok(),
        images: boot_images(&roots.boot),
        refusal,
    }
}

/// Renders the plan for the default preview.
#[must_use]
pub fn render_migration_preview(plan: &MigrationPlan) -> String {
    let mut text = String::from("HyperFlux Next retired-driver migration preview\n");
    let owners = plan
        .interface_drivers
        .iter()
        .map(|driver| driver.as_deref().unwrap_or("unbound"))
        .collect::<Vec<_>>();
    let interfaces = if owners.is_empty() {
        "none attached".to_owned()
    } else {
        owners.join(", ")
    };
    let claimants = if plan.alias_owners.is_empty() {
        "none".to_owned()
    } else {
        plan.alias_owners.join(", ")
    };
    let _ = writeln!(text, "Receiver interfaces: {interfaces}");
    let _ = writeln!(text, "Modules claiming the receiver: {claimants}");
    if !plan.is_needed() {
        text.push_str("No retired HyperFlux driver is registered. Nothing to do.\n");
        return text;
    }
    for (name, version) in &plan.registrations {
        let _ = writeln!(text, "Would deregister DKMS {name}/{version}");
    }
    for tree in &plan.source_trees {
        let _ = writeln!(
            text,
            "Would move {} into the migration record",
            tree.display()
        );
    }
    match plan.image_tool {
        Some(tool) => {
            for image in &plan.images {
                let _ = writeln!(
                    text,
                    "Would rebuild and inspect {} with {}",
                    image.display(),
                    tool.inspector()
                );
            }
        }
        None => text.push_str("No boot images would be rebuilt.\n"),
    }
    match &plan.refusal {
        Some(reason) => {
            let _ = writeln!(text, "Cannot apply: {reason}.");
        }
        None => text.push_str(
            "Run again with --apply as root to perform these steps, then reboot normally.\n",
        ),
    }
    text
}

fn record_file(directory: &Path, name: &str, contents: &str) -> Result<(), MigrationError> {
    fs::write(directory.join(name), contents)
        .map_err(|error| MigrationError(format!("cannot write migration record {name}: {error}")))
}

fn image_summary(images: &[PathBuf]) -> String {
    let mut summary = String::new();
    for image in images {
        let metadata = fs::metadata(image).ok();
        let size = metadata.as_ref().map_or(0, std::fs::Metadata::len);
        let modified = metadata
            .and_then(|value| value.modified().ok())
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |time| time.as_secs());
        let _ = writeln!(summary, "{} {size} {modified}", image.display());
    }
    summary
}

fn retired_module_markers() -> Vec<String> {
    RETIRED_KERNEL_DRIVERS
        .iter()
        .flat_map(|(name, _)| [format!("/{name}.ko"), format!("/{}.ko", module_key(name))])
        .collect()
}

/// Applies a plan. Callers must hold root and must have shown the preview.
///
/// # Errors
///
/// Returns the first failure and stops: a refused plan, a record that cannot
/// be written, a failed DKMS removal, a source tree that cannot be moved, a
/// failed image rebuild, or an image that cannot be positively inspected or
/// still contains a retired driver.
pub fn apply_migration(
    plan: &MigrationPlan,
    roots: &MigrationRoots,
    host: &impl MigrationHost,
    record_name: &str,
) -> Result<MigrationReceipt, MigrationError> {
    if let Some(reason) = &plan.refusal {
        return Err(MigrationError(reason.clone()));
    }
    let directory = roots.records.join(record_name);
    if !plan.is_needed() {
        return Ok(MigrationReceipt {
            record_directory: directory,
            removed_registrations: Vec::new(),
            moved_source_trees: Vec::new(),
            inspected_images: Vec::new(),
        });
    }
    let tool = plan
        .image_tool
        .ok_or_else(|| MigrationError("no boot-image tool is available".to_owned()))?;

    // 1. Rollback inputs first, so nothing is changed without a record.
    let sources = directory.join("src");
    fs::create_dir_all(&sources)
        .and_then(|()| fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)))
        .map_err(|error| MigrationError(format!("cannot create migration record: {error}")))?;
    let status = host.run(Path::new("dkms"), &["status"], None)?;
    record_file(&directory, "dkms-status-before.txt", &status)?;
    record_file(
        &directory,
        "receiver-alias-owners.txt",
        &(plan.alias_owners.join("\n") + "\n"),
    )?;
    record_file(
        &directory,
        "boot-images-before.txt",
        &image_summary(&plan.images),
    )?;

    // 2. Deregister exactly the known retired identities.
    let mut removed = Vec::new();
    for (name, version) in &plan.registrations {
        let identity = format!("{name}/{version}");
        host.run(Path::new("dkms"), &["remove", &identity, "--all"], None)?;
        removed.push(identity);
    }

    // 3. Move (never delete) the retired source trees into the record.
    let mut moved = Vec::new();
    for tree in &plan.source_trees {
        let name = tree
            .file_name()
            .ok_or_else(|| MigrationError("retired source tree has no name".to_owned()))?;
        fs::rename(tree, sources.join(name))
            .map_err(|error| MigrationError(format!("cannot move {}: {error}", tree.display())))?;
        moved.push(tree.clone());
    }

    // 4. Rebuild every boot image with the distribution's own tooling.
    match tool {
        ImageTool::DracutHook => {
            // The hook rebuilds all kernels when given a non-vmlinuz target,
            // exactly as it does after a DKMS change.
            let target = format!("usr/src/{KERNEL_MODULE_NAME}/dkms.conf\n");
            host.run(&roots.dracut_hook_script, &[], Some(&target))?;
        }
        ImageTool::Mkinitcpio => {
            host.run(Path::new("mkinitcpio"), &["-P"], None)?;
        }
    }

    // 5. Positively inspect every image. Empty or failed output is never proof.
    let markers = retired_module_markers();
    let mut inspected = Vec::new();
    for image in &plan.images {
        let path = image.to_str().ok_or_else(|| {
            MigrationError(format!("boot image path is not UTF-8: {}", image.display()))
        })?;
        let listing = host.run(Path::new(tool.inspector()), &[path], None)?;
        if !listing.contains("lib/modules/") {
            return Err(MigrationError(format!(
                "{} could not be positively inspected; reboot is not safe yet",
                image.display()
            )));
        }
        if markers
            .iter()
            .any(|marker| listing.contains(marker.as_str()))
        {
            return Err(MigrationError(format!(
                "{} still contains a retired HyperFlux driver",
                image.display()
            )));
        }
        inspected.push(image.clone());
    }
    record_file(
        &directory,
        "boot-images-after.txt",
        &image_summary(&inspected),
    )?;
    record_file(
        &directory,
        "removed-registrations.txt",
        &(removed.join("\n") + "\n"),
    )?;
    Ok(MigrationReceipt {
        record_directory: directory,
        removed_registrations: removed,
        moved_source_trees: moved,
        inspected_images: inspected,
    })
}

#[cfg(test)]
mod tests;
