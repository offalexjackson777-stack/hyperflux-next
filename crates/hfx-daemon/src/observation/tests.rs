// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_kernel_transport::HFX_UAPI_ENDPOINT_SLOT_RECEIVER;

const RETURN_RETRIES: &[u64] = &[
    0, 100, 200, 300, 500, 750, 1_000, 1_500, 2_250, 3_250, 4_500, 6_000,
];
const QUIESCENCE_CANDIDATE_MS: u64 = 60_000;

fn translator() -> PassiveObservationTranslator {
    PassiveObservationTranslator::new(
        ReceiverId::try_from("receiver-test").expect("receiver id is valid"),
        GenerationId::try_from(7_u64).expect("generation is valid"),
        RuntimeProfileCatalog::load().expect("profile catalog is valid"),
    )
}

fn raw(sequence: u64, kind: u32, slot: u32, value: u32) -> RawKernelObservation {
    raw_at(sequence, sequence, kind, slot, value)
}

fn raw_at(
    sequence: u64,
    observed_at_ms: u64,
    kind: u32,
    slot: u32,
    value: u32,
) -> RawKernelObservation {
    RawKernelObservation {
        sequence,
        observed_boottime_ns: observed_at_ms * 1_000_000,
        kind,
        endpoint_slot: slot,
        source: HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
        confidence: HFX_UAPI_OBSERVATION_CONFIDENCE_RAW,
        value,
        auxiliary: 0,
    }
}

fn activity(sequence: u64, observed_at_ms: u64, slot: u32) -> RawKernelObservation {
    let mut observation = raw_at(
        sequence,
        observed_at_ms,
        HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
        slot,
        1,
    );
    observation.source = HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT;
    observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED;
    observation
}

fn identify(
    translator: &mut PassiveObservationTranslator,
    sequence: u64,
    slot: u32,
    product_id: u32,
) -> PassiveTranslation {
    let mut observation = raw(
        sequence,
        HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
        slot,
        product_id,
    );
    observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
    translator
        .translate(observation)
        .expect("identity observation translates")
}

#[test]
fn bounded_feature_inventory_uses_the_same_generation_identity_gate() {
    let mut translator = translator();
    let mut observation = raw(
        1,
        HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    observation.source = HFX_UAPI_OBSERVATION_SOURCE_HID_FEATURE;
    observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;

    let identity = translator
        .translate(observation)
        .expect("feature inventory identity translates");
    assert_eq!(identity.disposition, PassiveDisposition::Applied);
    assert!(matches!(
        &identity.observations[0].kind,
        LifecycleObservationKind::RegisterDevice(child)
            if child.device_kind() == DeviceKind::Mouse
                && child.product_id().get() == 0x00cd
    ));
}

#[test]
fn cached_identity_replay_without_child_route_evidence_remains_identity_only() {
    let mut translator = translator();
    let read_only = identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    assert_eq!(read_only.observations.len(), 3);
    assert!(!read_only.observations.iter().any(|observation| matches!(
        observation.kind,
        LifecycleObservationKind::Route {
            value: RouteState::Available,
            ..
        }
    )));

    for (sequence, slot, product_id) in [
        (2, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE, 0x00cd),
        (3, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, 0x0296),
    ] {
        let replay = identify(&mut translator, sequence, slot, product_id);
        assert_eq!(replay.observations.len(), 3);
        assert!(
            !replay.observations.iter().any(|observation| matches!(
                observation.kind,
                LifecycleObservationKind::Route { .. }
            ))
        );
        assert!(!replay.observations.iter().any(|observation| matches!(
            observation.kind,
            LifecycleObservationKind::Freshness { .. }
        )));
    }
}

#[test]
fn explicit_unavailable_route_survives_cached_identity_replay() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    translator
        .translate(raw_at(
            2,
            1_000,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            2,
        ))
        .expect("route departure translates");

    let replay = identify(
        &mut translator,
        3,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    assert_eq!(replay.observations.len(), 3);
    assert!(!replay.observations.iter().any(|observation| matches!(
        observation.kind,
        LifecycleObservationKind::Route { .. } | LifecycleObservationKind::Freshness { .. }
    )));

    let returned = translator
        .translate(raw_at(
            4,
            1_010,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            3,
        ))
        .expect("route return translates");
    assert_eq!(
        returned.restore_ready.map(|readiness| readiness.phase),
        Some(DeviceRestoreReadinessPhase::EpochOpened)
    );
}

#[test]
fn qualified_mouse_decoder_projects_identity_contact_route_and_battery() {
    let mut translator = translator();
    let identity = identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    assert_eq!(identity.disposition, PassiveDisposition::Applied);
    assert_eq!(identity.observations.len(), 3);
    assert!(matches!(
        &identity.observations[0].kind,
        LifecycleObservationKind::RegisterDevice(child)
            if child.device_kind() == DeviceKind::Mouse
                && child.product_id().get() == 0x00cd
    ));

    let contact = translator
        .translate(raw(
            2,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        ))
        .expect("contact translates");
    assert!(matches!(
        &contact.observations[0].kind,
        LifecycleObservationKind::Contact {
            value: ContactState::OnMat,
            ..
        }
    ));
    assert!(matches!(
        &contact.observations[1].kind,
        LifecycleObservationKind::Route {
            value: RouteState::Available,
            ..
        }
    ));
    assert!(matches!(
        &contact.observations[2].kind,
        LifecycleObservationKind::Freshness {
            value: FreshnessState::Fresh,
            ..
        }
    ));
    assert_eq!(contact.restore_ready, None);

    let battery = translator
        .translate(raw(
            3,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            128,
        ))
        .expect("battery translates");
    assert!(matches!(
        &battery.observations[0].kind,
        LifecycleObservationKind::BatteryReported { percentage, .. }
            if percentage.get() == 50
    ));
    assert_eq!(battery.restore_ready, None);
}

fn charge_after_contact(
    translator: &mut PassiveObservationTranslator,
    sequence: u64,
    contact: Option<u32>,
    charge: u32,
) -> PassiveTranslation {
    if let Some(contact) = contact {
        translator
            .translate(raw(
                sequence - 1,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                contact,
            ))
            .expect("contact translates");
    }
    translator
        .translate(raw(
            sequence,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CHARGE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            charge,
        ))
        .expect("charge translates")
}

#[test]
fn qualified_mouse_charge_field_reads_in_the_context_of_its_report() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    translator
        .translate(raw(
            2,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            200,
        ))
        .expect("battery translates");

    let off_mat = charge_after_contact(&mut translator, 4, Some(0), 0);
    assert_eq!(off_mat.disposition, PassiveDisposition::Applied);
    assert!(off_mat.observations.is_empty());
    assert_eq!(off_mat.restore_ready, None);
    assert_eq!(
        off_mat.charge,
        Some((ChildBatteryLane::Pointer, ChildChargeReading::Discharging))
    );

    // (sequence, contact in the same report, charge field, decoded reading)
    let cases = [
        // Only byte 3 = 00 proves discharging; a not-charging value beside
        // any other byte 3 value stays unknown.
        (6, Some(1), 0, ChildChargeReading::Unknown),
        (8, Some(1), 1, ChildChargeReading::Charging),
        (10, Some(0), 1, ChildChargeReading::Charging),
        (12, Some(0), 7, ChildChargeReading::Unknown),
        (14, Some(9), 0, ChildChargeReading::Unknown),
        // The contact of the previous report was already consumed.
        (15, None, 0, ChildChargeReading::Unknown),
        // Byte 3 read 06 on a USB cable: charging still reads charging, and
        // not charging beside it is unknown.
        (17, Some(6), 1, ChildChargeReading::Charging),
        (19, Some(6), 0, ChildChargeReading::Unknown),
        (21, Some(6), 7, ChildChargeReading::Unknown),
    ];
    for (sequence, contact, charge, expected) in cases {
        let translation = charge_after_contact(&mut translator, sequence, contact, charge);
        assert_eq!(
            translation.charge,
            Some((ChildBatteryLane::Pointer, expected)),
            "contact {contact:?} with charge {charge}"
        );
    }

    // A contact that is not immediately before the charge field belongs to
    // another report and proves nothing about this one.
    translator
        .translate(raw(
            22,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0,
        ))
        .expect("contact translates");
    let gap = charge_after_contact(&mut translator, 24, None, 0);
    assert_eq!(
        gap.charge,
        Some((ChildBatteryLane::Pointer, ChildChargeReading::Unknown))
    );
}

#[test]
fn the_observed_cable_plug_in_reads_discharging_then_charging() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    // Each status report arrives as battery, byte 3 and charge fields with
    // consecutive sequence numbers. On plug-in the first report still read
    // byte 3 = 00 and charge 00; the next read byte 3 = 06 and charge 01.
    let mut sequence = 2;
    let mut report = |byte3, charge| {
        translator
            .translate(raw(
                sequence,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                0xcd,
            ))
            .expect("battery translates");
        let translation = charge_after_contact(&mut translator, sequence + 2, Some(byte3), charge);
        sequence += 3;
        translation.charge
    };
    assert_eq!(
        report(0x00, 0x00),
        Some((ChildBatteryLane::Pointer, ChildChargeReading::Discharging))
    );
    assert_eq!(
        report(0x06, 0x01),
        Some((ChildBatteryLane::Pointer, ChildChargeReading::Charging))
    );
}

#[test]
fn charge_without_a_qualified_decoder_or_lane_is_ignored() {
    let mut translator = translator();
    let charge = |translator: &mut PassiveObservationTranslator, sequence, slot| {
        translator
            .translate(raw(
                sequence,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CHARGE_RAW,
                slot,
                1,
            ))
            .expect("charge translates")
    };

    for slot in [
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
    ] {
        let unbound = charge(&mut translator, 1, slot);
        assert_eq!(unbound.disposition, PassiveDisposition::IgnoredUnknownLane);
        assert_eq!(unbound.charge, None);
    }

    identify(
        &mut translator,
        2,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    let keyboard = charge(&mut translator, 3, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
    assert_eq!(
        keyboard.disposition,
        PassiveDisposition::IgnoredUnqualifiedSemantic
    );
    assert_eq!(keyboard.charge, None);

    identify(
        &mut translator,
        4,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x1234,
    );
    let unknown = charge(&mut translator, 5, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
    assert_eq!(
        unknown.disposition,
        PassiveDisposition::IgnoredUnqualifiedSemantic
    );
    assert_eq!(unknown.charge, None);
}

#[test]
fn qualified_keyboard_route_decoder_preserves_available_and_unavailable() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    for (sequence, raw_value, expected) in [
        (2, 3, RouteState::Available),
        (3, 2, RouteState::Unavailable),
    ] {
        let translated = translator
            .translate(raw(
                sequence,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
                HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
                raw_value,
            ))
            .expect("route translates");
        assert!(matches!(
            &translated.observations[0].kind,
            LifecycleObservationKind::Route { value, .. } if *value == expected
        ));
        assert!(matches!(
            &translated.observations[1].kind,
            LifecycleObservationKind::Freshness {
                value: FreshnessState::Fresh,
                ..
            }
        ));
    }
}

#[test]
fn exact_unavailable_to_available_route_edge_opens_a_keyboard_return_epoch() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    let unavailable = translator
        .translate(raw_at(
            2,
            1_000,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            2,
        ))
        .expect("route departure translates");
    assert_eq!(unavailable.restore_ready, None);

    let returned = translator
        .translate(raw_at(
            3,
            1_010,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            3,
        ))
        .expect("route return translates");
    let readiness = returned.restore_ready.expect("route edge opens an epoch");
    assert_eq!(readiness.phase, DeviceRestoreReadinessPhase::EpochOpened);
    assert_eq!(
        readiness.epoch_sequence,
        SequenceNumber::try_from(3).unwrap()
    );
    assert_eq!(
        readiness.opened_at_ms,
        MonotonicMs::try_from(1_010).unwrap()
    );
    assert_eq!(readiness.retry_offsets_ms, RETURN_RETRIES);
}

#[test]
fn mouse_contact_accelerates_once_and_does_not_open_without_activity() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let first_contact = translator
        .translate(raw_at(
            2,
            900,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        ))
        .expect("contact telemetry translates");
    assert_eq!(first_contact.restore_ready, None);
    translator
        .translate(activity(3, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("activity opens an epoch");
    let accelerated = translator
        .translate(raw_at(
            4,
            1_050,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        ))
        .expect("contact accelerates the active epoch");
    assert_eq!(
        accelerated.restore_ready.as_ref().map(|value| value.phase),
        Some(DeviceRestoreReadinessPhase::EpochAccelerated)
    );
    let repeated = translator
        .translate(raw_at(
            5,
            1_100,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        ))
        .expect("repeated contact remains telemetry");
    assert_eq!(repeated.restore_ready, None);
}

#[test]
fn profiles_share_one_engine_policy_and_select_evidence_without_pid_branches() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    identify(
        &mut translator,
        2,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    let mouse = translator
        .lanes
        .get(&HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE)
        .and_then(|binding| binding.return_lifecycle.as_ref())
        .expect("qualified mouse has a generated policy");
    let keyboard = translator
        .lanes
        .get(&HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE)
        .and_then(|binding| binding.return_lifecycle.as_ref())
        .expect("qualified keyboard has a generated policy");
    assert_eq!(mouse.policy().retry_offsets_ms(), RETURN_RETRIES);
    assert_eq!(keyboard.policy().retry_offsets_ms(), RETURN_RETRIES);
    assert_eq!(
        mouse.policy().open_evidence(),
        &[ReturnEvidence::EndpointActivity]
    );
    assert_eq!(
        keyboard.policy().open_evidence(),
        &[
            ReturnEvidence::EndpointActivity,
            ReturnEvidence::RouteReturn
        ]
    );
}

#[test]
fn externally_powered_keyboard_route_cannot_override_observed_activity() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    let active = translator
        .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("keyboard activity translates");
    assert!(matches!(
        &active.observations[0].kind,
        LifecycleObservationKind::Route {
            value: RouteState::Available,
            ..
        }
    ));

    let mut external_power = raw(
        3,
        HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0101,
    );
    external_power.auxiliary = 2;
    let route = translator
        .translate(external_power)
        .expect("indeterminate external-power route is bounded");
    assert_eq!(
        route.disposition,
        PassiveDisposition::IgnoredIndeterminateSemantic
    );
    assert!(route.observations.is_empty());
}

#[test]
fn unknown_child_keeps_identity_and_activity_but_inherits_no_decoder() {
    let mut translator = translator();
    let identity = identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x1234,
    );
    assert!(matches!(
        &identity.observations[0].kind,
        LifecycleObservationKind::RegisterDevice(child)
            if child.device_kind() == DeviceKind::Unknown
    ));

    let battery = translator
        .translate(raw(
            2,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            200,
        ))
        .expect("unknown battery is safely ignored");
    assert_eq!(
        battery.disposition,
        PassiveDisposition::IgnoredUnqualifiedSemantic
    );

    let mut activity = raw(
        3,
        HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        1,
    );
    activity.source = HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT;
    activity.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED;
    let activity = translator
        .translate(activity)
        .expect("activity translates without a model decoder");
    assert_eq!(activity.observations.len(), 5);
    assert_eq!(activity.restore_ready, None);
}

#[test]
fn first_generation_activity_and_first_activity_after_quiet_emit_restore_readiness() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );

    let first = translator
        .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("first activity translates");
    assert_eq!(
        first
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-mouse-00cd")
    );
    let first_readiness = first.restore_ready.as_ref().expect("startup epoch opens");
    assert_eq!(
        first_readiness.opened_at_ms,
        MonotonicMs::try_from(1_000).unwrap()
    );
    assert_eq!(
        first_readiness.evidence_at_ms,
        MonotonicMs::try_from(1_000).unwrap()
    );
    assert_eq!(first_readiness.retry_offsets_ms, RETURN_RETRIES);
    assert_eq!(
        first
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.phase),
        Some(DeviceRestoreReadinessPhase::EpochOpened)
    );

    let resumed = translator
        .translate(activity(
            3,
            1_000 + QUIESCENCE_CANDIDATE_MS,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        ))
        .expect("post-quiet activity translates");
    assert_eq!(
        resumed
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-mouse-00cd")
    );

    let continuous = translator
        .translate(activity(
            4,
            1_001 + QUIESCENCE_CANDIDATE_MS,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        ))
        .expect("continuous activity translates");
    let continuous = continuous
        .restore_ready
        .expect("fresh activity accelerates the existing bounded epoch");
    assert_eq!(
        continuous.phase,
        DeviceRestoreReadinessPhase::EpochAccelerated
    );
    assert_eq!(
        continuous.epoch_sequence,
        SequenceNumber::try_from(3).unwrap()
    );
}

#[test]
fn keyboard_battery_accelerates_one_active_epoch_without_extending_it() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );

    let candidate = translator
        .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("keyboard activity opens a bounded return window");
    let readiness = candidate
        .restore_ready
        .expect("keyboard activity schedules a fallback");
    assert_eq!(readiness.retry_offsets_ms, RETURN_RETRIES);
    assert_eq!(readiness.phase, DeviceRestoreReadinessPhase::EpochOpened);
    assert_eq!(
        readiness.opened_at_ms,
        MonotonicMs::try_from(1_000).unwrap()
    );

    let confirmed = translator
        .translate(raw_at(
            3,
            2_900,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            128,
        ))
        .expect("fresh keyboard battery evidence confirms return readiness");
    let readiness = confirmed
        .restore_ready
        .expect("the first fresh battery report confirms the child route");
    assert_eq!(
        readiness.phase,
        DeviceRestoreReadinessPhase::EpochAccelerated
    );
    assert_eq!(
        readiness.epoch_sequence,
        SequenceNumber::try_from(2).unwrap()
    );
    assert_eq!(
        readiness.opened_at_ms,
        MonotonicMs::try_from(1_000).unwrap()
    );
    assert_eq!(
        readiness.evidence_at_ms,
        MonotonicMs::try_from(2_900).unwrap()
    );
    assert_eq!(readiness.retry_offsets_ms, RETURN_RETRIES);

    let repeated = translator
        .translate(raw_at(
            4,
            3_000,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            128,
        ))
        .expect("repeated battery evidence remains telemetry only");
    assert_eq!(repeated.restore_ready, None);
}

#[test]
fn late_keyboard_battery_cannot_reopen_an_expired_return_window() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    translator
        .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("keyboard activity opens a bounded return window");

    let late = translator
        .translate(raw_at(
            3,
            7_001,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            128,
        ))
        .expect("late battery evidence remains valid telemetry");
    assert_eq!(late.restore_ready, None);
}

#[test]
fn identity_replay_preserves_activity_continuity_and_sibling_isolation() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    identify(
        &mut translator,
        2,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    translator
        .translate(activity(3, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("mouse activity translates");
    translator
        .translate(activity(4, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("keyboard activity translates");

    identify(
        &mut translator,
        5,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let mouse = translator
        .translate(activity(
            6,
            1_000 + QUIESCENCE_CANDIDATE_MS,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        ))
        .expect("mouse resumes after identity replay");
    assert_eq!(
        mouse
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-mouse-00cd")
    );
    assert_eq!(
        mouse
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.retry_offsets_ms.as_slice()),
        Some(RETURN_RETRIES)
    );

    let keyboard = translator
        .translate(activity(7, 1_001, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("keyboard remains continuous");
    let keyboard = keyboard
        .restore_ready
        .expect("fresh sibling activity accelerates only its own epoch");
    assert_eq!(
        keyboard.phase,
        DeviceRestoreReadinessPhase::EpochAccelerated
    );
    assert_eq!(
        keyboard.epoch_sequence,
        SequenceNumber::try_from(4).unwrap()
    );
}

#[test]
fn a_new_generation_first_activity_is_restore_ready() {
    let mut next_generation = PassiveObservationTranslator::new(
        ReceiverId::try_from("receiver-test").expect("receiver id is valid"),
        GenerationId::try_from(8_u64).expect("generation is valid"),
        RuntimeProfileCatalog::load().expect("profile catalog is valid"),
    );
    identify(
        &mut next_generation,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let first = next_generation
        .translate(activity(
            2,
            QUIESCENCE_CANDIDATE_MS * 10,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        ))
        .expect("first generation-bound activity translates");
    assert_eq!(
        first
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-mouse-00cd")
    );
}

#[test]
fn lane_mismatch_and_identity_replacement_fail_closed() {
    let mut translator = translator();
    assert!(matches!(
        translator.translate(raw(
            1,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x00cd,
        )),
        Err(PassiveTranslationError::ProfileLaneMismatch(_))
    ));

    identify(
        &mut translator,
        2,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let conflict = identify(
        &mut translator,
        3,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x1234,
    );
    assert_eq!(conflict.disposition, PassiveDisposition::IdentityConflict);
    assert!(conflict.observations.is_empty());
}

#[test]
fn suspend_and_resume_are_generation_preserving_receiver_facts() {
    let mut translator = translator();
    for (sequence, raw_value, expected) in [
        (1, 1, ReceiverLifecycleState::Suspended),
        (2, 0, ReceiverLifecycleState::Active),
    ] {
        let mut observation = raw(
            sequence,
            HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
            HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
            raw_value,
        );
        observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
        let translated = translator
            .translate(observation)
            .expect("power observation translates");
        assert!(matches!(
            translated.observations[0].kind,
            LifecycleObservationKind::ReceiverState(value) if value == expected
        ));
    }
}

#[test]
fn short_same_generation_resume_requires_fresh_input_from_each_device() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    identify(
        &mut translator,
        2,
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        0x0296,
    );
    translator
        .translate(activity(3, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("initial mouse activity translates");
    translator
        .translate(activity(4, 1_001, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("initial keyboard activity translates");

    for (sequence, observed_at_ms, raw_value) in [(5, 1_002, 1), (6, 1_003, 0)] {
        let mut observation = raw_at(
            sequence,
            observed_at_ms,
            HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
            HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
            raw_value,
        );
        observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
        translator
            .translate(observation)
            .expect("power observation translates");
    }

    let mouse = translator
        .translate(activity(7, 1_004, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("post-resume mouse activity translates");
    assert_eq!(
        mouse
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-mouse-00cd")
    );
    assert_eq!(
        mouse
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.phase),
        Some(DeviceRestoreReadinessPhase::EpochOpened)
    );
    let keyboard = translator
        .translate(activity(8, 1_005, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
        .expect("post-resume keyboard activity translates");
    assert_eq!(
        keyboard
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.device_id.as_str()),
        Some("paired-keyboard-0296")
    );
    assert_eq!(
        keyboard
            .restore_ready
            .as_ref()
            .map(|readiness| readiness.phase),
        Some(DeviceRestoreReadinessPhase::EpochOpened)
    );
    let continuous = translator
        .translate(activity(9, 1_006, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("continuous mouse activity translates");
    let continuous = continuous
        .restore_ready
        .expect("fresh activity accelerates without opening another epoch");
    assert_eq!(
        continuous.phase,
        DeviceRestoreReadinessPhase::EpochAccelerated
    );
    assert_eq!(
        continuous.epoch_sequence,
        SequenceNumber::try_from(7).unwrap()
    );
}

#[test]
fn activity_between_suspend_and_active_remains_the_one_readiness_signal() {
    let mut translator = translator();
    identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    translator
        .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("initial activity translates");

    for (sequence, observed_at_ms, raw_value) in [(3, 1_001, 1), (5, 1_003, 0)] {
        let mut observation = raw_at(
            sequence,
            observed_at_ms,
            HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
            HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
            raw_value,
        );
        observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
        translator
            .translate(observation)
            .expect("power observation translates");

        if raw_value == 1 {
            let early = translator
                .translate(activity(4, 1_002, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
                .expect("pre-active input translates");
            assert_eq!(
                early
                    .restore_ready
                    .as_ref()
                    .map(|readiness| readiness.device_id.as_str()),
                Some("paired-mouse-00cd")
            );
        }
    }

    let continuous = translator
        .translate(activity(6, 1_004, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
        .expect("post-active input remains continuous");
    let continuous = continuous
        .restore_ready
        .expect("post-active input accelerates the already-open epoch");
    assert_eq!(
        continuous.phase,
        DeviceRestoreReadinessPhase::EpochAccelerated
    );
    assert_eq!(
        continuous.epoch_sequence,
        SequenceNumber::try_from(4).unwrap()
    );
}

#[test]
fn battery_lane_maps_only_identified_children_to_their_lane_and_model() {
    let mut translator = translator();
    let identity = identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let LifecycleObservationKind::RegisterDevice(child) = &identity.observations[0].kind else {
        panic!("identity registers the child");
    };
    let (lane, model_name) = translator
        .battery_lane(child.device_id())
        .expect("identified child has a battery lane");
    assert_eq!(lane, ChildBatteryLane::Pointer);
    assert_eq!(
        Some(model_name),
        translator
            .catalog
            .child(ProductId::try_from(0x00cd_u16).expect("product id is valid"))
            .map(|profile| profile.model_name)
    );

    let other = LogicalDeviceId::try_from("paired-keyboard-0000").expect("device id is valid");
    assert_eq!(translator.battery_lane(&other), None);
}

#[test]
fn wired_child_pid_zero_identity_is_rejected_and_keeps_the_wireless_mouse() {
    // A Basilisk on its USB cable makes the receiver send `05 3b 01 00 00`.
    // The kernel drops that report; this pins the daemon's second safety net.
    let mut translator = translator();
    let identity = identify(
        &mut translator,
        1,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    let LifecycleObservationKind::RegisterDevice(child) = &identity.observations[0].kind else {
        panic!("identity registers the child");
    };
    let mouse = child.device_id().clone();
    assert_eq!(mouse.as_str(), "paired-mouse-00cd");

    for (sequence, product_id) in [(2, 0x0000), (3, 0xffff)] {
        let mut observation = raw(
            sequence,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            product_id,
        );
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
        assert!(matches!(
            translator.translate(observation),
            Err(PassiveTranslationError::InvalidObservationValue)
        ));
        assert_eq!(
            translator.battery_lane(&mouse).map(|(lane, _)| lane),
            Some(ChildBatteryLane::Pointer)
        );
    }

    // The same child keeps its lane: later pointer reports still name it and
    // a repeated 00CD identity is not an identity conflict.
    let contact = translator
        .translate(raw(
            4,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        ))
        .expect("contact translates");
    assert_eq!(contact.disposition, PassiveDisposition::Applied);
    assert!(matches!(
        &contact.observations[0].kind,
        LifecycleObservationKind::Contact { device_id, value: ContactState::OnMat, .. }
            if *device_id == mouse
    ));
    let replay = identify(
        &mut translator,
        5,
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        0x00cd,
    );
    assert_eq!(replay.disposition, PassiveDisposition::Applied);
    assert!(matches!(
        &replay.observations[0].kind,
        LifecycleObservationKind::RegisterDevice(child) if *child.device_id() == mouse
    ));
}
