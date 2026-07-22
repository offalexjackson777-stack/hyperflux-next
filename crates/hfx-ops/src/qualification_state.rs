// SPDX-License-Identifier: GPL-2.0-only

use crate::probe::write_private_file;
use crate::qualification::StageProgress;
use crate::qualification_lifecycle::LifecycleRunState;
use crate::qualification_runner::PendingLightingTransaction;
use hfx_profiles::PROFILE_SOURCE_SHA256;
use hfx_runtime::{LINUX_RUNTIME_SHA256, PRODUCT_VERSION};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt::{self, Write as _};
use std::fs;
use std::io;
use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
use std::path::{Path, PathBuf};

const SESSION_SCHEMA: &str = "hyperflux-qualification-session-v2";
const MAX_SESSION_BYTES: u64 = 256 * 1024;
const MAX_STAGE_RESULTS: usize = 64;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct QualificationSessionState {
    pub schema: String,
    pub product_version: String,
    pub coordinator_sha256: String,
    pub runtime_sha256: String,
    pub profile_catalog_sha256: String,
    pub revision: u64,
    pub identity_fingerprint: Option<[u8; 32]>,
    pub run_id: Option<String>,
    pub results: BTreeMap<String, StageProgress>,
    pub pending_transaction: Option<PendingLightingTransaction>,
    pub lifecycle_run: Option<LifecycleRunState>,
    pub hardware_write_executed: bool,
}

impl Default for QualificationSessionState {
    fn default() -> Self {
        Self {
            schema: SESSION_SCHEMA.to_owned(),
            product_version: PRODUCT_VERSION.to_owned(),
            coordinator_sha256: coordinator_sha256(),
            runtime_sha256: LINUX_RUNTIME_SHA256.to_owned(),
            profile_catalog_sha256: PROFILE_SOURCE_SHA256.to_owned(),
            revision: 0,
            identity_fingerprint: None,
            run_id: None,
            results: BTreeMap::new(),
            pending_transaction: None,
            lifecycle_run: None,
            hardware_write_executed: false,
        }
    }
}

impl QualificationSessionState {
    fn validate(&self) -> Result<(), QualificationStateError> {
        if !valid_digest(&self.coordinator_sha256)
            || !valid_digest(&self.runtime_sha256)
            || !valid_digest(&self.profile_catalog_sha256)
        {
            return Err(QualificationStateError::Invalid);
        }
        if self.schema != SESSION_SCHEMA
            || self.product_version != PRODUCT_VERSION
            || self.coordinator_sha256 != coordinator_sha256()
            || self.runtime_sha256 != LINUX_RUNTIME_SHA256
            || self.profile_catalog_sha256 != PROFILE_SOURCE_SHA256
        {
            return Err(QualificationStateError::Stale);
        }
        if self.results.len() > MAX_STAGE_RESULTS
            || self.run_id.as_deref().is_some_and(|value| {
                value.is_empty()
                    || value.len() > 96
                    || !value.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
                    })
            })
            || self.results.iter().any(|(stage_id, progress)| {
                stage_id.is_empty()
                    || stage_id.len() > 128
                    || !stage_id.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
                    })
                    || progress.checkpoint.as_ref().is_some_and(|checkpoint| {
                        checkpoint.checkpoint_id.is_empty()
                            || checkpoint.checkpoint_id.len() > 128
                            || checkpoint.instructions.len() > 16
                            || checkpoint.observations.len() > 16
                            || checkpoint.evidence_refs.len() > 32
                    })
            })
            || self.pending_transaction.as_ref().is_some_and(|pending| {
                pending.stage_id.is_empty()
                    || pending.stage_id.len() > 128
                    || !pending.stage_id.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
                    })
            })
        {
            return Err(QualificationStateError::Invalid);
        }
        if self
            .lifecycle_run
            .as_ref()
            .is_some_and(|run| run.validate().is_err())
        {
            return Err(QualificationStateError::Invalid);
        }
        Ok(())
    }
}

#[derive(Debug)]
pub(crate) enum QualificationStateError {
    Io(io::Error),
    Invalid,
    Stale,
    TooLarge,
    Insecure,
}

impl fmt::Display for QualificationStateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Io(_) => "qualification checkpoint state could not be stored safely",
            Self::Invalid => "qualification checkpoint state is invalid",
            Self::Stale => "qualification checkpoint state belongs to a different build",
            Self::TooLarge => "qualification checkpoint state exceeds its size bound",
            Self::Insecure => "qualification checkpoint state is not private",
        })
    }
}

impl std::error::Error for QualificationStateError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Invalid | Self::Stale | Self::TooLarge | Self::Insecure => None,
        }
    }
}

impl From<io::Error> for QualificationStateError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

pub(crate) fn default_state_path() -> Result<PathBuf, QualificationStateError> {
    let base = std::env::var_os("XDG_STATE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .filter(|value| !value.is_empty())
                .map(|home| PathBuf::from(home).join(".local/state"))
        })
        .ok_or(QualificationStateError::Invalid)?;
    if !base.is_absolute() {
        return Err(QualificationStateError::Invalid);
    }
    Ok(base
        .join("hyperflux-next")
        .join("qualification-session.json"))
}

pub(crate) fn load_state(
    path: &Path,
) -> Result<Option<QualificationSessionState>, QualificationStateError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !metadata.file_type().is_file()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o077 != 0
    {
        return Err(QualificationStateError::Insecure);
    }
    if metadata.len() > MAX_SESSION_BYTES {
        return Err(QualificationStateError::TooLarge);
    }
    let payload = fs::read(path)?;
    let state: QualificationSessionState =
        serde_json::from_slice(&payload).map_err(|_| QualificationStateError::Invalid)?;
    state.validate()?;
    Ok(Some(state))
}

pub(crate) fn save_state(
    path: &Path,
    state: &QualificationSessionState,
) -> Result<(), QualificationStateError> {
    state.validate()?;
    let parent = path.parent().ok_or(QualificationStateError::Invalid)?;
    fs::create_dir_all(parent)?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if !parent_metadata.file_type().is_dir()
        || parent_metadata.uid() != rustix::process::geteuid().as_raw()
    {
        return Err(QualificationStateError::Insecure);
    }
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    let payload = serde_json::to_vec_pretty(state).map_err(|_| QualificationStateError::Invalid)?;
    if payload.len() as u64 > MAX_SESSION_BYTES {
        return Err(QualificationStateError::TooLarge);
    }
    let temporary = temporary_path(path);
    match fs::symlink_metadata(&temporary) {
        Ok(metadata)
            if metadata.file_type().is_file()
                && metadata.uid() == rustix::process::geteuid().as_raw() =>
        {
            fs::remove_file(&temporary)?;
        }
        Ok(_) => return Err(QualificationStateError::Insecure),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    write_private_file(&temporary, &payload)?;
    fs::rename(&temporary, path)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}

fn temporary_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(format!(".{}.tmp", std::process::id()));
    PathBuf::from(name)
}

fn coordinator_sha256() -> String {
    let mut digest = Sha256::new();
    digest.update(include_bytes!("qualification.rs"));
    digest.update(include_bytes!("qualification_lifecycle.rs"));
    digest.update(include_bytes!("qualification_runner.rs"));
    digest.update(include_bytes!("qualification_server.rs"));
    digest.update(include_bytes!("qualification_state.rs"));
    digest.update(option_env!("HFX_SOURCE_REVISION").unwrap_or("development-unbound"));
    let mut encoded = String::with_capacity(64);
    for byte in digest.finalize() {
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "hyperflux-next-qualification-state-{}-{label}/session.json",
            std::process::id()
        ))
    }

    #[test]
    fn state_round_trips_privately_and_rejects_stale_authority() {
        let path = test_path("round-trip");
        let _ = fs::remove_dir_all(path.parent().expect("parent"));
        let mut state = QualificationSessionState {
            revision: 7,
            identity_fingerprint: Some([9_u8; 32]),
            run_id: Some("local-test".to_owned()),
            pending_transaction: Some(PendingLightingTransaction {
                stage_id: "mouse-00cd-lighting".to_owned(),
                transaction_id: hfx_domain::TransactionId::try_from("qualification-test")
                    .expect("transaction id"),
                step: crate::qualification_runner::LightingStep::LightingOff,
                prior_progress: Some(StageProgress {
                    status: crate::StageStatus::AwaitingObservation,
                    result: None,
                    checkpoint: Some(crate::qualification::StageCheckpoint {
                        checkpoint_id: "lighting-blue".to_owned(),
                        instructions: Vec::new(),
                        observations: Vec::new(),
                        evidence_refs: vec!["bridge-transaction:previous".to_owned()],
                    }),
                }),
            }),
            ..QualificationSessionState::default()
        };
        save_state(&path, &state).expect("state saves");
        assert_eq!(
            fs::metadata(&path).expect("metadata").permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(load_state(&path).expect("state loads"), Some(state.clone()));
        state.coordinator_sha256 = "0".repeat(64);
        let payload = serde_json::to_vec_pretty(&state).expect("state serializes");
        fs::write(&path, payload).expect("test state changes");
        assert!(matches!(
            load_state(&path),
            Err(QualificationStateError::Stale)
        ));
        fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
    }

    #[test]
    fn state_rejects_world_readable_files() {
        let path = test_path("permissions");
        let _ = fs::remove_dir_all(path.parent().expect("parent"));
        save_state(&path, &QualificationSessionState::default()).expect("state saves");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("permissions change");
        assert!(matches!(
            load_state(&path),
            Err(QualificationStateError::Insecure)
        ));
        fs::remove_dir_all(path.parent().expect("parent")).expect("test state removes");
    }
}
