// SPDX-License-Identifier: GPL-2.0-only

//! Releases the `OpenRazer` driver from one virtual endpoint before
//! `UHID_DESTROY` (REL-020).
//!
//! `uhid` fails a Feature GET with EIO when the device stops running after
//! the reply arrived but before the waiting driver resumed. Unbinding the
//! driver first drains every in-flight sysfs show/store, so a request the
//! relay already answered always completes with that answer.

use crate::direct::parse_hid_identity;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub(super) const DRIVER_RELEASE_DEADLINE: Duration = Duration::from_secs(5);
const MAX_UEVENT_BYTES: u64 = 4096;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ReleaseOutcome {
    /// No device with this endpoint's phys is bound to the driver.
    NotBound,
    /// The unbind write completed and the driver link is gone.
    Released,
    /// The unbind write completed but the driver link is still present.
    StillBound,
    /// The unbind write failed, for example on a read-only sysfs.
    UnbindFailed,
    /// The unbind did not finish before the deadline.
    DeadlineExceeded,
}

impl ReleaseOutcome {
    pub(super) const fn label(self) -> &'static str {
        match self {
            Self::NotBound => "not-bound",
            Self::Released => "released",
            Self::StillBound => "still-bound",
            Self::UnbindFailed => "unbind-failed",
            Self::DeadlineExceeded => "deadline-exceeded",
        }
    }
}

/// The driver directory and exact `HID_PHYS` of one virtual endpoint.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct DriverBinding {
    driver_family: String,
    driver_directory: PathBuf,
    phys: String,
}

impl DriverBinding {
    /// Returns `None` when there is no absolute driver root to act on, as in
    /// the qualification runtime, or the family is not one directory name.
    pub(super) fn new(driver_root: &Path, driver_family: &str, phys: &str) -> Option<Self> {
        if !driver_root.is_absolute()
            || driver_family.is_empty()
            || driver_family.contains('/')
            || driver_family.starts_with('.')
        {
            return None;
        }
        Some(Self {
            driver_family: driver_family.to_owned(),
            driver_directory: driver_root.join(driver_family),
            phys: phys.to_owned(),
        })
    }

    pub(super) fn driver_family(&self) -> &str {
        &self.driver_family
    }

    /// Starts one unbind of the exact bound device on a worker thread. The
    /// write blocks in the kernel until in-flight requests drain, so the
    /// caller keeps serving the endpoint until [`DriverRelease::poll`]
    /// reports an outcome.
    pub(super) fn start_release(
        &self,
        deadline: Duration,
    ) -> Result<DriverRelease, ReleaseOutcome> {
        let device = self.bound_device().ok_or(ReleaseOutcome::NotBound)?;
        let unbind = self.driver_directory.join("unbind");
        let name = device.clone();
        let worker = thread::Builder::new()
            .name("hfx-relay-unbind".to_owned())
            .spawn(move || write_unbind(&unbind, &name))
            .map_err(|_| ReleaseOutcome::UnbindFailed)?;
        Ok(DriverRelease {
            device_link: self.driver_directory.join(device),
            worker: Some(worker),
            deadline: Instant::now() + deadline,
        })
    }

    fn bound_device(&self) -> Option<String> {
        let expected = format!("HID_PHYS={}", self.phys);
        fs::read_dir(&self.driver_directory)
            .ok()?
            .flatten()
            .filter_map(|entry| entry.file_name().into_string().ok())
            .filter(|name| parse_hid_identity(name).is_some())
            .find(|name| {
                read_uevent(&self.driver_directory.join(name).join("uevent"))
                    .is_some_and(|uevent| uevent.lines().any(|line| line == expected))
            })
    }
}

pub(super) struct DriverRelease {
    device_link: PathBuf,
    worker: Option<JoinHandle<io::Result<()>>>,
    deadline: Instant,
}

impl DriverRelease {
    /// Returns the outcome once the unbind finished or the deadline passed.
    /// A worker still blocked at the deadline is left to finish on its own;
    /// the forced `UHID_DESTROY` that follows ends its wait.
    pub(super) fn poll(&mut self, now: Instant) -> Option<ReleaseOutcome> {
        if self.worker.as_ref().is_some_and(JoinHandle::is_finished) {
            let result = self.worker.take()?.join();
            return Some(match result {
                Ok(Ok(())) if fs::symlink_metadata(&self.device_link).is_err() => {
                    ReleaseOutcome::Released
                }
                Ok(Ok(())) => ReleaseOutcome::StillBound,
                Ok(Err(_)) | Err(_) => ReleaseOutcome::UnbindFailed,
            });
        }
        (now >= self.deadline).then_some(ReleaseOutcome::DeadlineExceeded)
    }
}

fn write_unbind(path: &Path, device: &str) -> io::Result<()> {
    OpenOptions::new()
        .write(true)
        .open(path)?
        .write_all(device.as_bytes())
}

fn read_uevent(path: &Path) -> Option<String> {
    let mut value = String::new();
    File::open(path)
        .ok()?
        .take(MAX_UEVENT_BYTES)
        .read_to_string(&mut value)
        .ok()?;
    Some(value)
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ID: AtomicU64 = AtomicU64::new(1);
    const PHYS: &str = "hyperflux-next/native/v1/teardown-test";

    pub(in crate::runtime) struct DriverTree(pub(in crate::runtime) PathBuf);

    impl DriverTree {
        pub(in crate::runtime) fn create() -> Self {
            let id = TEST_ID.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "hyperflux-relay-teardown-{}-{id}",
                std::process::id()
            ));
            fs::create_dir_all(path.join("razermouse")).expect("driver directory is created");
            fs::write(path.join("razermouse").join("unbind"), b"").expect("unbind is created");
            Self(path)
        }

        pub(in crate::runtime) fn bind(&self, name: &str, phys: &str) -> PathBuf {
            let device = self.0.join("razermouse").join(name);
            fs::create_dir_all(&device).expect("device directory is created");
            fs::write(
                device.join("uevent"),
                format!("DRIVER=razermouse\nHID_ID=0003:00001532:000000CD\nHID_PHYS={phys}\n"),
            )
            .expect("uevent is written");
            device
        }

        pub(in crate::runtime) fn unbind_request(&self) -> String {
            fs::read_to_string(self.0.join("razermouse").join("unbind"))
                .expect("unbind request reads")
        }
    }

    impl Drop for DriverTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn finish(release: &mut DriverRelease) -> ReleaseOutcome {
        loop {
            if let Some(outcome) = release.poll(Instant::now()) {
                return outcome;
            }
            thread::sleep(Duration::from_millis(1));
        }
    }

    fn binding(tree: &DriverTree) -> DriverBinding {
        DriverBinding::new(&tree.0, "razermouse", PHYS).expect("binding is valid")
    }

    #[test]
    fn unbinds_only_the_device_with_this_endpoint_phys() {
        let tree = DriverTree::create();
        tree.bind("0003:1532:00CD.0001", "usb-0000:00:14.0-3/input2");
        let ours = tree.bind("0003:1532:00CD.0002", PHYS);
        fs::create_dir_all(tree.0.join("razermouse").join("module")).expect("module link");
        let mut release = binding(&tree)
            .start_release(DRIVER_RELEASE_DEADLINE)
            .expect("bound device is found");
        while release
            .worker
            .as_ref()
            .is_some_and(|worker| !worker.is_finished())
        {
            thread::sleep(Duration::from_millis(1));
        }
        fs::remove_dir_all(ours).expect("kernel drops the driver link");
        assert_eq!(finish(&mut release), ReleaseOutcome::Released);
        assert_eq!(tree.unbind_request(), "0003:1532:00CD.0002");
    }

    #[test]
    fn a_driver_link_that_survives_unbind_is_reported() {
        let tree = DriverTree::create();
        tree.bind("0003:1532:00CD.0003", PHYS);
        let mut release = binding(&tree)
            .start_release(DRIVER_RELEASE_DEADLINE)
            .expect("bound device is found");
        assert_eq!(finish(&mut release), ReleaseOutcome::StillBound);
    }

    #[test]
    fn missing_or_foreign_bindings_fall_back_to_an_immediate_destroy() {
        let tree = DriverTree::create();
        tree.bind("0003:1532:00CD.0004", "hyperflux-next/native/v1/other");
        assert_eq!(
            binding(&tree).start_release(DRIVER_RELEASE_DEADLINE).err(),
            Some(ReleaseOutcome::NotBound)
        );
        let absent = DriverBinding::new(&tree.0, "razerkbd", PHYS).expect("binding is valid");
        assert_eq!(
            absent.start_release(DRIVER_RELEASE_DEADLINE).err(),
            Some(ReleaseOutcome::NotBound)
        );
        assert_eq!(tree.unbind_request(), "");
        assert!(DriverBinding::new(Path::new(""), "razermouse", PHYS).is_none());
        assert!(DriverBinding::new(&tree.0, "../razermouse", PHYS).is_none());
        assert!(DriverBinding::new(&tree.0, "", PHYS).is_none());
    }

    #[test]
    fn an_unwritable_unbind_is_a_typed_failure() {
        let tree = DriverTree::create();
        tree.bind("0003:1532:00CD.0005", PHYS);
        let unbind = tree.0.join("razermouse").join("unbind");
        fs::remove_file(&unbind).expect("unbind is removed");
        fs::create_dir(&unbind).expect("unbind becomes unwritable");
        let mut release = binding(&tree)
            .start_release(DRIVER_RELEASE_DEADLINE)
            .expect("bound device is found");
        assert_eq!(finish(&mut release), ReleaseOutcome::UnbindFailed);
    }

    #[test]
    fn a_blocked_unbind_is_bounded_by_the_deadline() {
        let tree = DriverTree::create();
        tree.bind("0003:1532:00CD.0006", PHYS);
        let unbind = tree.0.join("razermouse").join("unbind");
        fs::remove_file(&unbind).expect("unbind is removed");
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &unbind,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
            0,
        )
        .expect("blocking unbind is created");
        let started = Instant::now();
        let mut release = binding(&tree)
            .start_release(Duration::from_millis(50))
            .expect("bound device is found");
        assert_eq!(finish(&mut release), ReleaseOutcome::DeadlineExceeded);
        assert!(started.elapsed() >= Duration::from_millis(50));
        // Opening the read side releases the detached worker.
        let mut reader = File::open(&unbind).expect("blocked writer is released");
        let mut request = String::new();
        reader.read_to_string(&mut request).expect("request reads");
        assert_eq!(request, "0003:1532:00CD.0006");
    }
}
