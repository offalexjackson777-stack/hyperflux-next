// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ActivityState, BatteryPercent, ClientId, ClientName, ColorChannel, ConnectionMode,
    ContactState, DeviceKind, EvidenceConfidence, FreshnessState, GenerationId, PairingState,
    PowerState, PresenceState, ProductId, ProjectionRevision, ProtocolFeatureId, ProtocolVersion,
    ReceiverLifecycleState, RestoreState, RouteKind, RouteState, SequenceNumber, SleepState,
    StreamEpoch, SupportLevel, TelemetryAvailability,
};
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, ClientHello, EndpointSnapshot, EventCursor,
    LogicalDeviceSnapshot, MAX_WIRE_MESSAGE_BYTES, NegotiationRequestEnvelope, ProtocolWireError,
    ReceiverSnapshot, RpcRequest, RpcResponse, SnapshotValidationError, SuccessEnvelope,
    decode_rpc_request, decode_rpc_response, validate_bridge_snapshot,
};

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
            lifecycle: ReceiverLifecycleState::Active,
            devices: vec![LogicalDeviceSnapshot {
                device_id: text("mouse-1"),
                device_kind: DeviceKind::Mouse,
                product_id: ProductId::try_from(0x00cd_u16).expect("product id is valid"),
                profile_id: Some(text("child.razer.basilisk-v3-pro-35k.00cd")),
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
