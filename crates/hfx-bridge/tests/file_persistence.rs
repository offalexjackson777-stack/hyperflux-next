// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    BRIDGE_PERSISTENCE_SCHEMA, BridgePersistenceDocument, FilePersistenceConfig,
    FilePersistenceError, FilePersistenceStore, PersistenceCommitter, PersistenceIoStage,
};
use hfx_core::{
    CURRENT_PERSISTENCE_SCHEMA_VERSION, PersistedRestorePolicy, PersistedStableEntry,
    PersistenceCasOutcome, PersistenceStore, RestoreRecord, RestoreRecordStatus,
    StableIntentChange, StableIntentTombstone,
};
use hfx_domain::{
    GenerationId, IntentDigest, IntentRevision, LogicalDeviceId, PersistenceRevision,
    PersistenceSchemaVersion, ReceiverId, RestoreClaimId, RestoreTriggerId, RestoreTriggerKind,
    WallClockUnixMs,
};
use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(1);

#[derive(Debug)]
struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new() -> Self {
        let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "hyperflux-next-persistence-{}-{sequence}",
            std::process::id()
        ));
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700);
        builder.create(&path).expect("private test directory");
        Self(path)
    }

    fn state_path(&self) -> PathBuf {
        self.0.join("bridge-state.json")
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[derive(Debug)]
struct RejectCommitter;

impl PersistenceCommitter for RejectCommitter {
    fn commit(&mut self, _path: &Path, _bytes: &[u8]) -> Result<(), FilePersistenceError> {
        Err(FilePersistenceError::Io {
            stage: PersistenceIoStage::ReplaceState,
            kind: std::io::ErrorKind::Other,
            replacement_visible: false,
        })
    }
}

#[derive(Debug, Default)]
struct VisibleThenRejectCommitter(hfx_bridge::AtomicFileCommitter);

impl PersistenceCommitter for VisibleThenRejectCommitter {
    fn commit(&mut self, path: &Path, bytes: &[u8]) -> Result<(), FilePersistenceError> {
        self.0.commit(path, bytes)?;
        Err(FilePersistenceError::Io {
            stage: PersistenceIoStage::SyncDirectory,
            kind: std::io::ErrorKind::Other,
            replacement_visible: true,
        })
    }
}

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identifier")
}

fn schema_version() -> PersistenceSchemaVersion {
    PersistenceSchemaVersion::try_from(CURRENT_PERSISTENCE_SCHEMA_VERSION)
        .expect("current schema version")
}

fn receiver(sequence: usize) -> ReceiverId {
    id(&format!("receiver-{sequence:02}"))
}

fn revision(value: u64) -> PersistenceRevision {
    PersistenceRevision::try_from(value).expect("test persistence revision")
}

fn policy(receiver_id: ReceiverId, value: u64, enabled: bool) -> PersistedRestorePolicy {
    PersistedRestorePolicy {
        schema_version: schema_version(),
        receiver_id,
        enabled,
        revision: revision(value),
    }
}

fn tombstone(receiver_id: ReceiverId, sequence: usize) -> PersistedStableEntry {
    PersistedStableEntry::Deleted(StableIntentTombstone {
        schema_version: schema_version(),
        receiver_id,
        device_id: id(&format!("device-{sequence:02}")),
        revision: IntentRevision::try_from(1_u64).expect("test intent revision"),
        previous_content_digest: None,
        deleted_at: WallClockUnixMs::try_from(1_u64).expect("test wall clock"),
    })
}

fn restore_record(receiver_id: ReceiverId, claim: &str) -> RestoreRecord {
    RestoreRecord {
        schema_version: schema_version(),
        claim_id: id::<RestoreClaimId>(claim),
        trigger_id: id::<RestoreTriggerId>(&format!("trigger-{claim}")),
        trigger_kind: RestoreTriggerKind::ServiceStart,
        receiver_id,
        generation_id: GenerationId::try_from(1_u64).expect("test generation"),
        device_id: id::<LogicalDeviceId>(&format!("device-{claim}")),
        intent_revision: IntentRevision::try_from(1_u64).expect("test intent revision"),
        intent_digest: id::<IntentDigest>(&"a".repeat(64)),
        revision: revision(1),
        last_attempt: None,
        status: RestoreRecordStatus::Planned,
    }
}

fn write_private(path: &Path, bytes: &[u8]) {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .expect("private test file");
    file.write_all(bytes).expect("test file write");
    file.sync_all().expect("test file sync");
}

#[test]
fn policy_commit_is_private_durable_and_exclusively_locked() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let receiver_id = receiver(1);
    let expected = policy(receiver_id.clone(), 1, true);

    let mut store = FilePersistenceStore::open(config.clone()).expect("new store opens");
    assert_eq!(
        store
            .compare_and_set_restore_policy(None, &expected)
            .expect("policy commit"),
        PersistenceCasOutcome::Applied
    );
    assert_eq!(
        fs::metadata(&path)
            .expect("state metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        FilePersistenceStore::open(config.clone()).expect_err("second writer is excluded"),
        FilePersistenceError::AlreadyLocked
    );

    drop(store);
    let reopened = FilePersistenceStore::open(config).expect("durable state reopens");
    assert_eq!(
        reopened
            .restore_policy(&receiver_id)
            .expect("policy lookup"),
        Some(expected)
    );
}

#[test]
fn failed_commit_changes_neither_memory_nor_existing_disk_state() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let receiver_id = receiver(1);
    let original = policy(receiver_id.clone(), 1, false);
    {
        let mut seed = FilePersistenceStore::open(config.clone()).expect("seed store");
        seed.compare_and_set_restore_policy(None, &original)
            .expect("seed policy");
    }
    let before = fs::read(&path).expect("seed bytes");
    let mut store = FilePersistenceStore::open_with_committer(config, RejectCommitter)
        .expect("failing store opens");
    let replacement = policy(receiver_id.clone(), 2, true);

    assert!(
        store
            .compare_and_set_restore_policy(Some(revision(1)), &replacement)
            .is_err()
    );
    assert_eq!(
        store
            .restore_policy(&receiver_id)
            .expect("in-memory policy"),
        Some(original)
    );
    assert_eq!(fs::read(&path).expect("state bytes"), before);
}

#[test]
fn visible_replacement_advances_cas_view_but_reports_uncertain_durability() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let receiver_id = receiver(1);
    let expected = policy(receiver_id.clone(), 1, true);
    let mut store = FilePersistenceStore::open_with_committer(
        config.clone(),
        VisibleThenRejectCommitter::default(),
    )
    .expect("store opens");

    let error = store
        .compare_and_set_restore_policy(None, &expected)
        .expect_err("directory durability is reported");
    assert!(error.replacement_visible());
    assert_eq!(
        store
            .restore_policy(&receiver_id)
            .expect("visible in-memory policy"),
        Some(expected.clone())
    );
    assert_eq!(
        store
            .compare_and_set_restore_policy(None, &expected)
            .expect("stale retry is data"),
        PersistenceCasOutcome::Conflict
    );

    drop(store);
    let reopened = FilePersistenceStore::open(config).expect("visible state reopens");
    assert_eq!(
        reopened
            .restore_policy(&receiver_id)
            .expect("durable policy lookup"),
        Some(expected)
    );
}

#[test]
fn stable_batch_is_atomic_and_capacity_is_per_receiver() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let mut store = FilePersistenceStore::open(config).expect("store opens");
    let first_receiver = receiver(1);
    let second_receiver = receiver(2);
    let first_batch = (0..32)
        .map(|sequence| StableIntentChange {
            expected_revision: None,
            entry: tombstone(first_receiver.clone(), sequence),
        })
        .collect::<Vec<_>>();
    let second_batch = (0..32)
        .map(|sequence| StableIntentChange {
            expected_revision: None,
            entry: tombstone(second_receiver.clone(), sequence),
        })
        .collect::<Vec<_>>();
    assert_eq!(
        store
            .compare_and_set_stable_entries(&first_batch)
            .expect("first receiver batch"),
        PersistenceCasOutcome::Applied
    );
    assert_eq!(
        store
            .compare_and_set_stable_entries(&second_batch)
            .expect("second receiver batch"),
        PersistenceCasOutcome::Applied
    );

    let before = store.document().clone();
    let conflicting = [
        StableIntentChange {
            expected_revision: Some(IntentRevision::try_from(1_u64).expect("revision")),
            entry: tombstone(first_receiver.clone(), 0),
        },
        StableIntentChange {
            expected_revision: None,
            entry: tombstone(first_receiver.clone(), 31),
        },
    ];
    assert_eq!(
        store
            .compare_and_set_stable_entries(&conflicting)
            .expect("conflict is data"),
        PersistenceCasOutcome::Conflict
    );
    assert_eq!(store.document(), &before);

    let overflow = [StableIntentChange {
        expected_revision: None,
        entry: tombstone(first_receiver, 32),
    }];
    assert_eq!(
        store
            .compare_and_set_stable_entries(&overflow)
            .expect_err("receiver capacity is enforced"),
        FilePersistenceError::Capacity
    );
    assert_eq!(store.document(), &before);
}

#[test]
fn malformed_oversized_and_permissive_state_fail_closed() {
    let malformed_directory = TestDirectory::new();
    let malformed_path = malformed_directory.state_path();
    write_private(
        &malformed_path,
        br#"{"schema":"hyperflux-bridge-persistence-v1","policies":[],"stable_entries":[],"restore_records":[],"extra":true}"#,
    );
    assert_eq!(
        FilePersistenceStore::open(FilePersistenceConfig::new(&malformed_path))
            .expect_err("unknown field is rejected"),
        FilePersistenceError::MalformedState
    );

    let oversized_directory = TestDirectory::new();
    let oversized_path = oversized_directory.state_path();
    write_private(&oversized_path, &[b'x'; 64]);
    let mut oversized_config = FilePersistenceConfig::new(&oversized_path);
    oversized_config.max_bytes = 32;
    assert_eq!(
        FilePersistenceStore::open(oversized_config).expect_err("oversized state is rejected"),
        FilePersistenceError::StateTooLarge
    );

    let permissive_directory = TestDirectory::new();
    let permissive_path = permissive_directory.state_path();
    let empty = serde_json::to_vec(&BridgePersistenceDocument::default()).expect("empty document");
    write_private(&permissive_path, &empty);
    fs::set_permissions(&permissive_path, fs::Permissions::from_mode(0o644))
        .expect("set permissive mode");
    assert_eq!(
        FilePersistenceStore::open(FilePersistenceConfig::new(&permissive_path))
            .expect_err("permissive state is rejected"),
        FilePersistenceError::UntrustedFile
    );
}

#[test]
fn state_directory_and_lock_symlinks_never_enter_the_trust_boundary() {
    let state_directory = TestDirectory::new();
    let outside = TestDirectory::new();
    let outside_state = outside.state_path();
    let empty = serde_json::to_vec(&BridgePersistenceDocument::default()).expect("empty document");
    write_private(&outside_state, &empty);
    let state_link = state_directory.state_path();
    symlink(&outside_state, &state_link).expect("state symlink");
    let state_error = FilePersistenceStore::open(FilePersistenceConfig::new(&state_link))
        .expect_err("state symlink is rejected");
    assert!(matches!(
        state_error,
        FilePersistenceError::Io {
            stage: PersistenceIoStage::OpenState,
            ..
        }
    ));
    assert!(!state_error.to_string().contains("/tmp/"));

    let lock_directory = TestDirectory::new();
    let lock_target = outside.0.join("lock-target");
    write_private(&lock_target, b"lock");
    let lock_path = lock_directory.0.join(".bridge-state.json.lock");
    symlink(&lock_target, &lock_path).expect("lock symlink");
    let lock_error =
        FilePersistenceStore::open(FilePersistenceConfig::new(lock_directory.state_path()))
            .expect_err("lock symlink is rejected");
    assert!(matches!(
        lock_error,
        FilePersistenceError::Io {
            stage: PersistenceIoStage::OpenLock,
            ..
        }
    ));

    let parent_holder = TestDirectory::new();
    let linked_parent = parent_holder.0.join("linked-state");
    symlink(&outside.0, &linked_parent).expect("parent symlink");
    assert_eq!(
        FilePersistenceStore::open(FilePersistenceConfig::new(
            linked_parent.join("bridge-state.json")
        ))
        .expect_err("parent symlink is rejected"),
        FilePersistenceError::UntrustedDirectory
    );
}

#[test]
fn document_rejects_unknown_schema_and_duplicate_claim_identity() {
    let schema_directory = TestDirectory::new();
    let schema_path = schema_directory.state_path();
    let unknown = BridgePersistenceDocument {
        schema: "hyperflux-bridge-persistence-v999".to_owned(),
        ..BridgePersistenceDocument::default()
    };
    write_private(
        &schema_path,
        &serde_json::to_vec(&unknown).expect("unknown schema document"),
    );
    assert_eq!(
        FilePersistenceStore::open(FilePersistenceConfig::new(&schema_path))
            .expect_err("unknown schema is rejected"),
        FilePersistenceError::UnsupportedSchema
    );

    let claims_directory = TestDirectory::new();
    let claims_path = claims_directory.state_path();
    let duplicate = BridgePersistenceDocument {
        schema: BRIDGE_PERSISTENCE_SCHEMA.to_owned(),
        policies: Vec::new(),
        stable_entries: Vec::new(),
        restore_records: vec![
            restore_record(receiver(1), "claim-1"),
            restore_record(receiver(2), "claim-1"),
        ],
    };
    write_private(
        &claims_path,
        &serde_json::to_vec(&duplicate).expect("duplicate claim document"),
    );
    assert_eq!(
        FilePersistenceStore::open(FilePersistenceConfig::new(&claims_path))
            .expect_err("claim identity is globally unique"),
        FilePersistenceError::NoncanonicalState("restore claim identities are not globally unique")
    );
}
