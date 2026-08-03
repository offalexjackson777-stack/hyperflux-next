// SPDX-License-Identifier: GPL-2.0-only

use crate::uhid::FEATURE_REPORT_BYTES;
use hfx_domain::{
    ClientId, ClientName, LeaseDurationMs, LeaseId, NativeExchangeTimeoutMs, NativeReportHex,
    NativeRouteAccess, NativeRouteState, ProtocolFeatureId, ProtocolVersion,
};
use hfx_protocol::{
    CURRENT_PROTOCOL_VERSION, LeaseResult, NativeExchangeResult, NativeRoute,
    NativeRouteEventBatch, NativeRouteInventory, ResourceKey,
};
use hfx_sdk::{
    EventSubscription, HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig, SdkError,
};
use std::fmt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

const NATIVE_FEATURE: &str = "native-receiver-tunnel";
const OWNERSHIP_FEATURE: &str = "ownership-leases";
const LEASE_DURATION_MS: u32 = 30_000;
const SOCKET_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeExchangeResult {
    MatchedResponse([u8; FEATURE_REPORT_BYTES]),
    NotSent(BridgeExchangeFailure),
    MayHaveBeenSent(BridgeExchangeFailure),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeExchangeFailure {
    MissingRouteAuthority,
    RequestEncoding,
    TimeoutEncoding,
    LeaseConflict,
    LeaseRejected,
    LeaseSdk,
    PrepareSdk,
    TransactSdk,
    TransportNotSent,
    TransportMayHaveBeenSent,
    ResponseEncoding,
}

pub trait BridgeExchange {
    fn exchange(&mut self, report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NativeBridgeError {
    InvalidRoute,
    InvalidIdentity,
    SocketUnavailable,
    SocketConfiguration,
    Sdk(SdkError),
}

impl fmt::Display for NativeBridgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidRoute => "native route is not qualified and writable",
            Self::InvalidIdentity => "native relay client identity is invalid",
            Self::SocketUnavailable => "HyperFlux bridge socket is unavailable",
            Self::SocketConfiguration => "HyperFlux bridge socket could not be configured",
            Self::Sdk(error) => return write!(formatter, "{error}"),
        })
    }
}

impl std::error::Error for NativeBridgeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Sdk(error) => Some(error),
            Self::InvalidRoute
            | Self::InvalidIdentity
            | Self::SocketUnavailable
            | Self::SocketConfiguration => None,
        }
    }
}

pub struct NativeBridge {
    client: HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
    route: NativeRoute,
}

trait EphemeralExchange {
    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure>;
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
    let lease_id = match channel.acquire(resource) {
        Ok(lease_id) => lease_id,
        Err(failure) => return BridgeExchangeResult::NotSent(failure),
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
        let config = monitor_client_config(
            ClientId::try_from("hfx-openrazer-monitor")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
            ClientName::try_from("HyperFlux OpenRazer route monitor")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
        )?;
        let client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(NativeBridgeError::Sdk)?;
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
            .map_err(NativeBridgeError::Sdk)
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
            .map_err(NativeBridgeError::Sdk)
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
        instance: u32,
    ) -> Result<Self, NativeBridgeError> {
        validate_route(&route)?;
        let stream = configured_stream(socket_path)?;
        let config = endpoint_client_config(
            ClientId::try_from(format!("hfx-openrazer-{instance:08x}"))
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
            ClientName::try_from("HyperFlux OpenRazer native relay")
                .map_err(|_| NativeBridgeError::InvalidIdentity)?,
        )?;
        let client = HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .map_err(NativeBridgeError::Sdk)?;
        Ok(Self { client, route })
    }

    fn transact_with_lease(
        &mut self,
        lease_id: LeaseId,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult {
        let Some(route_token) = self.route.route_token.clone() else {
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::MissingRouteAuthority);
        };
        let wire_request = report;
        let Ok(report) = NativeReportHex::try_from(encode_hex(&wire_request)) else {
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::RequestEncoding);
        };
        let Ok(timeout) = NativeExchangeTimeoutMs::try_from(1_000_u16) else {
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::TimeoutEncoding);
        };
        let Ok(submission) =
            self.client
                .prepare_native_exchange(lease_id, route_token, report, timeout)
        else {
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::PrepareSdk);
        };
        let Ok(outcome) = self.client.native_transact(&submission) else {
            return BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::TransactSdk);
        };
        match outcome.result {
            NativeExchangeResult::NotSent(_) => {
                BridgeExchangeResult::NotSent(BridgeExchangeFailure::TransportNotSent)
            }
            NativeExchangeResult::MayHaveBeenSent(_) => BridgeExchangeResult::MayHaveBeenSent(
                BridgeExchangeFailure::TransportMayHaveBeenSent,
            ),
            NativeExchangeResult::MatchedResponse(matched) => {
                let Some(response) = decode_report(matched.response.as_str()) else {
                    return BridgeExchangeResult::MayHaveBeenSent(
                        BridgeExchangeFailure::ResponseEncoding,
                    );
                };
                BridgeExchangeResult::MatchedResponse(response)
            }
        }
    }
}

impl EphemeralExchange for NativeBridge {
    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure> {
        let duration = LeaseDurationMs::try_from(LEASE_DURATION_MS)
            .map_err(|_| BridgeExchangeFailure::LeaseSdk)?;
        match self.client.acquire_lease(vec![resource], duration) {
            Ok(LeaseResult::Granted(grant)) => Ok(grant.lease_id),
            Ok(LeaseResult::Conflict(_)) => Err(BridgeExchangeFailure::LeaseConflict),
            Ok(LeaseResult::Rejected(_)) => Err(BridgeExchangeFailure::LeaseRejected),
            Err(_) => Err(BridgeExchangeFailure::LeaseSdk),
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
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::MissingRouteAuthority);
        };
        exchange_ephemeral(self, resource, report)
    }
}

fn configured_stream(socket_path: &Path) -> Result<UnixStream, NativeBridgeError> {
    let stream =
        UnixStream::connect(socket_path).map_err(|_| NativeBridgeError::SocketUnavailable)?;
    stream
        .set_read_timeout(Some(SOCKET_TIMEOUT))
        .map_err(|_| NativeBridgeError::SocketConfiguration)?;
    stream
        .set_write_timeout(Some(SOCKET_TIMEOUT))
        .map_err(|_| NativeBridgeError::SocketConfiguration)?;
    Ok(stream)
}

fn monitor_client_config(
    client_id: ClientId,
    client_name: ClientName,
) -> Result<SdkClientConfig, NativeBridgeError> {
    client_config(client_id, client_name, &[NATIVE_FEATURE])
}

fn endpoint_client_config(
    client_id: ClientId,
    client_name: ClientName,
) -> Result<SdkClientConfig, NativeBridgeError> {
    client_config(client_id, client_name, &[NATIVE_FEATURE, OWNERSHIP_FEATURE])
}

fn client_config(
    client_id: ClientId,
    client_name: ClientName,
    required_features: &[&str],
) -> Result<SdkClientConfig, NativeBridgeError> {
    let version = ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION)
        .map_err(|_| NativeBridgeError::InvalidIdentity)?;
    let required_features = required_features
        .iter()
        .map(|feature| {
            ProtocolFeatureId::try_from(*feature).map_err(|_| NativeBridgeError::InvalidIdentity)
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(SdkClientConfig {
        client_id,
        client_name,
        minimum_version: version,
        maximum_version: version,
        required_features,
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
    use hfx_bridge::{
        BridgeSession, BridgeSessionConfig, SessionError, SessionIdentityError,
        SessionIdentitySource,
    };
    use hfx_domain::{
        ComponentVersion, GenerationId, LogicalDeviceId, ReceiverId, RequestId, ResourceKind,
        ServerInstanceId,
    };
    use hfx_protocol::{
        ClientHello, LeaseRequest, NegotiationRequestEnvelope, RpcRequest, SessionRequestEnvelope,
    };
    use std::os::unix::net::UnixListener;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Call {
        Acquire,
        Exchange,
        Release,
    }

    struct FakeExchange {
        acquire: Option<Result<LeaseId, BridgeExchangeFailure>>,
        result: BridgeExchangeResult,
        calls: Vec<Call>,
    }

    struct DeterministicSessionIdentities(u8);

    impl SessionIdentitySource for DeterministicSessionIdentities {
        fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
            for byte in destination {
                *byte = self.0;
                self.0 = self.0.wrapping_add(1);
            }
            Ok(())
        }
    }

    impl EphemeralExchange for FakeExchange {
        fn acquire(&mut self, _resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure> {
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

    fn session() -> BridgeSession {
        BridgeSession::new(BridgeSessionConfig {
            server_instance_id: text::<ServerInstanceId>("relay-feature-test"),
            bridge_version: text::<ComponentVersion>("0.0.0-test"),
            event_buffer_capacity: hfx_domain::QueueCapacity::try_from(8_u16)
                .expect("capacity is valid"),
        })
    }

    fn negotiation(config: SdkClientConfig) -> RpcRequest {
        RpcRequest::Negotiate(NegotiationRequestEnvelope {
            request_id: text::<RequestId>("relay-negotiate"),
            params: ClientHello {
                client_id: config.client_id,
                client_name: config.client_name,
                minimum_version: config.minimum_version,
                maximum_version: config.maximum_version,
                required_features: config.required_features,
                optional_features: config.optional_features,
            },
        })
    }

    fn acquire_request(client_id: ClientId, hello: &hfx_protocol::ServerHello) -> RpcRequest {
        RpcRequest::AcquireLease(SessionRequestEnvelope {
            request_id: text::<RequestId>("relay-acquire"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: LeaseRequest {
                request_id: text::<RequestId>("relay-acquire"),
                client_id,
                resources: vec![resource()],
                duration_ms: LeaseDurationMs::try_from(LEASE_DURATION_MS)
                    .expect("lease duration is valid"),
            },
        })
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
            acquire: Some(Err(BridgeExchangeFailure::LeaseConflict)),
            result: BridgeExchangeResult::MatchedResponse([0; FEATURE_REPORT_BYTES]),
            calls: Vec::new(),
        };
        assert_eq!(
            exchange_ephemeral(&mut exchange, resource(), [0x42; FEATURE_REPORT_BYTES]),
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::LeaseConflict)
        );
        assert_eq!(exchange.calls, vec![Call::Acquire]);
    }

    #[test]
    fn uncertain_and_not_sent_exchanges_still_release_their_lease() {
        for result in [
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::TransportNotSent),
            BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::TransportMayHaveBeenSent),
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

    #[test]
    fn monitor_negotiates_only_read_only_native_route_authority() {
        let config = monitor_client_config(
            text::<ClientId>("monitor-test"),
            text::<ClientName>("Monitor test"),
        )
        .expect("monitor configuration is valid");
        assert_eq!(
            config.required_features,
            vec![text::<ProtocolFeatureId>(NATIVE_FEATURE)]
        );
    }

    #[test]
    fn configured_monitor_stream_retains_the_normal_production_timeout() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let socket_path = std::env::temp_dir().join(format!(
            "hyperflux-relay-monitor-timeout-{}-{unique}.sock",
            std::process::id()
        ));
        let listener = UnixListener::bind(&socket_path).expect("test listener binds");

        let stream = configured_stream(&socket_path).expect("monitor stream connects");

        assert_eq!(
            stream.read_timeout().expect("read timeout reads"),
            Some(SOCKET_TIMEOUT)
        );
        assert_eq!(
            stream.write_timeout().expect("write timeout reads"),
            Some(SOCKET_TIMEOUT)
        );
        drop(stream);
        drop(listener);
        std::fs::remove_file(socket_path).expect("test socket is removed");
    }

    #[test]
    fn endpoint_negotiates_every_feature_used_by_an_exchange() {
        let config = endpoint_client_config(
            text::<ClientId>("endpoint-test"),
            text::<ClientName>("Endpoint test"),
        )
        .expect("endpoint configuration is valid");
        assert_eq!(
            config.required_features,
            vec![
                text::<ProtocolFeatureId>(NATIVE_FEATURE),
                text::<ProtocolFeatureId>(OWNERSHIP_FEATURE),
            ]
        );
    }

    #[test]
    fn real_bridge_authorization_accepts_endpoint_leases_and_rejects_monitor_leases() {
        let endpoint_config = endpoint_client_config(
            text::<ClientId>("endpoint-boundary"),
            text::<ClientName>("Endpoint boundary"),
        )
        .expect("endpoint configuration is valid");
        let endpoint_id = endpoint_config.client_id.clone();
        let mut endpoint_session = session();
        let mut endpoint_identities = DeterministicSessionIdentities(1);
        let endpoint_hello = endpoint_session
            .negotiate_request(&negotiation(endpoint_config), &mut endpoint_identities)
            .expect("endpoint negotiation succeeds");
        endpoint_session
            .authorize_request(&acquire_request(endpoint_id, &endpoint_hello))
            .expect("endpoint lease feature is negotiated");

        let monitor_config = monitor_client_config(
            text::<ClientId>("monitor-boundary"),
            text::<ClientName>("Monitor boundary"),
        )
        .expect("monitor configuration is valid");
        let monitor_id = monitor_config.client_id.clone();
        let mut monitor_session = session();
        let mut monitor_identities = DeterministicSessionIdentities(32);
        let monitor_hello = monitor_session
            .negotiate_request(&negotiation(monitor_config), &mut monitor_identities)
            .expect("monitor negotiation succeeds");
        assert_eq!(
            monitor_session.authorize_request(&acquire_request(monitor_id, &monitor_hello)),
            Err(SessionError::FeatureNotNegotiated)
        );
    }
}
