// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, BridgeSessionConfig, RpcFailure,
    SessionIdentityError, SessionIdentitySource, SessionRegistry, serve_connection,
};
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, DroppedEventCount, LeaseId, NativeExchangeTimeoutMs,
    NativeReportHex, NativeRouteToken, ProjectionRevision, ProtocolFeatureId, ProtocolVersion,
    QueueCapacity, RequestId, SequenceNumber, ServerInstanceId, StreamEpoch, StreamId,
    SubscriptionId,
};
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EventBatch, EventCursor, FrameError, IntegrationView,
    LeaseRequest, LeaseResult, NativeExchangeNotSent, NativeExchangeOutcome, NativeExchangeRequest,
    NativeExchangeResult, NativeRouteEventBatch, NativeRouteInventory, ProtocolWireError,
    ReleaseLeaseRequest, RenewLeaseRequest, RpcRequest, RpcResponse, ServerHello,
    SubscriptionRequest, SuccessEnvelope, TransactionLookup, TransactionRequest, TransactionResult,
    decode_rpc_request_for_version, read_rpc_request, read_rpc_request_for_version,
    write_rpc_response, write_rpc_response_for_version,
};
use hfx_sdk::{
    FramedIoChannel, HyperFluxClient, RequestIdentityError, RequestIdentitySource, SdkClientConfig,
    SdkError,
};
use serde_json::json;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::thread;
use std::time::Duration;

#[derive(Debug)]
struct DeterministicSessionIdentities {
    next: u8,
}

impl DeterministicSessionIdentities {
    const fn new() -> Self {
        Self { next: 1 }
    }
}

impl SessionIdentitySource for DeterministicSessionIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.next;
            self.next = self.next.wrapping_add(1);
        }
        Ok(())
    }
}

#[derive(Debug, Default)]
struct DeterministicRequestIdentities {
    next: usize,
}

impl RequestIdentitySource for DeterministicRequestIdentities {
    fn next_request_id(&mut self) -> Result<RequestId, RequestIdentityError> {
        self.next += 1;
        RequestId::try_from(format!("sdk-test-request-{}", self.next))
            .map_err(|_| RequestIdentityError::InvalidGeneratedIdentity)
    }
}

#[derive(Debug, Default)]
struct SnapshotBackend {
    snapshot_calls: usize,
    integration_view_calls: usize,
    disconnect_calls: usize,
    observed_client: Option<ClientId>,
    native_enumerate_calls: usize,
    native_transact_calls: usize,
    native_watch_calls: usize,
}

impl BridgeRpcBackend for SnapshotBackend {
    fn snapshot(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        self.snapshot_calls += 1;
        self.observed_client = Some(context.session().client_id.clone());
        Ok(empty_snapshot())
    }

    fn integration_view(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        self.integration_view_calls += 1;
        self.observed_client = Some(context.session().client_id.clone());
        Ok(empty_integration_view())
    }

    fn acquire_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn renew_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn release_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn submit_transaction(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn transaction_outcome(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn subscribe(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn diagnostics(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn native_enumerate(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<NativeRouteInventory, RpcFailure> {
        self.native_enumerate_calls += 1;
        Ok(NativeRouteInventory {
            cursor: empty_snapshot().cursor,
            routes: Vec::new(),
        })
    }

    fn native_transact(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        self.native_transact_calls += 1;
        Ok(NativeExchangeOutcome {
            request_id: request.request_id,
            route_token: request.route_token,
            result: NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: hfx_domain::ProtocolErrorKind::TransportFailure,
                finding_id: id("HFX-TRANSPORT-001"),
            }),
        })
    }

    fn native_watch(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: SubscriptionRequest,
    ) -> Result<NativeRouteEventBatch, RpcFailure> {
        self.native_watch_calls += 1;
        let cursor = empty_snapshot().cursor;
        Ok(NativeRouteEventBatch {
            subscription_id: id::<SubscriptionId>("native-subscription-1"),
            next_cursor: cursor.clone(),
            events: Vec::new(),
            oldest_available: cursor.sequence,
            latest_available: cursor.sequence,
            dropped_events: DroppedEventCount::try_from(0_u64).expect("zero drops are valid"),
            cursor_gap: false,
            has_more: false,
        })
    }

    fn disconnect(&mut self, _session: &AuthorizedSession) -> Result<(), RpcFailure> {
        self.disconnect_calls += 1;
        Ok(())
    }
}

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity must be valid")
}

fn client_config() -> SdkClientConfig {
    SdkClientConfig {
        client_id: id::<ClientId>("sdk-client-test"),
        client_name: id::<ClientName>("SDK contract test"),
        minimum_version: ProtocolVersion::try_from(5_u16).expect("v5 is valid"),
        maximum_version: ProtocolVersion::try_from(5_u16).expect("v5 is valid"),
        required_features: Vec::new(),
        optional_features: Vec::new(),
    }
}

fn integration_client_config() -> SdkClientConfig {
    SdkClientConfig {
        client_id: id::<ClientId>("sdk-integration-client-test"),
        client_name: id::<ClientName>("SDK integration projection test"),
        minimum_version: ProtocolVersion::try_from(5_u16).expect("v5 is valid"),
        maximum_version: ProtocolVersion::try_from(5_u16).expect("v5 is valid"),
        required_features: vec![id::<ProtocolFeatureId>("integration-view-projection")],
        optional_features: Vec::new(),
    }
}

fn native_client_config() -> SdkClientConfig {
    SdkClientConfig {
        client_id: id::<ClientId>("sdk-native-client-test"),
        client_name: id::<ClientName>("SDK native tunnel test"),
        minimum_version: ProtocolVersion::try_from(7_u16).expect("v7 is valid"),
        maximum_version: ProtocolVersion::try_from(7_u16).expect("v7 is valid"),
        required_features: vec![
            id::<ProtocolFeatureId>("native-receiver-tunnel"),
            id::<ProtocolFeatureId>("ownership-leases"),
        ],
        optional_features: Vec::new(),
    }
}

fn bridge_config() -> BridgeSessionConfig {
    BridgeSessionConfig {
        server_instance_id: id::<ServerInstanceId>("sdk-server-test"),
        bridge_version: id::<ComponentVersion>("0.0.0-test"),
        event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity is valid"),
    }
}

fn empty_snapshot() -> BridgeSnapshot {
    BridgeSnapshot {
        cursor: EventCursor {
            stream_id: id::<StreamId>("sdk-stream-test"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("revision is valid"),
            sequence: SequenceNumber::try_from(0_u64).expect("sequence is valid"),
        },
        receivers: Vec::new(),
    }
}

fn empty_integration_view() -> IntegrationView {
    let snapshot = empty_snapshot();
    IntegrationView {
        cursor: snapshot.cursor,
        receivers: Vec::new(),
    }
}

fn scripted_hello(server: &str) -> ServerHello {
    ServerHello {
        selected_version: ProtocolVersion::try_from(5_u16).expect("v5 is valid"),
        server_instance_id: id::<ServerInstanceId>(server),
        protocol_session_id: id("sdk-protocol-session"),
        negotiation_token: id("sdk-negotiation-token"),
        bridge_version: id::<ComponentVersion>("0.0.0-test"),
        enabled_features: Vec::new(),
        event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity is valid"),
    }
}

fn configured_pair() -> (UnixStream, UnixStream) {
    let (client, server) = UnixStream::pair().expect("Unix stream pair creates");
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("client timeout config succeeds");
    server
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("server timeout config succeeds");
    (client, server)
}

#[derive(Debug, Default)]
struct RecordingIo {
    bytes: Vec<u8>,
}

impl Read for RecordingIo {
    fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
        Ok(0)
    }
}

impl Write for RecordingIo {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn sdk_negotiates_and_binds_session_data_over_the_real_bridge_runtime() {
    let (client_stream, mut server_stream) = configured_pair();
    let server = thread::spawn(move || {
        let mut identities = DeterministicSessionIdentities::new();
        let mut sessions =
            SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity is valid"));
        let mut backend = SnapshotBackend::default();
        let result = serve_connection(
            &mut server_stream,
            bridge_config(),
            &mut identities,
            &mut sessions,
            &mut backend,
        );
        (result, backend, sessions.len())
    });

    let mut client = HyperFluxClient::connect(
        client_stream,
        client_config(),
        DeterministicRequestIdentities::default(),
    )
    .expect("SDK negotiation succeeds");
    assert_eq!(client.server_hello().selected_version.get(), 5);
    assert_eq!(client.snapshot(), Ok(empty_snapshot()));
    client
        .into_inner()
        .shutdown(std::net::Shutdown::Write)
        .expect("client closes its write side");

    let (result, backend, remaining_sessions) = server.join().expect("server thread joins");
    let report = result.expect("bridge connection exits cleanly");
    assert_eq!(report.requests_served, 2);
    assert_eq!(backend.snapshot_calls, 1);
    assert_eq!(backend.disconnect_calls, 1);
    assert_eq!(backend.observed_client, Some(id("sdk-client-test")));
    assert_eq!(remaining_sessions, 0);
}

#[test]
fn sdk_requests_the_v5_view_over_the_real_bridge_runtime() {
    let (client_stream, mut server_stream) = configured_pair();
    let server = thread::spawn(move || {
        let mut identities = DeterministicSessionIdentities::new();
        let mut sessions =
            SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity is valid"));
        let mut backend = SnapshotBackend::default();
        let result = serve_connection(
            &mut server_stream,
            bridge_config(),
            &mut identities,
            &mut sessions,
            &mut backend,
        );
        (result, backend, sessions.len())
    });

    let mut client = HyperFluxClient::connect(
        client_stream,
        integration_client_config(),
        DeterministicRequestIdentities::default(),
    )
    .expect("SDK v5 negotiation succeeds");
    assert_eq!(client.server_hello().selected_version.get(), 5);
    assert_eq!(client.integration_view(), Ok(empty_integration_view()));
    client
        .into_inner()
        .shutdown(std::net::Shutdown::Write)
        .expect("client closes its write side");

    let (result, backend, remaining_sessions) = server.join().expect("server thread joins");
    let report = result.expect("bridge connection exits cleanly");
    assert_eq!(report.requests_served, 2);
    assert_eq!(backend.integration_view_calls, 1);
    assert_eq!(backend.disconnect_calls, 1);
    assert_eq!(
        backend.observed_client,
        Some(id("sdk-integration-client-test"))
    );
    assert_eq!(remaining_sessions, 0);
}

#[test]
fn sdk_v7_exposes_replayable_native_enumerate_transact_and_watch() {
    let (client_stream, mut server_stream) = configured_pair();
    let server = thread::spawn(move || {
        let mut identities = DeterministicSessionIdentities::new();
        let mut sessions =
            SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity is valid"));
        let mut backend = SnapshotBackend::default();
        let result = serve_connection(
            &mut server_stream,
            bridge_config(),
            &mut identities,
            &mut sessions,
            &mut backend,
        );
        (result, backend, sessions.len())
    });

    let mut client = HyperFluxClient::connect(
        client_stream,
        native_client_config(),
        DeterministicRequestIdentities::default(),
    )
    .expect("SDK v7 negotiation succeeds");
    assert_eq!(client.server_hello().selected_version.get(), 7);
    assert!(
        client
            .native_enumerate()
            .expect("routes enumerate")
            .routes
            .is_empty()
    );

    let prepared = client
        .prepare_native_exchange(
            id::<LeaseId>("native-lease-1"),
            id::<NativeRouteToken>(&"a".repeat(64)),
            id::<NativeReportHex>(&"0".repeat(180)),
            NativeExchangeTimeoutMs::try_from(100_u16).expect("timeout is valid"),
        )
        .expect("native request identity is prepared");
    let first = client
        .native_transact(&prepared)
        .expect("native outcome is returned");
    let repeated = client
        .native_transact(&prepared)
        .expect("exact prepared identity is replayable");
    assert_eq!(first, repeated);
    assert_eq!(first.request_id, prepared.request_id);

    let watched = client
        .native_watch(hfx_sdk::EventSubscription {
            subscription_id: None,
            expected_cursor: None,
            max_events: hfx_domain::EventBatchLimit::try_from(8_u16).expect("batch limit is valid"),
        })
        .expect("native routes are watchable");
    assert!(watched.events.is_empty());

    client
        .into_inner()
        .shutdown(std::net::Shutdown::Write)
        .expect("client closes its write side");
    let (result, backend, remaining_sessions) = server.join().expect("server thread joins");
    let report = result.expect("bridge connection exits cleanly");
    assert_eq!(report.requests_served, 5);
    assert_eq!(backend.native_enumerate_calls, 1);
    assert_eq!(backend.native_transact_calls, 2);
    assert_eq!(backend.native_watch_calls, 1);
    assert_eq!(backend.disconnect_calls, 1);
    assert_eq!(remaining_sessions, 0);
}

#[test]
fn clean_eof_and_malformed_server_responses_are_distinct() {
    let (client_stream, mut server_stream) = configured_pair();
    let eof_server = thread::spawn(move || {
        let _ = read_rpc_request(&mut server_stream).expect("request frame reads");
    });
    assert_eq!(
        HyperFluxClient::connect(
            client_stream,
            client_config(),
            DeterministicRequestIdentities::default(),
        )
        .expect_err("clean EOF must fail negotiation"),
        SdkError::ConnectionClosed
    );
    eof_server.join().expect("EOF server joins");

    let (client_stream, mut server_stream) = configured_pair();
    let malformed_server = thread::spawn(move || {
        let _ = read_rpc_request(&mut server_stream).expect("request frame reads");
        server_stream
            .write_all(&1_u32.to_be_bytes())
            .expect("malformed length writes");
        server_stream
            .write_all(b"{")
            .expect("malformed JSON writes");
        server_stream.flush().expect("malformed response flushes");
    });
    assert!(matches!(
        HyperFluxClient::connect(
            client_stream,
            client_config(),
            DeterministicRequestIdentities::default(),
        ),
        Err(SdkError::Frame(hfx_protocol::FrameError::InvalidResponse(
            hfx_protocol::ProtocolWireError::MalformedJson
        )))
    ));
    malformed_server.join().expect("malformed server joins");
}

#[test]
fn sdk_rejects_misattributed_negotiation_response() {
    let (client_stream, mut server_stream) = configured_pair();
    let server = thread::spawn(move || {
        let _request = read_rpc_request(&mut server_stream)
            .expect("request frame reads")
            .expect("request is present");
        let hello = scripted_hello("scripted-server-a");
        let response = RpcResponse::NegotiateSuccess(SuccessEnvelope {
            request_id: id::<RequestId>("another-request"),
            server_instance_id: hello.server_instance_id.clone(),
            result: hello,
        });
        write_rpc_response(&mut server_stream, &response).expect("response writes");
    });
    assert_eq!(
        HyperFluxClient::connect(
            client_stream,
            client_config(),
            DeterministicRequestIdentities::default(),
        )
        .expect_err("wrong request identity must fail"),
        SdkError::ResponseRequestMismatch
    );
    server.join().expect("scripted server joins");
}

#[test]
fn sdk_rejects_bridge_instance_change_after_negotiation() {
    let (client_stream, mut server_stream) = configured_pair();
    let server = thread::spawn(move || {
        let negotiation = read_rpc_request(&mut server_stream)
            .expect("negotiation frame reads")
            .expect("negotiation is present");
        let hello = scripted_hello("scripted-server-a");
        write_rpc_response(
            &mut server_stream,
            &RpcResponse::NegotiateSuccess(SuccessEnvelope {
                request_id: negotiation.request_id().clone(),
                server_instance_id: hello.server_instance_id.clone(),
                result: hello.clone(),
            }),
        )
        .expect("negotiation response writes");

        let snapshot = read_rpc_request_for_version(&mut server_stream, hello.selected_version)
            .expect("snapshot frame reads")
            .expect("snapshot request is present");
        write_rpc_response_for_version(
            &mut server_stream,
            &RpcResponse::SnapshotSuccess(SuccessEnvelope {
                request_id: snapshot.request_id().clone(),
                server_instance_id: id("scripted-server-b"),
                result: empty_snapshot(),
            }),
            hello.selected_version,
        )
        .expect("snapshot response writes");
    });

    let mut client = HyperFluxClient::connect(
        client_stream,
        client_config(),
        DeterministicRequestIdentities::default(),
    )
    .expect("scripted negotiation succeeds");
    assert_eq!(client.snapshot(), Err(SdkError::ServerInstanceMismatch));
    server.join().expect("scripted server joins");
}

#[test]
fn sdk_channel_emits_nothing_for_an_unsafe_legacy_downgrade() {
    let bytes = serde_json::to_vec(&json!({
        "method": "native-enumerate",
        "request": {
            "request_id": "request-native-enumerate",
            "protocol_session_id": "protocol-session-1",
            "negotiation_token": "negotiation-1",
            "params": {}
        }
    }))
    .expect("request serializes");
    let request = decode_rpc_request_for_version(
        &bytes,
        ProtocolVersion::try_from(7_u16).expect("v7 is valid"),
    )
    .expect("v7 native request decodes");
    assert!(matches!(request, RpcRequest::NativeEnumerate(_)));

    let mut channel = FramedIoChannel::new(RecordingIo::default());
    assert_eq!(
        channel.exchange(
            &request,
            Some(ProtocolVersion::try_from(5_u16).expect("v5 is valid")),
        ),
        Err(SdkError::Frame(FrameError::InvalidRequest(
            ProtocolWireError::UnsupportedVersionMethod
        )))
    );
    assert!(channel.into_inner().bytes.is_empty());
}
