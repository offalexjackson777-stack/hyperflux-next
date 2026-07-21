// SPDX-License-Identifier: GPL-2.0-only

use hfx_runtime::KERNEL_DEVICE_PREFIX;
use std::fmt;
use std::fs;
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EndpointName {
    pub bus_number: u16,
    pub device_number: u16,
    pub generation: u64,
    file_name: String,
}

impl EndpointName {
    /// Parses only the kernel-owned canonical endpoint form.
    ///
    /// # Errors
    ///
    /// Rejects aliases, overflow, zero values, and noncanonical padding.
    pub fn parse(value: &str) -> Result<Self, EndpointDiscoveryError> {
        let suffix = value
            .strip_prefix(KERNEL_DEVICE_PREFIX)
            .and_then(|value| value.strip_prefix('-'))
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        let (bus, suffix) = suffix
            .split_once('-')
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        let (device, generation) = suffix
            .split_once("-g")
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        if bus.len() != 3
            || device.len() != 3
            || !bus.bytes().all(|byte| byte.is_ascii_digit())
            || !device.bytes().all(|byte| byte.is_ascii_digit())
            || generation.is_empty()
            || !generation.bytes().all(|byte| byte.is_ascii_digit())
            || (generation.len() > 1 && generation.starts_with('0'))
        {
            return Err(EndpointDiscoveryError::MalformedName);
        }
        let bus_number = bus
            .parse::<u16>()
            .ok()
            .filter(|value| *value != 0)
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        let device_number = device
            .parse::<u16>()
            .ok()
            .filter(|value| *value != 0)
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        let generation = generation
            .parse::<u64>()
            .ok()
            .filter(|value| *value != 0)
            .ok_or(EndpointDiscoveryError::MalformedName)?;
        Ok(Self {
            bus_number,
            device_number,
            generation,
            file_name: value.to_owned(),
        })
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.file_name
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EndpointCandidate {
    pub name: EndpointName,
    pub device_path: PathBuf,
    pub topology_path: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EndpointDiscoveryError {
    MalformedName,
    DirectoryUnavailable,
    EntryUnavailable,
    TopologyUnavailable,
}

impl fmt::Display for EndpointDiscoveryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::MalformedName => "kernel endpoint name is malformed",
            Self::DirectoryUnavailable => "kernel endpoint directory is unavailable",
            Self::EntryUnavailable => "kernel endpoint entry is unavailable",
            Self::TopologyUnavailable => "kernel endpoint topology is unavailable",
        })
    }
}

impl std::error::Error for EndpointDiscoveryError {}

#[derive(Clone, Debug)]
pub struct EndpointDiscovery {
    device_directory: PathBuf,
    sysfs_misc_directory: PathBuf,
}

impl EndpointDiscovery {
    #[must_use]
    pub fn linux() -> Self {
        Self {
            device_directory: PathBuf::from("/dev"),
            sysfs_misc_directory: PathBuf::from("/sys/class/misc"),
        }
    }

    #[must_use]
    pub fn new(device_directory: PathBuf, sysfs_misc_directory: PathBuf) -> Self {
        Self {
            device_directory,
            sysfs_misc_directory,
        }
    }

    /// Finds only real character devices with a resolvable kernel topology.
    ///
    /// # Errors
    ///
    /// Returns a bounded directory error. Individual transient entries are
    /// skipped because hotplug can remove them during a scan.
    pub fn scan(&self) -> Result<Vec<EndpointCandidate>, EndpointDiscoveryError> {
        let entries = fs::read_dir(&self.device_directory)
            .map_err(|_| EndpointDiscoveryError::DirectoryUnavailable)?;
        let mut candidates = Vec::new();
        for entry in entries {
            let Ok(entry) = entry else {
                continue;
            };
            let Some(file_name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            if !file_name.starts_with(KERNEL_DEVICE_PREFIX) {
                continue;
            }
            let Ok(name) = EndpointName::parse(&file_name) else {
                continue;
            };
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if !file_type.is_char_device() {
                continue;
            }
            let topology_link = self.sysfs_misc_directory.join(name.as_str()).join("device");
            let Ok(device_topology_path) = fs::canonicalize(topology_link) else {
                continue;
            };
            let Some(topology_path) = usb_topology_path(&device_topology_path) else {
                continue;
            };
            candidates.push(EndpointCandidate {
                name,
                device_path: entry.path(),
                topology_path,
            });
        }
        candidates.sort_by(|left, right| left.name.cmp(&right.name));
        Ok(candidates)
    }

    #[must_use]
    pub fn device_directory(&self) -> &Path {
        &self.device_directory
    }
}

fn usb_topology_path(device_path: &Path) -> Option<PathBuf> {
    device_path.ancestors().find_map(|ancestor| {
        (ancestor.join("idVendor").is_file() && ancestor.join("idProduct").is_file())
            .then(|| ancestor.to_path_buf())
    })
}

impl Default for EndpointDiscovery {
    fn default() -> Self {
        Self::linux()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn canonical_endpoint_name_parses_exact_fields() {
        let name = EndpointName::parse("hyperflux-next-003-004-g17").expect("name parses");
        assert_eq!(name.bus_number, 3);
        assert_eq!(name.device_number, 4);
        assert_eq!(name.generation, 17);
        assert_eq!(name.as_str(), "hyperflux-next-003-004-g17");
    }

    #[test]
    fn aliases_zero_padding_drift_and_overflow_are_rejected() {
        for value in [
            "hyperflux-next-3-004-g1",
            "hyperflux-next-003-004-g01",
            "hyperflux-next-000-004-g1",
            "hyperflux-next-003-000-g1",
            "hyperflux-next-003-004-g0",
            "hyperflux-next-003-004-g18446744073709551616",
            "other-003-004-g1",
            "hyperflux-next-003-004-g1-extra",
        ] {
            assert_eq!(
                EndpointName::parse(value),
                Err(EndpointDiscoveryError::MalformedName),
                "{value}"
            );
        }
    }

    #[test]
    fn hid_instance_paths_normalize_to_the_stable_usb_port_ancestor() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock is after epoch")
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("hfx-usb-topology-{}-{nonce}", std::process::id()));
        let usb_device = root.join("usb3/3-2");
        let hid_instance = usb_device.join("3-2:1.1/0003:1532:00CF.0099");
        fs::create_dir_all(&hid_instance).expect("fake topology creates");
        fs::write(usb_device.join("idVendor"), "1532\n").expect("vendor marker writes");
        fs::write(usb_device.join("idProduct"), "00cf\n").expect("product marker writes");
        assert_eq!(usb_topology_path(&hid_instance), Some(usb_device));
        fs::remove_dir_all(root).expect("fake topology removes");
    }
}
