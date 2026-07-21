// SPDX-License-Identifier: GPL-2.0-only

use hfx_runtime::{BridgeConfig, BridgeMode, RestorationConfig, SocketConfig};
use serde::Deserialize;
use serde_json::Value;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{self, Write as _};
use std::os::unix::fs::{OpenOptionsExt as _, PermissionsExt as _};
use std::path::{Path, PathBuf};

const MAX_CONFIGURATION_BYTES: u64 = 64 * 1024;
const LEGACY_SCHEMA: &str = "hyperflux-bridge-config-v0";
const DEFAULT_CONFIG: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../packaging/generated/bridge.json"
));

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigMigrationPlan {
    CreateDefault,
    Current,
    MigrateLegacyV0,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ConfigMigrationOutcome {
    Created,
    Current,
    Migrated { rollback_path: PathBuf },
}

#[derive(Debug)]
pub enum ConfigMigrationError {
    Io(io::Error),
    TooLarge,
    Invalid,
    UnsupportedSchema(String),
    RollbackAlreadyExists,
}

impl fmt::Display for ConfigMigrationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(_) => formatter.write_str("bridge configuration storage failed"),
            Self::TooLarge => formatter.write_str("bridge configuration exceeds its size bound"),
            Self::Invalid => formatter.write_str("bridge configuration is invalid"),
            Self::UnsupportedSchema(schema) => {
                write!(
                    formatter,
                    "bridge configuration schema {schema} is unsupported"
                )
            }
            Self::RollbackAlreadyExists => {
                formatter.write_str("an unconfirmed configuration rollback already exists")
            }
        }
    }
}

impl std::error::Error for ConfigMigrationError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for ConfigMigrationError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyConfigV0 {
    #[serde(rename = "$schema")]
    schema_path: String,
    schema: String,
    write_enabled: bool,
    restore_enabled: bool,
}

/// Computes and atomically applies a supported configuration migration.
///
/// # Errors
///
/// Returns without replacing the live configuration when validation, backup,
/// or commit fails.
pub fn migrate_configuration(path: &Path) -> Result<ConfigMigrationOutcome, ConfigMigrationError> {
    if !path.exists() {
        let config = parse_current(DEFAULT_CONFIG.as_bytes())?;
        commit(path, DEFAULT_CONFIG.as_bytes(), 0o640)?;
        config
            .validate()
            .map_err(|_| ConfigMigrationError::Invalid)?;
        return Ok(ConfigMigrationOutcome::Created);
    }
    let metadata = fs::metadata(path)?;
    if metadata.len() > MAX_CONFIGURATION_BYTES {
        return Err(ConfigMigrationError::TooLarge);
    }
    let bytes = fs::read(path)?;
    match migration_plan(&bytes)? {
        ConfigMigrationPlan::Current => Ok(ConfigMigrationOutcome::Current),
        ConfigMigrationPlan::CreateDefault => unreachable!("existing file cannot create default"),
        ConfigMigrationPlan::MigrateLegacyV0 => {
            let legacy: LegacyConfigV0 =
                serde_json::from_slice(&bytes).map_err(|_| ConfigMigrationError::Invalid)?;
            if legacy.schema != LEGACY_SCHEMA
                || legacy.schema_path != "/usr/share/hyperflux-next/schemas/bridge-config-v0.json"
            {
                return Err(ConfigMigrationError::Invalid);
            }
            let current: BridgeConfig =
                serde_json::from_str(DEFAULT_CONFIG).map_err(|_| ConfigMigrationError::Invalid)?;
            let migrated = BridgeConfig {
                mode: if legacy.write_enabled {
                    BridgeMode::QualifiedLive
                } else {
                    BridgeMode::ReadOnly
                },
                restoration: RestorationConfig {
                    enabled: legacy.restore_enabled,
                },
                socket: SocketConfig {
                    group: current.socket.group,
                    mode: current.socket.mode,
                },
                ..current
            };
            migrated
                .validate()
                .map_err(|_| ConfigMigrationError::Invalid)?;
            let payload =
                serde_json::to_vec_pretty(&migrated).map_err(|_| ConfigMigrationError::Invalid)?;
            let rollback_path = rollback_path(path);
            if rollback_path.exists() {
                return Err(ConfigMigrationError::RollbackAlreadyExists);
            }
            write_new(
                &rollback_path,
                &bytes,
                metadata.permissions().mode() & 0o777,
            )?;
            if let Err(error) = commit(path, &payload, metadata.permissions().mode() & 0o777) {
                let _ = fs::remove_file(&rollback_path);
                return Err(error);
            }
            Ok(ConfigMigrationOutcome::Migrated { rollback_path })
        }
    }
}

/// Removes the retained rollback only after the current configuration has
/// validated and the caller has observed a successful service start.
///
/// # Errors
///
/// Returns a typed validation or storage error.
pub fn confirm_configuration(path: &Path) -> Result<(), ConfigMigrationError> {
    let bytes = fs::read(path)?;
    parse_current(&bytes)?;
    match fs::remove_file(rollback_path(path)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn migration_plan(bytes: &[u8]) -> Result<ConfigMigrationPlan, ConfigMigrationError> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| ConfigMigrationError::Invalid)?;
    let schema = value
        .get("schema")
        .and_then(Value::as_str)
        .ok_or(ConfigMigrationError::Invalid)?;
    match schema {
        "hyperflux-bridge-config-v1" => {
            parse_current(bytes)?;
            Ok(ConfigMigrationPlan::Current)
        }
        LEGACY_SCHEMA => Ok(ConfigMigrationPlan::MigrateLegacyV0),
        value => Err(ConfigMigrationError::UnsupportedSchema(value.to_owned())),
    }
}

fn parse_current(bytes: &[u8]) -> Result<BridgeConfig, ConfigMigrationError> {
    if bytes.len() as u64 > MAX_CONFIGURATION_BYTES {
        return Err(ConfigMigrationError::TooLarge);
    }
    let config: BridgeConfig =
        serde_json::from_slice(bytes).map_err(|_| ConfigMigrationError::Invalid)?;
    config
        .validate()
        .map_err(|_| ConfigMigrationError::Invalid)?;
    Ok(config)
}

fn rollback_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("bridge.json");
    path.with_file_name(format!("{name}.rollback-v0"))
}

fn commit(path: &Path, payload: &[u8], mode: u32) -> Result<(), ConfigMigrationError> {
    let parent = path.parent().ok_or(ConfigMigrationError::Invalid)?;
    fs::create_dir_all(parent)?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or(ConfigMigrationError::Invalid)?;
    let temporary = path.with_file_name(format!(".{name}.tmp-{}", std::process::id()));
    if temporary.exists() {
        fs::remove_file(&temporary)?;
    }
    write_new(&temporary, payload, mode)?;
    fs::rename(&temporary, path)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}

fn write_new(path: &Path, payload: &[u8], mode: u32) -> Result<(), ConfigMigrationError> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(path)?;
    file.write_all(payload)?;
    file.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_directory() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("hfx-config-{unique}"));
        fs::create_dir(&path).expect("temporary directory");
        path
    }

    #[test]
    fn default_creation_is_current_and_conservative() {
        let directory = temporary_directory();
        let path = directory.join("bridge.json");
        assert_eq!(
            migrate_configuration(&path).expect("create"),
            ConfigMigrationOutcome::Created
        );
        let config = parse_current(&fs::read(&path).expect("read")).expect("parse");
        assert_eq!(config.mode, BridgeMode::ReadOnly);
        assert!(!config.restoration.enabled);
        assert_eq!(
            migrate_configuration(&path).expect("current"),
            ConfigMigrationOutcome::Current
        );
        fs::remove_dir_all(directory).expect("cleanup");
    }

    #[test]
    fn legacy_migration_retains_rollback_until_confirmation() {
        let directory = temporary_directory();
        let path = directory.join("bridge.json");
        let legacy = br#"{
          "$schema":"/usr/share/hyperflux-next/schemas/bridge-config-v0.json",
          "schema":"hyperflux-bridge-config-v0",
          "write_enabled":true,
          "restore_enabled":true
        }"#;
        fs::write(&path, legacy).expect("legacy");
        let ConfigMigrationOutcome::Migrated { rollback_path } =
            migrate_configuration(&path).expect("migrate")
        else {
            panic!("expected migration");
        };
        assert!(rollback_path.is_file());
        let config = parse_current(&fs::read(&path).expect("read")).expect("parse");
        assert_eq!(config.mode, BridgeMode::QualifiedLive);
        assert!(config.restoration.enabled);
        confirm_configuration(&path).expect("confirm");
        assert!(!rollback_path.exists());
        fs::remove_dir_all(directory).expect("cleanup");
    }

    #[test]
    fn unknown_future_schema_is_never_rewritten() {
        let directory = temporary_directory();
        let path = directory.join("bridge.json");
        let future = br#"{"schema":"hyperflux-bridge-config-v99"}"#;
        fs::write(&path, future).expect("future");
        assert!(matches!(
            migrate_configuration(&path),
            Err(ConfigMigrationError::UnsupportedSchema(_))
        ));
        assert_eq!(fs::read(&path).expect("unchanged"), future);
        fs::remove_dir_all(directory).expect("cleanup");
    }
}
