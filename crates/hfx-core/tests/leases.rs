// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{lease_request, resource, text, time};
use hfx_core::{LeaseManager, LeaseManagerError};
use hfx_domain::{LeaseDurationMs, LeaseState, ResourceKind};
use hfx_protocol::{LeaseResult, ReleaseLeaseRequest, RenewLeaseRequest, ResourceKey};

fn resource_kind(
    receiver: &str,
    generation_id: u64,
    device: &str,
    kind: ResourceKind,
) -> ResourceKey {
    ResourceKey {
        kind,
        ..resource(receiver, generation_id, device)
    }
}

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
fn native_control_excludes_semantic_resources_only_for_the_same_child_epoch() {
    for semantic_kind in [
        ResourceKind::Lighting,
        ResourceKind::Settings,
        ResourceKind::Pairing,
    ] {
        for (owned_kind, requested_kind) in [
            (ResourceKind::NativeControl, semantic_kind),
            (semantic_kind, ResourceKind::NativeControl),
        ] {
            let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
            let owned = resource_kind("receiver-1", 1, "mouse-1", owned_kind);
            let requested = resource_kind("receiver-1", 1, "mouse-1", requested_kind);
            manager
                .acquire(
                    lease_request("request-owned", "client-owned", vec![owned]),
                    text("lease-owned"),
                    time(100),
                )
                .expect("first resource is granted");

            let result = manager
                .acquire(
                    lease_request(
                        "request-contender",
                        "client-contender",
                        vec![requested.clone()],
                    ),
                    text("lease-contender"),
                    time(101),
                )
                .expect("cross-kind ownership conflict is a normal result");
            let LeaseResult::Conflict(detail) = result else {
                panic!("native control must exclude same-device semantic writes");
            };
            assert_eq!(detail.conflicting_resource, requested);
        }
    }

    let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
    manager
        .acquire(
            lease_request(
                "request-native",
                "client-native",
                vec![resource_kind(
                    "receiver-1",
                    1,
                    "mouse-1",
                    ResourceKind::NativeControl,
                )],
            ),
            text("lease-native"),
            time(100),
        )
        .expect("native resource is granted");
    for (request_id, resource) in [
        (
            "request-sibling",
            resource_kind("receiver-1", 1, "keyboard-1", ResourceKind::Lighting),
        ),
        (
            "request-generation",
            resource_kind("receiver-1", 2, "mouse-1", ResourceKind::Lighting),
        ),
        (
            "request-receiver",
            resource_kind("receiver-2", 1, "mouse-1", ResourceKind::Lighting),
        ),
    ] {
        assert!(matches!(
            manager
                .acquire(
                    lease_request(request_id, request_id, vec![resource]),
                    text(&format!("lease-{request_id}")),
                    time(101),
                )
                .expect("independent route scope is grantable"),
            LeaseResult::Granted(_)
        ));
    }
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
fn native_integrations_take_bounded_turns_without_idle_ownership_or_starvation() {
    let mut manager = LeaseManager::new(8, 32).expect("manager bounds are valid");
    let native = resource_kind("receiver-1", 1, "mouse-1", ResourceKind::NativeControl);

    for (round, owner, contender) in [
        (1_u64, "hfx-openrazer", "hfx-openrgb"),
        (2_u64, "hfx-openrgb", "hfx-openrazer"),
    ] {
        assert!(
            manager
                .ownership_snapshot(&text("receiver-1"), common::generation(1), time(round * 10))
                .is_empty(),
            "an idle integration must not retain native ownership"
        );

        let owner_lease = format!("lease-{owner}-{round}");
        assert!(matches!(
            manager
                .acquire(
                    lease_request(
                        &format!("request-{owner}-{round}"),
                        owner,
                        vec![native.clone()],
                    ),
                    text(&owner_lease),
                    time(round * 10 + 1),
                )
                .expect("the first simultaneous writer is granted"),
            LeaseResult::Granted(_)
        ));

        let conflict = manager
            .acquire(
                lease_request(
                    &format!("request-{contender}-conflict-{round}"),
                    contender,
                    vec![native.clone()],
                ),
                text(&format!("lease-{contender}-unused-{round}")),
                time(round * 10 + 2),
            )
            .expect("a simultaneous contender receives a typed conflict");
        assert!(matches!(conflict, LeaseResult::Conflict(_)));

        manager
            .release(&text(owner), &text(&owner_lease), time(round * 10 + 3))
            .expect("the transaction owner releases immediately");

        let contender_lease = format!("lease-{contender}-{round}");
        assert!(matches!(
            manager
                .acquire(
                    lease_request(
                        &format!("request-{contender}-retry-{round}"),
                        contender,
                        vec![native.clone()],
                    ),
                    text(&contender_lease),
                    time(round * 10 + 4),
                )
                .expect("the contender's fresh retry is granted"),
            LeaseResult::Granted(_)
        ));
        manager
            .release(
                &text(contender),
                &text(&contender_lease),
                time(round * 10 + 5),
            )
            .expect("the contender also releases after its transaction");
    }
}

#[test]
fn crashed_native_client_expires_and_its_stale_lease_is_denied() {
    let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
    let native = resource_kind("receiver-1", 1, "mouse-1", ResourceKind::NativeControl);
    let short = hfx_protocol::LeaseRequest {
        duration_ms: LeaseDurationMs::try_from(1_000_u32).expect("duration is valid"),
        ..lease_request("request-openrazer", "hfx-openrazer", vec![native.clone()])
    };
    manager
        .acquire(short, text("lease-openrazer"), time(100))
        .expect("OpenRazer lease is granted");

    assert!(!manager.owns(
        &text("hfx-openrazer"),
        &text("lease-openrazer"),
        std::slice::from_ref(&native),
        time(1_100),
    ));
    let expired = manager.expire(time(1_100));
    assert_eq!(expired.len(), 1);
    assert_eq!(expired[0].state, LeaseState::Expired);
    assert_eq!(
        manager.release(
            &text("hfx-openrazer"),
            &text("lease-openrazer"),
            time(1_101),
        ),
        Err(LeaseManagerError::UnknownLease)
    );
    assert!(matches!(
        manager
            .acquire(
                lease_request("request-openrgb", "hfx-openrgb", vec![native]),
                text("lease-openrgb"),
                time(1_101),
            )
            .expect("OpenRGB acquires after crash expiry"),
        LeaseResult::Granted(_)
    ));
}

#[test]
fn persistent_lighting_and_settings_do_not_require_persistent_ownership() {
    for (index, kind) in [ResourceKind::Lighting, ResourceKind::Settings]
        .into_iter()
        .enumerate()
    {
        let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
        let persistent = resource_kind("receiver-1", 1, "mouse-1", kind);
        let lease_id = format!("lease-writer-{index}");
        manager
            .acquire(
                lease_request(
                    &format!("request-writer-{index}"),
                    "writer",
                    vec![persistent.clone()],
                ),
                text(&lease_id),
                time(1),
            )
            .expect("persistent write authority is granted");

        // The resulting device state belongs to the device/restoration model,
        // not to the lease. Completing the write releases ownership.
        manager
            .release(&text("writer"), &text(&lease_id), time(2))
            .expect("completed persistent write releases authority");
        assert!(
            manager
                .ownership_snapshot(&text("receiver-1"), common::generation(1), time(2))
                .is_empty()
        );
        assert!(matches!(
            manager
                .acquire(
                    lease_request(
                        &format!("request-next-writer-{index}"),
                        "next-writer",
                        vec![persistent],
                    ),
                    text(&format!("lease-next-writer-{index}")),
                    time(3),
                )
                .expect("a later writer is not blocked by persistent state"),
            LeaseResult::Granted(_)
        ));
    }
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
    let expired = manager.expire(time(1_500));
    assert_eq!(expired.len(), 1);
    assert_eq!(expired[0].lease_id, text("lease-short"));
    assert_eq!(expired[0].state, LeaseState::Expired);
    assert!(manager.expire(time(1_500)).is_empty());
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
fn ownership_projection_is_canonical_generation_scoped_and_expiry_aware() {
    let mut manager = LeaseManager::new(4, 8).expect("manager bounds are valid");
    let receiver_one_mouse = resource("receiver-1", 1, "mouse-1");
    let receiver_one_keyboard = resource("receiver-1", 1, "keyboard-1");
    let receiver_two_mouse = resource("receiver-2", 1, "mouse-2");
    let mut receiver_one_resources =
        vec![receiver_one_mouse.clone(), receiver_one_keyboard.clone()];
    receiver_one_resources.sort_unstable();
    manager
        .acquire(
            lease_request("request-a", "client-a", receiver_one_resources),
            text("lease-a"),
            time(100),
        )
        .expect("first receiver resources are granted");
    manager
        .acquire(
            lease_request("request-b", "client-b", vec![receiver_two_mouse.clone()]),
            text("lease-b"),
            time(100),
        )
        .expect("second receiver resource is granted");

    let ownership =
        manager.ownership_snapshot(&text("receiver-1"), common::generation(1), time(101));
    assert_eq!(ownership.len(), 2);
    assert!(ownership[0].resource < ownership[1].resource);
    assert!(
        ownership
            .iter()
            .all(|entry| entry.resource.receiver_id == text("receiver-1"))
    );
    assert!(
        ownership
            .iter()
            .all(|entry| entry.client_id == text("client-a"))
    );
    assert!(
        ownership
            .iter()
            .all(|entry| entry.lease_id == text("lease-a"))
    );

    assert!(
        manager
            .ownership_snapshot(&text("receiver-1"), common::generation(2), time(101))
            .is_empty()
    );
    assert!(
        manager
            .ownership_snapshot(&text("receiver-1"), common::generation(1), time(10_100))
            .is_empty()
    );
    assert!(!manager.owns(
        &text("client-b"),
        &text("lease-b"),
        &[receiver_two_mouse],
        time(10_100)
    ));
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
