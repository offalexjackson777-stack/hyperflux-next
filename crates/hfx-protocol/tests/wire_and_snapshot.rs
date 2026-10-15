// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ActivityState, BatteryPercent, ClientId, ClientName, ColorChannel, ConnectionMode,
    ContactState, ControllerAvailability, DeviceKind, EventBatchLimit, EvidenceConfidence,
    FreshnessState, GenerationId, InventoryAvailability, LeaseDurationMs, LedCount, ModelName,
    MonotonicMs, NativeExchangeTimeoutMs, NativeProtocolFamily, NativeReportByteCount,
    NativeReportHex, NativeRouteAccess, NativeRouteEventKind, NativeRouteState, NativeRouteToken,
    PairingState, PowerState, PresenceState, ProductId, ProjectionRevision, ProtocolFeatureId,
    ProtocolVersion, ReceiverLifecycleState, ResourceKind, RestoreDeliveryState, RestoreState,
    ReturnEvidence, ReturnPhase, RouteKind, RouteState, SequenceNumber, SleepState,
    StableLightingMode, StreamEpoch, SupportLevel, TelemetryAvailability, TransactionClass,
    VendorId,
};
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, ClientHello, ControllerActions, ControllerOwnership,
    ControllerView, DeviceInventoryView, EmptyRequest, EndpointSnapshot, EventCursor,
    IntegrationReceiverView, IntegrationView, LeaseRequest, LightingTopologyView,
    LogicalDeviceSnapshot, MAX_WIRE_MESSAGE_BYTES, NativeExchangeMatchedResponse,
    NativeExchangeOutcome, NativeExchangeRequest, NativeExchangeResult, NativeRoute,
    NativeRouteEvent, NativeRouteEventBatch, NativeRouteInventory, NegotiationRequestEnvelope,
    PresentationView, ProfileBindingView, ProtocolWireError, ReceiverSnapshot, ResourceKey,
    ResourceOwnershipSnapshot, ReturnEpoch, RpcRequest, RpcResponse, SessionRequestEnvelope,
    SnapshotValidationError, SubscriptionRequest, SuccessEnvelope, UnownedController,
    decode_rpc_request, decode_rpc_request_for_version, decode_rpc_response,
    decode_rpc_response_for_version, encode_rpc_request_for_version,
    encode_rpc_response_for_version, validate_bridge_snapshot, validate_rpc_response_for_version,
};
use serde_json::{Value, json};
use std::fs;
use std::io::Cursor;
use std::path::Path;

fn text<T: TryFrom<String>>(raw: &str) -> T
where
    T::Error: std::fmt::Debug,
{
    T::try_from(raw.to_owned()).expect("test text is valid")
}

fn number<T: TryFrom<u16>>(raw: u16) -> T
where
    T::Error: std::fmt::Debug,
{
    T::try_from(raw).expect("test number is valid")
}

fn cursor() -> EventCursor {
    EventCursor {
        stream_id: text("stream-1"),
        stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch is valid"),
        projection_revision: ProjectionRevision::try_from(1_u32)
            .expect("projection revision is valid"),
        sequence: SequenceNumber::try_from(0_u64).expect("sequence is valid"),
    }
}

fn native_route(access: NativeRouteAccess, route_state: NativeRouteState) -> NativeRoute {
    let writable =
        access == NativeRouteAccess::Qualified && route_state == NativeRouteState::Available;
    NativeRoute {
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: text("mouse-1"),
        endpoint_id: text("endpoint-hyperflux"),
        device_kind: DeviceKind::Mouse,
        vendor_id: VendorId::try_from(0x1532_u16).expect("vendor id is valid"),
        product_id: ProductId::try_from(0x00cd_u16).expect("product id is valid"),
        route_state,
        access,
        protocol_family: text::<NativeProtocolFamily>("razer-feature-report-v1"),
        report_bytes: number::<NativeReportByteCount>(90),
        route_token: writable.then(|| text::<NativeRouteToken>(&"a".repeat(64))),
        resource: writable.then(|| ResourceKey {
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
            device_id: text("mouse-1"),
            kind: ResourceKind::NativeControl,
        }),
    }
}

fn native_exchange_request(report: &str) -> RpcRequest {
    RpcRequest::NativeTransact(SessionRequestEnvelope {
        request_id: text("native-request-1"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: NativeExchangeRequest {
            request_id: text("native-request-1"),
            client_id: text("client-1"),
            lease_id: text("native-lease-1"),
            route_token: text::<NativeRouteToken>(&"a".repeat(64)),
            report: text::<NativeReportHex>(report),
            timeout_ms: number::<NativeExchangeTimeoutMs>(100),
        },
    })
}

fn endpoint() -> EndpointSnapshot {
    EndpointSnapshot {
        endpoint_id: text("endpoint-hyperflux"),
        route_kind: RouteKind::HyperfluxWireless,
        route_state: RouteState::Available,
        connection_mode: ConnectionMode::Hyperflux24ghz,
        power_state: PowerState::On,
        sleep_state: SleepState::Awake,
        activity_state: ActivityState::Active,
        contact_state: ContactState::OnMat,
        freshness: FreshnessState::Fresh,
        confidence: EvidenceConfidence::Observed,
        evidence_claim_id: Some(text("evidence-1")),
        observed_at_ms: Some(hfx_domain::MonotonicMs::try_from(10_u64).expect("time is valid")),
        return_epoch: None,
    }
}

fn return_epoch() -> ReturnEpoch {
    ReturnEpoch {
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: text("mouse-1"),
        endpoint_id: text("endpoint-hyperflux"),
        epoch_sequence: SequenceNumber::try_from(7_u64).expect("sequence is valid"),
        opened_at_ms: MonotonicMs::try_from(5_u64).expect("time is valid"),
        opening_evidence: ReturnEvidence::EndpointActivity,
        phase: ReturnPhase::Returning,
        delivery_state: RestoreDeliveryState::ReceiverDelivered,
    }
}

fn battery(availability: TelemetryAvailability, percentage: Option<u8>) -> BatteryObservation {
    BatteryObservation {
        availability,
        percentage: percentage
            .map(|value| BatteryPercent::try_from(value).expect("battery percentage is valid")),
        freshness: FreshnessState::Fresh,
        confidence: EvidenceConfidence::Observed,
        observed_at_ms: Some(hfx_domain::MonotonicMs::try_from(10_u64).expect("time is valid")),
    }
}

fn snapshot(battery: BatteryObservation) -> BridgeSnapshot {
    BridgeSnapshot {
        cursor: cursor(),
        receivers: vec![ReceiverSnapshot {
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
            profile_id: Some(text("receiver.razer.hyperflux-v2.1532-00cf")),
            profile_digest: Some(text(&"a".repeat(64))),
            lifecycle: ReceiverLifecycleState::Active,
            devices: vec![LogicalDeviceSnapshot {
                device_id: text("mouse-1"),
                device_kind: DeviceKind::Mouse,
                product_id: ProductId::try_from(0x00cd_u16).expect("product id is valid"),
                profile_id: Some(text("child.razer.basilisk-v3-pro-35k.00cd")),
                profile_digest: Some(text(&"b".repeat(64))),
                pairing: PairingState::Paired,
                presence: PresenceState::Available,
                support_level: SupportLevel::LightingQualified,
                endpoints: vec![endpoint()],
                battery,
                capabilities: vec![text("lighting.direct")],
            }],
            ownership: Vec::new(),
            stable_restore_enabled: false,
            restore_state: RestoreState::Idle,
        }],
    }
}

fn integration_view() -> IntegrationView {
    let receiver_profile = ProfileBindingView {
        profile_id: text("receiver.razer.hyperflux-v2.1532-00cf"),
        profile_digest: text(&"a".repeat(64)),
    };
    let device_profile = ProfileBindingView {
        profile_id: text("child.razer.basilisk-v3-pro-35k.00cd"),
        profile_digest: text(&"b".repeat(64)),
    };
    let model_name = text::<ModelName>("Razer Basilisk V3 Pro 35K");
    let battery = battery(TelemetryAvailability::Reported, Some(73));
    let capabilities = vec![text("lighting.direct-frame")];
    let inventory = DeviceInventoryView {
        device_id: text("mouse-1"),
        device_kind: DeviceKind::Mouse,
        product_id: ProductId::try_from(0x00cd_u16).expect("product id is valid"),
        profile: Some(device_profile.clone()),
        model_name: Some(model_name.clone()),
        pairing: PairingState::Paired,
        presence: PresenceState::Available,
        availability: InventoryAvailability::Available,
        support_level: SupportLevel::LightingQualified,
        endpoints: vec![endpoint()],
        battery: battery.clone(),
        capabilities: capabilities.clone(),
    };
    let resource = ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: text("mouse-1"),
        kind: ResourceKind::Lighting,
    };
    let controller = ControllerView {
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: text("mouse-1"),
        endpoint_id: text("endpoint-hyperflux"),
        device_kind: DeviceKind::Mouse,
        product_id: ProductId::try_from(0x00cd_u16).expect("product id is valid"),
        receiver_profile: receiver_profile.clone(),
        device_profile,
        model_name,
        presentation: PresentationView {
            upstream_id: text("openrgb-razer-basilisk-v3-pro-35k"),
            owner: text("OpenRGB"),
            project_version: text("1.0rc3"),
            source_revision: text("0123456789abcdef"),
            model_key: text("basilisk_v3_pro_35k_wireless_device"),
            layout_key: None,
            transport_variant: text("wireless"),
        },
        availability: ControllerAvailability::Ready,
        battery,
        capabilities,
        lighting: LightingTopologyView {
            physical_led_count: number::<LedCount>(13),
            application_slot_count: number::<LedCount>(13),
            rows: number::<LedCount>(1),
            columns: number::<LedCount>(13),
        },
        resource,
        ownership: ControllerOwnership::Unowned(UnownedController {}),
        actions: ControllerActions {
            can_acquire: true,
            can_release: false,
            can_submit_now: false,
        },
    };
    IntegrationView {
        cursor: cursor(),
        receivers: vec![IntegrationReceiverView {
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
            profile: Some(receiver_profile),
            model_name: Some(text("Razer HyperFlux V2")),
            lifecycle: ReceiverLifecycleState::Active,
            stable_restore_enabled: false,
            restore_state: RestoreState::Idle,
            inventory: vec![inventory],
            controllers: vec![controller],
        }],
    }
}

fn framed_transaction_value() -> Value {
    let transaction: Value = serde_json::from_str(include_str!(
        "../../../protocol/v2/fixtures/transaction-request-canonical.json"
    ))
    .expect("frozen v2 fixture is JSON");
    json!({
        "method": "submit-transaction",
        "request": {
            "request_id": "request-digest",
            "protocol_session_id": "protocol-session-1",
            "negotiation_token": "negotiation-1",
            "params": transaction
        }
    })
}

fn negotiation_request(version: u16, feature: &str) -> RpcRequest {
    RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: text("request-1"),
        params: ClientHello {
            client_id: text("client-1"),
            client_name: text("OpenRGB"),
            minimum_version: number(version),
            maximum_version: number(version),
            required_features: vec![text(feature)],
            optional_features: if version == 1 {
                vec![text("atomic-transactions")]
            } else {
                Vec::new()
            },
        },
    })
}

fn next_property_value(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

fn snapshot_response(with_return_epoch: bool) -> RpcResponse {
    let mut value = snapshot(battery(TelemetryAvailability::Reported, Some(73)));
    if with_return_epoch {
        value.receivers[0].devices[0].endpoints[0].return_epoch = Some(return_epoch());
    }
    RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("request-snapshot"),
        server_instance_id: text("bridge-instance-1"),
        result: value,
    })
}

fn native_inventory_response() -> RpcResponse {
    RpcResponse::NativeEnumerateSuccess(SuccessEnvelope {
        request_id: text("native-enumerate-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeRouteInventory {
            cursor: cursor(),
            routes: vec![native_route(
                NativeRouteAccess::Qualified,
                NativeRouteState::Available,
            )],
        },
    })
}

fn native_watch_response() -> RpcResponse {
    RpcResponse::NativeWatchSuccess(SuccessEnvelope {
        request_id: text("native-watch-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeRouteEventBatch {
            subscription_id: text("native-subscription-1"),
            next_cursor: EventCursor {
                sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
                ..cursor()
            },
            events: vec![NativeRouteEvent {
                sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
                kind: NativeRouteEventKind::Available,
                route: native_route(NativeRouteAccess::Qualified, NativeRouteState::Available),
            }],
            oldest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            latest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            dropped_events: hfx_domain::DroppedEventCount::try_from(0_u64)
                .expect("dropped count is valid"),
            cursor_gap: false,
            has_more: false,
        },
    })
}

fn native_exchange_response() -> RpcResponse {
    RpcResponse::NativeTransactSuccess(SuccessEnvelope {
        request_id: text("native-request-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeExchangeOutcome {
            request_id: text("native-request-1"),
            route_token: text::<NativeRouteToken>(&"a".repeat(64)),
            result: NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: text::<NativeReportHex>(&"01".repeat(90)),
            }),
        },
    })
}

fn assert_wire_golden(relative: &str, bytes: &[u8]) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/protocol")
        .join(relative);
    let mut expected = bytes.to_vec();
    expected.push(b'\n');
    if std::env::var_os("HFX_UPDATE_PROTOCOL_GOLDENS").is_some() {
        fs::create_dir_all(path.parent().expect("golden path has a parent"))
            .expect("golden directory is created");
        fs::write(&path, &expected).expect("golden fixture is written");
    }
    assert_eq!(fs::read(&path).expect("golden fixture reads"), expected);
}

fn assert_version_wire_goldens() {
    let features = [
        "ownership-leases",
        "profile-bound-transactions",
        "semantic-stable-lighting",
        "snapshot-profile-bindings",
        "integration-view-projection",
        "return-lifecycle-projection",
        "native-receiver-tunnel",
    ];
    for version in 1_u16..=7 {
        let request = negotiation_request(version, features[usize::from(version - 1)]);
        assert_wire_golden(
            &format!("v{version}/negotiate-request.json"),
            &encode_rpc_request_for_version(&request, number(version))
                .expect("negotiation request is representable"),
        );
        assert_wire_golden(
            &format!("v{version}/snapshot-response.json"),
            &encode_rpc_response_for_version(&snapshot_response(version >= 6), number(version))
                .expect("snapshot response is representable"),
        );
    }
}

fn assert_transaction_wire_goldens() {
    let v2 = decode_rpc_request_for_version(
        &serde_json::to_vec(&framed_transaction_value()).expect("v2 request serializes"),
        number(2),
    )
    .expect("v2 transaction normalizes");
    assert_wire_golden(
        "v2/static-transaction-request.json",
        &encode_rpc_request_for_version(&v2, number(2)).expect("v2 transaction encodes"),
    );

    let mut off = v2;
    let RpcRequest::SubmitTransaction(envelope) = &mut off else {
        panic!("transaction request expected")
    };
    envelope.params.stable_intents[0].mode = StableLightingMode::Off;
    envelope.params.frames[0].colors[0] = hfx_protocol::RgbColor {
        red: ColorChannel::try_from(0_u8).expect("red is valid"),
        green: ColorChannel::try_from(0_u8).expect("green is valid"),
        blue: ColorChannel::try_from(0_u8).expect("blue is valid"),
    };
    assert_wire_golden(
        "v3/off-transaction-request.json",
        &encode_rpc_request_for_version(&off, number(3)).expect("v3 Off encodes"),
    );
}

fn assert_integration_wire_goldens() {
    let integration_request = RpcRequest::IntegrationView(SessionRequestEnvelope {
        request_id: text("request-integration-view"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: EmptyRequest {},
    });
    assert_wire_golden(
        "v5/integration-view-request.json",
        &encode_rpc_request_for_version(&integration_request, number(5))
            .expect("v5 integration request encodes"),
    );
    let integration_response = RpcResponse::IntegrationViewSuccess(SuccessEnvelope {
        request_id: text("request-integration-view"),
        server_instance_id: text("bridge-instance-1"),
        result: integration_view(),
    });
    assert_wire_golden(
        "v5/integration-view-response.json",
        &encode_rpc_response_for_version(&integration_response, number(5))
            .expect("v5 integration response encodes"),
    );
}

fn assert_native_wire_goldens() {
    let enumerate_request = RpcRequest::NativeEnumerate(SessionRequestEnvelope {
        request_id: text("native-enumerate-1"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: EmptyRequest {},
    });
    let watch_request = RpcRequest::NativeWatch(SessionRequestEnvelope {
        request_id: text("native-watch-1"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: SubscriptionRequest {
            client_id: text("client-1"),
            subscription_id: None,
            expected_cursor: Some(cursor()),
            max_events: EventBatchLimit::try_from(16_u16).expect("event limit is valid"),
        },
    });
    for (name, request) in [
        ("native-enumerate-request", enumerate_request),
        ("native-watch-request", watch_request),
        (
            "native-transact-request",
            native_exchange_request(&"00".repeat(90)),
        ),
    ] {
        assert_wire_golden(
            &format!("v7/{name}.json"),
            &encode_rpc_request_for_version(&request, number(7))
                .expect("v7 native request encodes"),
        );
    }
    for (name, response) in [
        ("native-enumerate-response", native_inventory_response()),
        ("native-watch-response", native_watch_response()),
        ("native-transact-response", native_exchange_response()),
    ] {
        assert_wire_golden(
            &format!("v7/{name}.json"),
            &encode_rpc_response_for_version(&response, number(7))
                .expect("v7 native response encodes"),
        );
    }
}

#[test]
fn retained_v1_through_v7_wire_surfaces_are_byte_golden() {
    assert_version_wire_goldens();
    assert_transaction_wire_goldens();
    assert_integration_wire_goldens();
    assert_native_wire_goldens();
}

#[test]
fn seeded_framing_and_version_adapter_mutations_fail_closed() {
    let features = [
        "ownership-leases",
        "profile-bound-transactions",
        "semantic-stable-lighting",
        "snapshot-profile-bindings",
        "integration-view-projection",
        "return-lifecycle-projection",
        "native-receiver-tunnel",
    ];
    let mut state = 0x6a09_e667_f3bc_c909_u64;
    for version in 1_u16..=7 {
        let protocol_version = number(version);
        let request = negotiation_request(version, features[usize::from(version - 1)]);
        let canonical = encode_rpc_request_for_version(&request, protocol_version)
            .expect("canonical version request encodes");
        for iteration in 0..256_u16 {
            let mut payload = canonical.clone();
            let mutations = usize::from(iteration % 3 + 1);
            for _ in 0..mutations {
                let value = next_property_value(&mut state);
                let index =
                    usize::try_from(value).expect("host usize contains u64") % payload.len();
                payload[index] ^=
                    u8::try_from(((value >> 32) & 0xff) | 1).expect("masked byte fits u8");
            }

            if let Ok(decoded) = decode_rpc_request_for_version(&payload, protocol_version) {
                let encoded = encode_rpc_request_for_version(&decoded, protocol_version)
                    .expect("accepted mutation remains representable by the same version");
                assert_eq!(
                    decode_rpc_request_for_version(&encoded, protocol_version),
                    Ok(decoded)
                );
            }

            let declared = match iteration % 4 {
                0 => payload.len(),
                1 => 0,
                2 => payload.len() + 1,
                _ => MAX_WIRE_MESSAGE_BYTES + 1,
            };
            let mut frame = u32::try_from(declared)
                .expect("framing test length fits u32")
                .to_be_bytes()
                .to_vec();
            frame.extend_from_slice(&payload);
            let result = hfx_protocol::read_rpc_request_for_version(
                &mut Cursor::new(frame),
                protocol_version,
            );
            if let Ok(Some(decoded)) = result {
                let encoded = encode_rpc_request_for_version(&decoded, protocol_version)
                    .expect("accepted frame remains representable by the same version");
                assert_eq!(
                    decode_rpc_request_for_version(&encoded, protocol_version),
                    Ok(decoded)
                );
            }
        }
    }
}

#[test]
fn oversized_wire_message_is_rejected_before_json_parsing() {
    let input = vec![b' '; MAX_WIRE_MESSAGE_BYTES + 1];
    assert_eq!(
        decode_rpc_request(&input),
        Err(ProtocolWireError::MessageTooLarge)
    );
}

#[test]
fn decoded_request_collections_are_checked_against_protocol_bounds() {
    let required_features = (0..65)
        .map(|index| text::<ProtocolFeatureId>(&format!("feature-{index}")))
        .collect();
    let request = RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: text("request-1"),
        params: ClientHello {
            client_id: text::<ClientId>("client-1"),
            client_name: text::<ClientName>("test-client"),
            minimum_version: number::<ProtocolVersion>(1),
            maximum_version: number::<ProtocolVersion>(1),
            required_features,
            optional_features: Vec::new(),
        },
    });
    let encoded = serde_json::to_vec(&request).expect("request serializes");
    assert_eq!(
        decode_rpc_request(&encoded),
        Err(ProtocolWireError::RequestBoundExceeded)
    );
}

#[test]
fn snapshot_preserves_zero_battery_and_rejects_availability_contradictions() {
    let valid = snapshot(battery(TelemetryAvailability::Reported, Some(0)));
    validate_bridge_snapshot(&valid).expect("zero percent is a reported battery value");

    let contradictory = snapshot(battery(TelemetryAvailability::Unavailable, Some(0)));
    assert_eq!(
        validate_bridge_snapshot(&contradictory),
        Err(SnapshotValidationError::BatteryValueContradiction)
    );
}

#[test]
fn valid_snapshot_response_round_trips_through_bounded_decoder() {
    let response = RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("request-1"),
        server_instance_id: text("bridge-instance-1"),
        result: snapshot(battery(TelemetryAvailability::Reported, Some(73))),
    });
    let encoded = serde_json::to_vec(&response).expect("response serializes");
    assert_eq!(
        decode_rpc_response(&encoded).expect("bounded response is valid"),
        response
    );
}

#[test]
fn v5_integration_view_round_trips_and_cannot_downgrade() {
    let request = RpcRequest::IntegrationView(SessionRequestEnvelope {
        request_id: text("request-integration-view"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: EmptyRequest {},
    });
    let encoded = encode_rpc_request_for_version(&request, number(5))
        .expect("v5 integration request encodes");
    assert_eq!(
        decode_rpc_request_for_version(&encoded, number(5)),
        Ok(request.clone())
    );
    for version in 1_u16..=4 {
        assert_eq!(
            encode_rpc_request_for_version(&request, number(version)),
            Err(ProtocolWireError::UnsupportedVersionMethod)
        );
    }

    let response = RpcResponse::IntegrationViewSuccess(SuccessEnvelope {
        request_id: text("request-integration-view"),
        server_instance_id: text("bridge-instance-1"),
        result: integration_view(),
    });
    let encoded = encode_rpc_response_for_version(&response, number(5))
        .expect("v5 integration response encodes");
    assert_eq!(
        decode_rpc_response_for_version(&encoded, number(5)),
        Ok(response.clone())
    );
    for version in 1_u16..=4 {
        assert_eq!(
            encode_rpc_response_for_version(&response, number(version)),
            Err(ProtocolWireError::UnsupportedVersionMethod)
        );
    }
}

#[test]
fn integration_view_rejects_cross_generation_controller_claims() {
    let mut view = integration_view();
    view.receivers[0].controllers[0].generation_id =
        GenerationId::try_from(2_u64).expect("generation is valid");
    let response = RpcResponse::IntegrationViewSuccess(SuccessEnvelope {
        request_id: text("request-contradictory-view"),
        server_instance_id: text("bridge-instance-1"),
        result: view,
    });
    assert!(matches!(
        validate_rpc_response_for_version(&response, number(5)),
        Err(ProtocolWireError::InvalidResponse(_))
    ));
}

#[test]
fn v4_preserves_profile_bindings_and_v3_downgrades_them_explicitly() {
    let response = RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("request-profile-snapshot"),
        server_instance_id: text("bridge-instance-1"),
        result: snapshot(battery(TelemetryAvailability::Reported, Some(73))),
    });
    let v4 = hfx_protocol::encode_rpc_response_for_version(&response, number(4))
        .expect("v4 snapshot encodes");
    assert_eq!(
        decode_rpc_response_for_version(&v4, number(4)).expect("v4 snapshot decodes"),
        response
    );

    let v3 = hfx_protocol::encode_rpc_response_for_version(&response, number(3))
        .expect("v3 snapshot explicitly omits v4-only bindings");
    let RpcResponse::SnapshotSuccess(legacy) =
        decode_rpc_response_for_version(&v3, number(3)).expect("v3 snapshot decodes")
    else {
        panic!("snapshot response expected")
    };
    let receiver = &legacy.result.receivers[0];
    assert!(receiver.profile_id.is_none());
    assert!(receiver.profile_digest.is_none());
    assert!(receiver.devices[0].profile_id.is_some());
    assert!(receiver.devices[0].profile_digest.is_none());
}

#[test]
fn unknown_rpc_methods_never_reach_dispatch() {
    let encoded = br#"{"method":"raw-hid","request":{}}"#;
    assert_eq!(
        decode_rpc_request(encoded),
        Err(ProtocolWireError::MalformedJson)
    );
}

#[test]
fn color_channel_type_remains_eight_bit_at_the_wire_boundary() {
    assert!(ColorChannel::try_from(255_u8).is_ok());
}

#[test]
fn frozen_v2_static_requests_normalize_to_conservative_static_semantics() {
    let value = framed_transaction_value();
    let encoded = serde_json::to_vec(&value).expect("request serializes");
    let decoded = decode_rpc_request_for_version(&encoded, number(2))
        .expect("frozen v2 Static request is safely normalized");
    let RpcRequest::SubmitTransaction(envelope) = decoded else {
        panic!("fixture must remain a transaction")
    };
    assert_eq!(
        envelope.params.transaction_class,
        TransactionClass::StaticLighting
    );
    assert_eq!(envelope.params.stable_intents.len(), 1);
    assert_eq!(
        envelope.params.stable_intents[0].mode,
        StableLightingMode::Static
    );

    let mut effect = value;
    effect["request"]["params"]["transaction_class"] = json!("effect-frame");
    let effect = serde_json::to_vec(&effect).expect("effect request serializes");
    let RpcRequest::SubmitTransaction(effect) =
        decode_rpc_request_for_version(&effect, number(2)).expect("v2 effect is normalized")
    else {
        panic!("fixture must remain a transaction")
    };
    assert!(effect.params.stable_intents.is_empty());
}

#[test]
fn frozen_v1_profileless_writes_fail_before_backend_dispatch() {
    let mut value = framed_transaction_value();
    let params = value["request"]["params"]
        .as_object_mut()
        .expect("transaction params are an object");
    params.remove("receiver_profile_id");
    params.remove("receiver_profile_digest");
    params.remove("device_profiles");
    let encoded = serde_json::to_vec(&value).expect("legacy request serializes");
    assert_eq!(
        decode_rpc_request_for_version(&encoded, number(1)),
        Err(ProtocolWireError::UnsupportedVersionMethod)
    );

    let snapshot = json!({
        "method": "snapshot",
        "request": {
            "request_id": "request-snapshot",
            "protocol_session_id": "protocol-session-1",
            "negotiation_token": "negotiation-1",
            "params": {}
        }
    });
    let snapshot = serde_json::to_vec(&snapshot).expect("snapshot serializes");
    assert!(matches!(
        decode_rpc_request_for_version(&snapshot, number(1)),
        Ok(RpcRequest::Snapshot(_))
    ));
}

#[test]
fn versioned_decoders_reject_changed_or_unknown_wire_shapes_instead_of_guessing() {
    let v2 = serde_json::to_vec(&framed_transaction_value()).expect("request serializes");
    assert_eq!(
        decode_rpc_request_for_version(&v2, number(3)),
        Err(ProtocolWireError::MalformedJson)
    );

    let normalized = decode_rpc_request_for_version(&v2, number(2)).expect("v2 request normalizes");
    let v3 = serde_json::to_vec(&normalized).expect("normalized request serializes");
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(2)),
        Err(ProtocolWireError::MalformedJson)
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(4)),
        Ok(normalized.clone())
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(5)),
        Ok(normalized.clone())
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(6)),
        Ok(normalized.clone())
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(7)),
        Ok(normalized)
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(8)),
        Err(ProtocolWireError::UnsupportedProtocolVersion)
    );
}

#[test]
fn outbound_requests_downgrade_only_when_semantics_are_exactly_representable() {
    let v2 = serde_json::to_vec(&framed_transaction_value()).expect("request serializes");
    let mut current =
        decode_rpc_request_for_version(&v2, number(2)).expect("v2 request normalizes");

    let encoded_v2 = encode_rpc_request_for_version(&current, number(2))
        .expect("Static semantics are representable by v2");
    assert_eq!(
        decode_rpc_request_for_version(&encoded_v2, number(2)),
        Ok(current.clone())
    );
    assert_eq!(
        encode_rpc_request_for_version(&current, number(1)),
        Err(ProtocolWireError::UnsupportedVersionMethod)
    );

    let RpcRequest::SubmitTransaction(envelope) = &mut current else {
        panic!("fixture must remain a transaction")
    };
    envelope.params.stable_intents[0].mode = StableLightingMode::Off;
    envelope.params.frames[0].colors[0].red = ColorChannel::try_from(0_u8).expect("black red");
    envelope.params.frames[0].colors[0].green = ColorChannel::try_from(0_u8).expect("black green");
    envelope.params.frames[0].colors[0].blue = ColorChannel::try_from(0_u8).expect("black blue");
    assert_eq!(
        encode_rpc_request_for_version(&current, number(2)),
        Err(ProtocolWireError::VersionTranslation)
    );

    let encoded_v3 =
        encode_rpc_request_for_version(&current, number(3)).expect("v3 preserves explicit Off");
    assert_eq!(
        decode_rpc_request_for_version(&encoded_v3, number(3)),
        Ok(current)
    );
}

#[test]
fn responses_are_checked_against_the_exact_negotiated_schema() {
    let response = RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("request-1"),
        server_instance_id: text("bridge-instance-1"),
        result: snapshot(battery(TelemetryAvailability::Reported, Some(73))),
    });
    for version in [1_u16, 2, 3] {
        let version = number(version);
        validate_rpc_response_for_version(&response, version)
            .expect("current response is representable by every retained response schema");
        let encoded = encode_rpc_response_for_version(&response, version)
            .expect("response encodes in the frozen schema");
        let RpcResponse::SnapshotSuccess(legacy) =
            decode_rpc_response_for_version(&encoded, version).expect("legacy response decodes")
        else {
            panic!("snapshot response expected")
        };
        assert!(legacy.result.receivers[0].profile_id.is_none());
        assert!(legacy.result.receivers[0].profile_digest.is_none());
        assert!(
            legacy.result.receivers[0].devices[0]
                .profile_digest
                .is_none()
        );
    }
    let v4 = number(4);
    validate_rpc_response_for_version(&response, v4).expect("v4 preserves complete bindings");
    let encoded = encode_rpc_response_for_version(&response, v4).expect("v4 response encodes");
    assert_eq!(
        decode_rpc_response_for_version(&encoded, v4),
        Ok(response.clone())
    );
    let v5 = number(5);
    validate_rpc_response_for_version(&response, v5).expect("v5 preserves complete bindings");
    let encoded = encode_rpc_response_for_version(&response, v5).expect("v5 response encodes");
    assert_eq!(decode_rpc_response_for_version(&encoded, v5), Ok(response));
    assert_eq!(
        validate_rpc_response_for_version(
            &RpcResponse::SnapshotSuccess(SuccessEnvelope {
                request_id: text("request-unsupported-version"),
                server_instance_id: text("bridge-instance-1"),
                result: snapshot(battery(TelemetryAvailability::Reported, Some(73))),
            }),
            number(8)
        ),
        Err(ProtocolWireError::UnsupportedProtocolVersion)
    );
}

#[test]
fn v6_projects_return_epochs_while_v5_omits_them_safely() {
    let mut current = snapshot(battery(TelemetryAvailability::Reported, Some(73)));
    current.receivers[0].devices[0].endpoints[0].return_epoch = Some(return_epoch());
    let response = RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("request-return-epoch"),
        server_instance_id: text("bridge-instance-1"),
        result: current,
    });

    let v6 = number(6);
    let encoded = encode_rpc_response_for_version(&response, v6)
        .expect("v6 preserves the return lifecycle projection");
    assert_eq!(
        decode_rpc_response_for_version(&encoded, v6),
        Ok(response.clone())
    );

    let v5 = number(5);
    let encoded = encode_rpc_response_for_version(&response, v5)
        .expect("v5 safely omits the optional lifecycle projection");
    let RpcResponse::SnapshotSuccess(legacy) =
        decode_rpc_response_for_version(&encoded, v5).expect("v5 snapshot decodes")
    else {
        panic!("snapshot response expected")
    };
    assert!(
        legacy.result.receivers[0].devices[0].endpoints[0]
            .return_epoch
            .is_none()
    );
}

#[test]
fn return_epoch_projection_rejects_cross_record_or_false_synchronization_claims() {
    let mut wrong_device = snapshot(battery(TelemetryAvailability::Reported, Some(73)));
    wrong_device.receivers[0].devices[0].endpoints[0].return_epoch = Some(ReturnEpoch {
        device_id: text("keyboard-1"),
        ..return_epoch()
    });
    assert_eq!(
        validate_bridge_snapshot(&wrong_device),
        Err(SnapshotValidationError::ReturnEpochContradiction)
    );

    let mut false_sync = snapshot(battery(TelemetryAvailability::Reported, Some(73)));
    false_sync.receivers[0].devices[0].endpoints[0].return_epoch = Some(ReturnEpoch {
        phase: ReturnPhase::Synchronized,
        delivery_state: RestoreDeliveryState::ReceiverDelivered,
        ..return_epoch()
    });
    assert_eq!(
        validate_bridge_snapshot(&false_sync),
        Err(SnapshotValidationError::ReturnEpochContradiction)
    );
}

#[test]
fn v7_native_exchange_round_trips_and_cannot_downgrade() {
    let report = "00".repeat(90);
    let request = native_exchange_request(&report);
    let encoded = encode_rpc_request_for_version(&request, number(7))
        .expect("canonical native request encodes in v7");
    assert_eq!(
        decode_rpc_request_for_version(&encoded, number(7)),
        Ok(request.clone())
    );
    for version in 1_u16..=6 {
        assert_eq!(
            encode_rpc_request_for_version(&request, number(version)),
            Err(ProtocolWireError::UnsupportedVersionMethod)
        );
    }

    let response = RpcResponse::NativeTransactSuccess(SuccessEnvelope {
        request_id: text("native-request-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeExchangeOutcome {
            request_id: text("native-request-1"),
            route_token: text::<NativeRouteToken>(&"a".repeat(64)),
            result: NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: text::<NativeReportHex>(&"01".repeat(90)),
            }),
        },
    });
    let encoded = encode_rpc_response_for_version(&response, number(7))
        .expect("canonical native response encodes in v7");
    assert_eq!(
        decode_rpc_response_for_version(&encoded, number(7)),
        Ok(response.clone())
    );
    for version in 1_u16..=6 {
        assert_eq!(
            encode_rpc_response_for_version(&response, number(version)),
            Err(ProtocolWireError::UnsupportedVersionMethod)
        );
    }
}

#[test]
fn native_exchange_rejects_noncanonical_reports_and_request_identity_conflicts() {
    let uppercase = native_exchange_request(&"AA".repeat(90));
    assert_eq!(
        encode_rpc_request_for_version(&uppercase, number(7)),
        Err(ProtocolWireError::RequestNotCanonical)
    );

    let mut mismatched = native_exchange_request(&"00".repeat(90));
    let RpcRequest::NativeTransact(envelope) = &mut mismatched else {
        panic!("native request expected")
    };
    envelope.params.request_id = text("different-request");
    assert_eq!(
        encode_rpc_request_for_version(&mismatched, number(7)),
        Err(ProtocolWireError::RequestNotCanonical)
    );
}

#[test]
fn native_inventory_fails_closed_on_authority_or_identity_contradictions() {
    let valid = RpcResponse::NativeEnumerateSuccess(SuccessEnvelope {
        request_id: text("native-enumerate-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeRouteInventory {
            cursor: cursor(),
            routes: vec![native_route(
                NativeRouteAccess::Qualified,
                NativeRouteState::Available,
            )],
        },
    });
    encode_rpc_response_for_version(&valid, number(7)).expect("qualified route is canonical");

    let mut candidate_with_authority = valid.clone();
    let RpcResponse::NativeEnumerateSuccess(envelope) = &mut candidate_with_authority else {
        panic!("native inventory expected")
    };
    envelope.result.routes[0].access = NativeRouteAccess::Candidate;
    assert!(matches!(
        encode_rpc_response_for_version(&candidate_with_authority, number(7)),
        Err(ProtocolWireError::InvalidResponse(_))
    ));

    let mut duplicate_identity = valid;
    let RpcResponse::NativeEnumerateSuccess(envelope) = &mut duplicate_identity else {
        panic!("native inventory expected")
    };
    envelope.result.routes.push(native_route(
        NativeRouteAccess::Qualified,
        NativeRouteState::Withdrawn,
    ));
    assert!(matches!(
        encode_rpc_response_for_version(&duplicate_identity, number(7)),
        Err(ProtocolWireError::InvalidResponse(_))
    ));
}

#[test]
fn native_route_events_require_consistent_transitions_and_cursors() {
    let response = RpcResponse::NativeWatchSuccess(SuccessEnvelope {
        request_id: text("native-watch-1"),
        server_instance_id: text("bridge-instance-1"),
        result: NativeRouteEventBatch {
            subscription_id: text("native-subscription-1"),
            next_cursor: EventCursor {
                sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
                ..cursor()
            },
            events: vec![NativeRouteEvent {
                sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
                kind: NativeRouteEventKind::Available,
                route: native_route(NativeRouteAccess::Qualified, NativeRouteState::Available),
            }],
            oldest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            latest_available: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            dropped_events: hfx_domain::DroppedEventCount::try_from(0_u64)
                .expect("dropped count is valid"),
            cursor_gap: false,
            has_more: false,
        },
    });
    encode_rpc_response_for_version(&response, number(7)).expect("native event batch is valid");

    let mut temporarily_unavailable = response.clone();
    let RpcResponse::NativeWatchSuccess(envelope) = &mut temporarily_unavailable else {
        panic!("native event response expected")
    };
    envelope.result.events[0].kind = NativeRouteEventKind::TemporarilyUnavailable;
    envelope.result.events[0].route = native_route(
        NativeRouteAccess::Qualified,
        NativeRouteState::TemporarilyUnavailable,
    );
    encode_rpc_response_for_version(&temporarily_unavailable, number(7))
        .expect("temporary route loss is canonical");

    let mut withdrawn = response.clone();
    let RpcResponse::NativeWatchSuccess(envelope) = &mut withdrawn else {
        panic!("native event response expected")
    };
    envelope.result.events[0].kind = NativeRouteEventKind::Withdrawn;
    envelope.result.events[0].route =
        native_route(NativeRouteAccess::Qualified, NativeRouteState::Withdrawn);
    encode_rpc_response_for_version(&withdrawn, number(7)).expect("route withdrawal is canonical");

    let mut contradictory = response;
    let RpcResponse::NativeWatchSuccess(envelope) = &mut contradictory else {
        panic!("native event response expected")
    };
    envelope.result.events[0].kind = NativeRouteEventKind::Withdrawn;
    assert!(matches!(
        encode_rpc_response_for_version(&contradictory, number(7)),
        Err(ProtocolWireError::InvalidResponse(_))
    ));
}

#[test]
fn legacy_versions_reject_native_leases_and_hide_native_snapshot_ownership() {
    let native_resource = ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: text("mouse-1"),
        kind: ResourceKind::NativeControl,
    };
    let request = RpcRequest::AcquireLease(SessionRequestEnvelope {
        request_id: text("native-lease-request"),
        protocol_session_id: text("protocol-session-1"),
        negotiation_token: text("negotiation-1"),
        params: LeaseRequest {
            request_id: text("native-lease-request"),
            client_id: text("client-1"),
            resources: vec![native_resource.clone()],
            duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is valid"),
        },
    });
    encode_rpc_request_for_version(&request, number(7)).expect("v7 native lease request is valid");
    assert_eq!(
        encode_rpc_request_for_version(&request, number(6)),
        Err(ProtocolWireError::UnsupportedVersionMethod)
    );
    let wire = serde_json::to_vec(&request).expect("request serializes");
    assert_eq!(
        decode_rpc_request_for_version(&wire, number(6)),
        Err(ProtocolWireError::UnsupportedVersionMethod)
    );

    let mut current = snapshot(battery(TelemetryAvailability::Reported, Some(73)));
    current.receivers[0]
        .ownership
        .push(ResourceOwnershipSnapshot {
            resource: native_resource,
            client_id: text("native-client"),
            lease_id: text("native-lease-1"),
            expires_at_ms: MonotonicMs::try_from(100_u64).expect("time is valid"),
        });
    let response = RpcResponse::SnapshotSuccess(SuccessEnvelope {
        request_id: text("snapshot-with-native-ownership"),
        server_instance_id: text("bridge-instance-1"),
        result: current,
    });
    let v7 = encode_rpc_response_for_version(&response, number(7))
        .expect("v7 preserves native ownership");
    let RpcResponse::SnapshotSuccess(v7) =
        decode_rpc_response_for_version(&v7, number(7)).expect("v7 snapshot decodes")
    else {
        panic!("snapshot response expected")
    };
    assert_eq!(v7.result.receivers[0].ownership.len(), 1);

    let v6 = encode_rpc_response_for_version(&response, number(6))
        .expect("v6 snapshot safely omits native ownership");
    let RpcResponse::SnapshotSuccess(v6) =
        decode_rpc_response_for_version(&v6, number(6)).expect("v6 snapshot decodes")
    else {
        panic!("snapshot response expected")
    };
    assert!(v6.result.receivers[0].ownership.is_empty());
}

fn assert_distinct_messages<E: std::fmt::Display>(errors: &[E]) {
    let messages: std::collections::BTreeSet<String> =
        errors.iter().map(ToString::to_string).collect();
    assert!(messages.iter().all(|message| !message.is_empty()));
    assert_eq!(messages.len(), errors.len(), "{messages:#?}");
}

#[test]
fn every_validation_and_snapshot_error_names_a_distinct_failure() {
    use hfx_protocol::{ProtocolValidationError, SnapshotValidationError};

    assert_distinct_messages(&[
        ProtocolValidationError::EmptyResources,
        ProtocolValidationError::TooManyResources,
        ProtocolValidationError::DuplicateResource,
        ProtocolValidationError::ResourcesNotCanonical,
        ProtocolValidationError::EmptyFrames,
        ProtocolValidationError::TooManyFrames,
        ProtocolValidationError::DuplicateFrameIndex,
        ProtocolValidationError::DuplicateFrameTarget,
        ProtocolValidationError::NonCanonicalFrameOrder,
        ProtocolValidationError::EmptyProfileBindings,
        ProtocolValidationError::TooManyProfileBindings,
        ProtocolValidationError::DuplicateProfileBinding,
        ProtocolValidationError::ProfileBindingsNotCanonical,
        ProtocolValidationError::ZeroApplicationSlots,
        ProtocolValidationError::FrameWithoutProfileBinding,
        ProtocolValidationError::ProfileBindingWithoutFrame,
        ProtocolValidationError::FrameColorCountMismatch,
        ProtocolValidationError::TooManyStableIntents,
        ProtocolValidationError::DuplicateStableIntent,
        ProtocolValidationError::StableIntentsNotCanonical,
        ProtocolValidationError::StableIntentWithoutFrame,
        ProtocolValidationError::FrameWithoutStableIntent,
        ProtocolValidationError::StableIntentOnNonStableTransaction,
        ProtocolValidationError::OffIntentHasLitColor,
        ProtocolValidationError::EmptyColors,
        ProtocolValidationError::TooManyColors,
        ProtocolValidationError::TooManyAggregateColors,
        ProtocolValidationError::FrameWithoutLightingLease,
        ProtocolValidationError::ResourceWithoutFrame,
        ProtocolValidationError::ResourceOutsideTransactionGeneration,
        ProtocolValidationError::UnsupportedTransactionClass,
    ]);
    assert_distinct_messages(&[
        SnapshotValidationError::TooManyReceivers,
        SnapshotValidationError::ReceiversNotCanonical,
        SnapshotValidationError::TooManyDevices,
        SnapshotValidationError::DevicesNotCanonical,
        SnapshotValidationError::TooManyEndpoints,
        SnapshotValidationError::EndpointsNotCanonical,
        SnapshotValidationError::TooManyCapabilities,
        SnapshotValidationError::CapabilitiesNotCanonical,
        SnapshotValidationError::TooManyOwnershipRecords,
        SnapshotValidationError::OwnershipNotCanonical,
        SnapshotValidationError::OwnershipOutsideReceiverGeneration,
        SnapshotValidationError::OwnershipTargetsUnknownDevice,
        SnapshotValidationError::BatteryValueContradiction,
        SnapshotValidationError::ReceiverProfileBindingContradiction,
        SnapshotValidationError::DeviceProfileBindingContradiction,
        SnapshotValidationError::PresenceContradiction,
        SnapshotValidationError::ReturnEpochContradiction,
    ]);
}

#[test]
fn every_wire_negotiation_and_framing_error_names_a_distinct_failure() {
    use hfx_protocol::{
        FrameError, FrameIoStage, NegotiationError, ProtocolValidationError, ProtocolWireError,
        SnapshotValidationError,
    };
    use std::io::ErrorKind;

    assert_distinct_messages(&[
        ProtocolWireError::MessageTooLarge,
        ProtocolWireError::MalformedJson,
        ProtocolWireError::RequestBoundExceeded,
        ProtocolWireError::RequestNotCanonical,
        ProtocolWireError::UnsupportedProtocolVersion,
        ProtocolWireError::UnsupportedVersionMethod,
        ProtocolWireError::VersionTranslation,
        ProtocolWireError::InvalidRequest(ProtocolValidationError::EmptyFrames),
        ProtocolWireError::InvalidSnapshot(SnapshotValidationError::TooManyDevices),
        ProtocolWireError::InvalidResponse("result shape"),
    ]);
    assert_distinct_messages(&[
        NegotiationError::InvalidClientRange,
        NegotiationError::InvalidServerRange,
        NegotiationError::IncompatibleVersion,
        NegotiationError::TooManyFeatures,
        NegotiationError::DuplicateFeature,
        NegotiationError::UnsupportedRequiredFeatures(Vec::new()),
        NegotiationError::InvalidBridgeContract,
    ]);
    assert_distinct_messages(&[
        FrameError::Io {
            stage: FrameIoStage::ReadLength,
            kind: ErrorKind::UnexpectedEof,
        },
        FrameError::Io {
            stage: FrameIoStage::ReadPayload,
            kind: ErrorKind::UnexpectedEof,
        },
        FrameError::Io {
            stage: FrameIoStage::WriteLength,
            kind: ErrorKind::BrokenPipe,
        },
        FrameError::Io {
            stage: FrameIoStage::WritePayload,
            kind: ErrorKind::BrokenPipe,
        },
        FrameError::Io {
            stage: FrameIoStage::Flush,
            kind: ErrorKind::BrokenPipe,
        },
        FrameError::TruncatedLength { received: 2 },
        FrameError::EmptyPayload,
        FrameError::PayloadTooLarge {
            declared: 9,
            maximum: 8,
        },
        FrameError::TruncatedPayload {
            declared: 9,
            received: 3,
        },
        FrameError::InvalidRequest(ProtocolWireError::MalformedJson),
        FrameError::InvalidResponse(ProtocolWireError::MalformedJson),
    ]);
}
