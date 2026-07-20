// SPDX-License-Identifier: GPL-2.0-only

#![allow(dead_code)]

use hfx_domain::{
    ColorChannel, FrameIndex, GenerationId, LeaseDurationMs, LedCount, MonotonicMs, ProfileDigest,
    ProfileId, ResourceKind, TransactionClass,
};
use hfx_protocol::{
    DeviceProfileBinding, LeaseRequest, LightingFrame, ResourceKey, RgbColor, TransactionRequest,
};

pub fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identifier is valid")
}

pub fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is valid")
}

pub fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("test time is valid")
}

pub fn resource(receiver: &str, generation_id: u64, device: &str) -> ResourceKey {
    ResourceKey {
        receiver_id: text(receiver),
        generation_id: generation(generation_id),
        device_id: text(device),
        kind: ResourceKind::Lighting,
    }
}

pub fn lease_request(request: &str, client: &str, resources: Vec<ResourceKey>) -> LeaseRequest {
    LeaseRequest {
        request_id: text(request),
        client_id: text(client),
        resources,
        duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("test duration is valid"),
    }
}

pub fn receiver_profile_id() -> ProfileId {
    text("profile.receiver")
}

pub fn receiver_profile_digest() -> ProfileDigest {
    text(&"a".repeat(64))
}

pub fn device_profile_id(device: &str) -> ProfileId {
    text(&format!("profile.{device}"))
}

pub fn device_profile_digest() -> ProfileDigest {
    text(&"b".repeat(64))
}

pub fn device_profile_binding(device: &str, slots: u16) -> DeviceProfileBinding {
    DeviceProfileBinding {
        device_id: text(device),
        profile_id: device_profile_id(device),
        profile_digest: device_profile_digest(),
        application_slot_count: LedCount::try_from(slots).expect("test LED count is valid"),
    }
}

pub fn transaction_request(
    id: &str,
    class: TransactionClass,
    deadline: u64,
    devices: &[&str],
) -> TransactionRequest {
    let resources = devices
        .iter()
        .map(|device| resource("receiver-1", 1, device))
        .collect::<Vec<_>>();
    let frames = devices
        .iter()
        .enumerate()
        .map(|(index, device)| LightingFrame {
            device_id: text(device),
            frame_index: FrameIndex::try_from(u32::try_from(index).expect("test index fits"))
                .expect("frame index is valid"),
            colors: vec![RgbColor {
                red: ColorChannel::try_from(1_u8).expect("color is valid"),
                green: ColorChannel::try_from(2_u8).expect("color is valid"),
                blue: ColorChannel::try_from(3_u8).expect("color is valid"),
            }],
        })
        .collect::<Vec<_>>();
    let mut device_profiles = devices
        .iter()
        .map(|device| device_profile_binding(device, 1))
        .collect::<Vec<_>>();
    device_profiles.sort_unstable_by(|left, right| left.device_id.cmp(&right.device_id));
    TransactionRequest {
        request_id: text(&format!("request-{id}")),
        transaction_id: text(&format!("transaction-{id}")),
        client_id: text("client-1"),
        lease_id: text("lease-1"),
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        receiver_profile_id: receiver_profile_id(),
        receiver_profile_digest: receiver_profile_digest(),
        device_profiles,
        transaction_class: class,
        deadline_ms: time(deadline),
        resources,
        frames,
    }
}
