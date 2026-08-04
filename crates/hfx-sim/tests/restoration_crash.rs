// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{
    RestoreAdvanceResult, RestorePlanResult, RestoreRecord, RestoreRecordStatus, StableLighting,
};
use hfx_domain::{
    ColorChannel, DeviceWriteReadiness, GenerationId, LedCount, RestoreRecordState,
    RestoreTriggerKind, TransactionState,
};
use hfx_protocol::RgbColor;
use hfx_sim::{
    CrashCheckpoint, CrashExecution, SimDeviceProfile, SimJournalState, SimRestorationConfig,
    SimRestorationError, SimRestorationHarness,
};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identifier is valid")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is valid")
}

fn color(red: u8, green: u8, blue: u8) -> RgbColor {
    RgbColor {
        red: ColorChannel::try_from(red).expect("test red is valid"),
        green: ColorChannel::try_from(green).expect("test green is valid"),
        blue: ColorChannel::try_from(blue).expect("test blue is valid"),
    }
}

fn device(id: &str, readiness: DeviceWriteReadiness) -> SimDeviceProfile {
    SimDeviceProfile {
        device_id: text(id),
        profile_id: text(&format!("profile.{id}")),
        profile_digest: text(&"b".repeat(64)),
        application_slot_count: LedCount::try_from(1_u16).expect("test LED count is valid"),
        readiness,
    }
}

fn harness(devices: Vec<SimDeviceProfile>) -> SimRestorationHarness {
    let config = SimRestorationConfig::new(
        text("receiver-1"),
        generation(1),
        text("profile.receiver"),
        text(&"a".repeat(64)),
        devices,
    )
    .expect("test restoration defaults are valid");
    SimRestorationHarness::new(config).expect("test restoration configuration is valid")
}

fn seed(harness: &mut SimRestorationHarness, devices: &[&str]) {
    for device_id in devices {
        harness
            .capture_stable(
                &text(device_id),
                StableLighting::Static(vec![color(1, 2, 3)]),
            )
            .expect("test stable intent is captured");
    }
    harness
        .set_restore_enabled(true)
        .expect("test restoration is enabled");
}

fn trigger(
    harness: &SimRestorationHarness,
    id: &str,
    kind: RestoreTriggerKind,
    target: Option<&str>,
) -> hfx_core::RestoreTrigger {
    harness.trigger(text(id), kind, target.map(text))
}

fn planned_claims(result: RestorePlanResult) -> Vec<RestoreRecord> {
    let RestorePlanResult::Planned(records) = result else {
        panic!("stable intent must produce a restore claim")
    };
    records
}

fn dispatch_queued(
    harness: &mut SimRestorationHarness,
    claim_id: &hfx_domain::RestoreClaimId,
) -> RestoreRecord {
    let advanced = harness
        .advance_claim(claim_id)
        .expect("claim advances to the queue");
    assert!(matches!(advanced, RestoreAdvanceResult::Queued(_)));
    harness
        .dispatch_claim(claim_id)
        .expect("queued claim reaches a terminal record")
}

fn record_state(
    harness: &SimRestorationHarness,
    claim_id: &hfx_domain::RestoreClaimId,
) -> RestoreRecordState {
    harness
        .store()
        .record(claim_id)
        .expect("claim remains durable")
        .status
        .state()
}

#[test]
fn crashes_around_claim_preparation_and_queueing_recover_without_duplicate_writes() {
    for checkpoint in [
        CrashCheckpoint::AfterRestoreRecordCas(RestoreRecordState::Prepared),
        CrashCheckpoint::AfterRestoreRecordCas(RestoreRecordState::Queued),
        CrashCheckpoint::AfterRestoreRecordCas(RestoreRecordState::Applying),
    ] {
        let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
        seed(&mut harness, &["mouse"]);
        let trigger = trigger(
            &harness,
            "trigger-start",
            RestoreTriggerKind::ServiceStart,
            None,
        );
        let claim = planned_claims(
            harness
                .plan_restore(&trigger)
                .expect("restore trigger is planned"),
        )
        .remove(0);

        harness.arm_crash(checkpoint);
        let crashed =
            if checkpoint == CrashCheckpoint::AfterRestoreRecordCas(RestoreRecordState::Applying) {
                let queued = harness
                    .advance_claim(&claim.claim_id)
                    .expect("claim queues before applying crash");
                assert!(matches!(queued, RestoreAdvanceResult::Queued(_)));
                harness
                    .dispatch_claim_crashable(&claim.claim_id)
                    .expect("simulated crash is returned as data")
                    .map_record()
            } else {
                harness
                    .advance_claim_crashable(&claim.claim_id)
                    .expect("simulated crash is returned as data")
                    .map_advance()
            };
        assert_eq!(crashed, checkpoint);
        assert_eq!(harness.process_incarnation(), 2);
        assert_eq!(harness.transport().generation_id(), Some(generation(1)));
        assert_eq!(harness.transport().metrics().physical_dispatches, 0);
        assert_eq!(harness.queued_transactions(), 0);

        let terminal = dispatch_queued(&mut harness, &claim.claim_id);
        assert!(matches!(terminal.status, RestoreRecordStatus::Succeeded(_)));
        assert_eq!(harness.transport().metrics().physical_dispatches, 1);
    }
}

#[test]
fn claim_creation_is_atomic_across_before_and_after_cas_crashes() {
    let mut before = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut before, &["mouse"]);
    let before_trigger = trigger(
        &before,
        "trigger-before",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    before.arm_crash(CrashCheckpoint::BeforeRestoreRecordCas(
        RestoreRecordState::Planned,
    ));
    assert_eq!(
        before
            .plan_restore_crashable(&before_trigger)
            .expect("simulated crash is returned as data"),
        CrashExecution::Crashed(CrashCheckpoint::BeforeRestoreRecordCas(
            RestoreRecordState::Planned
        ))
    );
    assert_eq!(before.store().record_count(), 0);
    assert_eq!(
        planned_claims(
            before
                .plan_restore(&before_trigger)
                .expect("trigger can be retried after a pre-CAS crash")
        )
        .len(),
        1
    );

    let mut after = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut after, &["mouse"]);
    let after_trigger = trigger(
        &after,
        "trigger-after",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    after.arm_crash(CrashCheckpoint::AfterRestoreRecordCas(
        RestoreRecordState::Planned,
    ));
    assert_eq!(
        after
            .plan_restore_crashable(&after_trigger)
            .expect("simulated crash is returned as data"),
        CrashExecution::Crashed(CrashCheckpoint::AfterRestoreRecordCas(
            RestoreRecordState::Planned
        ))
    );
    assert_eq!(after.store().record_count(), 1);
    let replay = planned_claims(
        after
            .plan_restore(&after_trigger)
            .expect("same trigger recovers its durable claim"),
    );
    assert_eq!(replay.len(), 1);
    assert_eq!(replay[0].status.state(), RestoreRecordState::Planned);
}

#[test]
fn terminal_journal_survives_process_crash_and_prevents_a_second_write() {
    let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut harness, &["mouse"]);
    let trigger = trigger(
        &harness,
        "trigger-terminal",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    let claim = planned_claims(
        harness
            .plan_restore(&trigger)
            .expect("restore trigger is planned"),
    )
    .remove(0);
    let queued = harness
        .advance_claim(&claim.claim_id)
        .expect("claim is queued");
    let RestoreAdvanceResult::Queued(queued_record) = queued else {
        panic!("claim must be queued")
    };
    let RestoreRecordStatus::Queued(attempt) = queued_record.status else {
        panic!("queued record must retain the exact attempt")
    };

    harness.arm_crash(CrashCheckpoint::AfterTransportTerminal);
    assert_eq!(
        harness
            .dispatch_claim_crashable(&claim.claim_id)
            .expect("simulated crash is returned as data"),
        CrashExecution::Crashed(CrashCheckpoint::AfterTransportTerminal)
    );
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);
    assert_eq!(
        record_state(&harness, &claim.claim_id),
        RestoreRecordState::Applying
    );
    let journal = harness
        .transport()
        .journal_record(&attempt.request.transaction_id)
        .expect("terminal is durable in the adapter journal");
    assert!(matches!(journal.state, SimJournalState::Retained(_)));
    assert_eq!(journal.dispatch.session_id, attempt.submission.session_id);
    assert_eq!(
        journal.dispatch.authorization_epoch,
        attempt.submission.authorization_epoch
    );
    assert_eq!(
        journal.dispatch.dispatch_nonce,
        attempt.submission.dispatch_nonce
    );
    assert_eq!(journal.dispatch.request_digest, attempt.request_digest);
    assert_eq!(
        journal.dispatch.device_profiles,
        attempt.request.device_profiles
    );
    assert_eq!(journal.dispatch.frames, attempt.request.frames);

    let recovered = harness
        .advance_claim(&claim.claim_id)
        .expect("retained terminal reconciles after restart");
    let RestoreAdvanceResult::Terminal(recovered) = recovered else {
        panic!("retained success must finish the claim")
    };
    let RestoreRecordStatus::Succeeded(completion) = recovered.status else {
        panic!("retained delivered outcome must remain successful")
    };
    assert_eq!(completion.state, TransactionState::Succeeded);
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);
}

#[test]
fn durable_success_survives_lost_process_acknowledgement() {
    let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut harness, &["mouse"]);
    let trigger = trigger(
        &harness,
        "trigger-success-ack",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    let claim = planned_claims(
        harness
            .plan_restore(&trigger)
            .expect("restore trigger is planned"),
    )
    .remove(0);
    let queued = harness
        .advance_claim(&claim.claim_id)
        .expect("claim is queued");
    assert!(matches!(queued, RestoreAdvanceResult::Queued(_)));

    harness.arm_crash(CrashCheckpoint::AfterRestoreRecordCas(
        RestoreRecordState::Succeeded,
    ));
    assert_eq!(
        harness
            .dispatch_claim_crashable(&claim.claim_id)
            .expect("simulated crash is returned as data"),
        CrashExecution::Crashed(CrashCheckpoint::AfterRestoreRecordCas(
            RestoreRecordState::Succeeded
        ))
    );
    assert_eq!(
        record_state(&harness, &claim.claim_id),
        RestoreRecordState::Succeeded
    );
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);

    let replay = harness
        .advance_claim(&claim.claim_id)
        .expect("durable terminal is replayed after lost acknowledgement");
    assert!(matches!(replay, RestoreAdvanceResult::Terminal(_)));
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);
}

#[test]
fn reservation_and_started_write_ambiguity_fail_closed_without_replay() {
    for checkpoint in [
        CrashCheckpoint::AfterTransportReservation,
        CrashCheckpoint::AfterPhysicalWrite,
    ] {
        let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
        seed(&mut harness, &["mouse"]);
        let first_trigger = trigger(
            &harness,
            "trigger-ambiguous",
            RestoreTriggerKind::ServiceStart,
            None,
        );
        let claim = planned_claims(
            harness
                .plan_restore(&first_trigger)
                .expect("restore trigger is planned"),
        )
        .remove(0);
        let queued = harness
            .advance_claim(&claim.claim_id)
            .expect("claim is queued");
        assert!(matches!(queued, RestoreAdvanceResult::Queued(_)));

        harness.arm_crash(checkpoint);
        assert_eq!(
            harness
                .dispatch_claim_crashable(&claim.claim_id)
                .expect("simulated crash is returned as data"),
            CrashExecution::Crashed(checkpoint)
        );
        let writes_at_crash = usize::from(checkpoint == CrashCheckpoint::AfterPhysicalWrite);
        assert_eq!(
            harness.transport().metrics().physical_dispatches,
            u64::try_from(writes_at_crash).expect("test count fits")
        );

        let recovered = harness
            .advance_claim(&claim.claim_id)
            .expect("ambiguous evidence becomes a terminal failure");
        let RestoreAdvanceResult::Terminal(record) = recovered else {
            panic!("ambiguous evidence must terminate rather than replay")
        };
        assert!(matches!(record.status, RestoreRecordStatus::Failed(_)));
        assert_eq!(
            harness.transport().metrics().physical_dispatches,
            u64::try_from(writes_at_crash).expect("test count fits")
        );

        let second_trigger = trigger(
            &harness,
            "trigger-after-ambiguity",
            RestoreTriggerKind::SystemResume,
            None,
        );
        let second_claim = planned_claims(
            harness
                .plan_restore(&second_trigger)
                .expect("a new trigger can be durably named before its safety barrier"),
        )
        .remove(0);
        assert!(matches!(
            harness.advance_claim(&second_claim.claim_id),
            Err(SimRestorationError::Restoration(
                hfx_core::RestorationError::PriorOutcomeUncertain
            ))
        ));
    }
}

#[test]
fn evicted_terminal_is_unknown_and_never_replayed() {
    let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut harness, &["mouse"]);
    let trigger = trigger(
        &harness,
        "trigger-eviction",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    let claim = planned_claims(
        harness
            .plan_restore(&trigger)
            .expect("restore trigger is planned"),
    )
    .remove(0);
    let queued = harness
        .advance_claim(&claim.claim_id)
        .expect("claim queues");
    let RestoreAdvanceResult::Queued(record) = queued else {
        panic!("claim must queue")
    };
    let RestoreRecordStatus::Queued(attempt) = record.status else {
        panic!("queued claim contains an attempt")
    };
    harness.arm_crash(CrashCheckpoint::AfterTransportTerminal);
    let _ = harness
        .dispatch_claim_crashable(&claim.claim_id)
        .expect("simulated crash is returned as data");
    assert!(
        harness
            .transport_mut()
            .evict_terminal(&attempt.request.transaction_id)
    );

    let recovered = harness
        .advance_claim(&claim.claim_id)
        .expect("eviction becomes a terminal unknown outcome");
    assert!(matches!(
        recovered,
        RestoreAdvanceResult::Terminal(RestoreRecord {
            status: RestoreRecordStatus::Failed(_),
            ..
        })
    ));
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);
}

#[test]
fn trigger_identity_is_idempotent_but_a_new_same_generation_trigger_is_real_work() {
    let mut harness = harness(vec![device("mouse", DeviceWriteReadiness::Ready)]);
    seed(&mut harness, &["mouse"]);
    let first_trigger = trigger(
        &harness,
        "trigger-one",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    let first_claim = planned_claims(
        harness
            .plan_restore(&first_trigger)
            .expect("first trigger is planned"),
    )
    .remove(0);
    let first_terminal = dispatch_queued(&mut harness, &first_claim.claim_id);
    assert!(matches!(
        first_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));

    let replay = planned_claims(
        harness
            .plan_restore(&first_trigger)
            .expect("same trigger returns its existing claim"),
    );
    assert_eq!(replay[0].claim_id, first_claim.claim_id);
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);

    let second_trigger = trigger(
        &harness,
        "trigger-two",
        RestoreTriggerKind::SystemResume,
        None,
    );
    let second_claim = planned_claims(
        harness
            .plan_restore(&second_trigger)
            .expect("new same-generation trigger is independently planned"),
    )
    .remove(0);
    assert_ne!(second_claim.claim_id, first_claim.claim_id);
    let second_terminal = dispatch_queued(&mut harness, &second_claim.claim_id);
    assert!(matches!(
        second_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(harness.transport().metrics().physical_dispatches, 2);
}

#[test]
fn sleeping_sibling_does_not_block_ready_device_and_device_return_is_scoped() {
    let mut harness = harness(vec![
        device("mouse", DeviceWriteReadiness::Ready),
        device("keyboard", DeviceWriteReadiness::Sleeping),
    ]);
    seed(&mut harness, &["mouse", "keyboard"]);
    let initial = trigger(
        &harness,
        "trigger-siblings",
        RestoreTriggerKind::ServiceStart,
        None,
    );
    let claims = planned_claims(
        harness
            .plan_restore(&initial)
            .expect("receiver-wide trigger plans both devices"),
    );
    let mouse = claims
        .iter()
        .find(|record| record.device_id.as_str() == "mouse")
        .expect("mouse claim exists")
        .clone();
    let keyboard = claims
        .iter()
        .find(|record| record.device_id.as_str() == "keyboard")
        .expect("keyboard claim exists")
        .clone();

    let deferred = harness
        .advance_claim(&keyboard.claim_id)
        .expect("sleeping keyboard is deferred independently");
    assert!(matches!(deferred, RestoreAdvanceResult::Deferred(_)));
    let mouse_terminal = dispatch_queued(&mut harness, &mouse.claim_id);
    assert!(matches!(
        mouse_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(harness.transport().metrics().physical_dispatches, 1);

    assert!(harness.set_device_readiness(&text("keyboard"), DeviceWriteReadiness::Ready));
    let returned = trigger(
        &harness,
        "trigger-keyboard-return",
        RestoreTriggerKind::DeviceReturn,
        Some("keyboard"),
    );
    let returned_claims = planned_claims(
        harness
            .plan_restore(&returned)
            .expect("device-return trigger is scoped"),
    );
    assert_eq!(returned_claims.len(), 1);
    assert_eq!(returned_claims[0].device_id.as_str(), "keyboard");
    let keyboard_terminal = dispatch_queued(&mut harness, &returned_claims[0].claim_id);
    assert!(matches!(
        keyboard_terminal.status,
        RestoreRecordStatus::Succeeded(_)
    ));
    assert_eq!(harness.transport().metrics().physical_dispatches, 2);
}

trait CrashExecutionExt {
    fn map_record(self) -> CrashCheckpoint;
    fn map_advance(self) -> CrashCheckpoint;
}

impl CrashExecutionExt for CrashExecution<RestoreRecord> {
    fn map_record(self) -> CrashCheckpoint {
        match self {
            CrashExecution::Crashed(checkpoint) => checkpoint,
            CrashExecution::Completed(_) => panic!("fault injection must crash"),
        }
    }

    fn map_advance(self) -> CrashCheckpoint {
        panic!("record execution cannot be mapped as an advance: {self:?}")
    }
}

impl CrashExecutionExt for CrashExecution<RestoreAdvanceResult> {
    fn map_record(self) -> CrashCheckpoint {
        panic!("advance execution cannot be mapped as a record: {self:?}")
    }

    fn map_advance(self) -> CrashCheckpoint {
        match self {
            CrashExecution::Crashed(checkpoint) => checkpoint,
            CrashExecution::Completed(_) => panic!("fault injection must crash"),
        }
    }
}
