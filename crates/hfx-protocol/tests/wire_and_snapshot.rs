// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ActivityState, BatteryPercent, ClientId, ClientName, ColorChannel, ConnectionMode,
    ContactState, ControllerAvailability, DeviceKind, EvidenceConfidence, FreshnessState,
    GenerationId, InventoryAvailability, LedCount, ModelName, PairingState, PowerState,
    PresenceState, ProductId, ProjectionRevision, ProtocolFeatureId, ProtocolVersion,
    ReceiverLifecycleState, ResourceKind, RestoreState, RouteKind, RouteState, SequenceNumber,
    SleepState, StableLightingMode, StreamEpoch, SupportLevel, TelemetryAvailability,
    TransactionClass,
};
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, ClientHello, ControllerActions, ControllerOwnership,
    ControllerView, DeviceInventoryView, EmptyRequest, EndpointSnapshot, EventCursor,
    IntegrationReceiverView, IntegrationView, LightingTopologyView, LogicalDeviceSnapshot,
    MAX_WIRE_MESSAGE_BYTES, NegotiationRequestEnvelope, PresentationView, ProfileBindingView,
    ProtocolWireError, ReceiverSnapshot, ResourceKey, RpcRequest, RpcResponse,
    SessionRequestEnvelope, SnapshotValidationError, SuccessEnvelope, UnownedController,
    decode_rpc_request, decode_rpc_request_for_version, decode_rpc_response,
    decode_rpc_response_for_version, encode_rpc_request_for_version,
    encode_rpc_response_for_version, validate_bridge_snapshot, validate_rpc_response_for_version,
};
use serde_json::{Value, json};

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
        Ok(normalized)
    );
    assert_eq!(
        decode_rpc_request_for_version(&v3, number(6)),
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
            number(6)
        ),
        Err(ProtocolWireError::UnsupportedProtocolVersion)
    );
}
