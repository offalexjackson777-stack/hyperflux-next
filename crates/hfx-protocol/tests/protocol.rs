// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ColorChannel, FrameIndex, GenerationId, LeaseDurationMs, MonotonicMs, ResourceKind,
    TransactionClass,
};
use hfx_protocol::{
    ClientHello, LeaseConflict, LeaseRequest, LeaseResult, LightingFrame, NegotiationContext,
    NegotiationError, NegotiationRequestEnvelope, ProtocolContract, ResourceKey, RgbColor,
    RpcRequest, RpcResponse, SuccessEnvelope, TransactionRequest, negotiate,
    negotiate_with_contract, validate_lease_request, validate_transaction,
};

fn value<T: TryFrom<String>>(raw: &str) -> T
where
    T::Error: std::fmt::Debug,
{
    T::try_from(raw.to_owned()).expect("test identifier is valid")
}

fn number<T: TryFrom<u16>>(raw: u16) -> T
where
    T::Error: std::fmt::Debug,
{
    T::try_from(raw).expect("test number is valid")
}

fn context(session: &str) -> NegotiationContext {
    NegotiationContext {
        server_instance_id: value("bridge-instance-1"),
        protocol_session_id: value(session),
        negotiation_token: value("negotiation-1"),
        bridge_version: value("0.0.0-dev.1"),
        event_buffer_capacity: number(256),
    }
}

fn resource(device: &str) -> ResourceKey {
    ResourceKey {
        receiver_id: value("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: value(device),
        kind: ResourceKind::Lighting,
    }
}

#[test]
fn feature_negotiation_selects_intersection_and_rejects_incompatible_versions() {
    let hello = ClientHello {
        client_id: value("client-1"),
        client_name: value("OpenRGB"),
        minimum_version: number(1),
        maximum_version: number(1),
        required_features: vec![value("ownership-leases")],
        optional_features: vec![value("future-feature")],
    };
    let response = negotiate(&hello, context("protocol-session-1")).expect("version overlaps");
    assert_eq!(response.selected_version.get(), 1);
    assert_eq!(response.enabled_features, vec![value("ownership-leases")]);

    let incompatible = ClientHello {
        minimum_version: number(2),
        maximum_version: number(2),
        ..hello
    };
    assert_eq!(
        negotiate(&incompatible, context("protocol-session-2")),
        Err(NegotiationError::IncompatibleVersion)
    );
}

#[test]
fn newer_bridge_serves_v1_client_and_required_features_fail_closed() {
    let hello = ClientHello {
        client_id: value("client-1"),
        client_name: value("OpenRGB"),
        minimum_version: number(1),
        maximum_version: number(1),
        required_features: vec![value("ownership-leases")],
        optional_features: Vec::new(),
    };
    let v2_bridge = ProtocolContract {
        minimum_version: 1,
        maximum_version: 2,
        features: &["ownership-leases", "future-feature"],
    };
    let selected = negotiate_with_contract(&hello, context("protocol-session-1"), v2_bridge)
        .expect("v2 bridge retains the frozen v1 service shape");
    assert_eq!(selected.selected_version.get(), 1);

    let unsupported = ClientHello {
        required_features: vec![value("unknown-required-feature")],
        ..hello
    };
    assert!(matches!(
        negotiate_with_contract(&unsupported, context("protocol-session-2"), v2_bridge),
        Err(NegotiationError::UnsupportedRequiredFeatures(features)) if features.len() == 1
    ));
}

#[test]
fn lease_resources_are_generation_scoped_canonical_and_duplicate_free() {
    let request = LeaseRequest {
        request_id: value("request-1"),
        client_id: value("client-1"),
        resources: vec![resource("keyboard-1"), resource("mouse-1")],
        duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is valid"),
    };
    validate_lease_request(&request).expect("canonical independent resources are valid");

    let duplicated = LeaseRequest {
        resources: vec![resource("mouse-1"), resource("mouse-1")],
        ..request.clone()
    };
    assert!(validate_lease_request(&duplicated).is_err());

    let reversed = LeaseRequest {
        resources: vec![resource("mouse-1"), resource("keyboard-1")],
        ..request
    };
    assert!(validate_lease_request(&reversed).is_err());
}

#[test]
fn transaction_requires_complete_lighting_ownership_set() {
    let transaction = TransactionRequest {
        request_id: value("request-2"),
        transaction_id: value("transaction-1"),
        client_id: value("client-1"),
        lease_id: value("lease-1"),
        receiver_id: value("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        transaction_class: TransactionClass::EffectFrame,
        deadline_ms: MonotonicMs::try_from(100_u64).expect("deadline is valid"),
        resources: vec![resource("mouse-1")],
        frames: vec![LightingFrame {
            device_id: value("mouse-1"),
            frame_index: FrameIndex::try_from(0_u32).expect("frame index is valid"),
            colors: vec![RgbColor {
                red: ColorChannel::try_from(1_u8).expect("color is valid"),
                green: ColorChannel::try_from(2_u8).expect("color is valid"),
                blue: ColorChannel::try_from(3_u8).expect("color is valid"),
            }],
        }],
    };
    validate_transaction(&transaction).expect("transaction structure is complete");

    let missing = TransactionRequest {
        resources: vec![resource("keyboard-1")],
        ..transaction
    };
    assert!(validate_transaction(&missing).is_err());
}

#[test]
fn generated_protocol_rejects_unknown_wire_fields() {
    let text = r#"{
        "client_id":"client-1",
        "client_name":"OpenRGB",
        "minimum_version":1,
        "maximum_version":1,
        "required_features":[],
        "optional_features":[],
        "raw_hid_payload":"forbidden"
    }"#;
    assert!(serde_json::from_str::<ClientHello>(text).is_err());
}

#[test]
fn tagged_lease_result_cannot_encode_a_grant_and_conflict_together() {
    let result = LeaseResult::Conflict(LeaseConflict {
        conflicting_client: value("client-2"),
        conflicting_resource: resource("mouse-1"),
    });
    let json = serde_json::to_value(result).expect("tagged result serializes");
    assert_eq!(json["outcome"], "conflict");
    assert!(json["detail"].get("lease_id").is_none());
}

#[test]
fn frozen_v1_envelopes_match_golden_wire_fixtures() {
    const NEGOTIATE: &str =
        include_str!("../../../tests/fixtures/protocol/v1/negotiate-request.json");
    const CONFLICT: &str =
        include_str!("../../../tests/fixtures/protocol/v1/lease-conflict-response.json");

    let request = RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: value("request-1"),
        params: ClientHello {
            client_id: value("client-1"),
            client_name: value("OpenRGB"),
            minimum_version: number(1),
            maximum_version: number(1),
            required_features: vec![value("ownership-leases")],
            optional_features: vec![value("atomic-transactions")],
        },
    });
    assert_eq!(
        format!(
            "{}\n",
            serde_json::to_string(&request).expect("request serializes")
        ),
        NEGOTIATE
    );
    let _: hfx_protocol::v1::RpcRequest =
        serde_json::from_str(NEGOTIATE).expect("frozen v1 request decodes");

    let response = RpcResponse::AcquireLeaseSuccess(SuccessEnvelope {
        request_id: value("request-2"),
        server_instance_id: value("bridge-instance-1"),
        result: LeaseResult::Conflict(LeaseConflict {
            conflicting_client: value("client-2"),
            conflicting_resource: resource("mouse-1"),
        }),
    });
    assert_eq!(
        format!(
            "{}\n",
            serde_json::to_string(&response).expect("response serializes")
        ),
        CONFLICT
    );
    let frozen: hfx_protocol::v1::RpcResponse =
        serde_json::from_str(CONFLICT).expect("frozen v1 response decodes");
    assert_eq!(
        format!(
            "{}\n",
            serde_json::to_string(&frozen).expect("frozen response reserializes")
        ),
        CONFLICT
    );
}
