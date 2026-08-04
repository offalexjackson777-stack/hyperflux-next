// SPDX-License-Identifier: GPL-2.0-only

use crate::PublicationIdentityFailure;
use serde::Serialize;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

const STATUS_SCHEMA: &str = "hyperflux-openrazer-relay-status-v1";
const MAX_STATUS_BYTES: usize = 128 * 1024;
const MAX_STATUS_ROUTES: usize = 256;
const MAX_PROFILE_BYTES: usize = 128;
const MAX_STATUS_ATTEMPT: u32 = u16::MAX as u32;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum RelayPublicationState {
    Pending,
    BackingOff,
    Blocked,
    DirectSuppressed,
    CreateSent,
    KernelStarted,
}

impl RelayPublicationState {
    pub(crate) const fn label(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::BackingOff => "backing-off",
            Self::Blocked => "blocked",
            Self::DirectSuppressed => "direct-suppressed",
            Self::CreateSent => "create-sent",
            Self::KernelStarted => "kernel-started",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(crate) struct RelayStatusFailure {
    stage: &'static str,
    class: &'static str,
    status: Option<u8>,
}

impl From<PublicationIdentityFailure> for RelayStatusFailure {
    fn from(failure: PublicationIdentityFailure) -> Self {
        Self {
            stage: failure.stage(),
            class: failure.class(),
            status: failure.status(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(crate) struct RelayRouteStatus {
    pub(crate) profile_id: String,
    pub(crate) vendor_id: u16,
    pub(crate) product_id: u16,
    pub(crate) device_kind: &'static str,
    pub(crate) state: RelayPublicationState,
    pub(crate) failure: Option<RelayStatusFailure>,
    pub(crate) attempt: u32,
    pub(crate) next_retry_ms: u32,
}

pub(crate) struct RelayRouteStatusIdentity {
    pub(crate) profile_id: String,
    pub(crate) vendor_id: u16,
    pub(crate) product_id: u16,
    pub(crate) device_kind: &'static str,
}

impl RelayRouteStatus {
    pub(crate) fn new(
        identity: RelayRouteStatusIdentity,
        state: RelayPublicationState,
        failure: Option<PublicationIdentityFailure>,
        attempt: u32,
        next_retry_ms: u32,
    ) -> Self {
        Self {
            profile_id: identity.profile_id,
            vendor_id: identity.vendor_id,
            product_id: identity.product_id,
            device_kind: identity.device_kind,
            state,
            failure: failure.map(Into::into),
            attempt,
            next_retry_ms,
        }
    }

    pub(crate) fn log_transition(&self) {
        let (stage, class, status) =
            self.failure
                .as_ref()
                .map_or(("none", "none", String::from("none")), |failure| {
                    (
                        failure.stage,
                        failure.class,
                        failure
                            .status
                            .map_or_else(|| String::from("none"), |value| value.to_string()),
                    )
                });
        eprintln!(
            "HFX-OPENRAZER-RELAY-STATE profile={} vid={:04x} pid={:04x} kind={} state={} stage={} class={} status={} attempt={} retry_ms={}",
            self.profile_id,
            self.vendor_id,
            self.product_id,
            self.device_kind,
            self.state.label(),
            stage,
            class,
            status,
            self.attempt,
            self.next_retry_ms,
        );
    }

    fn valid(&self) -> bool {
        !self.profile_id.is_empty()
            && self.profile_id.len() <= MAX_PROFILE_BYTES
            && self.profile_id.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
            })
            && self.vendor_id != 0
            && self.product_id != 0
            && matches!(self.device_kind, "keyboard" | "mouse")
            && self.attempt <= MAX_STATUS_ATTEMPT
            && self.next_retry_ms <= 30_000
    }
}

#[derive(Serialize)]
struct RelayStatusDocument<'a> {
    schema: &'static str,
    routes: &'a [RelayRouteStatus],
}

pub(crate) struct RelayStatusWriter {
    path: PathBuf,
    previous: Vec<u8>,
    sequence: u64,
}

impl RelayStatusWriter {
    pub(crate) fn new(path: PathBuf) -> Self {
        Self {
            path,
            previous: Vec::new(),
            sequence: 0,
        }
    }

    pub(crate) fn publish(&mut self, mut routes: Vec<RelayRouteStatus>) -> io::Result<()> {
        routes.sort_by(|left, right| {
            (
                &left.profile_id,
                left.vendor_id,
                left.product_id,
                left.device_kind,
            )
                .cmp(&(
                    &right.profile_id,
                    right.vendor_id,
                    right.product_id,
                    right.device_kind,
                ))
        });
        if routes.len() > MAX_STATUS_ROUTES || routes.iter().any(|route| !route.valid()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "relay status exceeds its bounded schema",
            ));
        }
        let bytes = serde_json::to_vec(&RelayStatusDocument {
            schema: STATUS_SCHEMA,
            routes: &routes,
        })?;
        if bytes.len() > MAX_STATUS_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "relay status exceeds its byte bound",
            ));
        }
        if bytes == self.previous {
            return Ok(());
        }
        write_atomic_regular(&self.path, &bytes, self.sequence)?;
        self.sequence = self.sequence.saturating_add(1);
        self.previous = bytes;
        Ok(())
    }
}

fn write_atomic_regular(path: &Path, bytes: &[u8], sequence: u64) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "status has no parent"))?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "status parent is not one ordinary directory",
        ));
    }
    if let Ok(metadata) = fs::symlink_metadata(path)
        && (!metadata.file_type().is_file() || metadata.file_type().is_symlink())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "status destination is not one regular file",
        ));
    }
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid status name"))?;
    let temporary = parent.join(format!(
        ".{file_name}.tmp-{}-{sequence}",
        std::process::id()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o644))?;
        fs::rename(&temporary, path)?;
        File::open(parent)?.sync_all()
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{MetadataExt, symlink};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_directory() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock is valid")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "hyperflux-relay-status-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("status directory is created");
        path
    }

    fn route(state: RelayPublicationState) -> RelayRouteStatus {
        RelayRouteStatus::new(
            RelayRouteStatusIdentity {
                profile_id: "child.razer.synthetic.future.1234".to_owned(),
                vendor_id: 0x1532,
                product_id: 0x1234,
                device_kind: "keyboard",
            },
            state,
            None,
            0,
            0,
        )
    }

    #[test]
    fn status_is_atomic_regular_public_and_privacy_neutral() {
        let directory = temporary_directory();
        let path = directory.join("status.json");
        let mut writer = RelayStatusWriter::new(path.clone());
        writer
            .publish(vec![route(RelayPublicationState::Pending)])
            .expect("status publishes");
        let metadata = fs::symlink_metadata(&path).expect("status exists");
        assert!(metadata.file_type().is_file());
        assert_eq!(metadata.mode() & 0o777, 0o644);
        let bytes = fs::read(&path).expect("status reads");
        assert!(bytes.len() < MAX_STATUS_BYTES);
        assert!(!bytes.windows(6).any(|window| window == b"serial"));
        assert!(!bytes.windows(5).any(|window| window == b"token"));
        assert_eq!(
            fs::read_dir(&directory).expect("directory reads").count(),
            1
        );
        fs::remove_dir_all(directory).expect("fixture is removed");
    }

    #[test]
    fn status_rejects_symlink_destination_and_oversized_inventory() {
        let directory = temporary_directory();
        let target = directory.join("target");
        fs::write(&target, b"unchanged").expect("target exists");
        let path = directory.join("status.json");
        symlink(&target, &path).expect("status symlink exists");
        let mut writer = RelayStatusWriter::new(path);
        assert!(
            writer
                .publish(vec![route(RelayPublicationState::Pending)])
                .is_err()
        );
        assert_eq!(fs::read(&target).expect("target reads"), b"unchanged");

        let mut writer = RelayStatusWriter::new(directory.join("other.json"));
        assert!(
            writer
                .publish(vec![route(RelayPublicationState::Pending); 257])
                .is_err()
        );
        fs::remove_dir_all(directory).expect("fixture is removed");
    }
}
