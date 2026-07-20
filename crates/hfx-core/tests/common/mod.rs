// SPDX-License-Identifier: GPL-2.0-only

#![allow(dead_code)]

use hfx_domain::{GenerationId, LeaseDurationMs, MonotonicMs, ResourceKind};
use hfx_protocol::{LeaseRequest, ResourceKey};

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
