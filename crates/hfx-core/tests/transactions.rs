// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{generation, text, time, transaction_request};
use hfx_core::{
    BoundedOutcomeJournal, BoundedTransactionQueue, OutcomeJournalError, OutcomeLookup,
    QueuedTransaction, RequestReplay, TransactionMachine, TransactionQueueError,
    TransactionTransitionError, canonical_request_digest,
};
use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DispatchNonce, FrameCount,
    QueueAdmission, SideEffectCertainty, TransactionClass, TransactionState,
};
use hfx_protocol::{
    TransactionProgress, TransactionRequest, TransactionResult, TransactionTerminal,
};

fn request(id: &str, class: TransactionClass, deadline: u64) -> TransactionRequest {
    transaction_request(id, class, deadline, &["mouse-1"])
}

fn queued(id: &str, class: TransactionClass, deadline: u64) -> QueuedTransaction {
    QueuedTransaction {
        request: request(id, class, deadline),
        request_digest: text(&"a".repeat(64)),
        session_id: text("session-1"),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64)
            .expect("authorization epoch is valid"),
        dispatch_nonce: DispatchNonce::try_from(id.bytes().map(u64::from).sum::<u64>().max(1))
            .expect("dispatch nonce is valid"),
        admission: QueueAdmission::Enqueued,
    }
}

fn progress(id: &str, digest: char) -> TransactionResult {
    TransactionResult::Progress(TransactionProgress {
        request_id: text(&format!("request-{id}")),
        request_digest: text(&digest.to_string().repeat(64)),
        transaction_id: text(&format!("transaction-{id}")),
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        state: TransactionState::Queued,
        admission: QueueAdmission::Enqueued,
        declared_frames: FrameCount::try_from(1_u16).expect("frame count is valid"),
        delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("delivered count is valid"),
        side_effect_certainty: SideEffectCertainty::None,
        live_write_executed: false,
    })
}

fn terminal(id: &str, digest: char, state: TransactionState) -> TransactionResult {
    TransactionResult::Terminal(TransactionTerminal {
        request_id: text(&format!("request-{id}")),
        request_digest: text(&digest.to_string().repeat(64)),
        transaction_id: text(&format!("transaction-{id}")),
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        state,
        declared_frames: FrameCount::try_from(1_u16).expect("frame count is valid"),
        delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("delivered count is valid"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Confirmed,
        terminal_sequence: hfx_domain::SequenceNumber::try_from(1_u64).expect("sequence is valid"),
        error_kind: None,
        superseded_by: None,
    })
}

fn sent_progress(id: &str, digest: char) -> TransactionResult {
    let mut result = progress(id, digest);
    let TransactionResult::Progress(progress) = &mut result else {
        unreachable!("test helper creates progress")
    };
    progress.state = TransactionState::Sent;
    result
}

#[test]
fn transaction_state_machine_cannot_skip_authority_or_leave_terminal_state() {
    let mut machine = TransactionMachine::default();
    assert_eq!(
        machine.advance(TransactionState::Queued),
        Err(TransactionTransitionError::InvalidTransition {
            from: TransactionState::Created,
            to: TransactionState::Queued,
        })
    );
    for state in [
        TransactionState::Validated,
        TransactionState::OwnershipBound,
        TransactionState::GenerationBound,
        TransactionState::Queued,
        TransactionState::Sent,
        TransactionState::HealthPending,
        TransactionState::Succeeded,
    ] {
        machine.advance(state).expect("declared edge is valid");
    }
    assert!(machine.is_terminal());
    assert!(machine.advance(TransactionState::Failed).is_err());
}

#[test]
fn obsolete_unsent_effect_frame_is_coalesced_even_when_queue_is_full() {
    let mut queue = BoundedTransactionQueue::new(1).expect("capacity is valid");
    queue
        .admit(queued("old", TransactionClass::EffectFrame, 100), time(1))
        .expect("first effect is admitted");
    let decision = queue
        .admit(queued("new", TransactionClass::EffectFrame, 100), time(2))
        .expect("new effect supersedes obsolete unsent work");
    assert_eq!(decision.admission, QueueAdmission::Coalesced);
    assert_eq!(
        decision
            .superseded
            .expect("coalescing reports the replaced request")
            .request
            .transaction_id
            .as_str(),
        "transaction-old"
    );
    assert_eq!(queue.len(), 1);
    assert_eq!(
        queue
            .take_next(time(3))
            .next
            .expect("new effect remains queued")
            .request
            .transaction_id
            .as_str(),
        "transaction-new"
    );
}

#[test]
fn stable_transactions_are_never_coalesced_and_deadlines_fail_closed() {
    let mut queue = BoundedTransactionQueue::new(1).expect("capacity is valid");
    queue
        .admit(
            queued("static-1", TransactionClass::StaticLighting, 100),
            time(1),
        )
        .expect("first stable transaction is admitted");
    assert_eq!(
        queue.admit(
            queued("static-2", TransactionClass::StaticLighting, 100),
            time(2)
        ),
        Err(TransactionQueueError::Full)
    );

    let mut empty = BoundedTransactionQueue::new(1).expect("capacity is valid");
    assert_eq!(
        empty.admit(queued("late", TransactionClass::EffectFrame, 10), time(10)),
        Err(TransactionQueueError::DeadlineElapsed)
    );
}

#[test]
fn dequeue_and_invalidation_return_every_removed_transaction_explicitly() {
    let mut queue = BoundedTransactionQueue::new(4).expect("capacity is valid");
    queue
        .admit(
            queued("expired", TransactionClass::StaticLighting, 5),
            time(1),
        )
        .expect("future deadline is admitted");
    queue
        .admit(
            queued("live", TransactionClass::StaticLighting, 20),
            time(1),
        )
        .expect("second transaction is admitted");
    let decision = queue.take_next(time(5));
    assert_eq!(decision.expired.len(), 1);
    assert_eq!(
        decision
            .next
            .expect("live transaction is returned")
            .request
            .transaction_id
            .as_str(),
        "transaction-live"
    );

    queue
        .admit(
            queued("generation", TransactionClass::StaticLighting, 30),
            time(6),
        )
        .expect("generation transaction is admitted");
    assert_eq!(
        queue
            .invalidate_generation(&text("receiver-1"), generation(1))
            .len(),
        1
    );
    assert!(queue.is_empty());
}

#[test]
fn outcome_journal_never_evicts_active_work_and_remembers_terminal_eviction() {
    let mut journal = BoundedOutcomeJournal::new(1).expect("capacity is valid");
    journal
        .record(text("client-1"), progress("one", 'a'))
        .expect("progress fits");
    assert_eq!(
        journal.record(text("client-1"), progress("two", 'b')),
        Err(OutcomeJournalError::CapacityExhausted)
    );
    journal
        .record(text("client-1"), sent_progress("one", 'a'))
        .expect("sent progress advances queued work");
    journal
        .record(
            text("client-1"),
            terminal("one", 'a', TransactionState::Succeeded),
        )
        .expect("same transaction reaches a terminal outcome");
    journal
        .record(text("client-1"), progress("two", 'b'))
        .expect("terminal record is evicted for new active work");
    assert_eq!(
        journal.lookup(&text("client-1"), &text("transaction-one")),
        OutcomeLookup::Evicted
    );
    assert!(matches!(
        journal.lookup(&text("client-1"), &text("transaction-two")),
        OutcomeLookup::Retained(_)
    ));
    assert_eq!(
        journal.lookup(&text("client-1"), &text("transaction-never-seen")),
        OutcomeLookup::Unknown
    );
    assert_eq!(
        journal.lookup(&text("client-2"), &text("transaction-two")),
        OutcomeLookup::Forbidden
    );
}

#[test]
fn terminal_outcome_is_immutable_and_request_digest_cannot_change() {
    let mut journal = BoundedOutcomeJournal::new(2).expect("capacity is valid");
    let succeeded = terminal("one", 'a', TransactionState::Succeeded);
    journal
        .record(text("client-1"), succeeded.clone())
        .expect("terminal result fits");
    journal
        .record(text("client-1"), succeeded)
        .expect("identical terminal replay is idempotent");
    assert_eq!(
        journal.record(
            text("client-1"),
            terminal("one", 'a', TransactionState::Failed)
        ),
        Err(OutcomeJournalError::TerminalOutcomeChanged)
    );

    let mut active = BoundedOutcomeJournal::new(2).expect("capacity is valid");
    active
        .record(text("client-1"), progress("one", 'a'))
        .expect("progress fits");
    assert_eq!(
        active.record(text("client-1"), progress("one", 'b')),
        Err(OutcomeJournalError::IdentityChanged)
    );
}

#[test]
fn request_replay_is_owner_scoped_exact_and_bounded() {
    let mut journal = BoundedOutcomeJournal::new(1).expect("capacity is valid");
    journal
        .record(text("client-1"), progress("one", 'a'))
        .expect("progress fits");
    let digest = text(&"a".repeat(64));
    assert!(matches!(
        journal.replay(&text("client-1"), &text("request-one"), &digest),
        RequestReplay::Retained(_)
    ));
    assert_eq!(
        journal.replay(
            &text("client-1"),
            &text("request-one"),
            &text(&"b".repeat(64))
        ),
        RequestReplay::Conflict
    );
    assert_eq!(
        journal.replay(&text("client-2"), &text("request-one"), &digest),
        RequestReplay::Unknown
    );

    journal
        .record(text("client-1"), sent_progress("one", 'a'))
        .expect("sent progress advances queued work");
    journal
        .record(
            text("client-1"),
            terminal("one", 'a', TransactionState::Succeeded),
        )
        .expect("terminal update fits");
    journal
        .record(text("client-1"), progress("two", 'b'))
        .expect("terminal result can be evicted");
    assert!(matches!(
        journal.replay(&text("client-1"), &text("request-one"), &digest),
        RequestReplay::Evicted(transaction_id)
            if transaction_id.as_str() == "transaction-one"
    ));
}

#[test]
fn nonterminal_outcomes_cannot_regress_or_skip_transport() {
    let mut journal = BoundedOutcomeJournal::new(2).expect("capacity is valid");
    journal
        .record(text("client-1"), progress("one", 'a'))
        .expect("queued progress fits");
    assert_eq!(
        journal.record(
            text("client-1"),
            terminal("one", 'a', TransactionState::Succeeded)
        ),
        Err(OutcomeJournalError::InvalidProgression)
    );
    journal
        .record(text("client-1"), sent_progress("one", 'a'))
        .expect("sent progress is valid");
    assert_eq!(
        journal.record(text("client-1"), progress("one", 'a')),
        Err(OutcomeJournalError::InvalidProgression)
    );
}

#[test]
fn request_digest_is_stable_and_changes_with_semantic_content() {
    let first = request("digest", TransactionClass::StaticLighting, 100);
    let repeat = request("digest", TransactionClass::StaticLighting, 100);
    let changed = request("digest", TransactionClass::StaticLighting, 101);
    let encoded = serde_json::to_string(&first).expect("request is serializable");
    assert_eq!(
        encoded,
        include_str!("../../../protocol/v3/fixtures/transaction-request-canonical.json").trim_end()
    );
    let first_digest = canonical_request_digest(&first).expect("request is digestible");
    assert_eq!(
        first_digest,
        canonical_request_digest(&repeat).expect("same request is digestible")
    );
    assert_ne!(
        first_digest,
        canonical_request_digest(&changed).expect("changed request is digestible")
    );
    assert_eq!(first_digest.as_str().len(), 64);
    assert_eq!(
        first_digest.as_str(),
        "bdfd4e6bb641d001ba5cc32e1ad0ee1288885deb368002bbb73b58d2efa9b2cf"
    );
    assert!(
        first_digest
            .as_str()
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    );
}
