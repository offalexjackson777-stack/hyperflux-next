// SPDX-License-Identifier: GPL-2.0-only

use rustix::fs::OFlags;
use serde::Deserialize;
use std::fs::{File, OpenOptions};
use std::io::Read as _;
use std::os::unix::fs::{MetadataExt as _, OpenOptionsExt as _};
use std::path::Path;

const DESCRIPTOR_SCHEMA_REF: &str =
    "https://hyperflux.dev/schemas/distribution-component-descriptor-v1.json";
const DESCRIPTOR_SCHEMA: &str = "hyperflux-distribution-component-v1";
const DESCRIPTOR_COMPONENT_ID: &str = "full";
const DESCRIPTOR_CAPABILITIES: [&str; 2] = ["openrazer-native-route", "openrgb-plugin"];
const MAX_DESCRIPTOR_BYTES: u64 = 4_096;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FullComponentState {
    NotInstalled,
    Ready,
    DescriptorInvalid,
    VersionMismatch,
    CapabilitiesIncomplete,
}

impl FullComponentState {
    #[must_use]
    pub const fn finding_value(self) -> &'static str {
        match self {
            Self::NotInstalled => "not-installed",
            Self::Ready => "ready",
            Self::DescriptorInvalid => "descriptor-invalid",
            Self::VersionMismatch => "version-mismatch",
            Self::CapabilitiesIncomplete => "capabilities-incomplete",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FullComponentSnapshot {
    pub state: FullComponentState,
    pub product_version: Option<String>,
}

impl FullComponentSnapshot {
    #[must_use]
    pub const fn not_installed() -> Self {
        Self {
            state: FullComponentState::NotInstalled,
            product_version: None,
        }
    }

    #[must_use]
    pub fn ready(product_version: &str) -> Self {
        Self {
            state: FullComponentState::Ready,
            product_version: Some(product_version.to_owned()),
        }
    }

    #[must_use]
    pub fn is_ready(&self) -> bool {
        self.state == FullComponentState::Ready
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Descriptor {
    #[serde(rename = "$schema")]
    schema_ref: String,
    schema: String,
    component_id: String,
    product_version: String,
    capabilities: Vec<String>,
}

fn invalid(state: FullComponentState) -> FullComponentSnapshot {
    FullComponentSnapshot {
        state,
        product_version: None,
    }
}

fn open_descriptor(path: &Path) -> Result<Option<File>, FullComponentState> {
    match OpenOptions::new()
        .read(true)
        .custom_flags(OFlags::NOFOLLOW.bits().cast_signed())
        .open(path)
    {
        Ok(file) => Ok(Some(file)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(FullComponentState::DescriptorInvalid),
    }
}

fn probe_with_ownership(
    path: &Path,
    expected_version: &str,
    required_user: u32,
    required_group: u32,
) -> FullComponentSnapshot {
    let file = match open_descriptor(path) {
        Ok(Some(file)) => file,
        Ok(None) => return FullComponentSnapshot::not_installed(),
        Err(state) => return invalid(state),
    };
    let Ok(metadata) = file.metadata() else {
        return invalid(FullComponentState::DescriptorInvalid);
    };
    if !metadata.is_file()
        || metadata.uid() != required_user
        || metadata.gid() != required_group
        || metadata.mode() & 0o7777 != 0o644
        || metadata.nlink() != 1
        || metadata.len() == 0
        || metadata.len() > MAX_DESCRIPTOR_BYTES
    {
        return invalid(FullComponentState::DescriptorInvalid);
    }
    let Ok(capacity) = usize::try_from(metadata.len()) else {
        return invalid(FullComponentState::DescriptorInvalid);
    };
    let mut bytes = Vec::with_capacity(capacity);
    if file
        .take(MAX_DESCRIPTOR_BYTES + 1)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() as u64 != metadata.len()
    {
        return invalid(FullComponentState::DescriptorInvalid);
    }
    let Ok(descriptor) = serde_json::from_slice::<Descriptor>(&bytes) else {
        return invalid(FullComponentState::DescriptorInvalid);
    };
    if descriptor.schema_ref != DESCRIPTOR_SCHEMA_REF
        || descriptor.schema != DESCRIPTOR_SCHEMA
        || descriptor.component_id != DESCRIPTOR_COMPONENT_ID
    {
        return invalid(FullComponentState::DescriptorInvalid);
    }
    if descriptor.product_version != expected_version {
        return invalid(FullComponentState::VersionMismatch);
    }
    if descriptor.capabilities != DESCRIPTOR_CAPABILITIES {
        return invalid(FullComponentState::CapabilitiesIncomplete);
    }
    FullComponentSnapshot::ready(expected_version)
}

#[must_use]
pub(crate) fn probe_full_component(path: &Path, expected_version: &str) -> FullComponentSnapshot {
    probe_with_ownership(path, expected_version, 0, 0)
}

#[cfg(test)]
mod tests;
