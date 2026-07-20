// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{ReceiverTransport, TransportDispatch, TransportReconciliation};
use hfx_domain::{
    AuthorizationEpoch, ColorChannel, DispatchNonce, FrameIndex, GenerationId, LedCount,
    LogicalDeviceId, ProfileDigest, ProfileId, ReceiverId, RequestDigest, SessionId, TransactionId,
};
use hfx_protocol::{DeviceProfileBinding, LightingFrame, RgbColor};
use hfx_sim::{SimReceiverTransport, SimTransportConfigError, SimTransportErrorKind};

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

fn color(value: u8) -> RgbColor {
    let channel = ColorChannel::try_from(value).expect("test channel is valid");
    RgbColor {
        red: channel,
        green: channel,
        blue: channel,
    }
}

fn dispatch(sequence: u64) -> TransportDispatch {
    let device_id: LogicalDeviceId = text(&format!("mouse-{sequence}"));
    TransportDispatch {
        session_id: text(&format!("session-{sequence}")),
        authorization_epoch: AuthorizationEpoch::try_from(sequence).expect("test epoch is valid"),
        dispatch_nonce: DispatchNonce::try_from(sequence).expect("test nonce is valid"),
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        transaction_id: text(&format!("transaction-{sequence}")),
        request_digest: text(&format!("{sequence:064x}")),
        receiver_profile_id: text("profile.receiver"),
        receiver_profile_digest: text(&"a".repeat(64)),
        device_profiles: vec![DeviceProfileBinding {
            device_id: device_id.clone(),
            profile_id: text(&format!("profile.mouse-{sequence}")),
            profile_digest: text(&"b".repeat(64)),
            application_slot_count: LedCount::try_from(1_u16).expect("test LED count is valid"),
        }],
        frames: vec![LightingFrame {
            device_id,
            frame_index: FrameIndex::try_from(0_u32).expect("test frame index is valid"),
            colors: vec![color(u8::try_from(sequence).unwrap_or(u8::MAX))],
        }],
    }
}

fn transport(entries: usize, tombstones: usize) -> SimReceiverTransport {
    SimReceiverTransport::new(text("receiver-1"), generation(1), entries, tombstones)
        .expect("test journal bounds are valid")
}

#[test]
fn exact_dispatch_is_physically_applied_once_and_conflicts_fail_closed() {
    let mut transport = transport(4, 4);
    let dispatch = dispatch(1);

    assert_eq!(
        transport.reconcile(&dispatch),
        TransportReconciliation::NotObserved
    );
    let first = transport
        .dispatch(&dispatch)
        .expect("first dispatch succeeds");
    let second = transport
        .dispatch(&dispatch)
        .expect("exact replay returns retained receipt");

    assert_eq!(first, second);
    assert_eq!(transport.metrics().dispatch_calls, 2);
    assert_eq!(transport.metrics().physical_dispatches, 1);
    assert_eq!(transport.metrics().physical_frames, 1);
    assert_eq!(
        transport.physical_colors(&dispatch.frames[0].device_id),
        Some(dispatch.frames[0].colors.as_slice())
    );

    let mut conflict = dispatch.clone();
    conflict.dispatch_nonce = DispatchNonce::try_from(2_u64).expect("test nonce is valid");
    assert_eq!(
        transport.reconcile(&conflict),
        TransportReconciliation::Conflict
    );
    assert!(transport.dispatch(&conflict).is_err());
    assert_eq!(transport.metrics().physical_dispatches, 1);
}

#[test]
fn evicted_and_forgotten_identities_never_regress_to_not_observed() {
    let mut transport = transport(1, 1);
    let first = dispatch(1);
    let second = dispatch(2);
    let third = dispatch(3);

    transport.dispatch(&first).expect("first dispatch succeeds");
    transport
        .dispatch(&second)
        .expect("second dispatch evicts first terminal");
    assert_eq!(
        transport.reconcile(&first),
        TransportReconciliation::Evicted
    );

    transport
        .dispatch(&third)
        .expect("third dispatch rotates the bounded tombstone");
    assert!(!transport.history_complete(generation(1)));
    assert_eq!(
        transport.reconcile(&first),
        TransportReconciliation::Unavailable
    );
    assert_ne!(
        transport.reconcile(&first),
        TransportReconciliation::NotObserved
    );
    assert!(transport.dispatch(&first).is_err());
    assert_eq!(transport.metrics().physical_dispatches, 3);

    transport
        .connect_generation(generation(2))
        .expect("a newer generation connects");
    assert!(transport.history_complete(generation(2)));
    let mut next_generation = dispatch(4);
    next_generation.generation_id = generation(2);
    assert_eq!(
        transport.reconcile(&next_generation),
        TransportReconciliation::NotObserved,
        "history loss in an old generation must not disable a new generation"
    );
    transport
        .dispatch(&next_generation)
        .expect("new generation has an independent safe history floor");
    assert_eq!(transport.metrics().physical_dispatches, 4);
}

#[test]
fn only_the_active_strictly_newer_generation_can_begin_a_new_write() {
    let mut transport = transport(4, 4);
    let mut future = dispatch(1);
    future.generation_id = generation(2);

    let error = transport
        .dispatch(&future)
        .expect_err("an unconnected generation cannot reach physical transport");
    assert_eq!(error.kind(), SimTransportErrorKind::RouteUnavailable);
    assert_eq!(transport.metrics().physical_dispatches, 0);

    transport.disconnect();
    assert_eq!(
        transport.connect_generation(generation(1)),
        Err(SimTransportConfigError::GenerationNotNewer)
    );
    transport
        .connect_generation(generation(2))
        .expect("a strictly newer generation connects");
    transport
        .dispatch(&future)
        .expect("the active newer generation can write");
    assert_eq!(transport.metrics().physical_dispatches, 1);
}

#[test]
fn retained_identity_binds_every_dispatch_field_and_frame() {
    let mut transport = transport(4, 4);
    let original = dispatch(1);
    transport
        .dispatch(&original)
        .expect("original dispatch succeeds");

    let mut changed_session = original.clone();
    changed_session.session_id = text::<SessionId>("session-other");
    let mut changed_generation = original.clone();
    changed_generation.generation_id = generation(2);
    let mut changed_transaction = original.clone();
    changed_transaction.transaction_id = text::<TransactionId>("transaction-other");
    let mut changed_request = original.clone();
    changed_request.request_digest = text::<RequestDigest>(&"f".repeat(64));
    let mut changed_receiver = original.clone();
    changed_receiver.receiver_id = text::<ReceiverId>("receiver-other");
    let mut changed_profile = original.clone();
    changed_profile.receiver_profile_id = text::<ProfileId>("profile.other");
    let mut changed_digest = original.clone();
    changed_digest.receiver_profile_digest = text::<ProfileDigest>(&"c".repeat(64));
    let mut changed_frame = original.clone();
    changed_frame.frames[0].colors[0] = color(99);

    for changed in [
        changed_session,
        changed_generation,
        changed_request,
        changed_receiver,
        changed_profile,
        changed_digest,
        changed_frame,
    ] {
        assert_eq!(
            transport.reconcile(&changed),
            TransportReconciliation::Conflict
        );
    }
    assert_eq!(
        transport.reconcile(&changed_transaction),
        TransportReconciliation::NotObserved,
        "a distinct transaction identity is new work rather than aliasing the retained record"
    );
}

#[test]
fn invalid_frame_count_is_rejected_before_reservation_or_physical_state() {
    let mut transport = transport(4, 4);
    let mut oversized = dispatch(9);
    oversized.frames = vec![oversized.frames[0].clone(); 4_097];
    let transaction_id = oversized.transaction_id.clone();

    assert!(transport.dispatch(&oversized).is_err());
    assert!(transport.journal_record(&transaction_id).is_none());
    assert_eq!(transport.metrics().physical_dispatches, 0);
    assert_eq!(transport.metrics().physical_frames, 0);
}
