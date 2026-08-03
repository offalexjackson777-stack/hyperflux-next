// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use hfx_domain::{
    DeviceKind, EndpointId, GenerationId, LogicalDeviceId, NativeProtocolFamily,
    NativeReportByteCount, NativeRouteAccess, NativeRouteState, ProductId, ProjectionRevision,
    ReceiverId, SequenceNumber, StreamEpoch, StreamId, VendorId,
};
use hfx_protocol::{EventCursor, NativeRoute};
use std::time::{SystemTime, UNIX_EPOCH};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test value is valid")
}

fn inventory() -> NativeRouteInventory {
    NativeRouteInventory {
        cursor: EventCursor {
            stream_id: text::<StreamId>("native-routes"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("projection is valid"),
            sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
        },
        routes: vec![NativeRoute {
            receiver_id: text::<ReceiverId>("receiver-private-id"),
            generation_id: GenerationId::try_from(7_u64).expect("generation is valid"),
            device_id: text::<LogicalDeviceId>("device-private-id"),
            endpoint_id: text::<EndpointId>("endpoint-private-id"),
            device_kind: DeviceKind::Mouse,
            vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is valid"),
            product_id: ProductId::try_from(0x007b_u16).expect("product is valid"),
            route_state: NativeRouteState::Available,
            access: NativeRouteAccess::Candidate,
            protocol_family: NativeProtocolFamily::try_from("razer-feature-report-v1".to_owned())
                .expect("protocol is valid"),
            report_bytes: NativeReportByteCount::try_from(90_u16).expect("report size is valid"),
            route_token: None,
            resource: None,
        }],
    }
}

#[test]
fn observation_strips_every_private_route_field() {
    let observations = build_native_route_observations(&inventory(), "2026-07-25T00:00:00Z")
        .expect("observation builds");
    let serialized = serde_json::to_string(&observations).expect("observation serializes");
    assert!(!serialized.contains("receiver-private-id"));
    assert!(!serialized.contains("device-private-id"));
    assert!(!serialized.contains("endpoint-private-id"));
    assert!(!serialized.contains("\"route_token\":"));
    assert_eq!(observations[0].product_id, 0x007b);
    assert!(serialized.contains("\"raw_reports_included\":false"));
}

#[test]
fn writer_uses_private_mode_and_refuses_overwrite() {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock is valid")
        .as_nanos();
    let directory = env::temp_dir().join(format!(
        "hyperflux-native-observation-{}-{unique}",
        std::process::id()
    ));
    let observations = build_native_route_observations(&inventory(), "2026-07-25T00:00:00Z")
        .expect("observation builds");
    let paths =
        write_native_route_observations(&directory, &observations).expect("observation writes");
    assert_eq!(
        fs::metadata(&paths[0])
            .expect("metadata exists")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        write_native_route_observations(&directory, &observations),
        Err(NativeObservationError::OutputExists)
    );
    fs::remove_dir_all(directory).expect("fixture cleans up");
}
