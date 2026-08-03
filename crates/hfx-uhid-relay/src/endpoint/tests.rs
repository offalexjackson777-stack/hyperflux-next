// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use crate::NativeRouteCatalog;
use crate::uhid::decode_transport_envelope;
use hfx_domain::{DeviceKind, ProductId, VendorId};
use hfx_integration_model::load_native_command_policies;
use std::collections::VecDeque;

struct FakeBridge {
    results: VecDeque<BridgeExchangeResult>,
    requests: Vec<[u8; FEATURE_REPORT_BYTES]>,
}

impl BridgeExchange for FakeBridge {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
        self.requests.push(report);
        self.results.pop_front().expect("test bridge has a result")
    }
}

fn endpoint(results: Vec<BridgeExchangeResult>) -> FeatureEndpoint<FakeBridge> {
    endpoint_for("child.razer.basilisk-v3-pro-35k.00cd", results)
}

fn endpoint_for(
    profile_id: &str,
    results: Vec<BridgeExchangeResult>,
) -> FeatureEndpoint<FakeBridge> {
    let policy = load_native_command_policies("1ef8a91712906a89fd332a2ed62bbd105315d6d1")
        .expect("generated policies load")
        .remove(profile_id)
        .expect("profile policy exists");
    let mut endpoint = FeatureEndpoint::new(
        FakeBridge {
            results: results.into(),
            requests: Vec::new(),
        },
        policy,
    );
    endpoint
        .handle(UhidEvent::Start { flags: 0 })
        .expect("endpoint starts");
    endpoint
}

fn original_basilisk_endpoint(results: Vec<BridgeExchangeResult>) -> FeatureEndpoint<FakeBridge> {
    let policy = NativeRouteCatalog::load()
        .expect("generated routes load")
        .route(
            VendorId::try_from(0x1532_u16).expect("vendor"),
            ProductId::try_from(0x00ab_u16).expect("product"),
            DeviceKind::Mouse,
        )
        .expect("original Basilisk route exists")
        .command_policy
        .clone();
    let mut endpoint = FeatureEndpoint::new(
        FakeBridge {
            results: results.into(),
            requests: Vec::new(),
        },
        policy,
    );
    endpoint
        .handle(UhidEvent::Start { flags: 0 })
        .expect("endpoint starts");
    endpoint
}

fn allowed_report(transaction_id: u8) -> [u8; FEATURE_REPORT_BYTES] {
    let mut report = [0_u8; FEATURE_REPORT_BYTES];
    report[1] = transaction_id;
    report[5] = 1;
    report[6] = 0;
    report[7] = 5;
    report[8] = 1;
    report[88] = report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    report
}

fn set(id: u32, transaction_id: u8) -> UhidEvent {
    UhidEvent::SetReport {
        id,
        report_number: 0,
        report_type: 0,
        data: allowed_report(transaction_id).to_vec(),
    }
}

fn device_mode_report(transaction_id: u8, command_id: u8, mode: u8) -> [u8; 90] {
    let mut report = [0_u8; 90];
    report[1] = transaction_id;
    report[5] = 2;
    report[6] = 0;
    report[7] = command_id;
    report[8] = mode;
    report[88] = report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    report
}

fn battery_report(transaction_id: u8) -> [u8; FEATURE_REPORT_BYTES] {
    let mut report = [0_u8; FEATURE_REPORT_BYTES];
    report[1] = transaction_id;
    report[5] = 2;
    report[6] = 0x07;
    report[7] = 0x80;
    report[88] = report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    report
}

fn brightness_write_report(
    transaction_id: u8,
    led_id: u8,
    brightness: u8,
) -> [u8; FEATURE_REPORT_BYTES] {
    let mut report = [0_u8; FEATURE_REPORT_BYTES];
    report[1] = transaction_id;
    report[5] = 3;
    report[6] = 0x0f;
    report[7] = 0x04;
    report[8..11].copy_from_slice(&[0x01, led_id, brightness]);
    report[88] = report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    report
}

fn set_report(id: u32, report: [u8; 90]) -> UhidEvent {
    UhidEvent::SetReport {
        id,
        report_number: 0,
        report_type: 0,
        data: report.to_vec(),
    }
}

fn get(id: u32) -> UhidEvent {
    UhidEvent::GetReport {
        id,
        report_number: 0,
        report_type: 0,
    }
}

#[test]
fn matched_response_is_returned_untouched_and_consumed_once() {
    let expected = [0xa5; FEATURE_REPORT_BYTES];
    let mut endpoint = endpoint(vec![BridgeExchangeResult::MatchedResponse(expected)]);
    assert_eq!(
        endpoint.handle(set(10, 0x42)).expect("set succeeds"),
        Some(UhidReply::SetReport {
            id: 10,
            success: true
        })
    );
    assert_eq!(endpoint.bridge.requests, vec![allowed_report(0x42)]);
    assert_eq!(
        endpoint.handle(get(11)).expect("get succeeds"),
        Some(UhidReply::GetReport {
            id: 11,
            data: Some(expected.to_vec())
        })
    );
    assert_eq!(
        endpoint.handle(get(12)).expect("empty get is answered"),
        Some(UhidReply::GetReport { id: 12, data: None })
    );
}

#[test]
fn transport_certainty_is_preserved_in_the_response_channel() {
    for (result, expected) in [
        (
            BridgeExchangeResult::NotSent(crate::BridgeExchangeFailure::TransportNotSent),
            TransportEnvelopeKind::NotSent,
        ),
        (
            BridgeExchangeResult::MayHaveBeenSent(
                crate::BridgeExchangeFailure::TransportMayHaveBeenSent,
            ),
            TransportEnvelopeKind::MayHaveBeenSent,
        ),
    ] {
        let mut endpoint = endpoint(vec![result]);
        endpoint.handle(set(1, 0x11)).expect("set succeeds");
        let Some(UhidReply::GetReport {
            data: Some(response),
            ..
        }) = endpoint.handle(get(2)).expect("get succeeds")
        else {
            panic!("response is present");
        };
        assert_eq!(decode_transport_envelope(&response), Some(expected));
    }
}

#[test]
fn persistent_battery_busy_retries_the_source_bound_and_never_returns_zero() {
    let mut response = battery_report(0x9f);
    response[0] = 0x01;
    response[9] = 0;
    response[88] = response[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint_for(
        "child.razer.deathstalker-v2-pro-tkl.0296",
        vec![
            BridgeExchangeResult::MatchedResponse(response);
            usize::from(SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS)
        ],
    );

    endpoint
        .handle(set_report(1, battery_report(0x9f)))
        .expect("battery request succeeds");

    assert_eq!(
        endpoint.take_battery_diagnostic(),
        Some(BatteryExchangeDiagnostic::Matched {
            transaction_id: 0x9f,
            status: 0x01,
            raw_value: 0,
            attempts: SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS,
        })
    );
    assert_eq!(
        endpoint.bridge_mut().requests.len(),
        usize::from(SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS)
    );
    assert_eq!(endpoint.take_battery_diagnostic(), None);
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("battery response is returned")
    else {
        panic!("battery response is present");
    };
    assert_eq!(
        decode_transport_envelope(&actual),
        Some(TransportEnvelopeKind::MayHaveBeenSent)
    );
}

#[test]
fn transient_battery_busy_retries_to_the_next_truthful_value() {
    let mut busy = battery_report(0x9f);
    busy[0] = RAZER_STATUS_BUSY;
    busy[9] = 0;
    busy[88] = busy[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut success = battery_report(0x9f);
    success[0] = 0x02;
    success[9] = 253;
    success[88] = success[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint_for(
        "child.razer.deathstalker-v2-pro-tkl.0296",
        vec![
            BridgeExchangeResult::MatchedResponse(busy),
            BridgeExchangeResult::MatchedResponse(success),
        ],
    );

    endpoint
        .handle(set_report(1, battery_report(0x9f)))
        .expect("battery request succeeds after one BUSY");
    assert_eq!(
        endpoint.take_battery_diagnostic(),
        Some(BatteryExchangeDiagnostic::Matched {
            transaction_id: 0x9f,
            status: 0x02,
            raw_value: 253,
            attempts: 2,
        })
    );
    assert_eq!(endpoint.bridge_mut().requests.len(), 2);
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("recovered battery response is returned")
    else {
        panic!("recovered battery response is present");
    };
    assert_eq!(actual, success);
}

#[test]
fn successful_battery_response_remains_byte_exact() {
    let mut response = battery_report(0x9f);
    response[0] = 0x02;
    response[9] = 253;
    response[88] = response[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint_for(
        "child.razer.deathstalker-v2-pro-tkl.0296",
        vec![BridgeExchangeResult::MatchedResponse(response)],
    );

    endpoint
        .handle(set_report(1, battery_report(0x9f)))
        .expect("battery request succeeds");
    assert_eq!(
        endpoint.take_battery_diagnostic(),
        Some(BatteryExchangeDiagnostic::Matched {
            transaction_id: 0x9f,
            status: 0x02,
            raw_value: 253,
            attempts: 1,
        })
    );
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("battery response is returned")
    else {
        panic!("battery response is present");
    };
    assert_eq!(actual, response);
}

#[test]
fn retained_route_timeout_completes_one_battery_read_from_confirmed_state() {
    let request = battery_report(0x1f);
    let mut confirmed = request;
    confirmed[0] = 0x02;
    confirmed[9] = 194;
    confirmed[88] = confirmed[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut timeout = request;
    timeout[0] = 0x04;
    timeout[9] = 0;
    timeout[88] = timeout[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint(vec![
        BridgeExchangeResult::MatchedResponse(confirmed),
        BridgeExchangeResult::MatchedResponse(timeout),
    ]);

    endpoint
        .handle(set_report(1, request))
        .expect("confirmed battery request succeeds");
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("confirmed battery response is returned")
    else {
        panic!("confirmed battery response is present");
    };
    assert_eq!(actual, confirmed);

    endpoint
        .handle(set_report(3, request))
        .expect("retained-route timeout is answered");
    assert!(!endpoint.retained_route_departure_ready());
    assert_eq!(
        endpoint.take_battery_diagnostic(),
        Some(BatteryExchangeDiagnostic::Matched {
            transaction_id: 0x1f,
            status: 0x04,
            raw_value: 0,
            attempts: 1,
        })
    );
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(4))
        .expect("departing battery read is completed")
    else {
        panic!("departing battery response is present");
    };
    assert_eq!(actual, confirmed);
    assert!(endpoint.retained_route_departure_ready());
}

#[test]
fn timeout_without_confirmed_battery_state_remains_truthful_and_withdraws() {
    let request = battery_report(0x1f);
    let mut timeout = request;
    timeout[0] = 0x04;
    timeout[88] = timeout[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint(vec![BridgeExchangeResult::MatchedResponse(timeout)]);

    endpoint
        .handle(set_report(1, request))
        .expect("battery timeout is answered");
    assert!(!endpoint.retained_route_departure_ready());
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("timeout response is returned")
    else {
        panic!("timeout response is present");
    };
    assert_eq!(actual, timeout);
    assert!(endpoint.retained_route_departure_ready());
}

#[test]
fn write_timeout_is_never_replayed_and_still_withdraws_the_route() {
    let request = brightness_write_report(0x9f, 0x05, 0x40);
    let mut timeout = request;
    timeout[0] = 0x04;
    timeout[88] = timeout[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint_for(
        "child.razer.deathstalker-v2-pro-tkl.0296",
        vec![BridgeExchangeResult::MatchedResponse(timeout)],
    );

    endpoint
        .handle(set_report(1, request))
        .expect("write timeout is answered");
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint.handle(get(2)).expect("write response is returned")
    else {
        panic!("write response is present");
    };
    assert_eq!(actual, timeout);
    assert!(endpoint.retained_route_departure_ready());
    assert_eq!(endpoint.bridge_mut().requests, vec![request]);
}

#[test]
fn endpoint_withdrawal_discards_confirmed_battery_state() {
    let request = battery_report(0x1f);
    let mut confirmed = request;
    confirmed[0] = 0x02;
    confirmed[9] = 194;
    confirmed[88] = confirmed[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut timeout = request;
    timeout[0] = 0x04;
    timeout[88] = timeout[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint(vec![
        BridgeExchangeResult::MatchedResponse(confirmed),
        BridgeExchangeResult::MatchedResponse(timeout),
    ]);

    endpoint
        .handle(set_report(1, request))
        .expect("confirmed request succeeds");
    endpoint
        .handle(get(2))
        .expect("confirmed response is consumed");
    endpoint.prepare_withdrawal();
    endpoint
        .handle(UhidEvent::Start { flags: 0 })
        .expect("endpoint restarts for the fixture");
    endpoint
        .handle(set_report(3, request))
        .expect("timeout request is answered");
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(4))
        .expect("timeout response is returned")
    else {
        panic!("timeout response is present");
    };
    assert_eq!(actual, timeout);
    assert!(endpoint.retained_route_departure_ready());
}

#[test]
fn busy_brightness_write_remains_byte_exact_for_stock_openrazer_shutdown() {
    let request = brightness_write_report(0x9f, 0x05, 0xff);
    let mut response = request;
    response[0] = RAZER_STATUS_BUSY;
    response[88] = response[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    let mut endpoint = endpoint_for(
        "child.razer.deathstalker-v2-pro-tkl.0296",
        vec![BridgeExchangeResult::MatchedResponse(response)],
    );

    endpoint
        .handle(set_report(1, request))
        .expect("brightness write receives the upstream response");
    assert_eq!(endpoint.take_battery_diagnostic(), None);
    let Some(UhidReply::GetReport {
        data: Some(actual), ..
    }) = endpoint
        .handle(get(2))
        .expect("brightness response is returned")
    else {
        panic!("brightness response is present");
    };
    assert_eq!(actual, response);
    assert_eq!(decode_transport_envelope(&actual), None);
    assert_eq!(endpoint.bridge_mut().requests.len(), 1);
}

#[test]
fn battery_transport_failures_never_report_a_numeric_value() {
    for (result, expected) in [
        (
            BridgeExchangeResult::NotSent(crate::BridgeExchangeFailure::TransportNotSent),
            BatteryExchangeDiagnostic::NotSent {
                transaction_id: 0x9f,
                attempts: 1,
            },
        ),
        (
            BridgeExchangeResult::MayHaveBeenSent(
                crate::BridgeExchangeFailure::TransportMayHaveBeenSent,
            ),
            BatteryExchangeDiagnostic::MayHaveBeenSent {
                transaction_id: 0x9f,
                attempts: 1,
            },
        ),
    ] {
        let mut endpoint = endpoint_for("child.razer.deathstalker-v2-pro-tkl.0296", vec![result]);
        endpoint
            .handle(set_report(1, battery_report(0x9f)))
            .expect("battery request receives a typed failure");
        assert_eq!(endpoint.take_battery_diagnostic(), Some(expected));
    }
}

#[test]
fn device_mode_is_virtualized_for_every_feature_only_openrazer_route() {
    for profile_id in [
        "child.razer.basilisk-v3-pro-35k.00cd",
        "child.razer.deathstalker-v2-pro-tkl.0296",
    ] {
        let mut endpoint = endpoint_for(profile_id, Vec::new());
        endpoint
            .handle(set_report(1, device_mode_report(0x21, 0x04, 0x03)))
            .expect("driver-mode set is answered");
        let Some(UhidReply::GetReport {
            data: Some(response),
            ..
        }) = endpoint.handle(get(2)).expect("set response is returned")
        else {
            panic!("virtual driver-mode response is present");
        };
        assert_eq!(response[0], 0x02);
        assert_eq!(&response[8..10], &[0x03, 0x00]);

        endpoint
            .handle(set_report(3, device_mode_report(0x22, 0x84, 0x00)))
            .expect("device-mode read is answered");
        let Some(UhidReply::GetReport {
            data: Some(response),
            ..
        }) = endpoint.handle(get(4)).expect("read response is returned")
        else {
            panic!("virtual device-mode read is present");
        };
        assert_eq!(&response[8..10], &[0x03, 0x00]);
        assert!(endpoint.bridge.requests.is_empty());
    }
}

#[test]
fn route_withdrawal_discards_virtual_mode_and_pending_state() {
    let mut endpoint = endpoint(Vec::new());
    endpoint
        .handle(set_report(1, device_mode_report(0x31, 0x04, 0x03)))
        .expect("virtual mode set is answered");
    assert!(endpoint.has_pending_response());

    endpoint.prepare_withdrawal();

    assert_eq!(endpoint.state(), FeatureEndpointState::Stopped);
    assert!(!endpoint.has_pending_response());
    endpoint
        .handle(UhidEvent::Start { flags: 0 })
        .expect("endpoint restarts");
    endpoint
        .handle(set_report(2, device_mode_report(0x32, 0x84, 0x00)))
        .expect("virtual mode read is answered");
    let Some(UhidReply::GetReport {
        data: Some(response),
        ..
    }) = endpoint.handle(get(3)).expect("read response is returned")
    else {
        panic!("virtual device-mode read is present");
    };
    assert_eq!(&response[8..10], &[0x00, 0x00]);
    assert!(endpoint.bridge.requests.is_empty());
}

#[test]
fn unapproved_device_mode_is_rejected_before_virtualization_or_transport() {
    let mut endpoint = endpoint(Vec::new());
    assert_eq!(
        endpoint
            .handle(set_report(1, device_mode_report(0x41, 0x04, 0x07)))
            .expect("invalid mode is answered"),
        Some(UhidReply::SetReport {
            id: 1,
            success: false,
        })
    );
    assert!(!endpoint.has_pending_response());
    assert!(endpoint.bridge.requests.is_empty());
}

#[test]
fn invalid_and_overlapping_requests_never_reach_the_bridge() {
    let mut endpoint = endpoint(vec![BridgeExchangeResult::NotSent(
        crate::BridgeExchangeFailure::TransportNotSent,
    )]);
    assert_eq!(
        endpoint
            .handle(UhidEvent::SetReport {
                id: 1,
                report_number: 1,
                report_type: 0,
                data: vec![0; FEATURE_REPORT_BYTES],
            })
            .expect("invalid set is answered"),
        Some(UhidReply::SetReport {
            id: 1,
            success: false
        })
    );
    assert!(endpoint.bridge.requests.is_empty());

    endpoint.handle(set(2, 0x22)).expect("first set succeeds");
    assert_eq!(
        endpoint.handle(set(3, 0x33)).expect("overlap is answered"),
        Some(UhidReply::SetReport {
            id: 3,
            success: false
        })
    );
    assert_eq!(endpoint.bridge.requests, vec![allowed_report(0x22)]);
}

#[test]
fn stop_discards_pending_data_and_blocks_exchange_until_restart() {
    let mut endpoint = endpoint(vec![BridgeExchangeResult::NotSent(
        crate::BridgeExchangeFailure::TransportNotSent,
    )]);
    endpoint.handle(set(1, 0x44)).expect("set succeeds");
    endpoint.handle(UhidEvent::Stop).expect("stop succeeds");
    assert_eq!(endpoint.state(), FeatureEndpointState::Stopped);
    assert!(!endpoint.has_pending_response());
    assert_eq!(
        endpoint.handle(get(2)).expect("stopped get is answered"),
        Some(UhidReply::GetReport { id: 2, data: None })
    );
}

#[test]
fn numbered_feature_start_is_rejected() {
    let policy = load_native_command_policies("1ef8a91712906a89fd332a2ed62bbd105315d6d1")
        .expect("generated policies load")
        .remove("child.razer.basilisk-v3-pro-35k.00cd")
        .expect("mouse policy exists");
    let mut endpoint = FeatureEndpoint::new(
        FakeBridge {
            results: VecDeque::new(),
            requests: Vec::new(),
        },
        policy,
    );
    assert_eq!(
        endpoint.handle(UhidEvent::Start { flags: 0x04 }),
        Err(EndpointError::InvalidFeatureRequest)
    );
    assert_eq!(endpoint.state(), FeatureEndpointState::Created);
}

#[test]
fn unknown_commands_are_denied_before_transport_without_disabling_the_endpoint() {
    let expected = [0x66; FEATURE_REPORT_BYTES];
    let mut endpoint =
        original_basilisk_endpoint(vec![BridgeExchangeResult::MatchedResponse(expected)]);
    let mut unknown = allowed_report(0x20);
    unknown[6] = 0x7e;
    unknown[7] = 0x01;
    unknown[88] = unknown[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte);
    assert_eq!(
        endpoint
            .handle(UhidEvent::SetReport {
                id: 1,
                report_number: 0,
                report_type: 0,
                data: unknown.to_vec(),
            })
            .expect("unknown set receives a local reply"),
        Some(UhidReply::SetReport {
            id: 1,
            success: false,
        })
    );
    assert!(endpoint.bridge.requests.is_empty());
    assert_eq!(endpoint.state(), FeatureEndpointState::Started);

    assert_eq!(
        endpoint.handle(set(2, 0x21)).expect("known set succeeds"),
        Some(UhidReply::SetReport {
            id: 2,
            success: true,
        })
    );
    assert_eq!(endpoint.bridge.requests, vec![allowed_report(0x21)]);
    assert_eq!(endpoint.state(), FeatureEndpointState::Started);
}
