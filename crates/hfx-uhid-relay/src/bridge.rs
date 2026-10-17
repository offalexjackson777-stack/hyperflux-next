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
use std::time::{Duration, Instant};

const NATIVE_FEATURE: &str = "native-receiver-tunnel";
const OWNERSHIP_FEATURE: &str = "ownership-leases";
const LEASE_DURATION_MS: u32 = 30_000;
const SOCKET_TIMEOUT: Duration = Duration::from_secs(3);

#[cfg(test)]
pub(crate) mod test_server;

/// How long a relay endpoint keeps its `NativeControl` lease after the last
/// matched exchange before it gives the lease back.
///
/// `OpenRazer` writes one custom lighting frame as a burst of back-to-back
/// Feature exchanges (one per row plus the custom-mode report) with about
/// 1 ms between one exchange ending and the next starting, so 25 ms keeps the
/// lease across a whole burst. Ripple's frames are about 140 ms apart and a
/// frame's burst takes about 90 ms, which leaves a gap of about 50 ms; the
/// lease is back with the bridge about halfway through that gap, so another
/// writer can take it between frames.
pub(crate) const LEASE_IDLE_RELEASE: Duration = Duration::from_millis(25);
/// A lease held this long is renewed before the next exchange uses it. Half
/// of the requested duration leaves 15 s, far more than one exchange deadline
/// (1 s) plus one bridge round trip (bounded by the 3 s socket timeout), so a
/// lease can never expire in the middle of an exchange.
const LEASE_RENEW_AFTER: Duration = Duration::from_millis(LEASE_DURATION_MS as u64 / 2);

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

    /// When a lease this bridge still holds becomes due for idle release.
    /// Bridges that never keep a lease between exchanges return `None`.
    fn lease_release_deadline(&self) -> Option<Instant> {
        None
    }

    /// Gives back a held lease whose idle deadline has passed at `now`.
    fn release_idle_lease(&mut self, _now: Instant) {}

    /// Gives back a held lease at once, for teardown, recycle, and shutdown.
    fn release_lease(&mut self) {}
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

/// One relay endpoint's bridge connection and the lease it keeps across a
/// burst of exchanges.
pub struct NativeBridge {
    channel: NativeChannel,
    lease: BurstLease,
}

struct NativeChannel {
    client: HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
    route: NativeRoute,
}

/// The lease operations one burst needs, kept apart from the bridge client so
/// the lease policy is tested without a socket.
trait LeaseChannel {
    fn now(&self) -> Instant;
    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure>;
    fn renew(&mut self, lease_id: LeaseId) -> Result<(), BridgeExchangeFailure>;
    fn exchange_leased(
        &mut self,
        lease_id: LeaseId,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult;
    fn release(&mut self, lease_id: LeaseId);
}

struct HeldLease {
    lease_id: LeaseId,
    /// Taken before the acquire or renew request was sent, so the bridge's
    /// expiry is never earlier than this plus the requested duration.
    granted_at: Instant,
    last_exchange: Instant,
}

/// Keeps one `NativeControl` lease across a burst of exchanges.
///
/// The lease is acquired by the first exchange of a burst, reused while
/// exchanges keep matching, renewed before it could expire, and given back
/// [`LEASE_IDLE_RELEASE`] after the last matched exchange. Any other outcome
/// gives it back at once, so the next exchange acquires a fresh lease exactly
/// as if nothing had been kept. No exchange is ever repeated here.
#[derive(Default)]
struct BurstLease {
    held: Option<HeldLease>,
}

impl BurstLease {
    fn exchange<C: LeaseChannel>(
        &mut self,
        channel: &mut C,
        resource: ResourceKey,
        report: [u8; FEATURE_REPORT_BYTES],
    ) -> BridgeExchangeResult {
        let started = channel.now();
        self.renew_if_due(channel, started);
        let lease_id = if let Some(held) = &self.held {
            held.lease_id.clone()
        } else {
            match channel.acquire(resource) {
                Ok(lease_id) => {
                    self.held = Some(HeldLease {
                        lease_id: lease_id.clone(),
                        granted_at: started,
                        last_exchange: started,
                    });
                    lease_id
                }
                Err(failure) => return BridgeExchangeResult::NotSent(failure),
            }
        };
        let result = channel.exchange_leased(lease_id, report);
        match (&result, &mut self.held) {
            (BridgeExchangeResult::MatchedResponse(_), Some(held)) => {
                held.last_exchange = channel.now();
            }
            _ => self.release(channel),
        }
        result
    }

    /// Renews before the next exchange once half the lease has passed. A
    /// failed renewal gives the lease back so this exchange acquires afresh;
    /// nothing has been sent yet, so nothing is repeated.
    fn renew_if_due<C: LeaseChannel>(&mut self, channel: &mut C, now: Instant) {
        let Some(held) = &mut self.held else {
            return;
        };
        if now.saturating_duration_since(held.granted_at) < LEASE_RENEW_AFTER {
            return;
        }
        if channel.renew(held.lease_id.clone()).is_ok() {
            held.granted_at = now;
        } else {
            self.release(channel);
        }
    }

    fn release_deadline(&self) -> Option<Instant> {
        self.held
            .as_ref()
            .map(|held| held.last_exchange + LEASE_IDLE_RELEASE)
    }

    fn release_if_idle<C: LeaseChannel>(&mut self, channel: &mut C, now: Instant) {
        if self
            .release_deadline()
            .is_some_and(|deadline| now >= deadline)
        {
            self.release(channel);
        }
    }

    fn release<C: LeaseChannel>(&mut self, channel: &mut C) {
        if let Some(held) = self.held.take() {
            channel.release(held.lease_id);
        }
    }
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
    /// and for bridge negotiation failures. The first Feature exchange of a
    /// burst obtains a lease, the burst reuses it, and the endpoint releases it
    /// `LEASE_IDLE_RELEASE` (25 ms) after the last exchange, or at once after any
    /// exchange that did not match, so an idle `OpenRazer` endpoint cannot
    /// lock out a native `OpenRGB` session.
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
        Ok(Self {
            channel: NativeChannel { client, route },
            lease: BurstLease::default(),
        })
    }
}

impl NativeChannel {
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

fn lease_duration() -> Result<LeaseDurationMs, BridgeExchangeFailure> {
    LeaseDurationMs::try_from(LEASE_DURATION_MS).map_err(|_| BridgeExchangeFailure::LeaseSdk)
}

fn granted_lease(result: Result<LeaseResult, SdkError>) -> Result<LeaseId, BridgeExchangeFailure> {
    match result {
        Ok(LeaseResult::Granted(grant)) => Ok(grant.lease_id),
        Ok(LeaseResult::Conflict(_)) => Err(BridgeExchangeFailure::LeaseConflict),
        Ok(LeaseResult::Rejected(_)) => Err(BridgeExchangeFailure::LeaseRejected),
        Err(_) => Err(BridgeExchangeFailure::LeaseSdk),
    }
}

impl LeaseChannel for NativeChannel {
    fn now(&self) -> Instant {
        Instant::now()
    }

    fn acquire(&mut self, resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure> {
        let duration = lease_duration()?;
        granted_lease(self.client.acquire_lease(vec![resource], duration))
    }

    fn renew(&mut self, lease_id: LeaseId) -> Result<(), BridgeExchangeFailure> {
        let duration = lease_duration()?;
        granted_lease(self.client.renew_lease(lease_id, duration)).map(|_| ())
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
        let Some(resource) = self.channel.route.resource.clone() else {
            return BridgeExchangeResult::NotSent(BridgeExchangeFailure::MissingRouteAuthority);
        };
        self.lease.exchange(&mut self.channel, resource, report)
    }

    fn lease_release_deadline(&self) -> Option<Instant> {
        self.lease.release_deadline()
    }

    fn release_idle_lease(&mut self, now: Instant) {
        self.lease.release_if_idle(&mut self.channel, now);
    }

    fn release_lease(&mut self) {
        self.lease.release(&mut self.channel);
    }
}

/// A dropped bridge gives its lease back explicitly instead of leaving it to
/// the bridge's disconnect cleanup.
impl Drop for NativeBridge {
    fn drop(&mut self) {
        self.lease.release(&mut self.channel);
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

    const ROW: Duration = Duration::from_millis(12);
    const BETWEEN_ROWS: Duration = Duration::from_millis(1);

    #[derive(Clone, Debug, Eq, PartialEq)]
    enum Call {
        Acquire,
        Renew(&'static str),
        Exchange(&'static str, u8),
        Release(&'static str),
    }

    /// A lease channel with a hand-driven clock. Each exchange takes one
    /// measured `OpenRazer` row (12 ms) and answers from `results`, matching
    /// by default.
    struct FakeChannel {
        now: Instant,
        leases: u8,
        acquire_failure: Option<BridgeExchangeFailure>,
        renew_failure: Option<BridgeExchangeFailure>,
        results: std::collections::VecDeque<BridgeExchangeResult>,
        calls: Vec<Call>,
    }

    impl FakeChannel {
        fn new() -> Self {
            Self {
                now: Instant::now(),
                leases: 0,
                acquire_failure: None,
                renew_failure: None,
                results: std::collections::VecDeque::new(),
                calls: Vec::new(),
            }
        }
    }

    fn lease_name(lease_id: &LeaseId) -> &'static str {
        match lease_id.as_str() {
            "lease-1" => "lease-1",
            "lease-2" => "lease-2",
            other => panic!("unexpected lease {other}"),
        }
    }

    impl LeaseChannel for FakeChannel {
        fn now(&self) -> Instant {
            self.now
        }

        fn acquire(&mut self, _resource: ResourceKey) -> Result<LeaseId, BridgeExchangeFailure> {
            self.calls.push(Call::Acquire);
            if let Some(failure) = self.acquire_failure.take() {
                return Err(failure);
            }
            self.leases += 1;
            Ok(text::<LeaseId>(&format!("lease-{}", self.leases)))
        }

        fn renew(&mut self, lease_id: LeaseId) -> Result<(), BridgeExchangeFailure> {
            self.calls.push(Call::Renew(lease_name(&lease_id)));
            self.renew_failure.take().map_or(Ok(()), Err)
        }

        fn exchange_leased(
            &mut self,
            lease_id: LeaseId,
            report: [u8; FEATURE_REPORT_BYTES],
        ) -> BridgeExchangeResult {
            self.calls
                .push(Call::Exchange(lease_name(&lease_id), report[0]));
            self.now += ROW;
            self.results
                .pop_front()
                .unwrap_or(BridgeExchangeResult::MatchedResponse(report))
        }

        fn release(&mut self, lease_id: LeaseId) {
            self.calls.push(Call::Release(lease_name(&lease_id)));
        }
    }

    fn just_before(deadline: Instant) -> Instant {
        deadline
            .checked_sub(Duration::from_millis(1))
            .expect("the deadline is past the clock origin")
    }

    fn send(lease: &mut BurstLease, channel: &mut FakeChannel, row: u8) -> BridgeExchangeResult {
        lease.exchange(channel, resource(), [row; FEATURE_REPORT_BYTES])
    }

    #[test]
    fn the_idle_bound_keeps_a_frame_burst_and_frees_the_gap_between_frames() {
        // Measured on a Ripple frame: 7 exchanges, 13 ms start to start,
        // frames about 140 ms apart.
        let frame_burst = (ROW + BETWEEN_ROWS) * 7;
        let gap_between_frames = Duration::from_millis(140)
            .checked_sub(frame_burst)
            .expect("a frame's burst fits in one frame");
        assert!(LEASE_IDLE_RELEASE > BETWEEN_ROWS * 5);
        assert!(LEASE_IDLE_RELEASE * 2 <= gap_between_frames + Duration::from_millis(1));
        assert!(
            LEASE_RENEW_AFTER + Duration::from_secs(1) + SOCKET_TIMEOUT * 2
                < Duration::from_millis(u64::from(LEASE_DURATION_MS))
        );
    }

    #[test]
    fn a_burst_does_one_acquire_and_one_release() {
        let mut channel = FakeChannel::new();
        let mut lease = BurstLease::default();
        for row in 0..7 {
            assert_eq!(
                send(&mut lease, &mut channel, row),
                BridgeExchangeResult::MatchedResponse([row; FEATURE_REPORT_BYTES])
            );
            channel.now += BETWEEN_ROWS;
        }
        let deadline = lease.release_deadline().expect("the burst keeps its lease");
        assert_eq!(deadline + BETWEEN_ROWS, channel.now + LEASE_IDLE_RELEASE);
        lease.release_if_idle(&mut channel, just_before(deadline));
        assert_eq!(lease.release_deadline(), Some(deadline));
        lease.release_if_idle(&mut channel, deadline);
        lease.release_if_idle(&mut channel, deadline + LEASE_IDLE_RELEASE);

        let mut expected = vec![Call::Acquire];
        expected.extend((0..7).map(|row| Call::Exchange("lease-1", row)));
        expected.push(Call::Release("lease-1"));
        assert_eq!(channel.calls, expected);
        assert_eq!(lease.release_deadline(), None);
    }

    #[test]
    fn the_idle_timeout_releases_and_the_next_burst_acquires_anew() {
        let mut channel = FakeChannel::new();
        let mut lease = BurstLease::default();
        send(&mut lease, &mut channel, 1);
        channel.now += LEASE_IDLE_RELEASE;
        let idle = channel.now;
        lease.release_if_idle(&mut channel, idle);
        assert_eq!(lease.release_deadline(), None);
        send(&mut lease, &mut channel, 2);
        assert_eq!(
            channel.calls,
            vec![
                Call::Acquire,
                Call::Exchange("lease-1", 1),
                Call::Release("lease-1"),
                Call::Acquire,
                Call::Exchange("lease-2", 2),
            ]
        );
    }

    #[test]
    fn a_failed_exchange_drops_the_lease_and_the_next_exchange_acquires_anew() {
        for failure in [
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::TransportNotSent),
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::PrepareSdk),
            BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::TransportMayHaveBeenSent),
            BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::TransactSdk),
        ] {
            let mut channel = FakeChannel::new();
            channel.results = [
                BridgeExchangeResult::MatchedResponse([0xa5; FEATURE_REPORT_BYTES]),
                failure,
            ]
            .into();
            let mut lease = BurstLease::default();
            send(&mut lease, &mut channel, 1);
            assert_eq!(send(&mut lease, &mut channel, 2), failure);
            assert_eq!(lease.release_deadline(), None);
            send(&mut lease, &mut channel, 3);
            assert_eq!(
                channel.calls,
                vec![
                    Call::Acquire,
                    Call::Exchange("lease-1", 1),
                    Call::Exchange("lease-1", 2),
                    Call::Release("lease-1"),
                    Call::Acquire,
                    Call::Exchange("lease-2", 3),
                ],
                "{failure:?} is answered once, never repeated"
            );
        }
    }

    #[test]
    fn a_refused_acquire_sends_nothing_and_keeps_nothing() {
        for failure in [
            BridgeExchangeFailure::LeaseConflict,
            BridgeExchangeFailure::LeaseRejected,
            BridgeExchangeFailure::LeaseSdk,
        ] {
            let mut channel = FakeChannel::new();
            channel.acquire_failure = Some(failure);
            let mut lease = BurstLease::default();
            assert_eq!(
                send(&mut lease, &mut channel, 1),
                BridgeExchangeResult::NotSent(failure)
            );
            assert_eq!(lease.release_deadline(), None);
            lease.release(&mut channel);
            send(&mut lease, &mut channel, 2);
            assert_eq!(
                channel.calls,
                vec![Call::Acquire, Call::Acquire, Call::Exchange("lease-1", 2)]
            );
        }
    }

    #[test]
    fn a_held_lease_is_renewed_before_it_could_expire() {
        let mut channel = FakeChannel::new();
        let granted = channel.now;
        let mut lease = BurstLease::default();
        send(&mut lease, &mut channel, 1);
        channel.now = just_before(granted + LEASE_RENEW_AFTER);
        send(&mut lease, &mut channel, 2);
        channel.now = granted + LEASE_RENEW_AFTER;
        send(&mut lease, &mut channel, 3);
        send(&mut lease, &mut channel, 4);
        assert_eq!(
            channel.calls,
            vec![
                Call::Acquire,
                Call::Exchange("lease-1", 1),
                Call::Exchange("lease-1", 2),
                Call::Renew("lease-1"),
                Call::Exchange("lease-1", 3),
                Call::Exchange("lease-1", 4),
            ]
        );
    }

    #[test]
    fn a_failed_renewal_releases_and_acquires_before_anything_is_sent() {
        let mut channel = FakeChannel::new();
        let mut lease = BurstLease::default();
        send(&mut lease, &mut channel, 1);
        channel.now += LEASE_RENEW_AFTER;
        channel.renew_failure = Some(BridgeExchangeFailure::LeaseRejected);
        send(&mut lease, &mut channel, 2);
        assert_eq!(
            channel.calls,
            vec![
                Call::Acquire,
                Call::Exchange("lease-1", 1),
                Call::Renew("lease-1"),
                Call::Release("lease-1"),
                Call::Acquire,
                Call::Exchange("lease-2", 2),
            ]
        );
    }

    #[test]
    fn teardown_releases_at_once_and_only_once() {
        let mut channel = FakeChannel::new();
        let mut lease = BurstLease::default();
        send(&mut lease, &mut channel, 1);
        lease.release(&mut channel);
        lease.release(&mut channel);
        assert_eq!(lease.release_deadline(), None);
        assert_eq!(
            channel.calls,
            vec![
                Call::Acquire,
                Call::Exchange("lease-1", 1),
                Call::Release("lease-1")
            ]
        );
    }

    fn connect_relay(bridge: &test_server::LeasingBridge) -> NativeBridge {
        NativeBridge::connect(bridge.socket(), test_server::leased_route(), 1)
            .expect("relay connects to the test bridge")
    }

    fn connect_other_client(
        bridge: &test_server::LeasingBridge,
    ) -> HyperFluxClient<UnixStream, KernelRequestIdentitySource> {
        let config = endpoint_client_config(
            text::<ClientId>("other-writer"),
            text::<ClientName>("Other writer"),
        )
        .expect("client configuration is valid");
        let stream = configured_stream(bridge.socket()).expect("other client connects");
        HyperFluxClient::connect(stream, config, KernelRequestIdentitySource)
            .expect("other client negotiates")
    }

    fn other_acquire(
        client: &mut HyperFluxClient<UnixStream, KernelRequestIdentitySource>,
    ) -> LeaseResult {
        client
            .acquire_lease(
                vec![test_server::route_resource()],
                lease_duration().expect("duration is valid"),
            )
            .expect("acquire is answered")
    }

    fn matched(report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
        BridgeExchangeResult::MatchedResponse(report)
    }

    #[test]
    fn over_a_real_bridge_another_client_acquires_after_the_relay_goes_idle() {
        let bridge = test_server::LeasingBridge::start();
        let mut relay = connect_relay(&bridge);
        let mut other = connect_other_client(&bridge);
        for row in 0..7 {
            let report = [row; FEATURE_REPORT_BYTES];
            assert_eq!(relay.exchange(report), matched(report));
        }
        {
            let ledger = bridge.ledger();
            assert_eq!(
                (ledger.acquires, ledger.transacts, ledger.releases),
                (1, 7, 0)
            );
        }
        assert!(matches!(
            other_acquire(&mut other),
            LeaseResult::Conflict(_)
        ));

        let deadline = relay
            .lease_release_deadline()
            .expect("the relay holds its lease through the burst");
        relay.release_idle_lease(just_before(deadline));
        assert!(bridge.ledger().held_by("hfx-openrazer-00000001"));
        relay.release_idle_lease(deadline);
        assert_eq!(bridge.ledger().releases, 1);
        assert_eq!(relay.lease_release_deadline(), None);

        let LeaseResult::Granted(grant) = other_acquire(&mut other) else {
            panic!("another client acquires once the relay is idle");
        };
        assert_eq!(
            relay.exchange([7; FEATURE_REPORT_BYTES]),
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::LeaseConflict)
        );
        assert_eq!(bridge.ledger().transacts, 7);
        other
            .release_lease(grant.lease_id)
            .expect("other client releases");
        assert_eq!(
            relay.exchange([8; FEATURE_REPORT_BYTES]),
            matched([8; FEATURE_REPORT_BYTES])
        );
        assert_eq!(bridge.ledger().acquires, 3);
    }

    fn age_lease(relay: &mut NativeBridge) {
        let held = relay.lease.held.as_mut().expect("relay holds a lease");
        held.granted_at = held
            .granted_at
            .checked_sub(LEASE_RENEW_AFTER)
            .expect("the monotonic clock is past the renewal age");
    }

    #[test]
    fn over_a_real_bridge_a_long_burst_renews_and_a_refused_renewal_acquires_anew() {
        let bridge = test_server::LeasingBridge::start();
        let mut relay = connect_relay(&bridge);
        relay.exchange([1; FEATURE_REPORT_BYTES]);
        age_lease(&mut relay);
        assert_eq!(
            relay.exchange([2; FEATURE_REPORT_BYTES]),
            matched([2; FEATURE_REPORT_BYTES])
        );
        {
            let ledger = bridge.ledger();
            assert_eq!((ledger.acquires, ledger.renewals), (1, 1));
        }
        age_lease(&mut relay);
        bridge.ledger().refuse_renewal = true;
        assert_eq!(
            relay.exchange([3; FEATURE_REPORT_BYTES]),
            matched([3; FEATURE_REPORT_BYTES])
        );
        let ledger = bridge.ledger();
        assert_eq!(
            (
                ledger.acquires,
                ledger.renewals,
                ledger.releases,
                ledger.transacts
            ),
            (2, 1, 1, 3)
        );
    }

    #[test]
    fn over_a_real_bridge_a_lost_lease_fails_one_exchange_without_a_repeat() {
        let bridge = test_server::LeasingBridge::start();
        let mut relay = connect_relay(&bridge);
        relay.exchange([1; FEATURE_REPORT_BYTES]);
        bridge.ledger().owner = None;
        assert_eq!(
            relay.exchange([2; FEATURE_REPORT_BYTES]),
            BridgeExchangeResult::NotSent(BridgeExchangeFailure::TransportNotSent)
        );
        assert_eq!(relay.lease_release_deadline(), None);
        assert_eq!(bridge.ledger().transacts, 2);
        assert_eq!(
            relay.exchange([3; FEATURE_REPORT_BYTES]),
            matched([3; FEATURE_REPORT_BYTES])
        );
        let ledger = bridge.ledger();
        assert_eq!((ledger.acquires, ledger.transacts), (2, 3));
    }

    #[test]
    fn over_a_real_bridge_teardown_and_drop_release_the_lease() {
        let bridge = test_server::LeasingBridge::start();
        let mut relay = connect_relay(&bridge);
        relay.exchange([1; FEATURE_REPORT_BYTES]);
        relay.release_lease();
        relay.release_lease();
        assert_eq!(bridge.ledger().releases, 1);
        relay.exchange([2; FEATURE_REPORT_BYTES]);
        drop(relay);
        let ledger = bridge.ledger();
        assert_eq!((ledger.releases, ledger.owner.is_none()), (2, true));
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
