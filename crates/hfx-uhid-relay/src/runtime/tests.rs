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
