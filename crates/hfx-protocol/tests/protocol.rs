// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    CapabilityId, ColorChannel, FrameIndex, GenerationId, LeaseDurationMs, MonotonicMs,
    ResourceKind, TransactionClass,
};
use hfx_protocol::{
    ClientHello, LeaseRequest, LightingFrame, NegotiationError, ResourceKey, RgbColor,
    TransactionRequest, negotiate, validate_lease_request, validate_transaction,
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

fn resource(device: &str) -> ResourceKey {
    ResourceKey {
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
        requested_features: vec![value("ownership-leases"), value("future-feature")],
    };
    let response = negotiate(&hello, value("0.0.0-dev.1"), number(256)).expect("version overlaps");
    assert_eq!(response.selected_version.get(), 1);
    assert_eq!(
        response.enabled_features,
        vec![value::<CapabilityId>("ownership-leases")]
    );

    let incompatible = ClientHello {
        minimum_version: number(2),
        maximum_version: number(2),
        ..hello
    };
    assert_eq!(
        negotiate(&incompatible, value("0.0.0-dev.1"), number(256)),
        Err(NegotiationError::IncompatibleVersion)
    );
}

#[test]
fn lease_resources_are_generic_atomic_and_duplicate_free() {
    let request = LeaseRequest {
        request_id: value("request-1"),
        client_id: value("client-1"),
        resources: vec![resource("mouse-1"), resource("keyboard-1")],
        duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is valid"),
    };
    validate_lease_request(&request).expect("independent device resources are valid");

    let duplicated = LeaseRequest {
        resources: vec![resource("mouse-1"), resource("mouse-1")],
        ..request
    };
    assert!(validate_lease_request(&duplicated).is_err());
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
        "requested_features":[],
        "raw_hid_payload":"forbidden"
    }"#;
    assert!(serde_json::from_str::<ClientHello>(text).is_err());
}
