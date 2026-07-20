// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    BatteryPercent, ConnectionMode, DeviceKind, DurationMs, GenerationId, ReceiverId,
};
use std::str::FromStr;

#[test]
fn bounded_numeric_types_reject_invalid_values() {
    assert!(BatteryPercent::try_from(100_u8).is_ok());
    assert!(BatteryPercent::try_from(101_u8).is_err());
    assert!(GenerationId::try_from(0_u64).is_err());
    assert!(DurationMs::try_from(86_400_001_u64).is_err());
}

#[test]
fn opaque_identifiers_reject_empty_and_oversized_values() {
    assert!(ReceiverId::try_from("receiver-1").is_ok());
    assert!(ReceiverId::try_from("").is_err());
    assert!(ReceiverId::try_from("x".repeat(129)).is_err());
}

#[test]
fn enums_round_trip_wire_values() {
    let kind = DeviceKind::from_str("keyboard").expect("keyboard is canonical");
    assert_eq!(kind, DeviceKind::Keyboard);
    assert_eq!(kind.as_str(), "keyboard");
    assert!(DeviceKind::from_str("key-board").is_err());
}

#[test]
fn unusual_enum_wire_values_round_trip_through_serde() {
    let encoded = serde_json::to_string(&ConnectionMode::Hyperflux24ghz)
        .expect("canonical connection mode serializes");
    assert_eq!(encoded, "\"hyperflux-2.4ghz\"");
    let decoded: ConnectionMode = serde_json::from_str(&encoded).expect("wire value deserializes");
    assert_eq!(decoded, ConnectionMode::Hyperflux24ghz);
}
