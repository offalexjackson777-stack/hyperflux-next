// SPDX-License-Identifier: GPL-2.0-only

use crate::RuntimeProfileAuthority;
use hfx_core::{EndpointLifecycle, ReceiverLifecycleRegistry};
use hfx_domain::{
    ActivityState, NativeProtocolFamily, NativeReportByteCount, NativeRouteAccess, PowerState,
    RouteKind, RouteState, SleepState, SupportLevel,
};
use hfx_protocol::NativeRoute;
use std::fmt;

/// Child capability required before an observed route may issue a writable
/// native-control token. Catalog membership alone never grants this claim.
pub const NATIVE_EXCHANGE_CAPABILITY_ID: &str = "native.feature-report-exchange";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeRouteProjectionError {
    InvalidProtocolFamily,
    InvalidReportSize,
    MissingReceiverVendor,
}

impl fmt::Display for NativeRouteProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidProtocolFamily => "native protocol family is invalid",
            Self::InvalidReportSize => "native report size is invalid",
            Self::MissingReceiverVendor => "native receiver profile has no USB vendor identity",
        })
    }
}

impl std::error::Error for NativeRouteProjectionError {}

/// Projects all receiver-backed child endpoints without exposing host paths.
///
/// Exact known children and unknown candidates share this path. Only a
/// production-qualified writable child capability changes access from
/// `candidate` to `qualified`; the bridge engine contains no PID branches.
///
/// # Errors
///
/// Returns a typed generated-profile error if the bound receiver's native
/// protocol family, report size, or USB vendor identity is invalid.
pub fn project_native_routes(
    receivers: &ReceiverLifecycleRegistry,
    profiles: &RuntimeProfileAuthority,
) -> Result<Vec<NativeRoute>, NativeRouteProjectionError> {
    let view = profiles.view(receivers);
    let mut routes = Vec::new();
    for receiver in receivers.iter() {
        let receiver_id = receiver.receiver_id();
        let Some(generation) = receiver.current() else {
            continue;
        };
        let Some(binding) = profiles.binding(receiver_id) else {
            continue;
        };
        if binding.generation_id != generation.generation_id() {
            continue;
        }
        let Some(receiver_profile) = profiles.catalog().profile(&binding.profile_id) else {
            continue;
        };
        let Some(native) = receiver_profile.native_exchange else {
            continue;
        };
        let vendor_id = receiver_profile
            .vendor_id
            .ok_or(NativeRouteProjectionError::MissingReceiverVendor)?;
        let protocol_family = NativeProtocolFamily::try_from(native.protocol_family)
            .map_err(|_| NativeRouteProjectionError::InvalidProtocolFamily)?;
        let report_bytes = NativeReportByteCount::try_from(native.report_bytes)
            .map_err(|_| NativeRouteProjectionError::InvalidReportSize)?;

        for device in generation.devices() {
            let child_profile =
                view.profile_for_device(receiver_id, generation.generation_id(), device);
            let access = if child_profile.is_some_and(native_exchange_is_qualified) {
                NativeRouteAccess::Qualified
            } else {
                NativeRouteAccess::Candidate
            };
            for endpoint in device
                .endpoints()
                .filter(|endpoint| endpoint.identity().route_kind() == RouteKind::HyperfluxWireless)
            {
                routes.push(NativeRoute {
                    receiver_id: receiver_id.clone(),
                    generation_id: generation.generation_id(),
                    device_id: device.identity().device_id().clone(),
                    endpoint_id: endpoint.identity().endpoint_id().clone(),
                    device_kind: device.identity().device_kind(),
                    vendor_id,
                    product_id: device.identity().product_id(),
                    route_state: native_route_state(endpoint),
                    access,
                    protocol_family: protocol_family.clone(),
                    report_bytes,
                    route_token: None,
                    resource: None,
                });
            }
        }
    }
    routes.sort_by(|left, right| {
        (
            &left.receiver_id,
            left.generation_id,
            &left.device_id,
            &left.endpoint_id,
        )
            .cmp(&(
                &right.receiver_id,
                right.generation_id,
                &right.device_id,
                &right.endpoint_id,
            ))
    });
    Ok(routes)
}

fn native_exchange_is_qualified(profile: &hfx_profiles::RuntimeProfile) -> bool {
    profile.capabilities.iter().any(|capability| {
        capability.id.as_str() == NATIVE_EXCHANGE_CAPABILITY_ID
            && capability.writable
            && capability.support_level == SupportLevel::ProductionQualified
    })
}

fn native_route_state(endpoint: &EndpointLifecycle) -> RouteState {
    match endpoint.route().value() {
        RouteState::Available => RouteState::Available,
        RouteState::Unavailable => RouteState::Unavailable,
        RouteState::Stale => RouteState::Stale,
        RouteState::Unknown if endpoint.power().value() == PowerState::Off => {
            RouteState::Unavailable
        }
        RouteState::Unknown if endpoint.activity().value() == ActivityState::Active => {
            RouteState::Available
        }
        RouteState::Unknown if endpoint.sleep().value() == SleepState::Asleep => RouteState::Stale,
        RouteState::Unknown => RouteState::Unknown,
    }
}
