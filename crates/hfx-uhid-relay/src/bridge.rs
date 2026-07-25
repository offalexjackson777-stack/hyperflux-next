// SPDX-License-Identifier: GPL-2.0-only

use crate::uhid::FEATURE_REPORT_BYTES;
use hfx_domain::{
    ClientId, ClientName, LeaseDurationMs, LeaseId, NativeExchangeTimeoutMs, NativeReportHex,
    NativeRouteAccess, ProtocolFeatureId, ProtocolVersion, RouteState,
};
use hfx_protocol::{
    CURRENT_PROTOCOL_VERSION, LeaseResult, NativeExchangeResult, NativeRoute,
    NativeRouteEventBatch, NativeRouteInventory,
};
use hfx_sdk::{EventSubscription, HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
use std::fmt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};

const NATIVE_FEATURE: &str = "native-receiver-tunnel";
const LEASE_DURATION_MS: u32 = 30_000;
const LEASE_RENEW_INTERVAL: Duration = Duration::from_secs(10);
const SOCKET_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeExchangeResult {
    MatchedResponse([u8; FEATURE_REPORT_BYTES]),
    NotSent,
    MayHaveBeenSent,
}

pub trait BridgeExchange {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeBridgeError {
    InvalidRoute,
    InvalidIdentity,
    BridgeUnavailable,
    LeaseConflict,
    LeaseRejected,
}

impl fmt::Display for NativeBridgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidRoute => "native route is not qualified and writable",
            Self::InvalidIdentity => "native relay client identity is invalid",
            Self::BridgeUnavailable => "HyperFlux bridge is unavailable",
            Self::LeaseConflict => "native route is owned by another application",
            Self::LeaseRejected => "native route lease was rejected",
        })
    }
}

impl std::error::Error for NativeBridgeError {}

pub struct NativeBridge {
    client: HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
    route: NativeRoute,
    lease_id: LeaseId,
    last_lease_renewal: Instant,
}

pub struct NativeRouteMonitor {
    client: HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
}

impl NativeRouteMonitor {
    /// Connects one read-only native-route monitor without acquiring a lease.
    ///
    /// # Errors
    ///
    /// Returns a typed bridge or identity failure.
    pub fn connect(socket_path: &Path) -> Result<Self, NativeBridgeError> {
        let stream = configured_stream(socket_path)?;
        let config = client_config(
            ClientId::try_from("hfx-openrazer-monitor")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
            ClientName::try_from("HyperFlux OpenRazer route monitor")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
        )?;
        let client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
        Ok(Self { client })
    }

    /// Enumerates the complete current native-route set.
    ///
    /// # Errors
    ///
    /// Returns a bridge failure when the connection cannot answer.
    pub fn enumerate(&mut self) -> Result<NativeRouteInventory, NativeBridgeError> {
        self.client
            .native_enumerate()
            .map_err(|_| NativeBridgeError::BridgeUnavailable)
    }

    /// Reads one bounded native-route event batch.
    ///
    /// # Errors
    ///
    /// Returns a bridge failure when the connection cannot answer.
    pub fn watch(
        &mut self,
        subscription: EventSubscription,
    ) -> Result<NativeRouteEventBatch, NativeBridgeError> {
        self.client
            .native_watch(subscription)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)
    }
}

impl NativeBridge {
    /// Connects to protocol v7 and acquires the exact route's native-control
    /// resource before any UHID endpoint becomes visible.
    ///
    /// # Errors
    ///
    /// Fails closed for candidate, unavailable, un-tokenized, malformed, or
    /// already-owned routes and for bridge negotiation failures.
    pub fn connect(
        socket_path: &Path,
        route: NativeRoute,
        instance: u32,
    ) -> Result<Self, NativeBridgeError> {
        validate_route(&route)?;
        let stream = configured_stream(socket_path)?;
        let config = client_config(
            ClientId::try_from(format!("hfx-openrazer-{instance:08x}"))
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
            ClientName::try_from("HyperFlux OpenRazer native relay")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
        )?;
        let mut client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
        let resource = route
            .resource
            .clone()
            .ok_or(NativeBridgeError::InvalidRoute)?;
        let duration = LeaseDurationMs::try_from(LEASE_DURATION_MS)
            .map_err(|_| NativeBridgeError::InvalidIdentity)?;
        let lease = client
            .acquire_lease(vec![resource], duration)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
        let lease_id = match lease {
            LeaseResult::Granted(grant) => grant.lease_id,
            LeaseResult::Conflict(_) => return Err(NativeBridgeError::LeaseConflict),
            LeaseResult::Rejected(_) => return Err(NativeBridgeError::LeaseRejected),
        };
        Ok(Self {
            client,
            route,
            lease_id,
            last_lease_renewal: Instant::now(),
        })
    }

    fn renew_if_due(&mut self) -> Result<(), NativeBridgeError> {
        if self.last_lease_renewal.elapsed() < LEASE_RENEW_INTERVAL {
            return Ok(());
        }
        let duration = LeaseDurationMs::try_from(LEASE_DURATION_MS)
            .map_err(|_| NativeBridgeError::InvalidIdentity)?;
        let result = self
            .client
            .renew_lease(self.lease_id.clone(), duration)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
        match result {
            LeaseResult::Granted(_) => {
                self.last_lease_renewal = Instant::now();
                Ok(())
            }
            LeaseResult::Conflict(_) => Err(NativeBridgeError::LeaseConflict),
            LeaseResult::Rejected(_) => Err(NativeBridgeError::LeaseRejected),
        }
    }
}

impl BridgeExchange for NativeBridge {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
        if self.renew_if_due().is_err() {
            return BridgeExchangeResult::NotSent;
        }
        let Some(route_token) = self.route.route_token.clone() else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(report) = NativeReportHex::try_from(encode_hex(&report)) else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(timeout) = NativeExchangeTimeoutMs::try_from(1_000_u16) else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(submission) = self.client.prepare_native_exchange(
            self.lease_id.clone(),
            route_token,
            report,
            timeout,
        ) else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(outcome) = self.client.native_transact(&submission) else {
            return BridgeExchangeResult::MayHaveBeenSent;
        };
        match outcome.result {
            NativeExchangeResult::NotSent(_) => BridgeExchangeResult::NotSent,
            NativeExchangeResult::MayHaveBeenSent(_) => BridgeExchangeResult::MayHaveBeenSent,
            NativeExchangeResult::MatchedResponse(matched) => {
                decode_report(matched.response.as_str()).map_or(
                    BridgeExchangeResult::MayHaveBeenSent,
                    BridgeExchangeResult::MatchedResponse,
                )
            }
        }
    }
}

impl Drop for NativeBridge {
    fn drop(&mut self) {
        let _ = self.client.release_lease(self.lease_id.clone());
    }
}

fn configured_stream(socket_path: &Path) -> Result<UnixStream, NativeBridgeError> {
    let stream =
        UnixStream::connect(socket_path).map_err(|_| NativeBridgeError::BridgeUnavailable)?;
    stream
        .set_read_timeout(Some(SOCKET_TIMEOUT))
        .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
    stream
        .set_write_timeout(Some(SOCKET_TIMEOUT))
        .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
    Ok(stream)
}

fn client_config(
    client_id: ClientId,
    client_name: ClientName,
) -> Result<SdkClientConfig, NativeBridgeError> {
    let version = ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION)
        .map_err(|_| NativeBridgeError::InvalidIdentity)?;
    Ok(SdkClientConfig {
        client_id,
        client_name,
        minimum_version: version,
        maximum_version: version,
        required_features: vec![
            ProtocolFeatureId::try_from(NATIVE_FEATURE)
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
        ],
        optional_features: Vec::new(),
    })
}

fn validate_route(route: &NativeRoute) -> Result<(), NativeBridgeError> {
    if route.access != NativeRouteAccess::Qualified
        || route.route_state != RouteState::Available
        || usize::from(route.report_bytes.get()) != FEATURE_REPORT_BYTES
        || route.protocol_family.as_str() != "razer-feature-report-v1"
        || route.route_token.is_none()
        || route.resource.is_none()
    {
        Err(NativeBridgeError::InvalidRoute)
    } else {
        Ok(())
    }
}

fn encode_hex(report: &[u8; FEATURE_REPORT_BYTES]) -> String {
    let mut output = String::with_capacity(FEATURE_REPORT_BYTES * 2);
    for byte in report {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

fn decode_report(value: &str) -> Option<[u8; FEATURE_REPORT_BYTES]> {
    if value.len() != FEATURE_REPORT_BYTES * 2 {
        return None;
    }
    let mut report = [0_u8; FEATURE_REPORT_BYTES];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        report[index] = (hex_nibble(pair[0])? << 4) | hex_nibble(pair[1])?;
    }
    Some(report)
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
}
