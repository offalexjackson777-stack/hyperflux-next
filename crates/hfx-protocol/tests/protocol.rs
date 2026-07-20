// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ColorChannel, FrameIndex, GenerationId, LeaseDurationMs, LedCount, MonotonicMs, ResourceKind,
    TransactionClass,
};
use hfx_protocol::{
    ClientHello, DeviceProfileBinding, LeaseConflict, LeaseRequest, LeaseResult, LightingFrame,
    NegotiationContext, NegotiationError, NegotiationRequestEnvelope, ProtocolContract,
    ProtocolValidationError, ResourceKey, RgbColor, RpcRequest, RpcResponse, SuccessEnvelope,
    TransactionRequest, negotiate, negotiate_with_contract, validate_lease_request,
    validate_transaction,
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

fn transaction() -> TransactionRequest {
    TransactionRequest {
        request_id: value("request-2"),
        transaction_id: value("transaction-1"),
        client_id: value("client-1"),
        lease_id: value("lease-1"),
        receiver_id: value("receiver-1"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        receiver_profile_id: value("profile.receiver"),
        receiver_profile_digest: value(&"a".repeat(64)),
        device_profiles: vec![DeviceProfileBinding {
            device_id: value("mouse-1"),
            profile_id: value("profile.mouse-1"),
            profile_digest: value(&"b".repeat(64)),
            application_slot_count: LedCount::try_from(1_u16).expect("LED count is valid"),
        }],
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

    let v2 = ClientHello {
        minimum_version: number(2),
        maximum_version: number(2),
        required_features: vec![value("profile-bound-transactions")],
        optional_features: Vec::new(),
        ..hello
    };
    let response = negotiate(&v2, context("protocol-session-2")).expect("v2 overlaps");
    assert_eq!(response.selected_version.get(), 2);

    let incompatible = ClientHello {
        minimum_version: number(3),
        maximum_version: number(3),
        ..v2
    };
    assert_eq!(
        negotiate(&incompatible, context("protocol-session-3")),
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
    let transaction = transaction();
    validate_transaction(&transaction).expect("transaction structure is complete");

    let missing = TransactionRequest {
        resources: vec![resource("keyboard-1")],
        ..transaction
    };
    assert!(validate_transaction(&missing).is_err());
}

#[test]
fn restore_uses_the_same_bounded_lighting_payload_contract() {
    let mut restore = transaction();
    restore.transaction_class = TransactionClass::Restore;
    validate_transaction(&restore).expect("restore lighting payload is structurally valid");

    restore.frames[0].colors.clear();
    assert_eq!(
        validate_transaction(&restore),
        Err(ProtocolValidationError::EmptyColors)
    );
}

#[test]
fn transaction_profile_bindings_are_exact_canonical_and_dimensioned() {
    let valid = transaction();
    validate_transaction(&valid).expect("exact profile binding is valid");

    let mut wrong_size = valid.clone();
    wrong_size.device_profiles[0].application_slot_count =
        LedCount::try_from(2_u16).expect("LED count is valid");
    assert_eq!(
        validate_transaction(&wrong_size),
        Err(ProtocolValidationError::FrameColorCountMismatch)
    );

    let mut extra_binding = valid.clone();
    extra_binding.device_profiles.push(DeviceProfileBinding {
        device_id: value("keyboard-1"),
        profile_id: value("profile.keyboard-1"),
        profile_digest: value(&"c".repeat(64)),
        application_slot_count: LedCount::try_from(1_u16).expect("LED count is valid"),
    });
    extra_binding
        .device_profiles
        .sort_unstable_by(|left, right| left.device_id.cmp(&right.device_id));
    assert_eq!(
        validate_transaction(&extra_binding),
        Err(ProtocolValidationError::ProfileBindingWithoutFrame)
    );

    let mut extra_resource = valid;
    extra_resource.resources.push(resource("other-1"));
    assert_eq!(
        validate_transaction(&extra_resource),
        Err(ProtocolValidationError::ResourceWithoutFrame)
    );
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
