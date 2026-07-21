// SPDX-License-Identifier: GPL-2.0-only

use crate::probe::write_private_file;
use hfx_runtime::{LINUX_RUNTIME_SHA256, PRODUCT_VERSION};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub const UPDATE_INTENT_SCHEMA: &str = "hyperflux-package-update-intent-v1";
const MAX_UPDATE_INTENT_BYTES: u64 = 16 * 1024;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateIntent {
    pub schema: String,
    pub runtime_sha256: String,
    pub previous_package_version: String,
    pub bridge_was_active: bool,
    pub loaded_module_identity: Option<String>,
}

impl UpdateIntent {
    #[must_use]
    pub fn new(bridge_was_active: bool, loaded_module_identity: Option<String>) -> Self {
        Self {
            schema: UPDATE_INTENT_SCHEMA.to_owned(),
            runtime_sha256: LINUX_RUNTIME_SHA256.to_owned(),
            previous_package_version: PRODUCT_VERSION.to_owned(),
            bridge_was_active,
            loaded_module_identity,
        }
    }

    fn validate(&self) -> Result<(), ActivationError> {
        if self.schema != UPDATE_INTENT_SCHEMA
            || !valid_digest(&self.runtime_sha256)
            || self.previous_package_version.is_empty()
            || self.previous_package_version.len() > 64
            || self
                .loaded_module_identity
                .as_deref()
                .is_some_and(|value| !valid_identity(value))
        {
            Err(ActivationError::InvalidIntent)
        } else {
            Ok(())
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ActivationAction {
    ResumeBridge,
    LeaveBridgeStopped,
    ActivateDriver,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ActivationDecision {
    pub action: ActivationAction,
    pub bridge_was_active: bool,
    pub installed_module_identity: Option<String>,
    pub loaded_module_identity: Option<String>,
}

#[derive(Debug)]
pub enum ActivationError {
    Io(io::Error),
    InvalidIntent,
    IntentTooLarge,
    InstalledModuleUnavailable,
}

impl fmt::Display for ActivationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Io(_) => "package update state could not be stored safely",
            Self::InvalidIntent => "package update state is invalid or stale",
            Self::IntentTooLarge => "package update state exceeds its size bound",
            Self::InstalledModuleUnavailable => "the installed kernel module is unavailable",
        })
    }
}

impl std::error::Error for ActivationError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for ActivationError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Selects one deterministic post-transaction action.
///
/// # Errors
///
/// Returns an error when the installed module identity is unavailable.
pub fn decide_post_update(
    intent: Option<&UpdateIntent>,
    installed_module_identity: Option<&str>,
    loaded_module_identity: Option<&str>,
) -> Result<ActivationDecision, ActivationError> {
    let Some(installed) = installed_module_identity else {
        return Err(ActivationError::InstalledModuleUnavailable);
    };
    let bridge_was_active = intent.is_some_and(|value| value.bridge_was_active);
    let action = match loaded_module_identity {
        Some(loaded) if loaded != installed => ActivationAction::ActivateDriver,
        _ if bridge_was_active => ActivationAction::ResumeBridge,
        _ => ActivationAction::LeaveBridgeStopped,
    };
    Ok(ActivationDecision {
        action,
        bridge_was_active,
        installed_module_identity: Some(installed.to_owned()),
        loaded_module_identity: loaded_module_identity.map(str::to_owned),
    })
}

/// Atomically records the pre-update state before the service is stopped.
///
/// # Errors
///
/// Returns a bounded storage error and leaves no partially named intent.
pub fn record_update_intent(path: &Path, intent: &UpdateIntent) -> Result<(), ActivationError> {
    intent.validate()?;
    let parent = path.parent().ok_or(ActivationError::InvalidIntent)?;
    fs::create_dir_all(parent)?;
    let temporary = temporary_path(path);
    let payload = serde_json::to_vec_pretty(intent).map_err(|_| ActivationError::InvalidIntent)?;
    if payload.len() as u64 > MAX_UPDATE_INTENT_BYTES {
        return Err(ActivationError::IntentTooLarge);
    }
    if temporary.exists() {
        fs::remove_file(&temporary)?;
    }
    write_private_file(&temporary, &payload)?;
    fs::rename(&temporary, path)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}

/// Reads and validates a bounded update intent.
///
/// # Errors
///
/// Returns a typed error for oversized, stale, malformed, or unreadable state.
pub fn load_update_intent(path: &Path) -> Result<Option<UpdateIntent>, ActivationError> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if metadata.len() > MAX_UPDATE_INTENT_BYTES {
        return Err(ActivationError::IntentTooLarge);
    }
    let payload = fs::read(path)?;
    let intent: UpdateIntent =
        serde_json::from_slice(&payload).map_err(|_| ActivationError::InvalidIntent)?;
    intent.validate()?;
    Ok(Some(intent))
}

/// Removes a consumed intent. Missing state is already considered removed.
///
/// # Errors
///
/// Returns a local storage error.
pub fn remove_update_intent(path: &Path) -> Result<(), ActivationError> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn temporary_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("update");
    path.with_file_name(format!(".{name}.tmp-{}", std::process::id()))
}

fn valid_identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
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
        let path = std::env::temp_dir().join(format!("hfx-activation-{unique}"));
        fs::create_dir(&path).expect("temporary directory");
        path
    }

    #[test]
    fn compatible_update_resumes_only_a_previously_active_bridge() {
        let active = UpdateIntent::new(true, Some("same".to_owned()));
        let decision =
            decide_post_update(Some(&active), Some("same"), Some("same")).expect("decision");
        assert_eq!(decision.action, ActivationAction::ResumeBridge);

        let inactive = UpdateIntent::new(false, Some("same".to_owned()));
        let decision =
            decide_post_update(Some(&inactive), Some("same"), Some("same")).expect("decision");
        assert_eq!(decision.action, ActivationAction::LeaveBridgeStopped);
    }

    #[test]
    fn driver_change_never_restarts_the_bridge() {
        let intent = UpdateIntent::new(true, Some("old".to_owned()));
        let decision =
            decide_post_update(Some(&intent), Some("new"), Some("old")).expect("decision");
        assert_eq!(decision.action, ActivationAction::ActivateDriver);
    }

    #[test]
    fn update_intent_round_trips_atomically() {
        let directory = temporary_directory();
        let path = directory.join("intent.json");
        let intent = UpdateIntent::new(true, Some("ABC123".to_owned()));
        record_update_intent(&path, &intent).expect("record");
        assert_eq!(load_update_intent(&path).expect("load"), Some(intent));
        remove_update_intent(&path).expect("remove");
        assert_eq!(load_update_intent(&path).expect("missing"), None);
        fs::remove_dir(directory).expect("cleanup");
    }

    #[test]
    fn a_well_formed_previous_runtime_digest_survives_an_update() {
        let mut intent = UpdateIntent::new(true, Some("ABC123".to_owned()));
        intent.runtime_sha256 = "a".repeat(64);
        assert!(intent.validate().is_ok());
        intent.runtime_sha256 = "not-a-digest".to_owned();
        assert!(matches!(
            intent.validate(),
            Err(ActivationError::InvalidIntent)
        ));
    }
}
