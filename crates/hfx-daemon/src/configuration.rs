// SPDX-License-Identifier: GPL-2.0-only

use hfx_runtime::{BridgeConfig, BridgeConfigError};
use rustix::fs::{Mode, OFlags, open};
use std::fmt;
use std::fs::File;
use std::io::Read as _;
use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
use std::path::Path;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProductionConfigError {
    Open,
    InvalidFile,
    TooLarge,
    Read,
    Decode,
    Policy(BridgeConfigError),
}

impl fmt::Display for ProductionConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Open => "bridge configuration could not be opened",
            Self::InvalidFile => "bridge configuration file authority is invalid",
            Self::TooLarge => "bridge configuration exceeds its declared size bound",
            Self::Read => "bridge configuration could not be read",
            Self::Decode => "bridge configuration is not valid canonical JSON",
            Self::Policy(_) => "bridge configuration violates runtime policy",
        })
    }
}

impl std::error::Error for ProductionConfigError {}

/// Opens and validates a production bridge configuration without following a
/// final symlink or trusting path metadata after the file has been opened.
///
/// # Errors
///
/// Rejects non-regular, linked, writable-by-others, oversized, malformed, or
/// policy-incompatible configuration files.
pub fn load_production_config(
    path: &Path,
    maximum_bytes: u64,
) -> Result<BridgeConfig, ProductionConfigError> {
    if maximum_bytes == 0 {
        return Err(ProductionConfigError::TooLarge);
    }
    let descriptor = open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )
    .map_err(|_| ProductionConfigError::Open)?;
    let mut file = File::from(descriptor);
    let metadata = file.metadata().map_err(|_| ProductionConfigError::Read)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.file_type().is_file()
        || metadata.nlink() != 1
        || (metadata.uid() != 0 && metadata.uid() != effective_uid)
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(ProductionConfigError::InvalidFile);
    }
    if metadata.len() > maximum_bytes {
        return Err(ProductionConfigError::TooLarge);
    }
    let mut payload = Vec::new();
    file.by_ref()
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut payload)
        .map_err(|_| ProductionConfigError::Read)?;
    if u64::try_from(payload.len()).unwrap_or(u64::MAX) > maximum_bytes {
        return Err(ProductionConfigError::TooLarge);
    }
    let config: BridgeConfig =
        serde_json::from_slice(&payload).map_err(|_| ProductionConfigError::Decode)?;
    config.validate().map_err(ProductionConfigError::Policy)?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    const DEFAULT_CONFIG: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../packaging/generated/bridge.json"
    ));

    fn temporary_directory() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "hfx-production-config-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("temporary directory creates");
        path
    }

    #[test]
    fn exact_private_configuration_loads() {
        let directory = temporary_directory();
        let path = directory.join("bridge.json");
        fs::write(&path, DEFAULT_CONFIG).expect("configuration writes");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640))
            .expect("configuration mode sets");
        let config = load_production_config(&path, 64 * 1024).expect("configuration loads");
        assert_eq!(config.schema, "hyperflux-bridge-config-v1");
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }

    #[test]
    fn symlink_permissive_linked_and_oversized_inputs_fail_closed() {
        let directory = temporary_directory();
        let target = directory.join("target.json");
        fs::write(&target, DEFAULT_CONFIG).expect("target writes");
        let linked = directory.join("linked.json");
        symlink(&target, &linked).expect("symlink creates");
        assert_eq!(
            load_production_config(&linked, 64 * 1024),
            Err(ProductionConfigError::Open)
        );

        fs::set_permissions(&target, fs::Permissions::from_mode(0o666))
            .expect("permissive mode sets");
        assert_eq!(
            load_production_config(&target, 64 * 1024),
            Err(ProductionConfigError::InvalidFile)
        );
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).expect("private mode sets");

        let hard_link = directory.join("hard-link.json");
        fs::hard_link(&target, &hard_link).expect("hard link creates");
        assert_eq!(
            load_production_config(&target, 64 * 1024),
            Err(ProductionConfigError::InvalidFile)
        );
        fs::remove_file(&hard_link).expect("hard link removes");
        assert_eq!(
            load_production_config(&target, 8),
            Err(ProductionConfigError::TooLarge)
        );
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }
}
