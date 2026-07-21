// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ActivityState, BatteryPercent, ClientId, ConnectionMode, ContactState, ControllerAvailability,
    DeviceKind, EvidenceConfidence, FreshnessState, GenerationId, InventoryAvailability, LeaseId,
    MonotonicMs, PairingState, PowerState, PresenceState, PresentationKey, ProductId, ProfileId,
    ProjectionRevision, ReceiverLifecycleState, RestoreState, RouteKind, RouteState,
    SequenceNumber, SleepState, StreamEpoch, SupportLevel, TelemetryAvailability, VendorId,
};
use hfx_integration_model::{ViewModelError, project_integration_view};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, ControllerOwnership, EndpointSnapshot, EventCursor,
    LogicalDeviceSnapshot, ReceiverSnapshot, ResourceKey, ResourceOwnershipSnapshot,
    SnapshotValidationError,
};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test value is canonical")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("test time is canonical")
}

fn product(value: u16) -> ProductId {
    ProductId::try_from(value).expect("test product id is canonical")
}

fn cursor() -> EventCursor {
    EventCursor {
        stream_id: text("integration-stream"),
        stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch is canonical"),
        projection_revision: ProjectionRevision::try_from(1_u32)
            .expect("projection revision is canonical"),
        sequence: SequenceNumber::try_from(7_u64).expect("sequence is canonical"),
    }
}

fn endpoint(
    endpoint_id: &str,
    route_kind: RouteKind,
    connection_mode: ConnectionMode,
    route_state: RouteState,
    power_state: PowerState,
    sleep_state: SleepState,
    freshness: FreshnessState,
) -> EndpointSnapshot {
    EndpointSnapshot {
        endpoint_id: text(endpoint_id),
        route_kind,
        route_state,
        connection_mode,
        power_state,
        sleep_state,
        activity_state: ActivityState::Active,
        contact_state: ContactState::NotApplicable,
        freshness,
        confidence: EvidenceConfidence::Observed,
        evidence_claim_id: Some(text(&format!("claim-{endpoint_id}"))),
        observed_at_ms: Some(time(10)),
    }
}

fn wireless_endpoint(endpoint_id: &str) -> EndpointSnapshot {
    endpoint(
        endpoint_id,
        RouteKind::HyperfluxWireless,
        ConnectionMode::Hyperflux24ghz,
        RouteState::Available,
        PowerState::On,
        SleepState::Awake,
        FreshnessState::Fresh,
    )
}

fn battery(
    availability: TelemetryAvailability,
    percentage: Option<u8>,
    freshness: FreshnessState,
) -> BatteryObservation {
    BatteryObservation {
        availability,
        percentage: percentage.map(|value| {
            BatteryPercent::try_from(value).expect("test battery percentage is canonical")
        }),
        freshness,
        confidence: EvidenceConfidence::Observed,
        observed_at_ms: Some(time(9)),
    }
}

fn qualified_device(
    catalog: &RuntimeProfileCatalog,
    device_id: &str,
    product_id: u16,
    presence: PresenceState,
    mut endpoints: Vec<EndpointSnapshot>,
) -> LogicalDeviceSnapshot {
    let profile = catalog
        .child(product(product_id))
        .expect("qualified child profile exists");
    endpoints.sort_unstable_by(|left, right| left.endpoint_id.cmp(&right.endpoint_id));
    LogicalDeviceSnapshot {
        device_id: text(device_id),
        device_kind: profile.device_kind,
        product_id: product(product_id),
        profile_id: Some(profile.profile_id.clone()),
        profile_digest: Some(profile.runtime_digest.clone()),
        pairing: PairingState::Paired,
        presence,
        support_level: profile.support_level(),
        endpoints,
        battery: battery(
            TelemetryAvailability::Reported,
            Some(73),
            FreshnessState::Fresh,
        ),
        capabilities: profile
            .capabilities
            .iter()
            .map(|capability| capability.id.clone())
            .collect(),
    }
}

fn unknown_device(device_id: &str) -> LogicalDeviceSnapshot {
    LogicalDeviceSnapshot {
        device_id: text(device_id),
        device_kind: DeviceKind::Unknown,
        product_id: product(0xffff),
        profile_id: None,
        profile_digest: None,
        pairing: PairingState::Paired,
        presence: PresenceState::Available,
        support_level: SupportLevel::ReadOnly,
        endpoints: vec![wireless_endpoint("unknown-wireless")],
        battery: battery(
            TelemetryAvailability::Unknown,
            None,
            FreshnessState::Unknown,
        ),
        capabilities: Vec::new(),
    }
}

fn snapshot(
    catalog: &RuntimeProfileCatalog,
    mut devices: Vec<LogicalDeviceSnapshot>,
) -> BridgeSnapshot {
    let profile = catalog
        .receiver(
            VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
            product(0x00cf),
        )
        .expect("qualified receiver profile exists");
    devices.sort_unstable_by(|left, right| left.device_id.cmp(&right.device_id));
    BridgeSnapshot {
        cursor: cursor(),
        receivers: vec![ReceiverSnapshot {
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is canonical"),
            profile_id: Some(profile.profile_id.clone()),
            profile_digest: Some(profile.runtime_digest.clone()),
            lifecycle: ReceiverLifecycleState::Active,
            devices,
            ownership: Vec::new(),
            stable_restore_enabled: false,
            restore_state: RestoreState::Idle,
        }],
    }
}

fn lighting_resource(device_id: &str) -> ResourceKey {
    ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is canonical"),
        device_id: text(device_id),
        kind: hfx_domain::ResourceKind::Lighting,
    }
}

#[test]
fn mouse_only_and_keyboard_only_register_independently() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    for (device_id, product_id, kind) in [
        ("mouse", 0x00cd, DeviceKind::Mouse),
        ("keyboard", 0x0296, DeviceKind::Keyboard),
    ] {
        let device = qualified_device(
            &catalog,
            device_id,
            product_id,
            PresenceState::Available,
            vec![wireless_endpoint(&format!("{device_id}-wireless"))],
        );
        let view = project_integration_view(&snapshot(&catalog, vec![device]), &catalog, None)
            .expect("independent child projects");
        assert_eq!(view.receivers[0].inventory.len(), 1);
        assert_eq!(view.receivers[0].controllers.len(), 1);
        assert_eq!(view.receivers[0].controllers[0].device_kind, kind);
    }
}

#[test]
fn unknown_child_remains_inventory_only_without_inherited_authority() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let view = project_integration_view(
        &snapshot(&catalog, vec![unknown_device("unknown")]),
        &catalog,
        None,
    )
    .expect("unknown inventory is safe");
    assert_eq!(view.receivers[0].inventory.len(), 1);
    assert_eq!(
        view.receivers[0].inventory[0].availability,
        InventoryAvailability::Available
    );
    assert!(view.receivers[0].inventory[0].model_name.is_none());
    assert!(view.receivers[0].controllers.is_empty());
}

#[test]
fn direct_usb_availability_never_creates_or_suppresses_a_hyperflux_controller() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let direct = endpoint(
        "mouse-direct",
        RouteKind::DirectUsb,
        ConnectionMode::DirectUsb,
        RouteState::Available,
        PowerState::On,
        SleepState::Awake,
        FreshnessState::Fresh,
    );
    let wireless = endpoint(
        "mouse-wireless",
        RouteKind::HyperfluxWireless,
        ConnectionMode::Hyperflux24ghz,
        RouteState::Unavailable,
        PowerState::Unknown,
        SleepState::Unknown,
        FreshnessState::Fresh,
    );
    let device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![direct, wireless],
    );
    let view = project_integration_view(&snapshot(&catalog, vec![device]), &catalog, None)
        .expect("direct route remains inventory evidence");
    assert_eq!(
        view.receivers[0].inventory[0].availability,
        InventoryAvailability::Available
    );
    assert!(view.receivers[0].controllers.is_empty());
}

#[test]
fn sleeping_wireless_route_retains_identity_but_cannot_submit() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let sleeping = endpoint(
        "mouse-wireless",
        RouteKind::HyperfluxWireless,
        ConnectionMode::Hyperflux24ghz,
        RouteState::Available,
        PowerState::On,
        SleepState::Asleep,
        FreshnessState::Fresh,
    );
    let device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Sleeping,
        vec![sleeping],
    );
    let viewer = text::<ClientId>("openrgb-client");
    let view = project_integration_view(&snapshot(&catalog, vec![device]), &catalog, Some(&viewer))
        .expect("sleeping route stays modeled");
    let controller = &view.receivers[0].controllers[0];
    assert_eq!(controller.availability, ControllerAvailability::Sleeping);
    assert!(controller.actions.can_acquire);
    assert!(!controller.actions.can_submit_now);
}

#[test]
fn ownership_actions_are_viewer_specific_and_generation_scoped() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    let viewer = text::<ClientId>("openrgb-client");
    let mut unowned = snapshot(&catalog, vec![device]);
    let view = project_integration_view(&unowned, &catalog, Some(&viewer))
        .expect("unowned controller projects");
    assert!(view.receivers[0].controllers[0].actions.can_acquire);

    unowned.receivers[0].ownership = vec![ResourceOwnershipSnapshot {
        resource: lighting_resource("mouse"),
        client_id: viewer.clone(),
        lease_id: text::<LeaseId>("lease-openrgb"),
        expires_at_ms: time(100),
    }];
    let view = project_integration_view(&unowned, &catalog, Some(&viewer))
        .expect("viewer ownership projects");
    let controller = &view.receivers[0].controllers[0];
    assert!(matches!(
        controller.ownership,
        ControllerOwnership::OwnedByViewer(_)
    ));
    assert!(controller.actions.can_release);
    assert!(controller.actions.can_submit_now);

    unowned.receivers[0].ownership[0].client_id = text("polychromatic-client");
    let view = project_integration_view(&unowned, &catalog, Some(&viewer))
        .expect("other ownership projects");
    let controller = &view.receivers[0].controllers[0];
    assert!(matches!(
        controller.ownership,
        ControllerOwnership::OwnedByOther(_)
    ));
    assert!(!controller.actions.can_acquire);
    assert!(!controller.actions.can_submit_now);
}

#[test]
fn exact_upstream_presentation_and_application_topology_are_exposed() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let mouse = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    let keyboard = qualified_device(
        &catalog,
        "keyboard",
        0x0296,
        PresenceState::Available,
        vec![wireless_endpoint("keyboard-wireless")],
    );
    let view = project_integration_view(&snapshot(&catalog, vec![mouse, keyboard]), &catalog, None)
        .expect("qualified presentation projects");
    let mouse = view.receivers[0]
        .controllers
        .iter()
        .find(|controller| controller.device_kind == DeviceKind::Mouse)
        .expect("mouse controller exists");
    assert_eq!(mouse.presentation.owner.as_str(), "OpenRGB");
    assert_eq!(
        mouse.presentation.model_key.as_str(),
        "basilisk_v3_pro_35k_wireless_device"
    );
    assert_eq!(mouse.lighting.application_slot_count.get(), 13);
    let keyboard = view.receivers[0]
        .controllers
        .iter()
        .find(|controller| controller.device_kind == DeviceKind::Keyboard)
        .expect("keyboard controller exists");
    assert_eq!(
        keyboard
            .presentation
            .layout_key
            .as_ref()
            .map(PresentationKey::as_str),
        Some("razer_deathstalker_v2_pro_tkl_layout")
    );
    assert_eq!(keyboard.lighting.application_slot_count.get(), 102);
    assert_eq!(
        (
            keyboard.lighting.rows.get(),
            keyboard.lighting.columns.get()
        ),
        (6, 17)
    );
    serde_json::to_vec(&view).expect("view has a portable serialized form");
}

#[test]
fn malformed_or_drifted_authority_fails_before_an_adapter_can_register() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let mut contradictory = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![endpoint(
            "mouse-wireless",
            RouteKind::HyperfluxWireless,
            ConnectionMode::Hyperflux24ghz,
            RouteState::Unavailable,
            PowerState::Unknown,
            SleepState::Unknown,
            FreshnessState::Fresh,
        )],
    );
    let invalid = snapshot(&catalog, vec![contradictory.clone()]);
    assert_eq!(
        project_integration_view(&invalid, &catalog, None),
        Err(ViewModelError::InvalidSnapshot(
            SnapshotValidationError::PresenceContradiction
        ))
    );

    contradictory.presence = PresenceState::Unknown;
    contradictory.profile_id = Some(text::<ProfileId>("child.unknown.profile"));
    assert!(matches!(
        project_integration_view(&snapshot(&catalog, vec![contradictory]), &catalog, None),
        Err(ViewModelError::UnknownDeviceProfile { .. })
    ));

    let mut drifted = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    drifted.capabilities.pop();
    assert_eq!(
        project_integration_view(&snapshot(&catalog, vec![drifted]), &catalog, None),
        Err(ViewModelError::DeviceCapabilitiesMismatch(text("mouse")))
    );
}

#[test]
fn legacy_incomplete_bindings_and_unqualified_capability_claims_fail_closed() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let mut incomplete = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    incomplete.profile_digest = None;
    assert_eq!(
        project_integration_view(&snapshot(&catalog, vec![incomplete]), &catalog, None),
        Err(ViewModelError::IncompleteDeviceProfileBinding(text(
            "mouse"
        )))
    );

    let mut invented = unknown_device("unknown");
    invented.capabilities = vec![text("lighting.direct-frame")];
    assert_eq!(
        project_integration_view(&snapshot(&catalog, vec![invented]), &catalog, None),
        Err(ViewModelError::UnqualifiedDeviceClaimsCapabilities(text(
            "unknown"
        )))
    );
}

#[test]
fn zero_and_stale_battery_truth_survives_the_view_model() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let mut device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    device.battery = battery(
        TelemetryAvailability::Reported,
        Some(0),
        FreshnessState::Stale,
    );
    let view = project_integration_view(&snapshot(&catalog, vec![device]), &catalog, None)
        .expect("battery truth projects");
    let battery = &view.receivers[0].inventory[0].battery;
    assert_eq!(battery.percentage.map(BatteryPercent::get), Some(0));
    assert_eq!(battery.freshness, FreshnessState::Stale);
}

#[test]
fn suspended_receiver_and_stale_wireless_route_create_no_writable_controller() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![wireless_endpoint("mouse-wireless")],
    );
    let mut suspended = snapshot(&catalog, vec![device.clone()]);
    suspended.receivers[0].lifecycle = ReceiverLifecycleState::Suspended;
    let view =
        project_integration_view(&suspended, &catalog, None).expect("suspended inventory projects");
    assert_eq!(
        view.receivers[0].inventory[0].availability,
        InventoryAvailability::ReceiverUnavailable
    );
    assert!(view.receivers[0].controllers.is_empty());

    let mut stale = device;
    stale.endpoints[0].freshness = FreshnessState::Stale;
    let view = project_integration_view(&snapshot(&catalog, vec![stale]), &catalog, None)
        .expect("stale route remains inventory only");
    assert!(view.receivers[0].controllers.is_empty());
}

#[test]
fn multiple_usable_wireless_routes_are_rejected_as_ambiguous() {
    let catalog = RuntimeProfileCatalog::load().expect("profile catalog is valid");
    let device = qualified_device(
        &catalog,
        "mouse",
        0x00cd,
        PresenceState::Available,
        vec![
            wireless_endpoint("mouse-wireless-a"),
            wireless_endpoint("mouse-wireless-b"),
        ],
    );
    assert_eq!(
        project_integration_view(&snapshot(&catalog, vec![device]), &catalog, None),
        Err(ViewModelError::AmbiguousHyperfluxRoute(text("mouse")))
    );
}
