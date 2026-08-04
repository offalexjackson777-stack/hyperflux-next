// SPDX-License-Identifier: GPL-2.0-only

use crate::RuntimeProfileAuthority;
use hfx_core::{EndpointLifecycle, ReceiverLifecycleRegistry};
use hfx_domain::{
    NativeProtocolFamily, NativeReportByteCount, NativeRouteAccess, NativeRouteState, RouteKind,
    RouteState,
};
use hfx_integration_model::SourceValidatedDeviceCatalog;
use hfx_protocol::NativeRoute;
use std::fmt;

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
/// Exact known children and unknown candidates share this path. An exact
/// source-validated VID/PID/kind intersection changes access from `candidate`
/// to `qualified`. An exact record requiring another transport backend remains
/// `backend-required`; hardware assurance is deliberately not an access gate.
///
/// # Errors
///
/// Returns a typed generated-profile error if the bound receiver's native
/// protocol family, report size, or USB vendor identity is invalid.
pub fn project_native_routes(
    receivers: &ReceiverLifecycleRegistry,
    profiles: &RuntimeProfileAuthority,
    source_validated: &SourceValidatedDeviceCatalog,
) -> Result<Vec<NativeRoute>, NativeRouteProjectionError> {
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
            let access = match source_validated.device(
                vendor_id,
                device.identity().product_id(),
                device.identity().device_kind(),
            ) {
                Some(record) if record.operational() => NativeRouteAccess::Qualified,
                Some(_) => NativeRouteAccess::BackendRequired,
                None => NativeRouteAccess::Candidate,
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

fn native_route_state(endpoint: &EndpointLifecycle) -> NativeRouteState {
    match endpoint.route().value() {
        RouteState::Available => NativeRouteState::Available,
        RouteState::Unavailable | RouteState::Stale | RouteState::Unknown => {
            NativeRouteState::TemporarilyUnavailable
        }
    }
}
