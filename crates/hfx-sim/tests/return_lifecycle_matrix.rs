// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{
    RestoreDeliveryState, ReturnEvidence, ReturnPhase, ReturnTransition, SubmissionBinding,
    VolatileEffectRegistry,
};
use hfx_domain::{
    AuthorizationEpoch, ClientId, ColorChannel, DispatchNonce, FrameIndex, GenerationId, LeaseId,
    LedCount, LogicalDeviceId, MonotonicMs, ProfileDigest, ProfileId, ReceiverId, RequestId,
    ResourceKind, SequenceNumber, TransactionClass, TransactionId,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_protocol::{
    DeviceProfileBinding, LightingFrame, ResourceKey, RgbColor, TransactionRequest,
};
use hfx_sim::SimReturnLifecycle;

const MOUSE_PROFILE: &str = "child.razer.basilisk-v3-pro-35k.00cd";
const KEYBOARD_PROFILE: &str = "child.razer.deathstalker-v2-pro-tkl.0296";

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity")
}

fn sequence(value: u64) -> SequenceNumber {
    SequenceNumber::try_from(value).expect("sequence")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("time")
}

fn lifecycle(profile: &str) -> SimReturnLifecycle {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog");
    SimReturnLifecycle::from_profile(&catalog, &id(profile))
        .expect("profile policy composes")
        .expect("qualified profile has lifecycle policy")
}

fn bootstrap_synchronized(lifecycle: &mut SimReturnLifecycle, at_ms: u64) {
    let transition = lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(at_ms));
    let ReturnTransition::EpochOpened(epoch) = transition else {
        panic!("bootstrap activity opens an epoch");
    };
    assert!(matches!(
        lifecycle.note_delivery(epoch.sequence(), RestoreDeliveryState::ChildConfirmed),
        ReturnTransition::Synchronized(_)
    ));
}

#[test]
fn randomized_legal_idle_durations_and_jitter_open_one_bounded_epoch() {
    for profile in [MOUSE_PROFILE, KEYBOARD_PROFILE] {
        let mut seed = 0x6a09_e667_f3bc_c909_u64;
        for _ in 0..512 {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            let idle_seconds = 60 + seed % 841;
            let coalescing_jitter_ms = (seed >> 16) % 501;
            let mut lifecycle = lifecycle(profile);
            bootstrap_synchronized(&mut lifecycle, 1_000);
            let wake_at = 1_000 + idle_seconds * 1_000 - coalescing_jitter_ms;
            let transition =
                lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(2), time(wake_at));
            let ReturnTransition::EpochOpened(epoch) = transition else {
                panic!("legal idle interval with bounded jitter must open an epoch");
            };
            let envelope = lifecycle
                .retry_envelope()
                .expect("retry envelope is representable")
                .expect("return epoch owns one envelope");
            assert_eq!(envelope.epoch_sequence, epoch.sequence());
            assert_eq!(envelope.due_at_ms[0], time(wake_at));
            assert!(envelope.due_at_ms[1].get() - wake_at <= 100);
            assert_eq!(envelope.due_at_ms.len(), 12);

            assert_eq!(
                lifecycle.note_delivery(epoch.sequence(), RestoreDeliveryState::ReceiverDelivered,),
                ReturnTransition::None
            );
            assert_eq!(lifecycle.phase(), ReturnPhase::Returning);
            assert!(matches!(
                lifecycle.advance(time(wake_at + 6_001)),
                ReturnTransition::WindowCompleted(_)
            ));
            assert_eq!(lifecycle.phase(), ReturnPhase::Stable);
        }
    }
}

#[test]
fn duplicate_reordered_and_accelerating_evidence_never_duplicates_an_epoch() {
    let mut lifecycle = lifecycle(MOUSE_PROFILE);
    bootstrap_synchronized(&mut lifecycle, 1_000);
    assert_eq!(
        lifecycle.observe(ReturnEvidence::RouteDeparture, sequence(2), time(2_000)),
        ReturnTransition::BecameUnavailable
    );
    assert_eq!(
        lifecycle.observe(ReturnEvidence::RouteDeparture, sequence(2), time(2_000)),
        ReturnTransition::None
    );
    assert_eq!(
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(2_100)),
        ReturnTransition::None
    );
    let transition = lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(3), time(2_100));
    let ReturnTransition::EpochOpened(epoch) = transition else {
        panic!("activity after explicit departure opens immediately");
    };
    let initial = lifecycle.retry_envelope().expect("envelope");
    assert!(matches!(
        lifecycle.observe(ReturnEvidence::ContactReport, sequence(4), time(2_110)),
        ReturnTransition::EpochAccelerated(candidate) if candidate.sequence() == epoch.sequence()
    ));
    assert_eq!(lifecycle.retry_envelope().expect("envelope"), initial);
}

#[test]
fn lifecycle_boundaries_and_simultaneous_children_remain_endpoint_local() {
    let mut mouse = lifecycle(MOUSE_PROFILE);
    let mut keyboard = lifecycle(KEYBOARD_PROFILE);
    bootstrap_synchronized(&mut mouse, 1_000);
    bootstrap_synchronized(&mut keyboard, 1_000);

    mouse.reset_observation_anchor();
    assert!(matches!(
        mouse.observe(ReturnEvidence::EndpointActivity, sequence(1), time(2_000)),
        ReturnTransition::EpochOpened(_)
    ));
    assert_eq!(keyboard.phase(), ReturnPhase::Synchronized);

    assert_eq!(
        keyboard.observe(ReturnEvidence::RouteDeparture, sequence(2), time(2_000)),
        ReturnTransition::BecameUnavailable
    );
    assert!(matches!(
        keyboard.observe(ReturnEvidence::RouteReturn, sequence(3), time(2_010)),
        ReturnTransition::EpochOpened(_)
    ));
    assert_eq!(mouse.phase(), ReturnPhase::Returning);

    let mouse_epoch = mouse.epoch().expect("mouse epoch");
    assert_eq!(
        mouse.observe(
            ReturnEvidence::ApplicationOwnershipLoss,
            sequence(2),
            time(2_020),
        ),
        ReturnTransition::None
    );
    assert_eq!(mouse.phase(), ReturnPhase::Stable);
    assert_eq!(mouse.epoch(), None);
    assert!(keyboard.epoch().is_some());
    assert_eq!(mouse_epoch.sequence(), sequence(1));
}

#[test]
fn unknown_profiles_fail_closed_without_a_return_writer() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog");
    assert!(
        SimReturnLifecycle::from_profile(&catalog, &id("child.research.unknown"))
            .expect("unknown profile is not an error")
            .is_none()
    );
}

#[test]
fn effect_intent_survives_rescan_but_not_owner_or_process_restart() {
    let mut effects = VolatileEffectRegistry::new(8).expect("effect registry");
    let request = effect_request();
    let owner = SubmissionBinding {
        session_id: id("session-openrgb"),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("epoch"),
        dispatch_nonce: DispatchNonce::try_from(1_u64).expect("nonce"),
    };
    effects
        .capture_admitted(&request, &owner)
        .expect("effect intent is admitted");
    assert!(
        effects
            .plan_replay(
                &request.receiver_id,
                request.generation_id,
                &request.frames[0].device_id,
                id("request-rescan"),
                id("transaction-rescan"),
                DispatchNonce::try_from(2_u64).expect("nonce"),
                time(20_000),
            )
            .expect("rescan replay planning")
            .is_some()
    );
    assert_eq!(effects.remove_session(&owner.session_id), 1);
    assert!(effects.is_empty());

    let restarted = VolatileEffectRegistry::new(8).expect("new process registry");
    assert!(restarted.is_empty());
}

fn effect_request() -> TransactionRequest {
    let receiver_id: ReceiverId = id("receiver-1");
    let generation_id = GenerationId::try_from(1_u64).expect("generation");
    let device_id: LogicalDeviceId = id("mouse-1");
    let profile_id: ProfileId = id("profile.mouse");
    let profile_digest: ProfileDigest = id(&"b".repeat(64));
    TransactionRequest {
        request_id: id::<RequestId>("request-effect"),
        transaction_id: id::<TransactionId>("transaction-effect"),
        client_id: id::<ClientId>("client-openrgb"),
        lease_id: id::<LeaseId>("lease-openrgb"),
        receiver_id: receiver_id.clone(),
        generation_id,
        receiver_profile_id: id("profile.receiver"),
        receiver_profile_digest: id(&"a".repeat(64)),
        device_profiles: vec![DeviceProfileBinding {
            device_id: device_id.clone(),
            profile_id,
            profile_digest,
            application_slot_count: LedCount::try_from(1_u16).expect("LED count"),
        }],
        transaction_class: TransactionClass::EffectFrame,
        stable_intents: Vec::new(),
        deadline_ms: time(10_000),
        resources: vec![ResourceKey {
            receiver_id,
            generation_id,
            device_id: device_id.clone(),
            kind: ResourceKind::Lighting,
        }],
        frames: vec![LightingFrame {
            device_id,
            frame_index: FrameIndex::try_from(0_u32).expect("frame index"),
            colors: vec![RgbColor {
                red: ColorChannel::try_from(0_u8).expect("red"),
                green: ColorChannel::try_from(0_u8).expect("green"),
                blue: ColorChannel::try_from(255_u8).expect("blue"),
            }],
        }],
    }
}
