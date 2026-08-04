// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{generation, text, time, transaction_request};
use hfx_core::{SubmissionBinding, VolatileEffectRegistry, canonical_request_digest};
use hfx_domain::{
    AuthorizationEpoch, DeliveredFrameCount, DeviceApplicationState, DispatchNonce, FrameCount,
    SequenceNumber, SideEffectCertainty, TransactionClass, TransactionState,
};
use hfx_protocol::{TransactionRequest, TransactionTerminal, validate_transaction};

fn binding(session: &str, nonce: u64) -> SubmissionBinding {
    SubmissionBinding {
        session_id: text(session),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("authorization epoch"),
        dispatch_nonce: DispatchNonce::try_from(nonce).expect("dispatch nonce"),
    }
}

fn successful_terminal(request: &TransactionRequest) -> TransactionTerminal {
    let frames = u16::try_from(request.frames.len()).expect("frame count fits");
    TransactionTerminal {
        request_id: request.request_id.clone(),
        request_digest: canonical_request_digest(request).expect("request is canonical"),
        transaction_id: request.transaction_id.clone(),
        receiver_id: request.receiver_id.clone(),
        generation_id: request.generation_id,
        state: TransactionState::Succeeded,
        declared_frames: FrameCount::try_from(frames).expect("declared frame count"),
        delivered_frames: DeliveredFrameCount::try_from(frames).expect("delivered frame count"),
        side_effect_certainty: SideEffectCertainty::Committed,
        live_write_executed: true,
        automatic_retry: false,
        device_application: DeviceApplicationState::Confirmed,
        terminal_sequence: SequenceNumber::try_from(1_u64).expect("terminal sequence"),
        error_kind: None,
        superseded_by: None,
    }
}

#[test]
fn admitted_effect_is_split_into_exact_owner_bound_replays() {
    let mut registry = VolatileEffectRegistry::new(8).expect("registry");
    let request = transaction_request(
        "effect",
        TransactionClass::EffectFrame,
        10_000,
        &["keyboard-1", "mouse-1"],
    );
    let owner = binding("session-app", 7);
    let capture = registry
        .capture_admitted(&request, &owner)
        .expect("effect capture succeeds");
    assert_eq!(capture.captured, 2);
    assert!(capture.evicted.is_empty());

    let replay = registry
        .plan_replay(
            &text("receiver-1"),
            generation(1),
            &text("mouse-1"),
            text("request-replay"),
            text("transaction-replay"),
            DispatchNonce::try_from(8_u64).expect("dispatch nonce"),
            time(20_000),
        )
        .expect("replay planning succeeds")
        .expect("mouse desired frame exists");
    validate_transaction(&replay.request).expect("replay remains canonical");
    assert_eq!(replay.request.frames.len(), 1);
    assert_eq!(replay.request.frames[0].device_id, text("mouse-1"));
    assert_eq!(replay.request.frames[0].frame_index.get(), 0);
    assert_eq!(replay.submission.session_id, owner.session_id);
    assert_eq!(
        replay.submission.authorization_epoch,
        owner.authorization_epoch
    );
    assert_ne!(replay.submission.dispatch_nonce, owner.dispatch_nonce);
}

#[test]
fn bounded_registry_evicts_oldest_target_without_cross_device_mutation() {
    let mut registry = VolatileEffectRegistry::new(1).expect("registry");
    let first = transaction_request(
        "first",
        TransactionClass::EffectFrame,
        10_000,
        &["keyboard-1"],
    );
    let second = transaction_request(
        "second",
        TransactionClass::EffectFrame,
        10_000,
        &["mouse-1"],
    );
    registry
        .capture_admitted(&first, &binding("session-app", 1))
        .expect("first capture");
    let capture = registry
        .capture_admitted(&second, &binding("session-app", 2))
        .expect("second capture");
    assert_eq!(registry.len(), 1);
    assert_eq!(capture.evicted.len(), 1);
    assert_eq!(capture.evicted[0].device_id, text("keyboard-1"));
}

#[test]
fn stable_success_replaces_effect_but_failed_stable_does_not() {
    let mut registry = VolatileEffectRegistry::new(8).expect("registry");
    let effect = transaction_request(
        "effect",
        TransactionClass::EffectFrame,
        10_000,
        &["mouse-1"],
    );
    registry
        .capture_admitted(&effect, &binding("session-app", 1))
        .expect("effect capture");

    let stable = transaction_request(
        "stable",
        TransactionClass::StaticLighting,
        10_000,
        &["mouse-1"],
    );
    let mut failed = successful_terminal(&stable);
    failed.state = TransactionState::Failed;
    registry.observe_completed(&stable, &failed);
    assert_eq!(registry.len(), 1);

    registry.observe_completed(&stable, &successful_terminal(&stable));
    assert!(registry.is_empty());
}

#[test]
fn owner_lease_session_and_generation_cleanup_are_exact() {
    let mut registry = VolatileEffectRegistry::new(8).expect("registry");
    let keyboard = transaction_request(
        "keyboard",
        TransactionClass::EffectFrame,
        10_000,
        &["keyboard-1"],
    );
    let mut mouse =
        transaction_request("mouse", TransactionClass::EffectFrame, 10_000, &["mouse-1"]);
    mouse.lease_id = text("lease-2");
    registry
        .capture_admitted(&keyboard, &binding("session-1", 1))
        .expect("keyboard capture");
    registry
        .capture_admitted(&mouse, &binding("session-2", 2))
        .expect("mouse capture");

    assert_eq!(registry.remove_lease(&text("lease-2")), 1);
    assert_eq!(registry.len(), 1);
    assert_eq!(registry.remove_session(&text("session-1")), 1);
    assert!(registry.is_empty());

    registry
        .capture_admitted(&keyboard, &binding("session-1", 3))
        .expect("keyboard recapture");
    assert_eq!(
        registry.remove_generation(&text("receiver-1"), generation(1)),
        1
    );
    assert!(registry.is_empty());
}
