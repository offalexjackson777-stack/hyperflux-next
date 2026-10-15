// SPDX-License-Identifier: GPL-2.0-only

use crate::catalog::{NativeRouteCatalog, NativeRouteRecord};
use crate::serial::{SerialFingerprint, fingerprint_serial_bytes};
#[cfg(test)]
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

const USB_HID_BUS: u16 = 0x0003;
const MAX_SERIAL_FILE_BYTES: usize = 256;

#[cfg(test)]
thread_local! {
    static SERIAL_READ_COUNT: Cell<usize> = const { Cell::new(0) };
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct DirectMatch {
    profile_id: String,
    fingerprint: SerialFingerprint,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct DirectDeviceKey {
    profile_id: String,
    path: PathBuf,
    device: u64,
    inode: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct DirectRouteSnapshot {
    profiles: BTreeSet<String>,
    matches: BTreeSet<DirectMatch>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DirectRouteTransition {
    Keep,
    Expose,
    Suppress,
}

impl DirectRouteSnapshot {
    #[must_use]
    pub fn has_profile_candidate(&self, profile_id: &str) -> bool {
        self.profiles.contains(profile_id)
    }

    #[must_use]
    pub fn suppresses(&self, profile_id: &str, fingerprint: &SerialFingerprint) -> bool {
        self.matches.contains(&DirectMatch {
            profile_id: profile_id.to_owned(),
            fingerprint: fingerprint.clone(),
        })
    }

    #[must_use]
    pub fn transition(
        &self,
        profile_id: &str,
        fingerprint: Option<&SerialFingerprint>,
        exposed: bool,
    ) -> DirectRouteTransition {
        let suppressed =
            fingerprint.is_some_and(|fingerprint| self.suppresses(profile_id, fingerprint));
        match (exposed, suppressed) {
            (true, true) => DirectRouteTransition::Suppress,
            (false, false) => DirectRouteTransition::Expose,
            (true, false) | (false, true) => DirectRouteTransition::Keep,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DirectRouteScanner {
    driver_root: PathBuf,
    fingerprints: BTreeMap<DirectDeviceKey, SerialFingerprint>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DirectRouteScanError {
    DriverDirectory,
}

impl fmt::Display for DirectRouteScanError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("direct USB route state could not be read")
    }
}

impl std::error::Error for DirectRouteScanError {}

impl DirectRouteScanner {
    #[must_use]
    pub fn new(driver_root: PathBuf) -> Self {
        Self {
            driver_root,
            fingerprints: BTreeMap::new(),
        }
    }

    /// Scans only exact direct-USB identities from the source-bound catalog.
    /// A data route is not considered a match until `OpenRazer`'s physically
    /// queried `device_serial` attribute is non-empty.
    ///
    /// # Errors
    ///
    /// Returns a bounded scanner error for an unreadable existing driver
    /// directory. A driver that is not loaded is an empty, valid snapshot.
    pub fn scan(
        &mut self,
        catalog: &NativeRouteCatalog,
    ) -> Result<DirectRouteSnapshot, DirectRouteScanError> {
        let mut snapshot = DirectRouteSnapshot::default();
        let mut next_fingerprints = BTreeMap::new();
        for record in catalog.routes() {
            self.scan_record(record, &mut snapshot, &mut next_fingerprints)?;
        }
        self.fingerprints = next_fingerprints;
        Ok(snapshot)
    }

    /// Refreshes the exact direct-device precedence state for one source-bound
    /// profile without treating another profile's unreadable driver directory
    /// as evidence that this route is absent.
    ///
    /// # Errors
    ///
    /// Returns a bounded scanner error when this profile's existing driver
    /// directory cannot be read.
    pub fn scan_profile(
        &mut self,
        catalog: &NativeRouteCatalog,
        profile_id: &str,
    ) -> Result<DirectRouteSnapshot, DirectRouteScanError> {
        let record = catalog
            .profile(profile_id)
            .ok_or(DirectRouteScanError::DriverDirectory)?;
        let mut snapshot = DirectRouteSnapshot::default();
        let mut next_fingerprints = BTreeMap::new();
        self.scan_record(record, &mut snapshot, &mut next_fingerprints)?;
        self.fingerprints
            .retain(|key, _| key.profile_id != profile_id);
        self.fingerprints.extend(next_fingerprints);
        Ok(snapshot)
    }

    fn scan_record(
        &self,
        record: &NativeRouteRecord,
        snapshot: &mut DirectRouteSnapshot,
        next_fingerprints: &mut BTreeMap<DirectDeviceKey, SerialFingerprint>,
    ) -> Result<(), DirectRouteScanError> {
        let directory = self.driver_root.join(&record.driver_family);
        let entries = match fs::read_dir(directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err(DirectRouteScanError::DriverDirectory),
        };
        for entry in entries {
            let entry = entry.map_err(|_| DirectRouteScanError::DriverDirectory)?;
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            let Some((bus, vendor, product)) = parse_hid_identity(&name) else {
                continue;
            };
            if bus != USB_HID_BUS
                || vendor != record.vendor_id.get()
                || !record
                    .direct_data_product_ids
                    .iter()
                    .any(|candidate| candidate.get() == product)
            {
                continue;
            }
            snapshot.profiles.insert(record.profile_id.clone());
            let serial_path = entry.path().join("device_serial");
            let Some(key) = direct_device_key(record, &entry.path(), &serial_path) else {
                continue;
            };
            let fingerprint = self.fingerprints.get(&key).cloned().or_else(|| {
                read_serial(&serial_path).and_then(|serial| fingerprint_serial_bytes(&serial))
            });
            let Some(fingerprint) = fingerprint else {
                continue;
            };
            next_fingerprints.insert(key, fingerprint.clone());
            snapshot.matches.insert(DirectMatch {
                profile_id: record.profile_id.clone(),
                fingerprint,
            });
        }
        Ok(())
    }
}

fn direct_device_key(
    record: &NativeRouteRecord,
    entry_path: &Path,
    serial_path: &Path,
) -> Option<DirectDeviceKey> {
    let metadata = fs::metadata(serial_path).ok()?;
    Some(DirectDeviceKey {
        profile_id: record.profile_id.clone(),
        path: fs::canonicalize(entry_path).unwrap_or_else(|_| entry_path.to_owned()),
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

pub(crate) fn parse_hid_identity(name: &str) -> Option<(u16, u16, u16)> {
    let prefix = name.split_once('.')?.0;
    let mut fields = prefix.split(':');
    let bus = u16::from_str_radix(fields.next()?, 16).ok()?;
    let vendor = u16::from_str_radix(fields.next()?, 16).ok()?;
    let product = u16::from_str_radix(fields.next()?, 16).ok()?;
    fields.next().is_none().then_some((bus, vendor, product))
}

fn read_serial(path: &Path) -> Option<Vec<u8>> {
    #[cfg(test)]
    SERIAL_READ_COUNT.with(|count| count.set(count.get() + 1));
    let file = File::open(path).ok()?;
    let mut value = Vec::new();
    let limit = u64::try_from(MAX_SERIAL_FILE_BYTES).ok()?.checked_add(1)?;
    file.take(limit).read_to_end(&mut value).ok()?;
    (value.len() <= MAX_SERIAL_FILE_BYTES).then_some(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ID: AtomicU64 = AtomicU64::new(1);

    struct TempTree(PathBuf);

    impl TempTree {
        fn create() -> Self {
            let id = TEST_ID.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "hyperflux-direct-route-{}-{id}",
                std::process::id()
            ));
            fs::create_dir_all(&path).expect("temporary tree is created");
            Self(path)
        }

        fn device(&self, driver: &str, identity: &str, serial: Option<&[u8]>) {
            let path = self.0.join(driver).join(identity);
            fs::create_dir_all(&path).expect("device directory is created");
            if let Some(serial) = serial {
                fs::write(path.join("device_serial"), serial).expect("serial fixture is written");
            }
        }

        fn linked_device(&self, driver: &str, identity: &str, serial: &[u8]) {
            let target = self.0.join("physical").join(identity);
            fs::create_dir_all(&target).expect("physical fixture is created");
            fs::write(target.join("device_serial"), serial).expect("serial fixture is written");
            let link = self.0.join(driver).join(identity);
            fs::create_dir_all(link.parent().expect("driver directory exists"))
                .expect("driver directory is created");
            symlink(target, link).expect("driver symlink is created");
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn reset_serial_read_count() {
        SERIAL_READ_COUNT.with(|count| count.set(0));
    }

    fn serial_read_count() -> usize {
        SERIAL_READ_COUNT.with(Cell::get)
    }

    #[test]
    fn exact_usb_pid_and_nonempty_physical_serial_are_both_required() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.linked_device("razermouse", "0003:1532:00CC.0001", b"MOUSE-A\n");
        tree.device("razermouse", "0003:1532:00CD.0002", Some(b"MOUSE-B\n"));
        tree.device("razerkbd", "0006:1532:0298.0003", Some(b"KEYBOARD-A\n"));
        tree.device("razerkbd", "0003:1532:0298.0004", Some(b"\n"));

        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let snapshot = scanner.scan(&catalog).expect("fixture scans");
        let mouse = fingerprint_serial_bytes(b"MOUSE-A").expect("mouse fingerprint exists");
        let keyboard =
            fingerprint_serial_bytes(b"KEYBOARD-A").expect("keyboard fingerprint exists");
        assert_eq!(scanner.fingerprints.len(), 1);
        assert!(snapshot.has_profile_candidate("child.razer.basilisk-v3-pro-35k.00cd"));
        assert!(snapshot.suppresses("child.razer.basilisk-v3-pro-35k.00cd", &mouse));
        assert!(!snapshot.suppresses("child.razer.deathstalker-v2-pro-tkl.0296", &mouse));
        assert!(!snapshot.suppresses("child.razer.deathstalker-v2-pro-tkl.0296", &keyboard));
    }

    #[test]
    fn power_only_cable_without_a_data_route_does_not_suppress() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        let snapshot = DirectRouteScanner::new(tree.0.clone())
            .scan(&catalog)
            .expect("empty fixture scans");
        assert!(snapshot.profiles.is_empty());
        assert!(snapshot.matches.is_empty());
    }

    #[test]
    fn profile_scan_isolates_an_unreadable_sibling_driver_directory() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        fs::write(tree.0.join("razerkbd"), b"not-a-directory")
            .expect("unreadable sibling fixture is created");
        let mut scanner = DirectRouteScanner::new(tree.0.clone());

        assert!(scanner.scan(&catalog).is_err());
        let mouse = scanner
            .scan_profile(&catalog, "child.razer.basilisk-v3-pro-35k.00cd")
            .expect("mouse direct state is independent of keyboard driver state");
        assert!(mouse.matches.is_empty());
    }

    #[test]
    fn missing_and_empty_serials_do_not_suppress() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.device("razermouse", "0003:1532:00CC.0001", None);
        tree.device("razerkbd", "0003:1532:0298.0002", Some(b"\n"));

        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let snapshot = scanner.scan(&catalog).expect("fixture scans");
        assert!(snapshot.has_profile_candidate("child.razer.basilisk-v3-pro-35k.00cd"));
        assert!(snapshot.has_profile_candidate("child.razer.deathstalker-v2-pro-tkl.0296"));
        assert!(snapshot.matches.is_empty());
    }

    #[test]
    fn bluetooth_and_virtual_entries_are_ignored_before_serial_access() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.device("razermouse", "0005:1532:00AC.0001", Some(b"BT-BASILISK\n"));
        tree.device("razerkbd", "0005:1532:025B.0001", Some(b"BT-BLACKWIDOW\n"));
        tree.device("razermouse", "0005:1532:00CC.0001", Some(b"BT-SAME-PID\n"));
        tree.device(
            "razermouse",
            "0006:1532:00CC.0002",
            Some(b"VIRTUAL-SAME-PID\n"),
        );
        reset_serial_read_count();

        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let snapshot = scanner.scan(&catalog).expect("fixture scans");
        let bluetooth = fingerprint_serial_bytes(b"BT-SAME-PID").expect("fingerprint exists");
        let profile = "child.razer.basilisk-v3-pro-35k.00cd";

        assert_eq!(serial_read_count(), 0);
        assert!(scanner.fingerprints.is_empty());
        assert!(snapshot.profiles.is_empty());
        assert!(snapshot.matches.is_empty());
        assert!(!snapshot.suppresses(profile, &bluetooth));
        assert_eq!(
            snapshot.transition(profile, Some(&bluetooth), true),
            DirectRouteTransition::Keep
        );
        assert_eq!(
            snapshot.transition(profile, Some(&bluetooth), false),
            DirectRouteTransition::Expose
        );
    }

    #[test]
    fn mixed_usb_and_bluetooth_scan_uses_only_the_usb_identity() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.device("razermouse", "0005:1532:00CC.0001", Some(b"BT-MOUSE\n"));
        tree.device("razermouse", "0003:1532:00CC.0002", Some(b"USB-MOUSE\n"));
        reset_serial_read_count();

        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let snapshot = scanner.scan(&catalog).expect("fixture scans");
        let profile = "child.razer.basilisk-v3-pro-35k.00cd";
        let usb = fingerprint_serial_bytes(b"USB-MOUSE").expect("fingerprint exists");
        let bluetooth = fingerprint_serial_bytes(b"BT-MOUSE").expect("fingerprint exists");

        assert_eq!(serial_read_count(), 1);
        assert_eq!(scanner.fingerprints.len(), 1);
        assert!(snapshot.has_profile_candidate(profile));
        assert!(snapshot.suppresses(profile, &usb));
        assert!(!snapshot.suppresses(profile, &bluetooth));
    }

    #[test]
    fn unrelated_physical_usb_device_is_ignored_before_serial_access() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.device("razermouse", "0003:1532:FFFF.0001", Some(b"UNRELATED\n"));
        reset_serial_read_count();

        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let snapshot = scanner.scan(&catalog).expect("fixture scans");
        assert_eq!(serial_read_count(), 0);
        assert!(scanner.fingerprints.is_empty());
        assert!(snapshot.profiles.is_empty());
        assert!(snapshot.matches.is_empty());
    }

    #[test]
    fn same_model_is_not_enough_without_serial_equality() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        tree.device("razerkbd", "0003:1532:0298.0001", Some(b"KEYBOARD-A\n"));
        let snapshot = DirectRouteScanner::new(tree.0.clone())
            .scan(&catalog)
            .expect("fixture scans");
        let other = fingerprint_serial_bytes(b"KEYBOARD-B").expect("fingerprint exists");
        assert!(snapshot.has_profile_candidate("child.razer.deathstalker-v2-pro-tkl.0296"));
        assert!(!snapshot.suppresses("child.razer.deathstalker-v2-pro-tkl.0296", &other));
    }

    #[test]
    fn exact_direct_appearance_and_removal_have_inverse_transitions() {
        let profile = "child.razer.basilisk-v3-pro-35k.00cd";
        let fingerprint = fingerprint_serial_bytes(b"MOUSE-A").expect("fingerprint exists");
        let mut snapshot = DirectRouteSnapshot::default();
        snapshot.matches.insert(DirectMatch {
            profile_id: profile.to_owned(),
            fingerprint: fingerprint.clone(),
        });
        assert_eq!(
            snapshot.transition(profile, Some(&fingerprint), true),
            DirectRouteTransition::Suppress
        );
        assert_eq!(
            DirectRouteSnapshot::default().transition(profile, Some(&fingerprint), false),
            DirectRouteTransition::Expose
        );
        assert_eq!(
            snapshot.transition(profile, None, true),
            DirectRouteTransition::Keep
        );
    }

    #[test]
    fn physical_serial_is_queried_once_per_concrete_sysfs_object() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let tree = TempTree::create();
        let profile = "child.razer.basilisk-v3-pro-35k.00cd";
        let identity = "0003:1532:00CC.0001";
        tree.device("razermouse", identity, Some(b"MOUSE-A\n"));
        let serial_path = tree
            .0
            .join("razermouse")
            .join(identity)
            .join("device_serial");
        let mut scanner = DirectRouteScanner::new(tree.0.clone());
        let first = scanner.scan(&catalog).expect("first scan succeeds");
        let serial_a = fingerprint_serial_bytes(b"MOUSE-A").expect("fingerprint exists");
        let serial_b = fingerprint_serial_bytes(b"MOUSE-B").expect("fingerprint exists");
        assert!(first.suppresses(profile, &serial_a));

        fs::write(&serial_path, b"MOUSE-B\n").expect("fixture content changes");
        let cached = scanner.scan(&catalog).expect("cached scan succeeds");
        assert!(cached.suppresses(profile, &serial_a));
        assert!(!cached.suppresses(profile, &serial_b));

        fs::remove_dir_all(tree.0.join("razermouse").join(identity))
            .expect("old sysfs object is removed");
        tree.device("razermouse", "0003:1532:00CC.0002", Some(b"MOUSE-B\n"));
        let replaced = scanner.scan(&catalog).expect("replacement scan succeeds");
        assert!(!replaced.suppresses(profile, &serial_a));
        assert!(replaced.suppresses(profile, &serial_b));
    }
}
