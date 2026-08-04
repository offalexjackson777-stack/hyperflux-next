// SPDX-License-Identifier: GPL-2.0-only

//! Private, bounded, crash-safe persistence for bridge restoration state.

use hfx_core::{
    CURRENT_PERSISTENCE_SCHEMA_VERSION, MAX_RESTORE_RECORDS_PER_RECEIVER,
    MAX_STABLE_ENTRIES_PER_RECEIVER, PersistedRestorePolicy, PersistedStableEntry,
    PersistenceCasOutcome, PersistenceStore, RestoreRecord, RestoreRecordChange,
    RestoreRecordStatus, StableIntentChange,
};
use hfx_domain::{PersistenceRevision, ReceiverId, RestoreClaimId};
use rustix::fs::{FlockOperation, Mode, OFlags, flock, open};
use rustix::process::geteuid;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::fmt;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Path, PathBuf};

pub const BRIDGE_PERSISTENCE_SCHEMA: &str = "hyperflux-bridge-persistence-v1";
pub const DEFAULT_MAX_PERSISTED_RECEIVERS: usize = 16;
pub const DEFAULT_MAX_PERSISTENCE_BYTES: u64 = 4 * 1024 * 1024;
const MIN_TERMINAL_REPLAY_RECORDS: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PersistenceIoStage {
    CreateDirectory,
    InspectDirectory,
    OpenLock,
    InspectLock,
    AcquireLock,
    OpenState,
    InspectState,
    ReadState,
    CreateTemporary,
    WriteTemporary,
    ReplaceState,
    SyncDirectory,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FilePersistenceError {
    InvalidConfig(&'static str),
    Io {
        stage: PersistenceIoStage,
        kind: std::io::ErrorKind,
        replacement_visible: bool,
    },
    AlreadyLocked,
    UntrustedDirectory,
    UntrustedFile,
    StateTooLarge,
    MalformedState,
    UnsupportedSchema,
    NoncanonicalState(&'static str),
    Capacity,
    DuplicateChange,
    IdentityConflict,
}

impl FilePersistenceError {
    fn io(stage: PersistenceIoStage, error: impl Into<std::io::Error>) -> Self {
        Self::Io {
            stage,
            kind: error.into().kind(),
            replacement_visible: stage == PersistenceIoStage::SyncDirectory,
        }
    }

    #[must_use]
    pub const fn replacement_visible(&self) -> bool {
        matches!(
            self,
            Self::Io {
                replacement_visible: true,
                ..
            }
        )
    }
}

impl fmt::Display for FilePersistenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig(reason) => {
                write!(formatter, "invalid persistence config: {reason}")
            }
            Self::Io { stage, kind, .. } => {
                write!(formatter, "persistence I/O failed during {stage:?}: {kind}")
            }
            Self::AlreadyLocked => formatter.write_str("another bridge owns the persistence lock"),
            Self::UntrustedDirectory => {
                formatter.write_str("persistence directory is not private and trusted")
            }
            Self::UntrustedFile => {
                formatter.write_str("persistence state is not a private regular file")
            }
            Self::StateTooLarge => formatter.write_str("persistence state exceeds its byte bound"),
            Self::MalformedState => formatter.write_str("persistence state is malformed"),
            Self::UnsupportedSchema => {
                formatter.write_str("persistence state uses an unsupported schema")
            }
            Self::NoncanonicalState(reason) => {
                write!(formatter, "persistence state is not canonical: {reason}")
            }
            Self::Capacity => formatter.write_str("persistence capacity is exhausted"),
            Self::DuplicateChange => {
                formatter.write_str("persistence batch contains a duplicate key")
            }
            Self::IdentityConflict => {
                formatter.write_str("persistence key conflicts with retained identity")
            }
        }
    }
}

impl std::error::Error for FilePersistenceError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FilePersistenceConfig {
    pub path: PathBuf,
    pub create_parent: bool,
    pub max_receivers: usize,
    pub max_bytes: u64,
}

impl FilePersistenceConfig {
    #[must_use]
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            create_parent: true,
            max_receivers: DEFAULT_MAX_PERSISTED_RECEIVERS,
            max_bytes: DEFAULT_MAX_PERSISTENCE_BYTES,
        }
    }

    fn validate(&self) -> Result<(), FilePersistenceError> {
        if !self.path.is_absolute() {
            return Err(FilePersistenceError::InvalidConfig(
                "state path must be absolute",
            ));
        }
        if self.path.file_name().is_none() {
            return Err(FilePersistenceError::InvalidConfig(
                "state path must name a file",
            ));
        }
        if self.max_receivers == 0 || self.max_bytes == 0 {
            return Err(FilePersistenceError::InvalidConfig(
                "persistence bounds must be nonzero",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgePersistenceDocument {
    pub schema: String,
    pub policies: Vec<PersistedRestorePolicy>,
    pub stable_entries: Vec<PersistedStableEntry>,
    pub restore_records: Vec<RestoreRecord>,
}

impl Default for BridgePersistenceDocument {
    fn default() -> Self {
        Self {
            schema: BRIDGE_PERSISTENCE_SCHEMA.to_owned(),
            policies: Vec::new(),
            stable_entries: Vec::new(),
            restore_records: Vec::new(),
        }
    }
}

pub trait PersistenceCommitter: fmt::Debug {
    /// Atomically commits bytes to the final state path.
    ///
    /// # Errors
    ///
    /// Returns a stage-classified I/O error without exposing the private path.
    fn commit(&mut self, path: &Path, bytes: &[u8]) -> Result<(), FilePersistenceError>;
}

#[derive(Debug, Default)]
pub struct AtomicFileCommitter {
    next_temporary: u64,
}

impl PersistenceCommitter for AtomicFileCommitter {
    fn commit(&mut self, path: &Path, bytes: &[u8]) -> Result<(), FilePersistenceError> {
        let parent = path.parent().ok_or(FilePersistenceError::InvalidConfig(
            "state path has no parent",
        ))?;
        let file_name = path.file_name().and_then(|name| name.to_str()).ok_or(
            FilePersistenceError::InvalidConfig("state file name is not portable UTF-8"),
        )?;
        self.next_temporary =
            self.next_temporary
                .checked_add(1)
                .ok_or(FilePersistenceError::InvalidConfig(
                    "temporary sequence exhausted",
                ))?;
        let temporary = parent.join(format!(
            ".{file_name}.tmp-{}-{}",
            std::process::id(),
            self.next_temporary
        ));
        let result = (|| {
            let descriptor = open(
                &temporary,
                OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                Mode::RUSR | Mode::WUSR,
            )
            .map_err(|error| {
                FilePersistenceError::io(PersistenceIoStage::CreateTemporary, error)
            })?;
            let mut file = File::from(descriptor);
            file.write_all(bytes)
                .and_then(|()| file.sync_all())
                .map_err(|error| {
                    FilePersistenceError::io(PersistenceIoStage::WriteTemporary, error)
                })?;
            fs::rename(&temporary, path).map_err(|error| {
                FilePersistenceError::io(PersistenceIoStage::ReplaceState, error)
            })?;
            sync_directory(parent)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

#[derive(Debug)]
pub struct FilePersistenceStore<C = AtomicFileCommitter> {
    config: FilePersistenceConfig,
    document: BridgePersistenceDocument,
    committer: C,
    _lock: File,
}

impl FilePersistenceStore<AtomicFileCommitter> {
    /// Opens and exclusively locks one bridge state document.
    ///
    /// # Errors
    ///
    /// Returns an error for unsafe paths, lock contention, malformed or
    /// incompatible state, exceeded bounds, or I/O failure.
    pub fn open(config: FilePersistenceConfig) -> Result<Self, FilePersistenceError> {
        Self::open_with_committer(config, AtomicFileCommitter::default())
    }
}

impl<C: PersistenceCommitter> FilePersistenceStore<C> {
    /// Opens a store with an injectable atomic commit boundary.
    ///
    /// # Errors
    ///
    /// Returns the same errors as [`FilePersistenceStore::open`].
    pub fn open_with_committer(
        config: FilePersistenceConfig,
        committer: C,
    ) -> Result<Self, FilePersistenceError> {
        config.validate()?;
        let parent = validate_parent(&config)?;
        let lock = acquire_lock(&config.path, &parent)?;
        let document = load_document(&config)?;
        validate_document(&document, &config)?;
        Ok(Self {
            config,
            document,
            committer,
            _lock: lock,
        })
    }

    #[must_use]
    pub const fn document(&self) -> &BridgePersistenceDocument {
        &self.document
    }

    fn commit(&mut self, candidate: BridgePersistenceDocument) -> Result<(), FilePersistenceError> {
        validate_document(&candidate, &self.config)?;
        let bytes =
            serde_json::to_vec(&candidate).map_err(|_| FilePersistenceError::MalformedState)?;
        if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > self.config.max_bytes {
            return Err(FilePersistenceError::StateTooLarge);
        }
        match self.committer.commit(&self.config.path, &bytes) {
            Ok(()) => {
                self.document = candidate;
                Ok(())
            }
            Err(error) => {
                if error.replacement_visible() {
                    self.document = candidate;
                }
                Err(error)
            }
        }
    }
}

impl<C: PersistenceCommitter> PersistenceStore for FilePersistenceStore<C> {
    type Error = FilePersistenceError;

    fn restore_policy(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Option<PersistedRestorePolicy>, Self::Error> {
        Ok(self
            .document
            .policies
            .iter()
            .find(|policy| &policy.receiver_id == receiver_id)
            .cloned())
    }

    fn compare_and_set_restore_policy(
        &mut self,
        expected_revision: Option<PersistenceRevision>,
        policy: &PersistedRestorePolicy,
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        validate_record_schema(policy.schema_version.get())?;
        let position = self
            .document
            .policies
            .iter()
            .position(|current| current.receiver_id == policy.receiver_id);
        let actual = position.map(|index| self.document.policies[index].revision);
        if actual != expected_revision {
            return Ok(PersistenceCasOutcome::Conflict);
        }
        let mut candidate = self.document.clone();
        if let Some(index) = position {
            candidate.policies[index] = policy.clone();
        } else {
            candidate.policies.push(policy.clone());
        }
        canonicalize(&mut candidate);
        self.commit(candidate)?;
        Ok(PersistenceCasOutcome::Applied)
    }

    fn stable_entries(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<PersistedStableEntry>, Self::Error> {
        Ok(self
            .document
            .stable_entries
            .iter()
            .filter(|entry| entry.receiver_id() == receiver_id)
            .cloned()
            .collect())
    }

    fn compare_and_set_stable_entries(
        &mut self,
        changes: &[StableIntentChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        if changes.is_empty() {
            return Ok(PersistenceCasOutcome::Applied);
        }
        let keys = changes
            .iter()
            .map(|change| {
                (
                    change.entry.receiver_id().clone(),
                    change.entry.device_id().clone(),
                )
            })
            .collect::<BTreeSet<_>>();
        if keys.len() != changes.len() {
            return Err(FilePersistenceError::DuplicateChange);
        }
        for change in changes {
            validate_stable_entry(&change.entry)?;
            let actual = self
                .document
                .stable_entries
                .iter()
                .find(|entry| {
                    entry.receiver_id() == change.entry.receiver_id()
                        && entry.device_id() == change.entry.device_id()
                })
                .map(PersistedStableEntry::revision);
            if actual != change.expected_revision {
                return Ok(PersistenceCasOutcome::Conflict);
            }
        }
        let mut candidate = self.document.clone();
        for change in changes {
            if let Some(index) = candidate.stable_entries.iter().position(|entry| {
                entry.receiver_id() == change.entry.receiver_id()
                    && entry.device_id() == change.entry.device_id()
            }) {
                candidate.stable_entries[index] = change.entry.clone();
            } else {
                candidate.stable_entries.push(change.entry.clone());
            }
        }
        canonicalize(&mut candidate);
        self.commit(candidate)?;
        Ok(PersistenceCasOutcome::Applied)
    }

    fn restore_records(&self, receiver_id: &ReceiverId) -> Result<Vec<RestoreRecord>, Self::Error> {
        Ok(self
            .document
            .restore_records
            .iter()
            .filter(|record| &record.receiver_id == receiver_id)
            .cloned()
            .collect())
    }

    fn restore_record(
        &self,
        claim_id: &RestoreClaimId,
    ) -> Result<Option<RestoreRecord>, Self::Error> {
        Ok(self
            .document
            .restore_records
            .iter()
            .find(|record| &record.claim_id == claim_id)
            .cloned())
    }

    fn compare_and_set_restore_records(
        &mut self,
        changes: &[RestoreRecordChange],
    ) -> Result<PersistenceCasOutcome, Self::Error> {
        if changes.is_empty() {
            return Ok(PersistenceCasOutcome::Applied);
        }
        let claims = changes
            .iter()
            .map(|change| change.record.claim_id.clone())
            .collect::<BTreeSet<_>>();
        if claims.len() != changes.len() {
            return Err(FilePersistenceError::DuplicateChange);
        }
        for change in changes {
            validate_restore_record(&change.record)?;
            let current = self
                .document
                .restore_records
                .iter()
                .find(|current| current.claim_id == change.record.claim_id);
            if current.is_some_and(|current| {
                current.receiver_id != change.record.receiver_id
                    || current.device_id != change.record.device_id
                    || current.trigger_id != change.record.trigger_id
            }) {
                return Err(FilePersistenceError::IdentityConflict);
            }
            if current.map(|record| record.revision) != change.expected_revision {
                return Ok(PersistenceCasOutcome::Conflict);
            }
        }
        let mut candidate = self.document.clone();
        for change in changes {
            if let Some(index) = candidate
                .restore_records
                .iter()
                .position(|current| current.claim_id == change.record.claim_id)
            {
                candidate.restore_records[index] = change.record.clone();
            } else {
                candidate.restore_records.push(change.record.clone());
            }
        }
        compact_restore_history(&mut candidate, &claims)?;
        canonicalize(&mut candidate);
        self.commit(candidate)?;
        Ok(PersistenceCasOutcome::Applied)
    }
}

fn compact_restore_history(
    document: &mut BridgePersistenceDocument,
    protected_claims: &BTreeSet<RestoreClaimId>,
) -> Result<(), FilePersistenceError> {
    let receivers = document
        .restore_records
        .iter()
        .map(|record| record.receiver_id.clone())
        .collect::<BTreeSet<_>>();
    let mut remove = BTreeSet::new();
    for receiver_id in receivers {
        let records = document
            .restore_records
            .iter()
            .filter(|record| record.receiver_id == receiver_id)
            .collect::<Vec<_>>();
        let overflow = records
            .len()
            .saturating_sub(MAX_RESTORE_RECORDS_PER_RECEIVER);
        if overflow == 0 {
            continue;
        }
        let replay_records = records
            .iter()
            .filter(|record| compactable_terminal(&record.status))
            .count();
        if overflow > replay_records.saturating_sub(MIN_TERMINAL_REPLAY_RECORDS) {
            return Err(FilePersistenceError::Capacity);
        }
        let mut eligible = records
            .into_iter()
            .filter(|record| {
                compactable_terminal(&record.status) && !protected_claims.contains(&record.claim_id)
            })
            .map(|record| (record.revision, record.claim_id.clone()))
            .collect::<Vec<_>>();
        eligible.sort_unstable();
        if eligible.len() < overflow {
            return Err(FilePersistenceError::Capacity);
        }
        remove.extend(
            eligible
                .into_iter()
                .take(overflow)
                .map(|(_, claim_id)| claim_id),
        );
    }
    document
        .restore_records
        .retain(|record| !remove.contains(&record.claim_id));
    Ok(())
}

const fn compactable_terminal(status: &RestoreRecordStatus) -> bool {
    matches!(
        status,
        RestoreRecordStatus::Succeeded(_) | RestoreRecordStatus::Invalidated(_)
    )
}

fn validate_parent(config: &FilePersistenceConfig) -> Result<PathBuf, FilePersistenceError> {
    let parent = config
        .path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or(FilePersistenceError::InvalidConfig(
            "state path has no parent",
        ))?;
    if config.create_parent && !parent.exists() {
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700);
        builder.create(parent).map_err(|error| {
            FilePersistenceError::io(PersistenceIoStage::CreateDirectory, error)
        })?;
    }
    let metadata = fs::symlink_metadata(parent)
        .map_err(|error| FilePersistenceError::io(PersistenceIoStage::InspectDirectory, error))?;
    if !metadata.file_type().is_dir()
        || metadata.uid() != geteuid().as_raw()
        || metadata.mode() & 0o077 != 0
    {
        return Err(FilePersistenceError::UntrustedDirectory);
    }
    Ok(parent.to_path_buf())
}

fn acquire_lock(state_path: &Path, parent: &Path) -> Result<File, FilePersistenceError> {
    let file_name = state_path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(FilePersistenceError::InvalidConfig(
            "state file name is not portable UTF-8",
        ))?;
    let lock_path = parent.join(format!(".{file_name}.lock"));
    let descriptor = open(
        &lock_path,
        OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::RUSR | Mode::WUSR,
    )
    .map_err(|error| FilePersistenceError::io(PersistenceIoStage::OpenLock, error))?;
    let file = File::from(descriptor);
    validate_private_file(&file, PersistenceIoStage::InspectLock)?;
    flock(&file, FlockOperation::NonBlockingLockExclusive).map_err(|error| {
        if error == rustix::io::Errno::WOULDBLOCK {
            FilePersistenceError::AlreadyLocked
        } else {
            FilePersistenceError::io(PersistenceIoStage::AcquireLock, error)
        }
    })?;
    Ok(file)
}

fn load_document(
    config: &FilePersistenceConfig,
) -> Result<BridgePersistenceDocument, FilePersistenceError> {
    let descriptor = match open(
        &config.path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    ) {
        Ok(descriptor) => descriptor,
        Err(rustix::io::Errno::NOENT) => return Ok(BridgePersistenceDocument::default()),
        Err(error) => {
            return Err(FilePersistenceError::io(
                PersistenceIoStage::OpenState,
                error,
            ));
        }
    };
    let mut file = File::from(descriptor);
    let metadata = validate_private_file(&file, PersistenceIoStage::InspectState)?;
    if metadata.len() > config.max_bytes {
        return Err(FilePersistenceError::StateTooLarge);
    }
    let reserve = usize::try_from(metadata.len()).unwrap_or(0);
    let mut bytes = Vec::with_capacity(reserve);
    Read::by_ref(&mut file)
        .take(config.max_bytes.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| FilePersistenceError::io(PersistenceIoStage::ReadState, error))?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > config.max_bytes {
        return Err(FilePersistenceError::StateTooLarge);
    }
    serde_json::from_slice(&bytes).map_err(|_| FilePersistenceError::MalformedState)
}

fn validate_private_file(
    file: &File,
    stage: PersistenceIoStage,
) -> Result<fs::Metadata, FilePersistenceError> {
    let metadata = file
        .metadata()
        .map_err(|error| FilePersistenceError::io(stage, error))?;
    if !metadata.file_type().is_file()
        || metadata.uid() != geteuid().as_raw()
        || metadata.mode() & 0o077 != 0
    {
        return Err(FilePersistenceError::UntrustedFile);
    }
    Ok(metadata)
}

fn validate_document(
    document: &BridgePersistenceDocument,
    config: &FilePersistenceConfig,
) -> Result<(), FilePersistenceError> {
    if document.schema != BRIDGE_PERSISTENCE_SCHEMA {
        return Err(FilePersistenceError::UnsupportedSchema);
    }
    if document.policies.len() > config.max_receivers {
        return Err(FilePersistenceError::Capacity);
    }
    if !strictly_ordered_by(&document.policies, |policy| policy.receiver_id.clone()) {
        return Err(FilePersistenceError::NoncanonicalState(
            "restore policies are duplicated or unordered",
        ));
    }
    for policy in &document.policies {
        validate_record_schema(policy.schema_version.get())?;
    }
    if !strictly_ordered_by(&document.stable_entries, |entry| {
        (entry.receiver_id().clone(), entry.device_id().clone())
    }) {
        return Err(FilePersistenceError::NoncanonicalState(
            "stable entries are duplicated or unordered",
        ));
    }
    for entry in &document.stable_entries {
        validate_stable_entry(entry)?;
    }
    if !strictly_ordered_by(&document.restore_records, |record| {
        (record.receiver_id.clone(), record.claim_id.clone())
    }) {
        return Err(FilePersistenceError::NoncanonicalState(
            "restore records are duplicated or unordered",
        ));
    }
    if document
        .restore_records
        .iter()
        .map(|record| &record.claim_id)
        .collect::<BTreeSet<_>>()
        .len()
        != document.restore_records.len()
    {
        return Err(FilePersistenceError::NoncanonicalState(
            "restore claim identities are not globally unique",
        ));
    }
    for record in &document.restore_records {
        validate_restore_record(record)?;
    }
    let receivers = document
        .policies
        .iter()
        .map(|policy| policy.receiver_id.clone())
        .chain(
            document
                .stable_entries
                .iter()
                .map(|entry| entry.receiver_id().clone()),
        )
        .chain(
            document
                .restore_records
                .iter()
                .map(|record| record.receiver_id.clone()),
        )
        .collect::<BTreeSet<_>>();
    if receivers.len() > config.max_receivers {
        return Err(FilePersistenceError::Capacity);
    }
    for receiver_id in receivers {
        if document
            .stable_entries
            .iter()
            .filter(|entry| entry.receiver_id() == &receiver_id)
            .count()
            > MAX_STABLE_ENTRIES_PER_RECEIVER
            || document
                .restore_records
                .iter()
                .filter(|record| record.receiver_id == receiver_id)
                .count()
                > MAX_RESTORE_RECORDS_PER_RECEIVER
        {
            return Err(FilePersistenceError::Capacity);
        }
    }
    Ok(())
}

fn validate_stable_entry(entry: &PersistedStableEntry) -> Result<(), FilePersistenceError> {
    let schema = match entry {
        PersistedStableEntry::Present(intent) => intent.schema_version.get(),
        PersistedStableEntry::Deleted(tombstone) => tombstone.schema_version.get(),
    };
    validate_record_schema(schema)
}

fn validate_restore_record(record: &RestoreRecord) -> Result<(), FilePersistenceError> {
    validate_record_schema(record.schema_version.get())
}

fn validate_record_schema(schema: u16) -> Result<(), FilePersistenceError> {
    if schema == CURRENT_PERSISTENCE_SCHEMA_VERSION {
        Ok(())
    } else {
        Err(FilePersistenceError::UnsupportedSchema)
    }
}

fn canonicalize(document: &mut BridgePersistenceDocument) {
    document
        .policies
        .sort_unstable_by(|left, right| left.receiver_id.cmp(&right.receiver_id));
    document.stable_entries.sort_unstable_by(|left, right| {
        (left.receiver_id(), left.device_id()).cmp(&(right.receiver_id(), right.device_id()))
    });
    document.restore_records.sort_unstable_by(|left, right| {
        (&left.receiver_id, &left.claim_id).cmp(&(&right.receiver_id, &right.claim_id))
    });
}

fn strictly_ordered_by<T, K: Ord>(values: &[T], key: impl Fn(&T) -> K) -> bool {
    values.windows(2).all(|pair| key(&pair[0]) < key(&pair[1]))
}

fn sync_directory(path: &Path) -> Result<(), FilePersistenceError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| FilePersistenceError::io(PersistenceIoStage::SyncDirectory, error))
}
