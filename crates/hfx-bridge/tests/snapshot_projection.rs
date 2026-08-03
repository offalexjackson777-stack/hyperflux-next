// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    DisabledRestorationSource, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationSnapshotSource, RuntimeProfileAuthority, SnapshotProjectionError, SnapshotProjector,
};
use hfx_core::{
    BoundedEventLog, ChildIdentity, EndpointIdentity, EventDraft, LeaseManager, LifecycleLimits,
    ObservationStamp, ReceiverLifecycleMachine, ReceiverLifecycleRegistry,
};
use hfx_domain::{
    ActivityState, BatteryPercent, ClientId, ConnectionMode, DeviceKind, EventKind,
    EvidenceClaimId, EvidenceConfidence, GenerationId, LeaseDurationMs, LeaseId, LogicalDeviceId,
    MonotonicMs, PairingState, ProductId, ProjectionRevision, ReceiverId, RestoreState, RouteKind,
    RouteState, SequenceNumber, StreamEpoch, SupportLevel, TelemetryAvailability,
};
use hfx_protocol::{LeaseRequest, LeaseResult, ResourceKey};
use std::collections::BTreeMap;

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity is canonical")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is canonical")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("test time is canonical")
}

fn stamp(sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(1),
        SequenceNumber::try_from(sequence).expect("test sequence is canonical"),
        time(sequence),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{sequence}")),
    )
    .expect("test evidence is explicit")
}

fn resource(device_id: &str) -> ResourceKey {
    ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        device_id: text(device_id),
        kind: hfx_domain::ResourceKind::Lighting,
    }
}

fn lifecycle_registry() -> ReceiverLifecycleRegistry {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle limits are valid");
    machine.discover(stamp(1));
    register_child(&mut machine, "mouse", DeviceKind::Mouse, 0x00cd, 2);
    register_child(&mut machine, "unknown", DeviceKind::Unknown, 0xffff, 20);
    register_child(
        &mut machine,
        "kind-mismatch",
        DeviceKind::Keyboard,
        0x00cd,
        30,
    );
    machine.observe_battery_reported(
        &text("mouse"),
        BatteryPercent::try_from(0_u8).expect("zero is valid"),
        stamp(10),
    );

    let absent = ReceiverLifecycleMachine::new(text("receiver-absent"), LifecycleLimits::default())
        .expect("lifecycle limits are valid");
    assert!(absent.current().is_none());

    let mut registry = ReceiverLifecycleRegistry::default();
    registry.register(machine).expect("active receiver fits");
    registry.register(absent).expect("absent receiver fits");
    registry
}

fn register_child(
    machine: &mut ReceiverLifecycleMachine,
    device_id: &str,
    kind: DeviceKind,
    product_id: u16,
    base: u64,
) {
    let logical_id: LogicalDeviceId = text(device_id);
    let endpoint_id = format!("{device_id}-wireless");
    machine
        .register_device(
            ChildIdentity::new(
                logical_id.clone(),
                kind,
                ProductId::try_from(product_id).expect("product id is valid"),
            )
            .expect("child identity is valid"),
            stamp(base),
        )
        .expect("child registration succeeds");
    machine.observe_pairing(&logical_id, PairingState::Paired, stamp(base + 1));
    machine
        .register_endpoint(
            &logical_id,
            EndpointIdentity::new(
                text(&endpoint_id),
                RouteKind::HyperfluxWireless,
                ConnectionMode::Hyperflux24ghz,
            )
            .expect("endpoint identity is valid"),
            stamp(base + 2),
        )
        .expect("endpoint registration succeeds");
    machine
        .observe_route(
            &logical_id,
            &text(&endpoint_id),
            RouteState::Available,
            stamp(base + 3),
        )
        .expect("route observation succeeds");
    machine
        .observe_activity(
            &logical_id,
            &text(&endpoint_id),
            ActivityState::Active,
            stamp(base + 4),
        )
        .expect("activity observation succeeds");
}

fn event_log() -> BoundedEventLog {
    let mut events = BoundedEventLog::new(
        text("stream-1"),
        StreamEpoch::try_from(1_u64).expect("stream epoch is valid"),
        ProjectionRevision::try_from(1_u32).expect("projection revision is valid"),
        16,
    )
    .expect("event log bound is valid");
    events
        .append(EventDraft {
            kind: EventKind::DeviceAvailable,
            receiver_id: Some(text("receiver-1")),
            generation_id: Some(generation(1)),
            device_id: Some(text("mouse")),
            lease_id: None,
            transaction_id: None,
            finding_id: None,
        })
        .expect("event appends");
    events
}

#[derive(Default)]
struct TestRestoration {
    values: BTreeMap<(ReceiverId, GenerationId), ReceiverRestorationSnapshot>,
    fail: bool,
}

impl RestorationSnapshotSource for TestRestoration {
    fn restoration(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        if self.fail {
            return Err(RestorationProjectionError::Unavailable);
        }
        self.values
            .get(&(receiver_id.clone(), generation_id))
            .copied()
            .ok_or(RestorationProjectionError::Unavailable)
    }
}

#[test]
fn complete_snapshot_is_canonical_profile_qualified_and_truthful() {
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profile authority is valid");
    bind_profiles(&mut profiles);
    let projector = SnapshotProjector::new(&profiles);
    let receivers = lifecycle_registry();
    let mut leases = LeaseManager::new(4, 8).expect("lease bounds are valid");
    let mut resources = vec![resource("mouse"), resource("unknown")];
    resources.sort_unstable();
    let lease = leases
        .acquire(
            LeaseRequest {
                request_id: text("request-lease"),
                client_id: text::<ClientId>("client-a"),
                resources,
                duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is canonical"),
            },
            text::<LeaseId>("lease-a"),
            time(100),
        )
        .expect("lease request succeeds");
    assert!(matches!(lease, LeaseResult::Granted(_)));
    let events = event_log();
    let mut restoration = TestRestoration::default();
    restoration.values.insert(
        (text("receiver-1"), generation(1)),
        ReceiverRestorationSnapshot {
            stable_restore_enabled: true,
            restore_state: RestoreState::Succeeded,
        },
    );

    let snapshot = projector
        .project(&receivers, &mut leases, &events, &restoration, time(101))
        .expect("snapshot is valid");
    assert_eq!(snapshot.cursor.sequence.get(), 1);
    assert_eq!(snapshot.receivers.len(), 1);
    let receiver = &snapshot.receivers[0];
    assert_eq!(receiver.receiver_id.as_str(), "receiver-1");
    assert_eq!(
        receiver
            .profile_id
            .as_ref()
            .map(hfx_domain::ProfileId::as_str),
        Some("receiver.razer.hyperflux-v2.1532-00cf")
    );
    assert_eq!(
        receiver
            .profile_digest
            .as_ref()
            .map(hfx_domain::ProfileDigest::as_str)
            .map(str::len),
        Some(64)
    );
    assert!(receiver.stable_restore_enabled);
    assert_eq!(receiver.restore_state, RestoreState::Succeeded);
    assert_eq!(receiver.ownership.len(), 2);
    assert!(
        receiver
            .ownership
            .windows(2)
            .all(|pair| pair[0].resource < pair[1].resource)
    );

    let mouse = receiver
        .devices
        .iter()
        .find(|device| device.device_id.as_str() == "mouse")
        .expect("mouse is projected");
    assert_eq!(
        mouse.profile_id.as_ref().map(hfx_domain::ProfileId::as_str),
        Some("child.razer.basilisk-v3-pro-35k.00cd")
    );
    assert_eq!(mouse.support_level, SupportLevel::ProductionQualified);
    assert_eq!(
        mouse
            .profile_digest
            .as_ref()
            .map(hfx_domain::ProfileDigest::as_str)
            .map(str::len),
        Some(64)
    );
    assert!(!mouse.capabilities.is_empty());
    assert_eq!(mouse.battery.availability, TelemetryAvailability::Reported);
    assert_eq!(mouse.battery.percentage.map(BatteryPercent::get), Some(0));
    assert_eq!(mouse.battery.freshness, hfx_domain::FreshnessState::Fresh);
    assert_eq!(
        mouse.endpoints[0]
            .evidence_claim_id
            .as_ref()
            .map(EvidenceClaimId::as_str),
        Some("claim-6")
    );

    for unqualified_id in ["kind-mismatch", "unknown"] {
        let unqualified = receiver
            .devices
            .iter()
            .find(|device| device.device_id.as_str() == unqualified_id)
            .expect("unqualified child stays visible");
        assert!(unqualified.profile_id.is_none());
        assert!(unqualified.profile_digest.is_none());
        assert!(unqualified.capabilities.is_empty());
        assert_eq!(unqualified.support_level, SupportLevel::ReadOnly);
    }
}

#[test]
fn projection_excludes_absent_generations_old_ownership_and_expired_leases() {
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profile authority is valid");
    bind_profiles(&mut profiles);
    let projector = SnapshotProjector::new(&profiles);
    let receivers = lifecycle_registry();
    let mut leases = LeaseManager::new(2, 4).expect("lease bounds are valid");
    leases
        .acquire(
            LeaseRequest {
                request_id: text("request-old"),
                client_id: text("client-old"),
                resources: vec![resource("mouse")],
                duration_ms: LeaseDurationMs::try_from(1_000_u32).expect("duration is canonical"),
            },
            text("lease-old"),
            time(1),
        )
        .expect("old lease is admitted");

    let snapshot = projector
        .project(
            &receivers,
            &mut leases,
            &event_log(),
            &DisabledRestorationSource,
            time(1_001),
        )
        .expect("expired ownership is omitted");
    assert_eq!(snapshot.receivers.len(), 1);
    assert!(snapshot.receivers[0].ownership.is_empty());
}

#[test]
fn unavailable_restoration_truth_fails_the_complete_snapshot() {
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profile authority is valid");
    bind_profiles(&mut profiles);
    let projector = SnapshotProjector::new(&profiles);
    let receivers = lifecycle_registry();
    let mut leases = LeaseManager::new(1, 1).expect("lease bounds are valid");
    let restoration = TestRestoration {
        fail: true,
        ..TestRestoration::default()
    };
    assert_eq!(
        projector.project(&receivers, &mut leases, &event_log(), &restoration, time(1),),
        Err(SnapshotProjectionError::Restoration(
            RestorationProjectionError::Unavailable
        ))
    );
}

fn bind_profiles(profiles: &mut RuntimeProfileAuthority) {
    profiles
        .bind_receiver(
            text("receiver-1"),
            generation(1),
            hfx_domain::VendorId::try_from(0x1532_u16).expect("vendor id is valid"),
            ProductId::try_from(0x00cf_u16).expect("product id is valid"),
        )
        .expect("receiver profile binds");
}
