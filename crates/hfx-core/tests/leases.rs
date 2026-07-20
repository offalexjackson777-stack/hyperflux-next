// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{lease_request, resource, text, time};
use hfx_core::{LeaseManager, LeaseManagerError};
use hfx_domain::{LeaseDurationMs, LeaseState};
use hfx_protocol::{LeaseResult, ReleaseLeaseRequest, RenewLeaseRequest};

#[test]
fn conflict_grants_none_of_an_atomic_resource_set() {
    let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
    let mouse = resource("receiver-1", 1, "mouse-1");
    let keyboard = resource("receiver-1", 1, "keyboard-1");
    manager
        .acquire(
            lease_request("request-a", "client-a", vec![mouse.clone()]),
            text("lease-a"),
            time(100),
        )
        .expect("first lease is granted");

    let conflict_request = lease_request(
        "request-b",
        "client-b",
        vec![keyboard.clone(), mouse.clone()],
    );
    let conflict = manager
        .acquire(conflict_request.clone(), text("lease-b"), time(101))
        .expect("ownership conflict is a normal result");
    let LeaseResult::Conflict(detail) = &conflict else {
        panic!("conflicting atomic request must return a conflict variant");
    };
    assert_eq!(detail.conflicting_resource, mouse);

    let repeated = manager
        .acquire(conflict_request, text("lease-unused"), time(102))
        .expect("identical request is idempotent");
    assert_eq!(repeated, conflict);

    let keyboard_grant = manager
        .acquire(
            lease_request("request-c", "client-c", vec![keyboard]),
            text("lease-c"),
            time(103),
        )
        .expect("failed atomic request did not retain the sibling resource");
    assert!(matches!(keyboard_grant, LeaseResult::Granted(_)));
}

#[test]
fn conflict_is_reported_even_when_the_lease_table_is_full() {
    let mut manager = LeaseManager::new(1, 2).expect("manager bounds are valid");
    let mouse = resource("receiver-1", 1, "mouse-1");
    manager
        .acquire(
            lease_request("request-a", "client-a", vec![mouse.clone()]),
            text("lease-a"),
            time(100),
        )
        .expect("only lease slot is granted");

    let result = manager
        .acquire(
            lease_request("request-b", "client-b", vec![mouse.clone()]),
            text("lease-b"),
            time(101),
        )
        .expect("an ownership conflict does not consume lease capacity");
    let LeaseResult::Conflict(detail) = result else {
        panic!("full tables must preserve the typed ownership conflict");
    };
    assert_eq!(detail.conflicting_resource, mouse);
}

#[test]
fn conflict_history_is_bounded_even_when_every_retained_grant_is_pinned() {
    let mut manager = LeaseManager::new(2, 2).expect("manager bounds are valid");
    let mouse = resource("receiver-1", 1, "mouse-1");
    let keyboard = resource("receiver-1", 1, "keyboard-1");
    manager
        .acquire(
            lease_request("request-a", "client-a", vec![mouse.clone()]),
            text("lease-a"),
            time(1),
        )
        .expect("first lease is granted");
    manager
        .acquire(
            lease_request("request-b", "client-b", vec![keyboard]),
            text("lease-b"),
            time(1),
        )
        .expect("second lease is granted");

    assert_eq!(
        manager.acquire(
            lease_request("request-conflict", "client-c", vec![mouse]),
            text("lease-unused"),
            time(2),
        ),
        Err(LeaseManagerError::HistoryCapacity)
    );
}

#[test]
fn request_identity_cannot_authorize_different_content() {
    let mut manager = LeaseManager::new(2, 4).expect("manager bounds are valid");
    manager
        .acquire(
            lease_request(
                "request-1",
                "client-1",
                vec![resource("receiver-1", 1, "mouse-1")],
            ),
            text("lease-1"),
            time(1),
        )
        .expect("first request succeeds");
    let changed = lease_request(
        "request-1",
        "client-1",
        vec![resource("receiver-1", 1, "keyboard-1")],
    );
    assert_eq!(
        manager.acquire(changed, text("lease-2"), time(2)),
        Err(LeaseManagerError::RequestIdReused)
    );
}

#[test]
fn expiration_and_generation_replacement_revoke_authority() {
    let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
    let old_mouse = resource("receiver-1", 1, "mouse-1");
    let other_mouse = resource("receiver-2", 1, "mouse-2");
    manager
        .acquire(
            lease_request("request-old", "client-1", vec![old_mouse.clone()]),
            text("lease-old"),
            time(100),
        )
        .expect("old generation lease is granted");
    manager
        .acquire(
            lease_request("request-other", "client-2", vec![other_mouse.clone()]),
            text("lease-other"),
            time(100),
        )
        .expect("independent receiver lease is granted");

    let revoked = manager.invalidate_generation(&text("receiver-1"), common::generation(1));
    assert_eq!(revoked.len(), 1);
    assert_eq!(revoked[0].state, LeaseState::Revoked);
    assert!(!manager.owns(
        &text("client-1"),
        &text("lease-old"),
        &[old_mouse],
        time(101)
    ));
    assert!(manager.owns(
        &text("client-2"),
        &text("lease-other"),
        &[other_mouse],
        time(101)
    ));

    let short = resource("receiver-1", 2, "keyboard-1");
    let short_request = hfx_protocol::LeaseRequest {
        duration_ms: LeaseDurationMs::try_from(1_000_u32).expect("duration is valid"),
        ..lease_request("request-short", "client-3", vec![short.clone()])
    };
    manager
        .acquire(short_request, text("lease-short"), time(500))
        .expect("short lease is granted");
    assert!(!manager.owns(
        &text("client-3"),
        &text("lease-short"),
        std::slice::from_ref(&short),
        time(1_500)
    ));
    let replacement = manager
        .acquire(
            lease_request("request-replacement", "client-4", vec![short]),
            text("lease-replacement"),
            time(1_500),
        )
        .expect("expiry is pruned before acquisition");
    assert!(matches!(replacement, LeaseResult::Granted(_)));
}

#[test]
fn owner_release_and_disconnect_are_explicit() {
    let mut manager = LeaseManager::new(2, 4).expect("manager bounds are valid");
    let mouse = resource("receiver-1", 1, "mouse-1");
    manager
        .acquire(
            lease_request("request-1", "client-1", vec![mouse.clone()]),
            text("lease-1"),
            time(1),
        )
        .expect("lease is granted");
    assert_eq!(
        manager.release(&text("client-2"), &text("lease-1"), time(2)),
        Err(LeaseManagerError::NotOwner)
    );
    let released = manager
        .release(&text("client-1"), &text("lease-1"), time(2))
        .expect("owner can release");
    assert_eq!(released.state, LeaseState::Released);

    manager
        .acquire(
            lease_request("request-2", "client-1", vec![mouse]),
            text("lease-2"),
            time(3),
        )
        .expect("resource can be reacquired");
    let disconnected = manager.release_client(&text("client-1"));
    assert_eq!(disconnected.len(), 1);
    assert_eq!(disconnected[0].state, LeaseState::Revoked);
}

#[test]
fn protocol_renew_and_release_are_exactly_idempotent() {
    let mut manager = LeaseManager::new(2, 8).expect("manager bounds are valid");
    manager
        .acquire(
            lease_request(
                "request-acquire",
                "client-1",
                vec![resource("receiver-1", 1, "mouse-1")],
            ),
            text("lease-1"),
            time(1),
        )
        .expect("lease is granted");

    let renew = RenewLeaseRequest {
        request_id: text("request-renew"),
        client_id: text("client-1"),
        lease_id: text("lease-1"),
        duration_ms: LeaseDurationMs::try_from(1_000_u32).expect("duration is valid"),
    };
    let first_renewal = manager
        .renew_request(renew.clone(), time(10))
        .expect("renewal succeeds");
    let replayed_renewal = manager
        .renew_request(renew.clone(), time(50))
        .expect("renewal replay succeeds");
    assert_eq!(replayed_renewal, first_renewal);
    let LeaseResult::Granted(grant) = replayed_renewal else {
        panic!("renewal must return a grant");
    };
    assert_eq!(grant.expires_at_ms, time(1_010));
    assert_eq!(grant.state, LeaseState::Renewed);

    let mut changed_renewal = renew;
    changed_renewal.duration_ms = LeaseDurationMs::try_from(2_000_u32).expect("duration is valid");
    assert_eq!(
        manager.renew_request(changed_renewal, time(60)),
        Err(LeaseManagerError::RequestIdReused)
    );

    let release = ReleaseLeaseRequest {
        request_id: text("request-release"),
        client_id: text("client-1"),
        lease_id: text("lease-1"),
    };
    let first_release = manager
        .release_request(release.clone(), time(70))
        .expect("release succeeds");
    let replayed_release = manager
        .release_request(release, time(80))
        .expect("release replay succeeds after the live lease is gone");
    assert_eq!(replayed_release, first_release);
    let LeaseResult::Granted(grant) = replayed_release else {
        panic!("release must return a grant");
    };
    assert_eq!(grant.state, LeaseState::Released);
}

#[test]
fn request_identity_cannot_cross_lease_methods() {
    let mut manager = LeaseManager::new(2, 8).expect("manager bounds are valid");
    manager
        .acquire(
            lease_request(
                "request-shared",
                "client-1",
                vec![resource("receiver-1", 1, "mouse-1")],
            ),
            text("lease-1"),
            time(1),
        )
        .expect("lease is granted");

    assert_eq!(
        manager.renew_request(
            RenewLeaseRequest {
                request_id: text("request-shared"),
                client_id: text("client-1"),
                lease_id: text("lease-1"),
                duration_ms: LeaseDurationMs::try_from(1_000_u32).expect("duration is valid"),
            },
            time(2),
        ),
        Err(LeaseManagerError::RequestIdReused)
    );
}
