// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    BRIDGE_PERSISTENCE_SCHEMA, BridgePersistenceDocument, DurableRestorationRuntime,
    FilePersistenceConfig, FilePersistenceError, FilePersistenceStore, PersistenceCommitter,
    PersistenceIoStage, RestorationRuntime, RestorationSnapshotSource,
};
use hfx_core::{
    CURRENT_PERSISTENCE_SCHEMA_VERSION, CompletedTransaction, MAX_RESTORE_RECORDS_PER_RECEIVER,
    PersistedRestorePolicy, PersistedStableEntry, PersistenceCasOutcome, PersistenceStore,
    RestoreCompletion, RestoreInvalidation, RestoreRecord, RestoreRecordChange,
    RestoreRecordStatus, StableCommitOutcome, StableIntentChange, StableIntentTombstone,
    StableLighting, canonical_request_digest,
};
use hfx_domain::{
    ColorChannel, DeliveredFrameCount, DeviceApplicationState, FrameCount, FrameIndex,
    GenerationId, IntentDigest, IntentRevision, LogicalDeviceId, PersistenceRevision,
    PersistenceSchemaVersion, ProtocolErrorKind, ReceiverId, RequestDigest, RestoreAttemptNumber,
    RestoreClaimId, RestoreInvalidationReason, RestoreState, RestoreTriggerId, RestoreTriggerKind,
    SequenceNumber, SideEffectCertainty, StableLightingMode, TransactionId, TransactionState,
    WallClockUnixMs,
};
use hfx_protocol::{StableLightingIntent, TransactionRequest, TransactionTerminal};
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

fn invalidated_record(receiver_id: ReceiverId, sequence: usize) -> RestoreRecord {
    let mut record = restore_record(receiver_id, &format!("claim-{sequence:04}"));
    record.revision = revision(u64::try_from(sequence + 1).expect("revision fits"));
    record.status = RestoreRecordStatus::Invalidated(RestoreInvalidation {
        reason: RestoreInvalidationReason::StaleGeneration,
    });
    record
}

fn uncertain_failed_record(receiver_id: ReceiverId, sequence: usize) -> RestoreRecord {
    let mut record = restore_record(receiver_id, &format!("failed-{sequence:04}"));
    record.revision = revision(u64::try_from(sequence + 1).expect("revision fits"));
    record.last_attempt = Some(RestoreAttemptNumber::try_from(1_u32).expect("attempt"));
    record.status = RestoreRecordStatus::Failed(RestoreCompletion {
        attempt_number: RestoreAttemptNumber::try_from(1_u32).expect("attempt"),
        transaction_id: id::<TransactionId>(&format!("failed-tx-{sequence:04}")),
        request_digest: id::<RequestDigest>(&"b".repeat(64)),
        state: TransactionState::Failed,
        delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("delivered frames"),
        side_effect_certainty: SideEffectCertainty::Possible,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Unverified,
        error_kind: Some(ProtocolErrorKind::TransportFailure),
    });
    record
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

fn completed_stable_transaction() -> CompletedTransaction {
    let mut request: TransactionRequest = serde_json::from_str(include_str!(
        "../../../protocol/v3/fixtures/transaction-request-canonical.json"
    ))
    .expect("canonical v3 transaction fixture");

    let mut second_profile = request.device_profiles[0].clone();
    second_profile.device_id = id("mouse-2");
    second_profile.profile_id = id("profile.mouse-2");
    request.device_profiles.push(second_profile);
    request.stable_intents.push(StableLightingIntent {
        device_id: id("mouse-2"),
        mode: StableLightingMode::Off,
    });
    let mut second_resource = request.resources[0].clone();
    second_resource.device_id = id("mouse-2");
    request.resources.push(second_resource);
    let mut second_frame = request.frames[0].clone();
    second_frame.device_id = id("mouse-2");
    second_frame.frame_index = FrameIndex::try_from(1_u32).expect("second frame index");
    second_frame.colors[0].red = ColorChannel::try_from(0_u8).expect("black red channel");
    second_frame.colors[0].green = ColorChannel::try_from(0_u8).expect("black green channel");
    second_frame.colors[0].blue = ColorChannel::try_from(0_u8).expect("black blue channel");
    request.frames.push(second_frame);

    let frame_count = u16::try_from(request.frames.len()).expect("test frame count fits");
    let terminal = TransactionTerminal {
        request_id: request.request_id.clone(),
        request_digest: canonical_request_digest(&request).expect("stable request is canonical"),
        transaction_id: request.transaction_id.clone(),
        receiver_id: request.receiver_id.clone(),
        generation_id: request.generation_id,
        state: TransactionState::Succeeded,
        declared_frames: FrameCount::try_from(frame_count).expect("declared frame count"),
        delivered_frames: DeliveredFrameCount::try_from(frame_count)
            .expect("delivered frame count"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Confirmed,
        terminal_sequence: SequenceNumber::try_from(1_u64).expect("terminal sequence"),
        error_kind: None,
        superseded_by: None,
    };
    CompletedTransaction { request, terminal }
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
fn restore_batch_conflict_changes_no_sibling_and_success_is_one_durable_commit() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let receiver_id = receiver(1);
    let first = restore_record(receiver_id.clone(), "claim-01");
    let second = restore_record(receiver_id.clone(), "claim-02");
    let mut store = FilePersistenceStore::open(config.clone()).expect("store opens");
    assert_eq!(
        store
            .compare_and_set_restore_records(&[
                RestoreRecordChange {
                    expected_revision: None,
                    record: first.clone(),
                },
                RestoreRecordChange {
                    expected_revision: None,
                    record: second.clone(),
                },
            ])
            .expect("initial restore batch commits"),
        PersistenceCasOutcome::Applied
    );

    let retire = |mut record: RestoreRecord| {
        record.revision = revision(2);
        record.status = RestoreRecordStatus::Invalidated(RestoreInvalidation {
            reason: RestoreInvalidationReason::StaleGeneration,
        });
        record
    };
    let retired_first = retire(first);
    let retired_second = retire(second);
    let before = store.document().clone();
    assert_eq!(
        store
            .compare_and_set_restore_records(&[
                RestoreRecordChange {
                    expected_revision: Some(revision(1)),
                    record: retired_first.clone(),
                },
                RestoreRecordChange {
                    expected_revision: None,
                    record: retired_second.clone(),
                },
            ])
            .expect("revision conflict is data"),
        PersistenceCasOutcome::Conflict
    );
    assert_eq!(store.document(), &before);

    assert_eq!(
        store
            .compare_and_set_restore_records(&[
                RestoreRecordChange {
                    expected_revision: Some(revision(1)),
                    record: retired_first.clone(),
                },
                RestoreRecordChange {
                    expected_revision: Some(revision(1)),
                    record: retired_second.clone(),
                },
            ])
            .expect("retirement batch commits"),
        PersistenceCasOutcome::Applied
    );
    drop(store);

    let reopened = FilePersistenceStore::open(config).expect("durable state reopens");
    assert_eq!(
        reopened
            .restore_records(&receiver_id)
            .expect("restore records load"),
        vec![retired_first, retired_second]
    );
}

#[test]
fn terminal_restore_history_compacts_atomically_and_never_becomes_permanently_full() {
    let directory = TestDirectory::new();
    let path = directory.state_path();
    let config = FilePersistenceConfig::new(&path);
    let receiver_id = receiver(1);
    let mut store = FilePersistenceStore::open(config.clone()).expect("store opens");
    let initial = (0..MAX_RESTORE_RECORDS_PER_RECEIVER)
        .map(|sequence| {
            let record = invalidated_record(receiver_id.clone(), sequence);
            RestoreRecordChange {
                expected_revision: None,
                record,
            }
        })
        .collect::<Vec<_>>();
    assert_eq!(
        store
            .compare_and_set_restore_records(&initial)
            .expect("bounded history initializes"),
        PersistenceCasOutcome::Applied
    );

    for sequence in MAX_RESTORE_RECORDS_PER_RECEIVER..(MAX_RESTORE_RECORDS_PER_RECEIVER + 64) {
        let record = invalidated_record(receiver_id.clone(), sequence);
        assert_eq!(
            store
                .compare_and_set_restore_record(None, &record)
                .expect("old terminal history compacts"),
            PersistenceCasOutcome::Applied
        );
    }

    let records = store
        .restore_records(&receiver_id)
        .expect("compacted records load");
    assert_eq!(records.len(), MAX_RESTORE_RECORDS_PER_RECEIVER);
    assert!(
        records
            .iter()
            .all(|record| record.claim_id.as_str() != "claim-0000")
    );
    assert!(records.iter().any(|record| {
        record.claim_id.as_str() == format!("claim-{:04}", MAX_RESTORE_RECORDS_PER_RECEIVER + 63)
    }));
    drop(store);

    let reopened = FilePersistenceStore::open(config).expect("compacted state reopens");
    assert_eq!(
        reopened
            .restore_records(&receiver_id)
            .expect("reopened history loads")
            .len(),
        MAX_RESTORE_RECORDS_PER_RECEIVER
    );
}

#[test]
fn compaction_preserves_nonterminal_and_uncertain_failure_records() {
    let directory = TestDirectory::new();
    let receiver_id = receiver(1);
    let mut store = FilePersistenceStore::open(FilePersistenceConfig::new(directory.state_path()))
        .expect("store opens");
    let compactable_count = MAX_RESTORE_RECORDS_PER_RECEIVER - 32;
    let mut initial = (0..compactable_count)
        .map(|sequence| RestoreRecordChange {
            expected_revision: None,
            record: invalidated_record(receiver_id.clone(), sequence),
        })
        .collect::<Vec<_>>();
    initial.extend((0..32).map(|sequence| RestoreRecordChange {
        expected_revision: None,
        record: uncertain_failed_record(receiver_id.clone(), sequence),
    }));
    assert_eq!(
        store
            .compare_and_set_restore_records(&initial)
            .expect("mixed history initializes"),
        PersistenceCasOutcome::Applied
    );

    let pending = restore_record(receiver_id.clone(), "pending-new");
    assert_eq!(
        store
            .compare_and_set_restore_record(None, &pending)
            .expect("one compactable terminal is retired"),
        PersistenceCasOutcome::Applied
    );
    let records = store.restore_records(&receiver_id).expect("records load");
    assert_eq!(records.len(), MAX_RESTORE_RECORDS_PER_RECEIVER);
    assert!(
        records
            .iter()
            .any(|record| record.claim_id == pending.claim_id)
    );
    for sequence in 0..32 {
        let claim = format!("failed-{sequence:04}");
        assert!(
            records
                .iter()
                .any(|record| record.claim_id.as_str() == claim),
            "uncertain record {claim} must remain"
        );
    }
}

#[test]
fn durable_runtime_projects_policy_and_generation_bound_restore_truth() {
    let directory = TestDirectory::new();
    let receiver_id = receiver(1);
    let mut store = FilePersistenceStore::open(FilePersistenceConfig::new(directory.state_path()))
        .expect("store opens");
    assert_eq!(
        store
            .compare_and_set_restore_policy(None, &policy(receiver_id.clone(), 1, true))
            .expect("policy commits"),
        PersistenceCasOutcome::Applied
    );
    assert_eq!(
        store
            .compare_and_set_restore_record(None, &restore_record(receiver_id.clone(), "claim-01"))
            .expect("restore claim commits"),
        PersistenceCasOutcome::Applied
    );

    let runtime = DurableRestorationRuntime::new(store);
    assert_eq!(
        runtime
            .restoration(
                &receiver_id,
                GenerationId::try_from(1_u64).expect("test generation")
            )
            .expect("restoration snapshot projects"),
        hfx_bridge::ReceiverRestorationSnapshot {
            stable_restore_enabled: true,
            restore_state: RestoreState::Planned,
        }
    );
    assert_eq!(
        runtime
            .restoration(
                &receiver_id,
                GenerationId::try_from(2_u64).expect("test generation")
            )
            .expect("unclaimed generation projects idle"),
        hfx_bridge::ReceiverRestorationSnapshot {
            stable_restore_enabled: true,
            restore_state: RestoreState::Idle,
        }
    );
}

#[test]
fn durable_runtime_captures_static_and_off_once_across_process_reopen() {
    let directory = TestDirectory::new();
    let config = FilePersistenceConfig::new(directory.state_path());
    let completed = completed_stable_transaction();
    let first_time = WallClockUnixMs::try_from(10_u64).expect("first wall clock");
    let replay_time = WallClockUnixMs::try_from(999_u64).expect("replay wall clock");

    let store = FilePersistenceStore::open(config.clone()).expect("store opens");
    let mut runtime = DurableRestorationRuntime::new(store);
    let first = runtime
        .capture_completed(&completed, first_time)
        .expect("stable completion persists");
    let StableCommitOutcome::Captured(first) = first else {
        panic!("stable completion must capture")
    };
    assert_eq!(first.len(), 2);
    assert!(matches!(first[0].lighting, StableLighting::Static(_)));
    assert_eq!(first[1].lighting, StableLighting::Off);
    assert!(first.iter().all(|intent| intent.revision.get() == 1));
    assert!(first.iter().all(|intent| intent.captured_at == first_time));
    drop(runtime);

    let reopened = FilePersistenceStore::open(config.clone()).expect("durable state reopens");
    let durable_before_replay = reopened
        .stable_entries(&completed.request.receiver_id)
        .expect("stable entries reload");
    assert_eq!(durable_before_replay.len(), 2);
    let mut restarted_runtime = DurableRestorationRuntime::new(reopened);
    let replay = restarted_runtime
        .capture_completed(&completed, replay_time)
        .expect("exact replay is idempotent");
    assert_eq!(replay, StableCommitOutcome::Captured(first));
    drop(restarted_runtime);

    let final_store = FilePersistenceStore::open(config).expect("final state reopens");
    assert_eq!(
        final_store
            .stable_entries(&completed.request.receiver_id)
            .expect("stable entries remain durable"),
        durable_before_replay
    );
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
