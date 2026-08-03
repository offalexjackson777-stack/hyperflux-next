// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::StageProgress;
use crate::qualification_lifecycle::LifecycleRunState;
use crate::qualification_runner::PendingLightingTransaction;
use hfx_profiles::PROFILE_SOURCE_SHA256;
use hfx_runtime::{LINUX_RUNTIME_SHA256, PRODUCT_VERSION};
use hfx_system_probe::write_private_file;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt::{self, Write as _};
use std::fs;
use std::io;
use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
use std::path::{Path, PathBuf};

const SESSION_SCHEMA: &str = "hyperflux-qualification-session-v2";
// Bump this only when persisted checkpoints cannot be interpreted safely by
// the current coordinator. Ordinary implementation fixes must remain resumable.
const COORDINATOR_STATE_CONTRACT: &str = "hyperflux-qualification-coordinator-state-v1";
const LEGACY_SOURCE_BOUND_COORDINATOR_SHA256: &str =
    "912f0a7cd4cba79f2293e0fc664ef42e2bba5e36b549cecea596580aea739858";
const MAX_SESSION_BYTES: u64 = 256 * 1024;
const MAX_STAGE_RESULTS: usize = 64;
const MAX_STAGE_AUTHORITIES: usize = 64;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct QualificationStageAuthority {
    pub receiver_id: String,
    pub generation_id: u64,
    pub receiver_profile_id: String,
    pub receiver_profile_digest: String,
    pub device_id: String,
    pub product_id: u16,
    pub profile_id: String,
    pub profile_digest: String,
    pub stage_contract_sha256: String,
}

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
    #[serde(default)]
    pub stage_authorities: BTreeMap<String, QualificationStageAuthority>,
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
            stage_authorities: BTreeMap::new(),
            pending_transaction: None,
            lifecycle_run: None,
            hardware_write_executed: false,
        }
    }
}

impl QualificationSessionState {
    fn validate_shape(&self) -> Result<(), QualificationStateError> {
        if !valid_digest(&self.coordinator_sha256)
            || !valid_digest(&self.runtime_sha256)
            || !valid_digest(&self.profile_catalog_sha256)
        {
            return Err(QualificationStateError::Invalid);
        }
        if self.results.len() > MAX_STAGE_RESULTS
            || self.stage_authorities.len() > MAX_STAGE_AUTHORITIES
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
            || self.stage_authorities.iter().any(|(stage_id, authority)| {
                !valid_identifier(stage_id, 128)
                    || !valid_identifier(&authority.receiver_id, 128)
                    || authority.generation_id == 0
                    || !valid_identifier(&authority.receiver_profile_id, 128)
                    || !valid_digest(&authority.receiver_profile_digest)
                    || !valid_identifier(&authority.device_id, 128)
                    || !valid_identifier(&authority.profile_id, 128)
                    || !valid_digest(&authority.profile_digest)
                    || !valid_digest(&authority.stage_contract_sha256)
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

    fn compatibility(&self) -> Result<QualificationStateCompatibility, QualificationStateError> {
        if self.schema != SESSION_SCHEMA
            || self.product_version != PRODUCT_VERSION
            || self.coordinator_sha256 != coordinator_sha256()
            || self.runtime_sha256 != LINUX_RUNTIME_SHA256
        {
            return Err(QualificationStateError::Stale);
        }
        if self.profile_catalog_sha256 == PROFILE_SOURCE_SHA256 {
            Ok(QualificationStateCompatibility::Current)
        } else {
            Ok(QualificationStateCompatibility::ProfileCatalogStale)
        }
    }

    fn validate(&self) -> Result<(), QualificationStateError> {
        self.validate_shape()?;
        if self.compatibility()? != QualificationStateCompatibility::Current {
            return Err(QualificationStateError::Stale);
        }
        Ok(())
    }
}

fn valid_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum QualificationStateCompatibility {
    Current,
    ProfileCatalogStale,
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
            Self::Stale => {
                "qualification checkpoint state belongs to an incompatible coordinator contract"
            }
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

#[cfg(test)]
pub(crate) fn load_state(
    path: &Path,
) -> Result<Option<QualificationSessionState>, QualificationStateError> {
    let Some((state, compatibility)) = load_state_with_compatibility(path)? else {
        return Ok(None);
    };
    if compatibility != QualificationStateCompatibility::Current {
        return Err(QualificationStateError::Stale);
    }
    Ok(Some(state))
}

pub(crate) fn load_state_with_compatibility(
    path: &Path,
) -> Result<
    Option<(QualificationSessionState, QualificationStateCompatibility)>,
    QualificationStateError,
> {
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
    let mut state: QualificationSessionState =
        serde_json::from_slice(&payload).map_err(|_| QualificationStateError::Invalid)?;
    if state.coordinator_sha256 == LEGACY_SOURCE_BOUND_COORDINATOR_SHA256 {
        state.coordinator_sha256 = coordinator_sha256();
    }
    state.validate_shape()?;
    let compatibility = state.compatibility()?;
    Ok(Some((state, compatibility)))
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

pub(crate) fn archive_stale_state(path: &Path) -> Result<PathBuf, QualificationStateError> {
    let payload = fs::read(path)?;
    if payload.len() as u64 > MAX_SESSION_BYTES {
        return Err(QualificationStateError::TooLarge);
    }
    let parent = path.parent().ok_or(QualificationStateError::Invalid)?;
    let digest = digest_hex(&Sha256::digest(&payload));
    let archive = parent.join(format!("qualification-session.incompatible-{digest}.json"));
    match fs::symlink_metadata(&archive) {
        Ok(metadata)
            if metadata.file_type().is_file()
                && metadata.uid() == rustix::process::geteuid().as_raw()
                && metadata.mode().trailing_zeros() >= 6
                && fs::read(&archive)? == payload => {}
        Ok(_) => return Err(QualificationStateError::Insecure),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            write_private_file(&archive, &payload)?;
            fs::File::open(parent)?.sync_all()?;
        }
        Err(error) => return Err(error.into()),
    }
    Ok(archive)
}

fn temporary_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(format!(".{}.tmp", std::process::id()));
    PathBuf::from(name)
}

fn coordinator_sha256() -> String {
    let mut digest = Sha256::new();
    digest.update(SESSION_SCHEMA.as_bytes());
    digest.update([0]);
    digest.update(COORDINATOR_STATE_CONTRACT.as_bytes());
    digest_hex(&digest.finalize())
}

fn digest_hex(value: &[u8]) -> String {
    let mut encoded = String::with_capacity(64);
    for byte in value {
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests;
