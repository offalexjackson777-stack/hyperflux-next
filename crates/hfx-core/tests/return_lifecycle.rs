// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{
    EndpointReturnLifecycle, EndpointReturnPolicy, LifecycleError, RestoreDeliveryState,
    ReturnEvidence, ReturnPhase, ReturnTransition,
};
use hfx_domain::{MonotonicMs, SequenceNumber};

const RETRIES: &[u64] = &[
    0, 100, 200, 300, 500, 750, 1_000, 1_500, 2_250, 3_250, 4_500, 6_000,
];
const OPEN: &[ReturnEvidence] = &[
    ReturnEvidence::EndpointActivity,
    ReturnEvidence::RouteReturn,
];
const ACCELERATORS: &[ReturnEvidence] = &[
    ReturnEvidence::BatteryReport,
    ReturnEvidence::ContactReport,
    ReturnEvidence::EndpointActivity,
    ReturnEvidence::RouteReturn,
];

fn sequence(value: u64) -> SequenceNumber {
    SequenceNumber::try_from(value).expect("sequence")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("monotonic time")
}

fn lifecycle() -> EndpointReturnLifecycle {
    EndpointReturnLifecycle::new(
        EndpointReturnPolicy::new(60_000, 500, RETRIES, OPEN, ACCELERATORS)
            .expect("return policy is valid"),
    )
}

#[test]
fn first_activity_and_post_quiescence_activity_open_distinct_epochs() {
    let mut lifecycle = lifecycle();
    let first = lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(100));
    let ReturnTransition::EpochOpened(first) = first else {
        panic!("first activity must open a startup return epoch");
    };
    assert_eq!(first.phase(), ReturnPhase::Returning);
    assert_eq!(first.opening_evidence(), ReturnEvidence::EndpointActivity);

    assert!(matches!(
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(2), time(500)),
        ReturnTransition::EpochAccelerated(_)
    ));
    let resumed = lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(3), time(60_501));
    let ReturnTransition::EpochOpened(resumed) = resumed else {
        panic!("activity after the conservative quiet candidate must open an epoch");
    };
    assert_eq!(resumed.sequence(), sequence(3));
    assert_eq!(resumed.opened_at_ms(), time(60_501));
}

#[test]
fn accelerator_evidence_never_extends_or_replaces_the_active_epoch() {
    let mut lifecycle = lifecycle();
    let ReturnTransition::EpochOpened(opened) =
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(10_000))
    else {
        panic!("epoch opens");
    };
    let accelerated = lifecycle.observe(ReturnEvidence::BatteryReport, sequence(2), time(12_000));
    let ReturnTransition::EpochAccelerated(accelerated) = accelerated else {
        panic!("profile-selected battery evidence accelerates the active epoch");
    };
    assert_eq!(accelerated.sequence(), opened.sequence());
    assert_eq!(accelerated.opened_at_ms(), opened.opened_at_ms());

    assert_eq!(
        lifecycle.advance(time(16_000)),
        ReturnTransition::None,
        "the final retry remains eligible at its exact deadline"
    );
    let ReturnTransition::WindowCompleted(completed) = lifecycle.advance(time(16_001)) else {
        panic!("the retry window closes after its final deadline");
    };
    assert_eq!(completed.phase(), ReturnPhase::Stable);
    assert_eq!(completed.opened_at_ms(), time(10_000));
    assert_eq!(lifecycle.phase(), ReturnPhase::Stable);
}

#[test]
fn route_return_opens_immediately_and_supersedes_an_older_epoch() {
    let mut lifecycle = lifecycle();
    lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(1_000));
    assert_eq!(
        lifecycle.observe(ReturnEvidence::RouteDeparture, sequence(2), time(1_005)),
        ReturnTransition::BecameUnavailable
    );
    assert_eq!(lifecycle.phase(), ReturnPhase::Unavailable);
    let transition = lifecycle.observe(ReturnEvidence::RouteReturn, sequence(3), time(1_010));
    let ReturnTransition::EpochOpened(epoch) = transition else {
        panic!("an explicit route-return edge opens immediately");
    };
    assert_eq!(epoch.sequence(), sequence(3));
    assert_eq!(epoch.opening_evidence(), ReturnEvidence::RouteReturn);
}

#[test]
fn activity_after_explicit_departure_opens_without_a_sleep_timer() {
    let mut lifecycle = lifecycle();
    let first = lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(100));
    let ReturnTransition::EpochOpened(first) = first else {
        panic!("initial activity opens bootstrap epoch");
    };
    lifecycle.note_delivery(first.sequence(), RestoreDeliveryState::ChildConfirmed);
    assert_eq!(
        lifecycle.observe(ReturnEvidence::RouteDeparture, sequence(2), time(200)),
        ReturnTransition::BecameUnavailable
    );
    assert!(matches!(
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(3), time(300)),
        ReturnTransition::EpochOpened(_)
    ));
}

#[test]
fn receiver_delivery_is_recorded_without_claiming_child_application() {
    let mut lifecycle = lifecycle();
    let ReturnTransition::EpochOpened(epoch) =
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(1_000))
    else {
        panic!("epoch opens");
    };
    assert_eq!(
        lifecycle.note_delivery(epoch.sequence(), RestoreDeliveryState::ReceiverDelivered),
        ReturnTransition::None
    );
    let recorded = lifecycle.epoch().expect("epoch remains inspectable");
    assert_eq!(recorded.delivery(), RestoreDeliveryState::ReceiverDelivered);
    assert_eq!(recorded.phase(), ReturnPhase::Returning);
    assert_eq!(
        lifecycle.note_delivery(sequence(2), RestoreDeliveryState::ReceiverRejected),
        ReturnTransition::None
    );
}

#[test]
fn only_child_confirmation_marks_an_epoch_synchronized() {
    let mut lifecycle = lifecycle();
    let ReturnTransition::EpochOpened(epoch) =
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(1_000))
    else {
        panic!("epoch opens");
    };
    let ReturnTransition::Synchronized(confirmed) =
        lifecycle.note_delivery(epoch.sequence(), RestoreDeliveryState::ChildConfirmed)
    else {
        panic!("child confirmation synchronizes the matching epoch");
    };
    assert_eq!(confirmed.sequence(), epoch.sequence());
    assert_eq!(lifecycle.phase(), ReturnPhase::Synchronized);
    assert_eq!(
        lifecycle
            .epoch()
            .expect("confirmed epoch remains inspectable")
            .delivery(),
        RestoreDeliveryState::ChildConfirmed
    );
}

#[test]
fn quiescence_is_a_candidate_not_a_sleep_claim() {
    let mut lifecycle = lifecycle();
    lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(100));
    assert!(matches!(
        lifecycle.advance(time(6_101)),
        ReturnTransition::WindowCompleted(_)
    ));
    assert_eq!(
        lifecycle.advance(time(60_100)),
        ReturnTransition::QuiescenceCandidate
    );
    assert_eq!(lifecycle.phase(), ReturnPhase::QuiescentCandidate);
    assert!(matches!(
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(2), time(60_101)),
        ReturnTransition::EpochOpened(_)
    ));
}

#[test]
fn stale_evidence_and_receiver_suspend_reset_are_bounded() {
    let mut lifecycle = lifecycle();
    lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(2), time(2_000));
    assert_eq!(
        lifecycle.observe(ReturnEvidence::RouteDeparture, sequence(1), time(2_100)),
        ReturnTransition::None
    );
    lifecycle.reset_observation_anchor();
    assert!(matches!(
        lifecycle.observe(ReturnEvidence::EndpointActivity, sequence(1), time(2_200)),
        ReturnTransition::EpochOpened(_)
    ));
}

#[test]
fn malformed_or_unbounded_policies_fail_closed() {
    for result in [
        EndpointReturnPolicy::new(0, 0, RETRIES, OPEN, ACCELERATORS),
        EndpointReturnPolicy::new(60_000, 60_000, RETRIES, OPEN, ACCELERATORS),
        EndpointReturnPolicy::new(60_000, 500, &[0], OPEN, ACCELERATORS),
        EndpointReturnPolicy::new(60_000, 500, &[0, 100, 100], OPEN, ACCELERATORS),
        EndpointReturnPolicy::new(60_000, 500, &[0, 30_001], OPEN, ACCELERATORS),
        EndpointReturnPolicy::new(60_000, 500, RETRIES, &[], ACCELERATORS),
        EndpointReturnPolicy::new(
            60_000,
            500,
            RETRIES,
            &[ReturnEvidence::RouteReturn],
            &[ReturnEvidence::EndpointActivity],
        ),
    ] {
        assert_eq!(result, Err(LifecycleError::InvalidReturnPolicy));
    }
}

#[test]
fn observation_jitter_prevents_an_exact_minimum_idle_boundary() {
    let mut before = lifecycle();
    let first = before.observe(ReturnEvidence::EndpointActivity, sequence(1), time(1_000));
    let ReturnTransition::EpochOpened(first) = first else {
        panic!("initial activity opens bootstrap epoch");
    };
    before.note_delivery(first.sequence(), RestoreDeliveryState::ChildConfirmed);
    assert_eq!(
        before.observe(ReturnEvidence::EndpointActivity, sequence(2), time(60_499),),
        ReturnTransition::None
    );

    let mut at_boundary = lifecycle();
    let first = at_boundary.observe(ReturnEvidence::EndpointActivity, sequence(1), time(1_000));
    let ReturnTransition::EpochOpened(first) = first else {
        panic!("initial activity opens bootstrap epoch");
    };
    at_boundary.note_delivery(first.sequence(), RestoreDeliveryState::ChildConfirmed);
    assert!(matches!(
        at_boundary.observe(ReturnEvidence::EndpointActivity, sequence(2), time(60_500),),
        ReturnTransition::EpochOpened(_)
    ));
}

#[test]
fn deterministic_event_fuzz_keeps_one_bounded_epoch_and_never_panics() {
    let mut lifecycle = lifecycle();
    let mut seed = 0x5eed_cafe_d00d_f00d_u64;
    let mut now = 0_u64;
    let mut openings = 0_usize;
    for index in 1..=10_000_u64 {
        seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
        now = now.saturating_add((seed >> 32) % 70_001);
        let evidence = match seed & 3 {
            0 => ReturnEvidence::BatteryReport,
            1 => ReturnEvidence::ContactReport,
            2 => ReturnEvidence::EndpointActivity,
            _ => ReturnEvidence::RouteReturn,
        };
        if matches!(
            lifecycle.observe(evidence, sequence(index), time(now)),
            ReturnTransition::EpochOpened(_)
        ) {
            openings += 1;
        }
        assert_eq!(lifecycle.policy().retry_offsets_ms().len(), RETRIES.len());
    }
    assert!(openings > 0);
    assert!(openings <= 10_000);
}
