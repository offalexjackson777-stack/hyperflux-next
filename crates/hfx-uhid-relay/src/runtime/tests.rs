// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use std::cell::RefCell;
use std::fs;
use std::rc::Rc;
use std::time::{SystemTime, UNIX_EPOCH};

struct NotSentBridge;

impl crate::BridgeExchange for NotSentBridge {
    fn exchange(
        &mut self,
        _report: [u8; crate::FEATURE_REPORT_BYTES],
    ) -> crate::BridgeExchangeResult {
        crate::BridgeExchangeResult::NotSent(crate::BridgeExchangeFailure::TransportNotSent)
    }
}

enum ScriptOutcome {
    Serial(&'static [u8]),
    Status(u8),
    Failure(crate::BridgeExchangeFailure),
}

struct PublicationScriptBridge {
    outcomes: VecDeque<ScriptOutcome>,
    calls: Rc<RefCell<Vec<[u8; crate::FEATURE_REPORT_BYTES]>>>,
}

impl crate::BridgeExchange for PublicationScriptBridge {
    fn exchange(
        &mut self,
        report: [u8; crate::FEATURE_REPORT_BYTES],
    ) -> crate::BridgeExchangeResult {
        self.calls.borrow_mut().push(report);
        let outcome = self.outcomes.pop_front().unwrap_or(ScriptOutcome::Failure(
            crate::BridgeExchangeFailure::TransportNotSent,
        ));
        if let ScriptOutcome::Failure(failure) = outcome {
            return crate::BridgeExchangeResult::NotSent(failure);
        }
        let mut response = report;
        response[0] = match outcome {
            ScriptOutcome::Serial(_) => 0x02,
            ScriptOutcome::Status(status) => status,
            ScriptOutcome::Failure(_) => unreachable!(),
        };
        response[8..88].fill(0);
        if let ScriptOutcome::Serial(serial) = outcome {
            let end = 8 + serial.len();
            assert!(end <= 8 + usize::from(response[5]));
            response[8..end].copy_from_slice(serial);
        }
        response[88] = response[2..88]
            .iter()
            .copied()
            .fold(0_u8, |value, byte| value ^ byte);
        crate::BridgeExchangeResult::MatchedResponse(response)
    }
}

fn publication_bridge(
    outcomes: Vec<ScriptOutcome>,
) -> (
    PublicationScriptBridge,
    Rc<RefCell<Vec<[u8; crate::FEATURE_REPORT_BYTES]>>>,
) {
    let calls = Rc::new(RefCell::new(Vec::new()));
    (
        PublicationScriptBridge {
            outcomes: outcomes.into(),
            calls: Rc::clone(&calls),
        },
        calls,
    )
}

fn temporary_uhid_file() -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock is after the Unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "hyperflux-relay-drop-{}-{unique}.uhid",
        std::process::id()
    ));
    File::create(&path).expect("temporary UHID file is created");
    path
}

fn test_paths(uhid_device: PathBuf) -> RelayPaths {
    RelayPaths {
        bridge_socket: PathBuf::from("/unused/bridge.sock"),
        uhid_device,
        hid_driver_root: PathBuf::from("/unused/hid/drivers"),
        status_file: PathBuf::from("/unused/status.json"),
    }
}

fn fixture() -> (VirtualDeviceSpec, NativeCommandPolicy) {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog.routes().first().expect("catalog has one route");
    fixture_from_record(&catalog, record, "drop-contract")
}

fn fixture_from_record(
    catalog: &NativeRouteCatalog,
    record: &NativeRouteRecord,
    label: &str,
) -> (VirtualDeviceSpec, NativeCommandPolicy) {
    let device = VirtualDeviceSpec::new(
        record.model_name.clone(),
        record.vendor_id.get(),
        record.wireless_product_id.get(),
        catalog.descriptor().to_vec(),
        label,
    )
    .expect("virtual device is valid");
    (device, record.command_policy.clone())
}

fn battery_report(transaction_id: u8) -> [u8; crate::FEATURE_REPORT_BYTES] {
    let mut report = [0_u8; crate::FEATURE_REPORT_BYTES];
    report[1] = transaction_id;
    report[5] = 2;
    report[6] = 0x07;
    report[7] = 0x80;
    report[88] = report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    report
}

fn set_report(id: u32, report: [u8; crate::FEATURE_REPORT_BYTES]) -> UhidEvent {
    UhidEvent::SetReport {
        id,
        report_number: 0,
        report_type: 0,
        data: report.to_vec(),
    }
}

fn get_report(id: u32) -> UhidEvent {
    UhidEvent::GetReport {
        id,
        report_number: 0,
        report_type: 0,
    }
}

fn assert_single_uhid_lifecycle(path: &Path, expected_events: usize) {
    let bytes = fs::read(path).expect("UHID lifecycle transcript reads");
    assert_eq!(bytes.len(), UHID_EVENT_BYTES * expected_events);
    assert_eq!(
        u32::from_ne_bytes(bytes[0..4].try_into().expect("create type")),
        11
    );
    let destroy_offset = UHID_EVENT_BYTES * (expected_events - 1);
    assert_eq!(
        u32::from_ne_bytes(
            bytes[destroy_offset..destroy_offset + 4]
                .try_into()
                .expect("destroy type")
        ),
        1
    );
}

#[test]
fn exactly_two_matching_source_serial_reads_publish_one_endpoint() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("keyboard route exists")
        .clone();
    let (device, _) = fixture_from_record(&catalog, &record, "ready-keyboard");
    let (bridge, calls) = publication_bridge(vec![
        ScriptOutcome::Serial(b"STABLE-ROUTE-SERIAL"),
        ScriptOutcome::Serial(b"STABLE-ROUTE-SERIAL"),
    ]);
    let mut publication = PendingPublication::new(bridge, Instant::now());
    let mut serial_fingerprint = None;
    let mut endpoint = None;
    let direct = DirectRouteSnapshot::default();

    publication.probe(&record);
    assert!(endpoint.is_none());
    assert_eq!(
        publication.gate.next_delay,
        ROUTE_IDENTITY_STABILITY_INTERVAL
    );
    assert_eq!(fs::metadata(&path).expect("transcript exists").len(), 0);

    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(publication.ready());
    publication.complete(
        &paths,
        &direct,
        &record,
        &device,
        &mut serial_fingerprint,
        &mut endpoint,
    );
    assert!(serial_fingerprint.is_some());
    assert!(publication.bridge.is_none());
    let endpoint = endpoint.expect("ready route publishes one endpoint automatically");
    let observed = calls.borrow();
    assert_eq!(observed.len(), 2);
    assert!(
        observed
            .iter()
            .all(|request| request == &record.publication_identity.request())
    );
    assert_eq!(
        fs::metadata(&path).expect("transcript exists").len(),
        u64::try_from(UHID_EVENT_BYTES).expect("event size fits")
    );

    drop(endpoint);
    let bytes = fs::read(&path).expect("temporary UHID transcript reads");
    fs::remove_file(path).expect("temporary UHID file is removed");
    assert_eq!(bytes.len(), UHID_EVENT_BYTES * 2);
    assert_eq!(
        u32::from_ne_bytes(bytes[0..4].try_into().expect("create type")),
        11
    );
    assert_eq!(
        u32::from_ne_bytes(
            bytes[UHID_EVENT_BYTES..UHID_EVENT_BYTES + 4]
                .try_into()
                .expect("destroy type")
        ),
        1
    );
}

#[test]
fn fresh_evidence_resets_only_a_backing_off_publication_once() {
    let now = Instant::now();

    // Backing off after a retryable failure: fresh evidence probes next tick.
    let (bridge, _) = publication_bridge(Vec::new());
    let mut backing_off =
        PendingPublication::retrying(bridge, PublicationIdentityFailure::Status(0x04), now);
    assert_eq!(backing_off.gate.state(), RelayPublicationState::BackingOff);
    let later = now + Duration::from_millis(5);
    backing_off.fresh_evidence(later);
    assert!(backing_off.gate.due(later), "the next tick probes");
    assert_eq!(
        backing_off.gate.attempts, 1,
        "a later failure keeps normal backoff"
    );

    // Healthy (pending, no failure) and terminal publications are untouched.
    let (bridge, _) = publication_bridge(Vec::new());
    let mut healthy = PendingPublication::new(bridge, now);
    let probe_before = healthy.gate.next_probe;
    healthy.fresh_evidence(later);
    assert_eq!(healthy.gate.next_probe, probe_before);

    let (bridge, _) = publication_bridge(Vec::new());
    let mut blocked = PendingPublication::new(bridge, now);
    blocked.block(PublicationIdentityFailure::CrcMismatch);
    blocked.fresh_evidence(later);
    assert!(blocked.terminal_failure.is_some());
    assert!(!blocked.ready());
}

#[test]
fn kernel_binding_stop_republishes_on_the_same_bridge_instead_of_blocking() {
    // REL-003: at boot hid-generic can bind the first virtual device and then be
    // re-probed away by a late-loading OpenRazer module, stopping the device.
    let first_path = temporary_uhid_file();
    let second_path = temporary_uhid_file();
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("keyboard route exists")
        .clone();
    let (device, policy) = fixture_from_record(&catalog, &record, "rebinding-keyboard");
    let (bridge, calls) = publication_bridge(vec![
        ScriptOutcome::Serial(b"KEYBOARD-SERIAL"),
        ScriptOutcome::Serial(b"KEYBOARD-SERIAL"),
    ]);
    let mut endpoint = RunningEndpoint::create(
        &test_paths(first_path.clone()),
        bridge,
        device.clone(),
        policy,
        None,
    )
    .expect("published endpoint is created");
    endpoint
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("hid-generic starts the device");
    endpoint
        .process_event(UhidEvent::Stop)
        .expect("re-probe stops the device");
    assert_eq!(endpoint.state(), FeatureEndpointState::Stopped);

    assert_eq!(
        PublicationIdentityFailure::KernelBinding.disposition(),
        PublicationFailureDisposition::Retryable
    );
    let (placeholder, _) = publication_bridge(Vec::new());
    let now = Instant::now();
    let mut publication = PendingPublication::new(placeholder, now);
    let mut fingerprint =
        Some(crate::fingerprint_serial_bytes(b"KEYBOARD-SERIAL").expect("serial is valid"));
    recover_kernel_binding(endpoint, &mut publication, &mut fingerprint, now);

    assert_eq!(fingerprint, None);
    assert_eq!(
        publication.gate.failure,
        Some(PublicationIdentityFailure::KernelBinding)
    );
    assert_eq!(publication.gate.state(), RelayPublicationState::BackingOff);
    assert!(
        publication.terminal_failure.is_none(),
        "the route is not blocked"
    );

    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(
        publication.ready(),
        "the same bridge re-qualifies the device"
    );
    let mut returned_fingerprint = None;
    let mut returned_endpoint = None;
    publication.complete(
        &test_paths(second_path.clone()),
        &DirectRouteSnapshot::default(),
        &record,
        &device,
        &mut returned_fingerprint,
        &mut returned_endpoint,
    );
    assert!(
        returned_endpoint.is_some(),
        "a fresh virtual device is published"
    );
    assert_eq!(calls.borrow().len(), 2);

    drop(returned_endpoint.take());
    fs::remove_file(first_path).expect("first transcript is removed");
    fs::remove_file(second_path).expect("second transcript is removed");
}

#[test]
fn retained_route_timeout_withdraws_once_and_requires_fresh_serial_identity() {
    let first_path = temporary_uhid_file();
    let second_path = temporary_uhid_file();
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists")
        .clone();
    let (device, policy) = fixture_from_record(&catalog, &record, "retained-mouse");
    let request = battery_report(0x1f);
    let (bridge, calls) = publication_bridge(vec![
        ScriptOutcome::Status(0x02),
        ScriptOutcome::Status(0x04),
        ScriptOutcome::Serial(b"RETURNED-MOUSE-SERIAL"),
        ScriptOutcome::Serial(b"RETURNED-MOUSE-SERIAL"),
    ]);
    let mut endpoint = RunningEndpoint::create(
        &test_paths(first_path.clone()),
        bridge,
        device.clone(),
        policy,
        None,
    )
    .expect("published endpoint is created");
    endpoint
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("kernel starts endpoint");

    endpoint
        .process_event(set_report(1, request))
        .expect("confirmed battery SET is handled");
    endpoint
        .process_event(get_report(2))
        .expect("confirmed battery GET is handled");
    endpoint.flush().expect("confirmed replies flush");
    assert_eq!(calls.borrow().as_slice(), &[request]);

    endpoint
        .process_event(set_report(3, request))
        .expect("timeout battery SET is handled");
    endpoint
        .process_event(get_report(4))
        .expect("cached battery GET is handled");
    endpoint.flush().expect("departure replies flush");
    assert!(matches!(
        endpoint.step(),
        Ok(RunningEndpointStep::RetainedRouteDeparture)
    ));
    assert_eq!(calls.borrow().as_slice(), &[request, request]);

    let (placeholder, _) = publication_bridge(Vec::new());
    let now = Instant::now();
    let mut publication = PendingPublication::new(placeholder, now);
    let mut fingerprint = Some(
        crate::fingerprint_serial_bytes(b"PREVIOUS-MOUSE-SERIAL")
            .expect("previous serial is valid"),
    );
    recycle_retained_route(endpoint, &mut publication, &mut fingerprint, now);
    assert_eq!(fingerprint, None);
    assert_eq!(
        publication.gate.failure,
        Some(PublicationIdentityFailure::Status(0x04))
    );
    assert_eq!(publication.gate.state(), RelayPublicationState::BackingOff);

    assert_single_uhid_lifecycle(&first_path, 6);

    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(!publication.ready());
    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(publication.ready());
    let mut returned_fingerprint = None;
    let mut returned_endpoint = None;
    publication.complete(
        &test_paths(second_path.clone()),
        &DirectRouteSnapshot::default(),
        &record,
        &device,
        &mut returned_fingerprint,
        &mut returned_endpoint,
    );
    assert!(returned_fingerprint.is_some());
    assert!(returned_endpoint.is_some());
    assert_eq!(calls.borrow().len(), 4);
    assert_eq!(calls.borrow()[2], record.publication_identity.request());
    assert_eq!(calls.borrow()[3], record.publication_identity.request());

    drop(returned_endpoint.take());
    assert_single_uhid_lifecycle(&second_path, 2);
    fs::remove_file(first_path).expect("first transcript is removed");
    fs::remove_file(second_path).expect("second transcript is removed");
}

#[test]
fn retained_route_requalification_leaves_the_sibling_endpoint_untouched() {
    let mouse_path = temporary_uhid_file();
    let keyboard_path = temporary_uhid_file();
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let mouse_record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists");
    let keyboard_record = catalog
        .profile("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("keyboard route exists");
    let (mouse_device, mouse_policy) =
        fixture_from_record(&catalog, mouse_record, "departing-mouse");
    let (keyboard_device, keyboard_policy) =
        fixture_from_record(&catalog, keyboard_record, "stable-keyboard");
    let (mouse_bridge, _) = publication_bridge(vec![ScriptOutcome::Status(0x04)]);
    let (keyboard_bridge, keyboard_calls) = publication_bridge(Vec::new());
    let mut mouse = RunningEndpoint::create(
        &test_paths(mouse_path.clone()),
        mouse_bridge,
        mouse_device,
        mouse_policy,
        None,
    )
    .expect("mouse endpoint is created");
    let mut keyboard = RunningEndpoint::create(
        &test_paths(keyboard_path.clone()),
        keyboard_bridge,
        keyboard_device,
        keyboard_policy,
        None,
    )
    .expect("keyboard endpoint is created");
    mouse
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("mouse starts");
    keyboard
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("keyboard starts");
    mouse
        .process_event(set_report(1, battery_report(0x1f)))
        .expect("mouse timeout is handled");
    mouse
        .process_event(get_report(2))
        .expect("mouse timeout response is handled");
    mouse.flush().expect("mouse replies flush");

    let (placeholder, _) = publication_bridge(Vec::new());
    let mut publication = PendingPublication::new(placeholder, Instant::now());
    let mut fingerprint = None;
    recycle_retained_route(mouse, &mut publication, &mut fingerprint, Instant::now());

    assert_eq!(keyboard.state(), FeatureEndpointState::Started);
    assert!(keyboard_calls.borrow().is_empty());
    assert_eq!(
        fs::metadata(&keyboard_path)
            .expect("keyboard transcript exists")
            .len(),
        u64::try_from(UHID_EVENT_BYTES).expect("event size fits")
    );
    drop(keyboard);
    assert_single_uhid_lifecycle(&mouse_path, 4);
    assert_single_uhid_lifecycle(&keyboard_path, 2);
    fs::remove_file(mouse_path).expect("mouse transcript is removed");
    fs::remove_file(keyboard_path).expect("keyboard transcript is removed");
}

#[test]
fn identity_gate_enforces_stability_reset_and_exact_backoff() {
    let first = crate::fingerprint_serial_bytes(b"FIRST-ROUTE-SERIAL")
        .expect("first fixture serial is valid");
    let second = crate::fingerprint_serial_bytes(b"SECOND-ROUTE-SERIAL")
        .expect("second fixture serial is valid");
    let start = Instant::now();
    let mut gate = RoutePublicationGate::new(start);

    assert_eq!(gate.observe(Ok(first.clone()), start), None);
    assert!(!gate.due(start + Duration::from_millis(249)));
    assert!(gate.due(start + ROUTE_IDENTITY_STABILITY_INTERVAL));
    assert_eq!(
        gate.observe(Ok(first.clone()), start + ROUTE_IDENTITY_STABILITY_INTERVAL),
        Some(first.clone())
    );

    let mut mismatch = RoutePublicationGate::new(start);
    assert_eq!(mismatch.observe(Ok(second), start), None);
    assert_eq!(
        mismatch.observe(
            Ok(crate::fingerprint_serial_bytes(b"DRIFTED-ROUTE-SERIAL")
                .expect("drifted fixture serial is valid")),
            start + ROUTE_IDENTITY_STABILITY_INTERVAL,
        ),
        None
    );
    assert_eq!(mismatch.pass_one, None);
    assert_eq!(
        mismatch.failure,
        Some(PublicationIdentityFailure::IdentityMismatch)
    );
    assert_eq!(mismatch.next_delay, Duration::from_millis(100));

    let mut recovering = RoutePublicationGate::new(start);
    assert_eq!(
        recovering.observe(
            Err(PublicationIdentityFailure::Bridge(
                crate::BridgeExchangeFailure::TransportNotSent,
            )),
            start,
        ),
        None
    );
    let first_recovery = recovering.next_probe;
    assert_eq!(recovering.observe(Ok(first.clone()), first_recovery), None);
    assert_eq!(recovering.attempts, 1);
    let stable_recovery = recovering.next_probe;
    assert_eq!(
        recovering.observe(Ok(first), stable_recovery),
        Some(
            crate::fingerprint_serial_bytes(b"FIRST-ROUTE-SERIAL")
                .expect("fixture serial is valid")
        )
    );
    assert_eq!(recovering.attempts, 1);
    recovering.mark_completed();
    assert_eq!(recovering.attempts, 0);

    let mut failed = RoutePublicationGate::new(start);
    let mut now = start;
    let expected = [
        100, 100, 100, 100, 100, 1_000, 2_000, 4_000, 8_000, 16_000, 30_000,
    ];
    for (index, delay_ms) in expected.into_iter().enumerate() {
        assert!(failed.due(now));
        assert_eq!(
            failed.observe(
                Err(PublicationIdentityFailure::Bridge(
                    crate::BridgeExchangeFailure::TransportNotSent,
                )),
                now,
            ),
            None
        );
        assert_eq!(
            failed.attempts,
            u32::try_from(index + 1).expect("attempt fits")
        );
        assert_eq!(failed.next_delay, Duration::from_millis(delay_ms));
        now = failed.next_probe;
    }

    let mut blocked = RoutePublicationGate::new(start);
    assert_eq!(
        blocked.observe(Err(PublicationIdentityFailure::Status(0x05)), start),
        None
    );
    assert!(blocked.blocked);
    assert!(!blocked.due(blocked.next_probe));

    failed.attempts = MAX_ROUTE_IDENTITY_ATTEMPTS;
    failed.fail(PublicationIdentityFailure::Status(0x03), now);
    assert_eq!(failed.attempts, MAX_ROUTE_IDENTITY_ATTEMPTS);
}

#[test]
fn retained_departure_reprobes_at_most_every_two_seconds_inside_window() {
    let start = Instant::now();
    let (bridge, _) = publication_bridge(Vec::new());
    let mut publication = PendingPublication::retrying_after_departure(bridge, start);
    let window_end = start + RETAINED_DEPARTURE_FAST_WINDOW;
    assert_eq!(publication.gate.fast_until, Some(window_end));
    assert_eq!(publication.gate.state(), RelayPublicationState::BackingOff);

    let mut delays = vec![publication.gate.next_delay];
    let mut now = publication.gate.next_probe;
    while now < window_end {
        assert!(publication.gate.due(now));
        assert_eq!(
            publication.gate.observe(
                Err(PublicationIdentityFailure::Status(RAZER_STATUS_TIMEOUT)),
                now,
            ),
            None
        );
        assert!(publication.gate.next_delay <= RETAINED_DEPARTURE_PROBE_CAP);
        assert!(publication.gate.next_retry_ms() <= 2_000);
        delays.push(publication.gate.next_delay);
        now = publication.gate.next_probe;
    }
    assert_eq!(
        delays[..8]
            .iter()
            .map(Duration::as_millis)
            .collect::<Vec<_>>(),
        [100, 100, 100, 100, 100, 1_000, 2_000, 2_000],
        "the fast attempts keep their schedule and only the long tail is capped"
    );
    assert!(
        publication.gate.attempts > 11,
        "the uncapped tier was reached"
    );

    publication.gate.fail(
        PublicationIdentityFailure::Status(RAZER_STATUS_TIMEOUT),
        now,
    );
    assert_eq!(publication.gate.next_delay, Duration::from_secs(30));
    assert_eq!(publication.gate.next_retry_ms(), 30_000);
}

#[test]
fn retained_departure_fast_window_covers_every_return_quiescence() {
    let longest = hfx_profiles::RETURN_POLICIES
        .iter()
        .map(|policy| policy.quiescence_candidate_ms)
        .max()
        .expect("at least one return policy is generated");
    assert!(RETAINED_DEPARTURE_FAST_WINDOW >= Duration::from_millis(longest));
    assert!(RETAINED_DEPARTURE_PROBE_CAP < RETAINED_DEPARTURE_FAST_WINDOW);
}

#[test]
fn retained_departure_window_keeps_blocked_failures_blocked() {
    let start = Instant::now();
    let (bridge, _) = publication_bridge(Vec::new());
    let mut publication = PendingPublication::retrying_after_departure(bridge, start);
    let now = publication.gate.next_probe;
    assert_eq!(
        publication
            .gate
            .observe(Err(PublicationIdentityFailure::EmptySerial), now),
        None
    );
    assert!(publication.gate.blocked);
    assert_eq!(publication.gate.state(), RelayPublicationState::Blocked);
    assert!(!publication.gate.due(now + RETAINED_DEPARTURE_FAST_WINDOW));
    publication.fresh_evidence(now);
    assert!(!publication.gate.due(now + RETAINED_DEPARTURE_FAST_WINDOW));
}

#[test]
fn fast_window_is_cleared_on_publication_and_not_used_by_kernel_binding_recovery() {
    let first_path = temporary_uhid_file();
    let second_path = temporary_uhid_file();
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("keyboard route exists")
        .clone();
    let (device, policy) = fixture_from_record(&catalog, &record, "returning-keyboard");
    let (bridge, _) = publication_bridge(vec![
        ScriptOutcome::Serial(b"KEYBOARD-SERIAL"),
        ScriptOutcome::Serial(b"KEYBOARD-SERIAL"),
    ]);
    let endpoint = RunningEndpoint::create(
        &test_paths(first_path.clone()),
        bridge,
        device.clone(),
        policy,
        None,
    )
    .expect("published endpoint is created");

    let (placeholder, _) = publication_bridge(Vec::new());
    let first = PendingPublication::new(placeholder, Instant::now());
    assert_eq!(
        first.gate.fast_until, None,
        "first publication is unchanged"
    );

    let mut publication = first;
    let mut fingerprint = None;
    let now = Instant::now();
    recycle_retained_route(endpoint, &mut publication, &mut fingerprint, now);
    assert_eq!(
        publication.gate.fast_until,
        Some(now + RETAINED_DEPARTURE_FAST_WINDOW)
    );

    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(publication.ready());
    let mut returned_endpoint = None;
    publication.complete(
        &test_paths(second_path.clone()),
        &DirectRouteSnapshot::default(),
        &record,
        &device,
        &mut fingerprint,
        &mut returned_endpoint,
    );
    assert_eq!(publication.gate.fast_until, None, "publication clears it");
    let mut returned = returned_endpoint.expect("the returned child is published");
    returned
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("hid-generic starts the device");
    returned
        .process_event(UhidEvent::Stop)
        .expect("re-probe stops the device");

    let now = Instant::now();
    recover_kernel_binding(returned, &mut publication, &mut fingerprint, now);
    assert_eq!(
        publication.gate.failure,
        Some(PublicationIdentityFailure::KernelBinding)
    );
    assert_eq!(publication.gate.fast_until, None);
    publication.gate.attempts = 10;
    publication
        .gate
        .fail(PublicationIdentityFailure::KernelBinding, now);
    assert_eq!(
        publication.gate.next_delay,
        Duration::from_secs(30),
        "kernel-binding recovery keeps the normal backoff"
    );

    fs::remove_file(first_path).expect("first transcript is removed");
    fs::remove_file(second_path).expect("second transcript is removed");
}

#[test]
fn unknown_direct_state_never_means_absent_and_repeats_both_serial_reads() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists")
        .clone();
    let (device, _) = fixture_from_record(&catalog, &record, "direct-unknown");
    let (bridge, calls) = publication_bridge(vec![
        ScriptOutcome::Serial(b"STABLE-MOUSE-SERIAL"),
        ScriptOutcome::Serial(b"STABLE-MOUSE-SERIAL"),
        ScriptOutcome::Serial(b"STABLE-MOUSE-SERIAL"),
        ScriptOutcome::Serial(b"STABLE-MOUSE-SERIAL"),
    ]);
    let mut publication = PendingPublication::new(bridge, Instant::now());
    let mut serial_fingerprint = None;
    let mut endpoint = None;

    publication.probe(&record);
    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(publication.ready());
    publication.direct_scan_failed();
    assert!(!publication.ready());
    assert_eq!(
        publication.gate.failure,
        Some(PublicationIdentityFailure::DirectStateUnavailable)
    );
    assert_eq!(fs::metadata(&path).expect("transcript exists").len(), 0);

    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(!publication.ready());
    assert_eq!(publication.gate.attempts, 1);
    publication.gate.next_probe = Instant::now();
    publication.probe(&record);
    assert!(publication.ready());
    publication.complete(
        &paths,
        &DirectRouteSnapshot::default(),
        &record,
        &device,
        &mut serial_fingerprint,
        &mut endpoint,
    );
    assert!(endpoint.is_some());
    assert_eq!(publication.gate.attempts, 0);
    assert_eq!(calls.borrow().len(), 4);

    drop(endpoint.take());
    fs::remove_file(path).expect("transcript is removed");
}

#[test]
fn blocked_child_does_not_hide_a_healthy_sibling() {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let keyboard_record = catalog
        .profile("child.razer.deathstalker-v2-pro-tkl.0296")
        .expect("keyboard route exists")
        .clone();
    let mouse_record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists")
        .clone();
    let (mouse_device, _) = fixture_from_record(&catalog, &mouse_record, "healthy-mouse");
    let keyboard_path = temporary_uhid_file();
    let mouse_path = temporary_uhid_file();
    let (keyboard_bridge, _) = publication_bridge(vec![ScriptOutcome::Status(0x05)]);
    let (mouse_bridge, _) = publication_bridge(vec![
        ScriptOutcome::Serial(b"MOUSE-SERIAL"),
        ScriptOutcome::Serial(b"MOUSE-SERIAL"),
    ]);
    let mut keyboard = PendingPublication::new(keyboard_bridge, Instant::now());
    let mut mouse = PendingPublication::new(mouse_bridge, Instant::now());
    let mut mouse_fingerprint = None;
    let mut mouse_endpoint = None;
    let direct = DirectRouteSnapshot::default();

    keyboard.probe(&keyboard_record);
    assert!(keyboard.gate.blocked);

    mouse.probe(&mouse_record);
    mouse.gate.next_probe = Instant::now();
    mouse.probe(&mouse_record);
    assert!(mouse.ready());
    mouse.complete(
        &test_paths(mouse_path.clone()),
        &direct,
        &mouse_record,
        &mouse_device,
        &mut mouse_fingerprint,
        &mut mouse_endpoint,
    );
    drop(mouse_endpoint.take().expect("healthy sibling publishes"));

    assert_eq!(
        fs::metadata(&keyboard_path)
            .expect("keyboard file exists")
            .len(),
        0
    );
    assert_eq!(
        fs::metadata(&mouse_path)
            .expect("mouse transcript exists")
            .len(),
        u64::try_from(UHID_EVENT_BYTES * 2).expect("lifecycle length fits")
    );
    fs::remove_file(keyboard_path).expect("keyboard file is removed");
    fs::remove_file(mouse_path).expect("mouse file is removed");
}

#[test]
fn unexpected_session_drop_emits_one_destroy_after_one_create() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let (device, policy) = fixture();
    let endpoint = RunningEndpoint::create(&paths, NotSentBridge, device, policy, None)
        .expect("endpoint is created");

    drop(endpoint);

    let bytes = fs::read(&path).expect("temporary UHID transcript reads");
    fs::remove_file(path).expect("temporary UHID file is removed");
    assert_eq!(bytes.len(), UHID_EVENT_BYTES * 2);
    assert_eq!(
        u32::from_ne_bytes(bytes[0..4].try_into().expect("create type")),
        11
    );
    assert_eq!(
        u32::from_ne_bytes(
            bytes[UHID_EVENT_BYTES..UHID_EVENT_BYTES + 4]
                .try_into()
                .expect("destroy type")
        ),
        1
    );
}

#[test]
fn teardown_answers_an_in_flight_get_before_unbinding_and_destroying() {
    let path = temporary_uhid_file();
    let tree = teardown::tests::DriverTree::create();
    let mut paths = test_paths(path.clone());
    paths.hid_driver_root = tree.0.clone();
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists");
    let (device, policy) = fixture_from_record(&catalog, record, "teardown-mouse");
    tree.bind("0003:1532:00CD.0007", &device.phys);
    let request = battery_report(0x1f);
    let (bridge, calls) = publication_bridge(vec![ScriptOutcome::Status(0x02)]);
    let mut endpoint =
        RunningEndpoint::create(&paths, bridge, device, policy, Some(&record.driver_family))
            .expect("endpoint is created");
    endpoint
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("kernel starts endpoint");
    endpoint
        .process_event(set_report(1, request))
        .expect("battery SET is handled");
    endpoint.flush().expect("SET reply flushes");

    // The driver's GET is already queued when the route is withdrawn.
    let mut get = vec![0_u8; UHID_EVENT_BYTES];
    get[0..4].copy_from_slice(&9_u32.to_ne_bytes());
    get[4..8].copy_from_slice(&2_u32.to_ne_bytes());
    OpenOptions::new()
        .append(true)
        .open(&path)
        .and_then(|mut file| file.write_all(&get))
        .expect("in-flight GET is queued");
    drop(endpoint);

    let bytes = fs::read(&path).expect("UHID transcript reads");
    fs::remove_file(&path).expect("temporary UHID file is removed");
    let event_type = |index: usize| {
        u32::from_ne_bytes(
            bytes[UHID_EVENT_BYTES * index..UHID_EVENT_BYTES * index + 4]
                .try_into()
                .expect("event type"),
        )
    };
    assert_eq!(bytes.len(), UHID_EVENT_BYTES * 5);
    assert_eq!(
        (0..5).map(event_type).collect::<Vec<_>>(),
        vec![11, 14, 9, 10, 1]
    );
    let reply = UHID_EVENT_BYTES * 3;
    assert_eq!(
        u32::from_ne_bytes(bytes[reply + 4..reply + 8].try_into().expect("reply id")),
        2
    );
    assert_eq!(
        u16::from_ne_bytes(bytes[reply + 8..reply + 10].try_into().expect("reply err")),
        0
    );
    assert_eq!(calls.borrow().as_slice(), &[request]);
    assert_eq!(tree.unbind_request(), "0003:1532:00CD.0007");
}

struct TranscriptLengthBridge {
    path: PathBuf,
    lengths: Rc<RefCell<Vec<usize>>>,
}

impl crate::BridgeExchange for TranscriptLengthBridge {
    fn exchange(
        &mut self,
        report: [u8; crate::FEATURE_REPORT_BYTES],
    ) -> crate::BridgeExchangeResult {
        let length = fs::metadata(&self.path).map_or(0, |metadata| metadata.len());
        self.lengths
            .borrow_mut()
            .push(usize::try_from(length).expect("transcript length fits usize"));
        let mut response = report;
        response[0] = 0x02;
        crate::BridgeExchangeResult::MatchedResponse(response)
    }
}

#[test]
fn set_reply_is_written_before_the_exchange_runs() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse route exists");
    let (device, policy) = fixture_from_record(&catalog, record, "early-set-reply");
    let lengths = Rc::new(RefCell::new(Vec::new()));
    let bridge = TranscriptLengthBridge {
        path: path.clone(),
        lengths: Rc::clone(&lengths),
    };
    let mut endpoint =
        RunningEndpoint::create(&paths, bridge, device, policy, None).expect("endpoint is created");
    endpoint
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("kernel starts endpoint");
    endpoint
        .process_event(set_report(1, battery_report(0x1f)))
        .expect("battery SET is handled");
    endpoint
        .process_event(get_report(2))
        .expect("battery GET is handled");
    endpoint.flush().expect("GET reply flushes");
    drop(endpoint);

    let bytes = fs::read(&path).expect("UHID transcript reads");
    fs::remove_file(&path).expect("temporary UHID file is removed");
    // CREATE2 and the SET reply were written before the bridge was asked.
    assert_eq!(lengths.borrow().as_slice(), &[UHID_EVENT_BYTES * 2]);
    let event_type = |index: usize| {
        u32::from_ne_bytes(
            bytes[UHID_EVENT_BYTES * index..UHID_EVENT_BYTES * index + 4]
                .try_into()
                .expect("event type"),
        )
    };
    assert_eq!(
        (0..4).map(event_type).collect::<Vec<_>>(),
        vec![11, 14, 10, 1]
    );
}

#[test]
fn endpoint_waits_for_write_room_only_while_replies_are_queued() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let (device, policy) = fixture();
    let mut endpoint = RunningEndpoint::create(&paths, NotSentBridge, device, policy, None)
        .expect("endpoint is created");
    // The test endpoint is a regular file, which is always readable and
    // writable, so the returned events are exactly the requested ones.
    let ready = |endpoint: &RunningEndpoint<NotSentBridge>| {
        let mut fds = [endpoint.poll_fd()];
        poll(&mut fds, Some(&Timespec::default())).expect("endpoint polls");
        fds[0].revents()
    };
    assert_eq!(ready(&endpoint), PollFlags::IN);

    endpoint
        .process_event(UhidEvent::Start { flags: 0 })
        .expect("kernel starts endpoint");
    endpoint
        .process_event(get_report(1))
        .expect("unsolicited GET is answered");
    assert!(!endpoint.pending.is_empty());
    assert_eq!(ready(&endpoint), PollFlags::IN | PollFlags::OUT);

    endpoint.flush().expect("queued reply flushes");
    assert_eq!(ready(&endpoint), PollFlags::IN);
    drop(endpoint);
    fs::remove_file(path).expect("temporary UHID file is removed");
}

#[test]
fn idle_wait_returns_as_soon_as_an_endpoint_is_ready() {
    let (mut writer, reader) =
        std::os::unix::net::UnixStream::pair().expect("socket pair is created");
    let started = Instant::now();
    wait_for_endpoints(
        vec![PollFd::new(&reader, PollFlags::IN)],
        Duration::from_millis(20),
    );
    assert!(started.elapsed() >= Duration::from_millis(20));

    writer.write_all(&[1]).expect("readiness byte is written");
    let started = Instant::now();
    wait_for_endpoints(
        vec![PollFd::new(&reader, PollFlags::IN)],
        Duration::from_secs(5),
    );
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn every_relay_failure_names_what_failed_and_only_bridge_failures_have_a_cause() {
    use std::error::Error as _;

    let operations = [
        (RelayBridgeOperation::MonitorConnect, "monitor connection"),
        (RelayBridgeOperation::Enumerate, "route enumeration"),
        (RelayBridgeOperation::Watch, "route watch"),
        (
            RelayBridgeOperation::RouteConnect,
            "device-route connection",
        ),
    ];
    for (operation, name) in operations {
        let error = bridge_error(operation, NativeBridgeError::SocketUnavailable);
        assert_eq!(
            error.to_string(),
            format!("HyperFlux native-route {name} failed: HyperFlux bridge socket is unavailable")
        );
        assert!(error.source().is_some());
    }

    let local = [
        RelayRuntimeError::Catalog,
        RelayRuntimeError::Supervisor,
        RelayRuntimeError::UhidOpen,
        RelayRuntimeError::UhidRead,
        RelayRuntimeError::UhidWrite,
        RelayRuntimeError::UhidCodec,
        RelayRuntimeError::Endpoint,
        RelayRuntimeError::ReplyQueue,
        RelayRuntimeError::InvalidBatchLimit,
        RelayRuntimeError::InstanceExhausted,
        RelayRuntimeError::Status,
    ];
    let messages: std::collections::BTreeSet<String> =
        local.iter().map(ToString::to_string).collect();
    assert_eq!(messages.len(), local.len(), "messages are distinct");
    assert!(local.iter().all(|error| error.source().is_none()));
}

#[test]
fn idle_wait_without_endpoints_sleeps_the_whole_interval() {
    let started = Instant::now();
    wait_for_endpoints(Vec::new(), Duration::from_millis(20));
    assert!(started.elapsed() >= Duration::from_millis(20));
}

#[test]
fn missing_uhid_start_has_one_route_local_deadline() {
    let path = temporary_uhid_file();
    let paths = test_paths(path.clone());
    let (device, policy) = fixture();
    let endpoint = RunningEndpoint::create(&paths, NotSentBridge, device, policy, None)
        .expect("endpoint is created");

    let before_deadline = (endpoint.created_at + UHID_START_TIMEOUT)
        .checked_sub(Duration::from_millis(1))
        .expect("deadline exceeds one millisecond");
    assert!(!endpoint.start_timed_out(before_deadline));
    assert!(endpoint.start_timed_out(endpoint.created_at + UHID_START_TIMEOUT));
    drop(endpoint);

    let bytes = fs::read(&path).expect("temporary UHID transcript reads");
    fs::remove_file(path).expect("temporary UHID file is removed");
    assert_eq!(bytes.len(), UHID_EVENT_BYTES * 2);
}

#[test]
fn startup_bridge_retry_converges_without_an_automatic_service_restart() {
    let termination = AtomicBool::new(false);
    let attempts = std::cell::Cell::new(0_usize);
    let waits = std::cell::Cell::new(0_usize);

    let result = retry_startup_bridge(
        &termination,
        3,
        || {
            attempts.set(attempts.get() + 1);
            if attempts.get() < 3 {
                Err(NativeBridgeError::SocketUnavailable)
            } else {
                Ok("ready")
            }
        },
        || waits.set(waits.get() + 1),
    )
    .expect("startup retry succeeds");

    assert_eq!(result, Some("ready"));
    assert_eq!(attempts.get(), 3);
    assert_eq!(waits.get(), 2);
}

#[test]
fn startup_bridge_retry_is_bounded_and_fails_fast_for_programming_errors() {
    let termination = AtomicBool::new(false);
    let attempts = std::cell::Cell::new(0_usize);
    let waits = std::cell::Cell::new(0_usize);
    let error = retry_startup_bridge::<(), _, _>(
        &termination,
        3,
        || {
            attempts.set(attempts.get() + 1);
            Err(NativeBridgeError::SocketUnavailable)
        },
        || waits.set(waits.get() + 1),
    )
    .expect_err("missing bridge reaches the fixed bound");
    assert_eq!(error, NativeBridgeError::SocketUnavailable);
    assert_eq!(attempts.get(), 3);
    assert_eq!(waits.get(), 2);

    attempts.set(0);
    waits.set(0);
    let error = retry_startup_bridge::<(), _, _>(
        &termination,
        3,
        || {
            attempts.set(attempts.get() + 1);
            Err(NativeBridgeError::InvalidIdentity)
        },
        || waits.set(waits.get() + 1),
    )
    .expect_err("invalid identity is not transient");
    assert_eq!(error, NativeBridgeError::InvalidIdentity);
    assert_eq!(attempts.get(), 1);
    assert_eq!(waits.get(), 0);
}

#[test]
fn startup_bridge_retry_honors_termination_before_connecting() {
    let termination = AtomicBool::new(true);
    let attempts = std::cell::Cell::new(0_usize);
    let result = retry_startup_bridge::<(), _, _>(
        &termination,
        3,
        || {
            attempts.set(attempts.get() + 1);
            Ok(())
        },
        || panic!("termination must not wait"),
    )
    .expect("termination is a clean startup stop");
    assert_eq!(result, None);
    assert_eq!(attempts.get(), 0);
}

// Wired precedence: a Basilisk on its USB cable is shown by OpenRazer as the
// real wired 1532:00CC device, so the relay must hide its virtual 00CD device
// while the same serial is present and bring it back when the cable goes.

const BASILISK_PROFILE: &str = "child.razer.basilisk-v3-pro-35k.00cd";
const WIRED_BASILISK: &str = "0003:1532:00CC.0001";
const BASILISK_SERIAL: &[u8] = b"BASILISK-SERIAL";

static WIRED_TEST_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

fn wired_test_id() -> u64 {
    WIRED_TEST_ID.fetch_add(1, Ordering::Relaxed)
}

/// An in-process bridge on a temporary socket. It only negotiates, which is
/// all a relay route needs to connect; serial reads are not served here.
struct NegotiatingBridge {
    socket: PathBuf,
    connections: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    stop: std::sync::Arc<AtomicBool>,
    acceptor: Option<thread::JoinHandle<()>>,
}

impl NegotiatingBridge {
    fn start() -> Self {
        let unique = wired_test_id();
        let socket =
            std::env::temp_dir().join(format!("hfx-wired-{}-{unique}.sock", std::process::id()));
        let listener =
            std::os::unix::net::UnixListener::bind(&socket).expect("test bridge socket binds");
        let connections = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let stop = std::sync::Arc::new(AtomicBool::new(false));
        let acceptor = {
            let connections = std::sync::Arc::clone(&connections);
            let stop = std::sync::Arc::clone(&stop);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(mut stream) = stream else {
                        break;
                    };
                    let number = connections.fetch_add(1, Ordering::SeqCst) + 1;
                    thread::spawn(move || {
                        crate::bridge::test_server::serve(
                            &mut stream,
                            number,
                            &mut crate::bridge::test_server::LeasingBackend::refusing(),
                        );
                    });
                }
            })
        };
        Self {
            socket,
            connections,
            stop,
            acceptor: Some(acceptor),
        }
    }

    fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }
}

impl Drop for NegotiatingBridge {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = std::os::unix::net::UnixStream::connect(&self.socket);
        if let Some(acceptor) = self.acceptor.take() {
            let _ = acceptor.join();
        }
        let _ = fs::remove_file(&self.socket);
    }
}

/// A temporary stand-in for the `OpenRazer` HID driver directories in sysfs.
struct DriverRoot(PathBuf);

impl DriverRoot {
    fn create() -> Self {
        let unique = wired_test_id();
        let path =
            std::env::temp_dir().join(format!("hfx-wired-drivers-{}-{unique}", std::process::id()));
        fs::create_dir_all(path.join("razermouse")).expect("driver directory is created");
        Self(path)
    }

    fn plug(&self, identity: &str, serial: Option<&[u8]>) -> PathBuf {
        let device = self.0.join("razermouse").join(identity);
        fs::create_dir_all(&device).expect("wired device directory is created");
        if let Some(serial) = serial {
            fs::write(device.join("device_serial"), serial).expect("serial is written");
        }
        device
    }

    fn unplug(&self, identity: &str) {
        fs::remove_dir_all(self.0.join("razermouse").join(identity))
            .expect("wired device directory is removed");
    }
}

impl Drop for DriverRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn basilisk_native_route() -> NativeRoute {
    let receiver_id =
        hfx_domain::ReceiverId::try_from("receiver-1".to_owned()).expect("receiver is valid");
    let generation_id = hfx_domain::GenerationId::try_from(1_u64).expect("generation is valid");
    let device_id = hfx_domain::LogicalDeviceId::try_from("paired-mouse-00cd".to_owned())
        .expect("device is valid");
    NativeRoute {
        receiver_id: receiver_id.clone(),
        generation_id,
        device_id: device_id.clone(),
        endpoint_id: hfx_domain::EndpointId::try_from("hyperflux-pointer".to_owned())
            .expect("endpoint is valid"),
        device_kind: hfx_domain::DeviceKind::Mouse,
        vendor_id: hfx_domain::VendorId::try_from(0x1532_u16).expect("vendor is valid"),
        product_id: hfx_domain::ProductId::try_from(0x00cd_u16).expect("product is valid"),
        route_state: hfx_domain::NativeRouteState::Available,
        access: hfx_domain::NativeRouteAccess::Qualified,
        protocol_family: hfx_domain::NativeProtocolFamily::try_from(
            "razer-feature-report-v1".to_owned(),
        )
        .expect("protocol family is valid"),
        report_bytes: hfx_domain::NativeReportByteCount::try_from(90_u16).expect("size is valid"),
        route_token: Some(
            hfx_domain::NativeRouteToken::try_from("a".repeat(64)).expect("token is valid"),
        ),
        resource: Some(hfx_protocol::ResourceKey {
            receiver_id,
            generation_id,
            device_id,
            kind: hfx_domain::ResourceKind::NativeControl,
        }),
    }
}

fn wired_paths(bridge: &NegotiatingBridge, drivers: &DriverRoot, uhid: PathBuf) -> RelayPaths {
    RelayPaths {
        bridge_socket: bridge.socket.clone(),
        uhid_device: uhid,
        hid_driver_root: drivers.0.clone(),
        status_file: PathBuf::from("/unused/status.json"),
    }
}

fn start_basilisk_route(paths: &RelayPaths, label: &str) -> ManagedRoute {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let record = catalog
        .profile(BASILISK_PROFILE)
        .expect("mouse route exists")
        .clone();
    let (device, _) = fixture_from_record(&catalog, &record, label);
    ManagedRoute::start(paths, basilisk_native_route(), record, device, 1)
        .expect("route connects to the test bridge")
}

/// Stands in for two matching serial reads from the wireless mouse; the
/// two-read gate itself is pinned by the publication tests above.
fn pass_serial_gate(route: &mut ManagedRoute, serial: &[u8]) {
    route.publication.ready_fingerprint =
        Some(crate::fingerprint_serial_bytes(serial).expect("serial is valid"));
    assert!(route.publication_ready());
}

/// Each virtual device opens its own UHID handle, which starts at offset 0 of
/// a regular file, so every publication below gets its own transcript file.
fn uhid_event_types(path: &Path) -> Vec<u32> {
    fs::read(path)
        .expect("UHID transcript reads")
        .chunks(UHID_EVENT_BYTES)
        .map(|event| u32::from_ne_bytes(event[0..4].try_into().expect("event type")))
        .collect()
}

const CREATE: u32 = 11;
const DESTROY: u32 = 1;

#[test]
fn wired_basilisk_with_matching_serial_hides_then_restores_the_virtual_device() {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let bridge = NegotiatingBridge::start();
    let drivers = DriverRoot::create();
    let uhid = temporary_uhid_file();
    let paths = wired_paths(&bridge, &drivers, uhid.clone());
    let mut scanner = DirectRouteScanner::new(drivers.0.clone());
    let mut next_instance = 2;
    let mut route = start_basilisk_route(&paths, "wired-precedence");
    assert_eq!(bridge.connections(), 1);
    // The idle wait watches only routes that have a published endpoint.
    assert!(route.poll_fd().is_none());

    // Wireless only: the virtual 00CD device is published.
    pass_serial_gate(&mut route, BASILISK_SERIAL);
    route.complete_publication(&paths, &scanner.scan(&catalog).expect("scan succeeds"));
    assert!(route.endpoint.is_some());
    assert!(route.poll_fd().is_some());
    assert_eq!(route.status().state, RelayPublicationState::CreateSent);
    assert_eq!(uhid_event_types(&uhid), vec![CREATE]);

    // Cable in: the wired 00CC with the same serial hides the virtual device.
    drivers.plug(WIRED_BASILISK, Some(b"BASILISK-SERIAL\n"));
    let wired = scanner.scan(&catalog).expect("scan succeeds");
    assert_eq!(
        wired.transition(BASILISK_PROFILE, route.serial_fingerprint.as_ref(), true),
        DirectRouteTransition::Suppress
    );
    route
        .refresh_direct(&paths, &wired, &mut next_instance)
        .expect("direct refresh succeeds");
    assert!(route.endpoint.is_none());
    assert!(route.publication.direct_suppressed);
    assert_eq!(
        route.status().state,
        RelayPublicationState::DirectSuppressed
    );
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);

    // Further scans while the cable stays in change nothing.
    for _ in 0..2 {
        let still_wired = scanner.scan(&catalog).expect("scan succeeds");
        route
            .refresh_direct(&paths, &still_wired, &mut next_instance)
            .expect("direct refresh succeeds");
        route.probe_publication();
        assert!(!route.publication_ready());
        assert_eq!(
            route.status().state,
            RelayPublicationState::DirectSuppressed
        );
    }
    assert_eq!(next_instance, 2);
    assert_eq!(bridge.connections(), 1);
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);

    // Cable out: a fresh publication starts on a new bridge connection and
    // must pass a new serial check before the virtual device returns.
    drivers.unplug(WIRED_BASILISK);
    let wireless = scanner.scan(&catalog).expect("scan succeeds");
    assert!(!wireless.has_profile_candidate(BASILISK_PROFILE));
    route
        .refresh_direct(&paths, &wireless, &mut next_instance)
        .expect("direct refresh succeeds");
    assert!(!route.publication.direct_suppressed);
    assert!(route.publication.bridge.is_some());
    assert!(route.serial_fingerprint.is_none());
    assert!(route.endpoint.is_none());
    assert_eq!(route.status().state, RelayPublicationState::Pending);
    assert_eq!(next_instance, 3);
    assert_eq!(bridge.connections(), 2);
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);

    let restored_uhid = temporary_uhid_file();
    let restored_paths = wired_paths(&bridge, &drivers, restored_uhid.clone());
    pass_serial_gate(&mut route, BASILISK_SERIAL);
    route.complete_publication(
        &restored_paths,
        &scanner.scan(&catalog).expect("scan succeeds"),
    );
    assert!(route.endpoint.is_some());
    assert_eq!(route.status().state, RelayPublicationState::CreateSent);
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);
    assert_eq!(uhid_event_types(&restored_uhid), vec![CREATE]);

    route.destroy();
    assert_eq!(uhid_event_types(&restored_uhid), vec![CREATE, DESTROY]);
    fs::remove_file(uhid).expect("transcript is removed");
    fs::remove_file(restored_uhid).expect("transcript is removed");
}

#[test]
fn wired_basilisk_present_at_publication_never_creates_the_virtual_device() {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let bridge = NegotiatingBridge::start();
    let drivers = DriverRoot::create();
    let uhid = temporary_uhid_file();
    let paths = wired_paths(&bridge, &drivers, uhid.clone());
    let mut scanner = DirectRouteScanner::new(drivers.0.clone());
    let mut next_instance = 2;
    let mut route = start_basilisk_route(&paths, "wired-at-publication");

    drivers.plug(WIRED_BASILISK, Some(b"BASILISK-SERIAL\n"));
    pass_serial_gate(&mut route, BASILISK_SERIAL);
    route.complete_publication(&paths, &scanner.scan(&catalog).expect("scan succeeds"));
    assert!(route.endpoint.is_none());
    assert!(route.publication.direct_suppressed);
    assert!(route.publication.bridge.is_none());
    assert_eq!(
        route.status().state,
        RelayPublicationState::DirectSuppressed
    );
    assert!(uhid_event_types(&uhid).is_empty());

    drivers.unplug(WIRED_BASILISK);
    route
        .refresh_direct(
            &paths,
            &scanner.scan(&catalog).expect("scan succeeds"),
            &mut next_instance,
        )
        .expect("direct refresh succeeds");
    assert!(!route.publication.direct_suppressed);
    assert!(route.publication.bridge.is_some());
    assert_eq!(route.status().state, RelayPublicationState::Pending);
    assert_eq!(bridge.connections(), 2);

    pass_serial_gate(&mut route, BASILISK_SERIAL);
    route.complete_publication(&paths, &scanner.scan(&catalog).expect("scan succeeds"));
    assert!(route.endpoint.is_some());
    route.destroy();
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);
    fs::remove_file(uhid).expect("transcript is removed");
}

#[test]
fn wired_basilisk_without_a_readable_matching_serial_hides_nothing() {
    let catalog = NativeRouteCatalog::load().expect("native catalog loads");
    let bridge = NegotiatingBridge::start();
    let drivers = DriverRoot::create();
    let uhid = temporary_uhid_file();
    let paths = wired_paths(&bridge, &drivers, uhid.clone());
    let mut scanner = DirectRouteScanner::new(drivers.0.clone());
    let mut next_instance = 2;
    let mut route = start_basilisk_route(&paths, "wired-unreadable");
    pass_serial_gate(&mut route, BASILISK_SERIAL);
    route.complete_publication(&paths, &scanner.scan(&catalog).expect("scan succeeds"));
    assert!(route.endpoint.is_some());

    // Missing, empty and unreadable serials, and another mouse's serial.
    for (number, serial) in [
        (1, None),
        (2, Some(&b""[..])),
        (3, Some(&b"\n"[..])),
        (4, Some(&b"ANOTHER-BASILISK\n"[..])),
    ] {
        let identity = format!("0003:1532:00CC.{number:04X}");
        drivers.plug(&identity, serial);
        let wired = scanner.scan(&catalog).expect("scan succeeds");
        assert!(wired.has_profile_candidate(BASILISK_PROFILE));
        route
            .refresh_direct(&paths, &wired, &mut next_instance)
            .expect("direct refresh succeeds");
        assert!(
            route.endpoint.is_some(),
            "serial case {number} hides nothing"
        );
        assert!(!route.publication.direct_suppressed);
        assert_eq!(route.status().state, RelayPublicationState::CreateSent);
        drivers.unplug(&identity);
    }
    let identity = "0003:1532:00CC.0005";
    let device = drivers.plug(identity, None);
    fs::create_dir(device.join("device_serial")).expect("unreadable serial is created");
    let wired = scanner.scan(&catalog).expect("scan succeeds");
    assert!(wired.has_profile_candidate(BASILISK_PROFILE));
    route
        .refresh_direct(&paths, &wired, &mut next_instance)
        .expect("direct refresh succeeds");
    assert!(
        route.endpoint.is_some(),
        "an unreadable serial hides nothing"
    );

    // The same holds when the cable is already in at publication time.
    let late_uhid = temporary_uhid_file();
    let late_paths = wired_paths(&bridge, &drivers, late_uhid.clone());
    let mut late = start_basilisk_route(&late_paths, "wired-unreadable-late");
    pass_serial_gate(&mut late, BASILISK_SERIAL);
    late.complete_publication(&late_paths, &wired);
    assert!(late.endpoint.is_some());
    assert!(!late.publication.direct_suppressed);

    assert_eq!(next_instance, 2);
    assert_eq!(uhid_event_types(&uhid), vec![CREATE]);
    assert_eq!(uhid_event_types(&late_uhid), vec![CREATE]);
    late.destroy();
    route.destroy();
    assert_eq!(uhid_event_types(&uhid), vec![CREATE, DESTROY]);
    assert_eq!(uhid_event_types(&late_uhid), vec![CREATE, DESTROY]);
    fs::remove_file(uhid).expect("transcript is removed");
    fs::remove_file(late_uhid).expect("transcript is removed");
}

/// Holds a lease that is due for idle release at `deadline`, like a
/// `NativeBridge` after a matched exchange, and records how it is given back.
struct LeaseRecordingBridge {
    deadline: Option<Instant>,
    releases: Rc<RefCell<Vec<&'static str>>>,
}

impl crate::BridgeExchange for LeaseRecordingBridge {
    fn exchange(
        &mut self,
        _report: [u8; crate::FEATURE_REPORT_BYTES],
    ) -> crate::BridgeExchangeResult {
        crate::BridgeExchangeResult::NotSent(crate::BridgeExchangeFailure::TransportNotSent)
    }

    fn lease_release_deadline(&self) -> Option<Instant> {
        self.deadline
    }

    fn release_idle_lease(&mut self, now: Instant) {
        if self.deadline.is_some_and(|deadline| now >= deadline) {
            self.deadline = None;
            self.releases.borrow_mut().push("idle");
        }
    }

    fn release_lease(&mut self) {
        if self.deadline.take().is_some() {
            self.releases.borrow_mut().push("teardown");
        }
    }
}

fn leased_bridge(deadline: Instant) -> (LeaseRecordingBridge, Rc<RefCell<Vec<&'static str>>>) {
    let releases = Rc::new(RefCell::new(Vec::new()));
    (
        LeaseRecordingBridge {
            deadline: Some(deadline),
            releases: Rc::clone(&releases),
        },
        releases,
    )
}

#[test]
fn endpoint_destroy_and_drop_release_the_held_lease() {
    let (device, policy) = fixture();
    let destroyed_path = temporary_uhid_file();
    let (bridge, destroyed) = leased_bridge(Instant::now() + Duration::from_mins(1));
    RunningEndpoint::create(
        &test_paths(destroyed_path.clone()),
        bridge,
        device.clone(),
        policy.clone(),
        None,
    )
    .expect("endpoint is created")
    .destroy();
    assert_eq!(destroyed.borrow().as_slice(), &["teardown"]);

    let dropped_path = temporary_uhid_file();
    let (bridge, dropped) = leased_bridge(Instant::now() + Duration::from_mins(1));
    drop(
        RunningEndpoint::create(
            &test_paths(dropped_path.clone()),
            bridge,
            device,
            policy,
            None,
        )
        .expect("endpoint is created"),
    );
    assert_eq!(dropped.borrow().as_slice(), &["teardown"]);
    assert_single_uhid_lifecycle(&destroyed_path, 2);
    assert_single_uhid_lifecycle(&dropped_path, 2);
    fs::remove_file(destroyed_path).expect("temporary UHID file is removed");
    fs::remove_file(dropped_path).expect("temporary UHID file is removed");
}

#[test]
fn recycled_and_recovered_routes_release_before_publishing_again() {
    let (device, policy) = fixture();
    let now = Instant::now();
    let mut fingerprint = None;
    let path = temporary_uhid_file();
    let (bridge, recycled) = leased_bridge(now + Duration::from_mins(1));
    let endpoint = RunningEndpoint::create(
        &test_paths(path.clone()),
        bridge,
        device.clone(),
        policy.clone(),
        None,
    )
    .expect("endpoint is created");
    let (placeholder, _) = leased_bridge(now);
    let mut publication = PendingPublication::new(placeholder, now);
    recycle_retained_route(endpoint, &mut publication, &mut fingerprint, now);
    assert_eq!(recycled.borrow().as_slice(), &["teardown"]);
    assert_eq!(publication.lease_release_deadline(), None);
    fs::remove_file(path).expect("temporary UHID file is removed");

    let path = temporary_uhid_file();
    let (bridge, recovered) = leased_bridge(now + Duration::from_mins(1));
    let endpoint = RunningEndpoint::create(&test_paths(path.clone()), bridge, device, policy, None)
        .expect("endpoint is created");
    recover_kernel_binding(endpoint, &mut publication, &mut fingerprint, now);
    assert_eq!(recovered.borrow().as_slice(), &["teardown"]);
    fs::remove_file(path).expect("temporary UHID file is removed");
}

#[test]
fn the_idle_lease_is_released_at_its_deadline_wherever_the_bridge_lives() {
    let (device, policy) = fixture();
    let deadline = Instant::now() + Duration::from_mins(1);
    let path = temporary_uhid_file();
    let (bridge, endpoint_releases) = leased_bridge(deadline);
    let mut endpoint =
        RunningEndpoint::create(&test_paths(path.clone()), bridge, device, policy, None)
            .expect("endpoint is created");
    assert_eq!(endpoint.lease_release_deadline(), Some(deadline));
    endpoint.release_idle_lease(just_before(deadline));
    assert!(endpoint_releases.borrow().is_empty());
    endpoint.release_idle_lease(deadline);
    assert_eq!(endpoint.lease_release_deadline(), None);
    endpoint.destroy();
    assert_eq!(endpoint_releases.borrow().as_slice(), &["idle"]);
    fs::remove_file(path).expect("temporary UHID file is removed");

    let (bridge, publication_releases) = leased_bridge(deadline);
    let mut publication = PendingPublication::new(bridge, Instant::now());
    assert_eq!(publication.lease_release_deadline(), Some(deadline));
    publication.release_idle_lease(just_before(deadline));
    publication.release_idle_lease(deadline);
    assert_eq!(publication.lease_release_deadline(), None);
    assert_eq!(publication_releases.borrow().as_slice(), &["idle"]);
}

#[test]
fn the_loop_wait_wakes_for_the_earliest_lease_deadline() {
    let now = Instant::now();
    assert_eq!(idle_wait(now, None), IDLE_INTERVAL);
    assert_eq!(
        idle_wait(now, Some(now + Duration::from_millis(3))),
        Duration::from_millis(3)
    );
    assert_eq!(
        idle_wait(now, Some(now + crate::bridge::LEASE_IDLE_RELEASE)),
        IDLE_INTERVAL
    );
    assert_eq!(idle_wait(now + IDLE_INTERVAL, Some(now)), Duration::ZERO);
}

#[test]
fn the_relay_loop_releases_idle_route_leases_and_shutdown_releases_the_rest() {
    let bridge = crate::bridge::test_server::LeasingBridge::start();
    let paths = RelayPaths {
        bridge_socket: bridge.socket().to_path_buf(),
        uhid_device: PathBuf::from("/unused/uhid"),
        hid_driver_root: PathBuf::from("/unused/hid/drivers"),
        status_file: PathBuf::from("/unused/status.json"),
    };
    let route = start_basilisk_route(&paths, "lease-route");
    let identity = RouteIdentity::from(&route.route);
    let mut routes = BTreeMap::from([(identity.clone(), route)]);
    let report = battery_report(0x1f);
    let exchange = |routes: &mut BTreeMap<RouteIdentity, ManagedRoute>| {
        crate::BridgeExchange::exchange(
            routes
                .get_mut(&identity)
                .and_then(|route| route.publication.bridge.as_mut())
                .expect("publication holds the bridge"),
            report,
        )
    };
    assert_eq!(
        exchange(&mut routes),
        crate::BridgeExchangeResult::MatchedResponse(report)
    );
    let deadline = routes[&identity]
        .lease_release_deadline()
        .expect("the route holds its lease after the exchange");
    release_idle_leases(&mut routes, just_before(deadline));
    assert_eq!(bridge.ledger().releases, 0);
    let waited = Instant::now();
    wait_for_routes(&routes);
    assert!(waited.elapsed() < crate::bridge::LEASE_IDLE_RELEASE * 4);
    release_idle_leases(&mut routes, deadline);
    assert_eq!(bridge.ledger().releases, 1);
    assert_eq!(routes[&identity].lease_release_deadline(), None);

    exchange(&mut routes);
    destroy_all(&mut routes);
    let ledger = bridge.ledger();
    assert_eq!((ledger.acquires, ledger.releases), (2, 2));
    assert!(ledger.owner.is_none());
}

fn just_before(deadline: Instant) -> Instant {
    deadline
        .checked_sub(Duration::from_millis(1))
        .expect("the deadline is past the clock origin")
}
