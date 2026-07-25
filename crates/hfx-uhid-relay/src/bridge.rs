// SPDX-License-Identifier: GPL-2.0-only

use crate::uhid::FEATURE_REPORT_BYTES;
use crate::{SerialFingerprint, SerialQuerySpec};
use hfx_domain::{
    ClientId, ClientName, LeaseDurationMs, LeaseId, NativeExchangeTimeoutMs, NativeReportHex,
    NativeRouteAccess, NativeRouteState, ProtocolFeatureId, ProtocolVersion,
};
use hfx_protocol::{
    CURRENT_PROTOCOL_VERSION, LeaseResult, NativeExchangeResult, NativeRoute,
    NativeRouteEventBatch, NativeRouteInventory, ResourceKey,
};
use hfx_sdk::{EventSubscription, HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
use std::fmt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

const NATIVE_FEATURE: &str = "native-receiver-tunnel";
const LEASE_DURATION_MS: u32 = 30_000;
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
}

impl fmt::Display for NativeBridgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidRoute => "native route is not qualified and writable",
            Self::InvalidIdentity => "native relay client identity is invalid",
            Self::BridgeUnavailable => "HyperFlux bridge is unavailable",
        })
    }
}

impl std::error::Error for NativeBridgeError {}

pub struct NativeBridge {
    client: HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
    route: NativeRoute,
    serial_query: SerialQuerySpec,
    observed_serial: Option<SerialFingerprint>,
}

trait EphemeralExchange {
    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, ()>;
    fn exchange_leased(
        &mut self,
        lease_id: LeaseId,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult;
    fn release(&mut self, lease_id: LeaseId);
}

fn exchange_ephemeral<C: EphemeralExchange>(
    channel: &mut C,
    resource: ResourceKey,
    report: [u8; FEATURE_REPORT_BYTES],
) -> BridgeExchangeResult {
    let Ok(lease_id) = channel.acquire(resource) else {
        return BridgeExchangeResult::NotSent;
    };
    let result = channel.exchange_leased(lease_id.clone(), report);
    channel.release(lease_id);
    result
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
    /// Connects to protocol v7 and validates the exact native route without
    /// reserving it for the lifetime of the always-on UHID endpoint.
    ///
    /// # Errors
    ///
    /// Fails closed for candidate, unavailable, un-tokenized, malformed routes
    /// and for bridge negotiation failures. Each Feature exchange obtains and
    /// releases its own lease so an idle `OpenRazer` endpoint cannot lock out a
    /// native `OpenRGB` session.
    pub fn connect(
        socket_path: &Path,
        route: NativeRoute,
        serial_query: SerialQuerySpec,
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
        let client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(|_| NativeBridgeError::BridgeUnavailable)?;
        Ok(Self {
            client,
            route,
            serial_query,
            observed_serial: None,
        })
    }

    pub(crate) fn take_observed_serial(&mut self) -> Option<SerialFingerprint> {
        self.observed_serial.take()
    }

    fn transact_with_lease(
        &mut self,
        lease_id: LeaseId,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult {
        let Some(route_token) = self.route.route_token.clone() else {
            return BridgeExchangeResult::NotSent;
        };
        let wire_request = report;
        let Ok(report) = NativeReportHex::try_from(encode_hex(&wire_request)) else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(timeout) = NativeExchangeTimeoutMs::try_from(1_000_u16) else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(submission) =
            self.client
                .prepare_native_exchange(lease_id, route_token, report, timeout)
        else {
            return BridgeExchangeResult::NotSent;
        };
        let Ok(outcome) = self.client.native_transact(&submission) else {
            return BridgeExchangeResult::MayHaveBeenSent;
        };
        match outcome.result {
            NativeExchangeResult::NotSent(_) => BridgeExchangeResult::NotSent,
            NativeExchangeResult::MayHaveBeenSent(_) => BridgeExchangeResult::MayHaveBeenSent,
            NativeExchangeResult::MatchedResponse(matched) => {
                let Some(response) = decode_report(matched.response.as_str()) else {
                    return BridgeExchangeResult::MayHaveBeenSent;
                };
                if let Some(fingerprint) = self
                    .serial_query
                    .fingerprint_response(&wire_request, &response)
                {
                    self.observed_serial = Some(fingerprint);
                }
                BridgeExchangeResult::MatchedResponse(response)
            }
        }
    }
}

impl EphemeralExchange for NativeBridge {
    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, ()> {
        let duration = LeaseDurationMs::try_from(LEASE_DURATION_MS).map_err(|_| ())?;
        match self.client.acquire_lease(vec![resource], duration) {
            Ok(LeaseResult::Granted(grant)) => Ok(grant.lease_id),
            Ok(LeaseResult::Conflict(_) | LeaseResult::Rejected(_)) | Err(_) => Err(()),
        }
    }

    fn exchange_leased(
        &mut self,
        lease_id: LeaseId,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult {
        self.transact_with_lease(lease_id, report)
    }

    fn release(&mut self, lease_id: LeaseId) {
        let _ = self.client.release_lease(lease_id);
    }
}

impl BridgeExchange for NativeBridge {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
        let Some(resource) = self.route.resource.clone() else {
            return BridgeExchangeResult::NotSent;
        };
        exchange_ephemeral(self, resource, report)
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
        || route.route_state != NativeRouteState::Available
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

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{GenerationId, LogicalDeviceId, ReceiverId, ResourceKind};

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Call {
        Acquire,
        Exchange,
        Release,
    }

    struct FakeExchange {
        acquire: Option<Result<LeaseId, ()>>,
        result: BridgeExchangeResult,
        calls: Vec<Call>,
    }

    impl EphemeralExchange for FakeExchange {
        fn acquire(&mut self, _resource: ResourceKey) -> Result<LeaseId, ()> {
            self.calls.push(Call::Acquire);
            self.acquire.take().expect("one acquisition is configured")
        }

        fn exchange_leased(
            &mut self,
            _lease_id: LeaseId,
            _report: [u8; FEATURE_REPORT_BYTES],
        ) -> BridgeExchangeResult {
            self.calls.push(Call::Exchange);
            self.result
        }

        fn release(&mut self, _lease_id: LeaseId) {
            self.calls.push(Call::Release);
        }
    }

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is valid")
    }

    fn resource() -> ResourceKey {
        ResourceKey {
            receiver_id: text::<ReceiverId>("receiver-1"),
            generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
            device_id: text::<LogicalDeviceId>("mouse-1"),
            kind: ResourceKind::NativeControl,
        }
    }

    fn fake(result: BridgeExchangeResult) -> FakeExchange {
        FakeExchange {
            acquire: Some(Ok(text::<LeaseId>("lease-1"))),
            result,
            calls: Vec::new(),
        }
    }

    #[test]
    fn one_atomic_exchange_holds_then_releases_one_ephemeral_lease() {
        let expected = BridgeExchangeResult::MatchedResponse([0xa5; FEATURE_REPORT_BYTES]);
        let mut exchange = fake(expected);
        assert_eq!(
            exchange_ephemeral(&mut exchange, resource(), [0x42; FEATURE_REPORT_BYTES]),
            expected
        );
        assert_eq!(
            exchange.calls,
            vec![Call::Acquire, Call::Exchange, Call::Release]
        );
    }

    #[test]
    fn an_owned_route_is_not_sent_and_never_released_by_the_contender() {
        let mut exchange = FakeExchange {
            acquire: Some(Err(())),
            result: BridgeExchangeResult::MatchedResponse([0; FEATURE_REPORT_BYTES]),
            calls: Vec::new(),
        };
        assert_eq!(
            exchange_ephemeral(&mut exchange, resource(), [0x42; FEATURE_REPORT_BYTES]),
            BridgeExchangeResult::NotSent
        );
        assert_eq!(exchange.calls, vec![Call::Acquire]);
    }

    #[test]
    fn uncertain_and_not_sent_exchanges_still_release_their_lease() {
        for result in [
            BridgeExchangeResult::NotSent,
            BridgeExchangeResult::MayHaveBeenSent,
        ] {
            let mut exchange = fake(result);
            assert_eq!(
                exchange_ephemeral(&mut exchange, resource(), [0x42; FEATURE_REPORT_BYTES]),
                result
            );
            assert_eq!(
                exchange.calls,
                vec![Call::Acquire, Call::Exchange, Call::Release]
            );
        }
    }
}
