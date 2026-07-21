// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, BridgeSessionConfig,
    ConnectionServeError, RpcFailure, SessionIdentityError, SessionIdentitySource, SessionRegistry,
    serve_connection,
};
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, ProjectionRevision, ProtocolVersion, QueueCapacity,
    RequestId, SequenceNumber, ServerInstanceId, StreamEpoch, StreamId,
};
use hfx_protocol::{
    BridgeSnapshot, ClientHello, DiagnosticSnapshot, EmptyRequest, EventBatch, EventCursor,
    IntegrationView, LeaseRequest, LeaseResult, NegotiationRequestEnvelope, ReleaseLeaseRequest,
    RenewLeaseRequest, RpcRequest, RpcResponse, SessionRequestEnvelope, SubscriptionRequest,
    TransactionLookup, TransactionRequest, TransactionResult, read_rpc_response,
    read_rpc_response_for_version, write_rpc_request, write_rpc_request_for_version,
};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::thread;
use std::time::Duration;

#[derive(Debug)]
struct DeterministicIdentities {
    next: u8,
}

impl DeterministicIdentities {
    const fn new() -> Self {
        Self { next: 1 }
    }
}

impl SessionIdentitySource for DeterministicIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.next;
            self.next = self.next.wrapping_add(1);
        }
        Ok(())
    }
}

#[derive(Debug, Default)]
struct FakeBackend {
    snapshot_calls: usize,
    disconnect_calls: usize,
    fail_disconnect: bool,
}

impl BridgeRpcBackend for FakeBackend {
    fn snapshot(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        self.snapshot_calls += 1;
        Ok(empty_snapshot())
    }

    fn integration_view(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        let snapshot = empty_snapshot();
        Ok(IntegrationView {
            cursor: snapshot.cursor,
            receivers: Vec::new(),
        })
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

    fn disconnect(&mut self, _session: &AuthorizedSession) -> Result<(), RpcFailure> {
        self.disconnect_calls += 1;
        if self.fail_disconnect {
            return Err(RpcFailure::internal());
        }
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

fn config() -> BridgeSessionConfig {
    BridgeSessionConfig {
        server_instance_id: id::<ServerInstanceId>("server-connection-test"),
        bridge_version: id::<ComponentVersion>("0.0.0-test"),
        event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity must be valid"),
    }
}

fn registry() -> SessionRegistry {
    SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity must be valid"))
}

fn negotiation() -> RpcRequest {
    RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: id::<RequestId>("request-negotiate"),
        params: ClientHello {
            client_id: id::<ClientId>("client-connection-test"),
            client_name: id::<ClientName>("Connection runtime test"),
            minimum_version: ProtocolVersion::try_from(2).expect("version must be valid"),
            maximum_version: ProtocolVersion::try_from(2).expect("version must be valid"),
            required_features: Vec::new(),
            optional_features: Vec::new(),
        },
    })
}

fn empty_snapshot() -> BridgeSnapshot {
    BridgeSnapshot {
        cursor: EventCursor {
            stream_id: id::<StreamId>("stream-connection-test"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch must be valid"),
            projection_revision: ProjectionRevision::try_from(1_u32)
                .expect("revision must be valid"),
            sequence: SequenceNumber::try_from(0_u64).expect("sequence must be valid"),
        },
        receivers: Vec::new(),
    }
}

fn negotiate(client: &mut UnixStream) -> hfx_protocol::ServerHello {
    write_rpc_request(client, &negotiation()).expect("negotiation request writes");
    let response = read_rpc_response(client)
        .expect("negotiation response frame reads")
        .expect("server returns a negotiation response");
    let RpcResponse::NegotiateSuccess(envelope) = response else {
        panic!("negotiation must succeed");
    };
    envelope.result
}

fn spawn_server(
    mut stream: UnixStream,
    fail_disconnect: bool,
) -> thread::JoinHandle<(
    Result<hfx_bridge::ConnectionServeReport, ConnectionServeError>,
    FakeBackend,
    usize,
)> {
    thread::spawn(move || {
        let mut identities = DeterministicIdentities::new();
        let mut sessions = registry();
        let mut backend = FakeBackend {
            fail_disconnect,
            ..FakeBackend::default()
        };
        let result = serve_connection(
            &mut stream,
            config(),
            &mut identities,
            &mut sessions,
            &mut backend,
        );
        (result, backend, sessions.len())
    })
}

#[test]
fn real_unix_stream_serves_exact_version_and_cleans_up_on_eof() {
    let (mut client, server) = UnixStream::pair().expect("Unix stream pair creates");
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("timeout config succeeds");
    let server = spawn_server(server, false);

    let hello = negotiate(&mut client);
    assert_eq!(hello.selected_version.get(), 2);
    let snapshot = RpcRequest::Snapshot(SessionRequestEnvelope {
        request_id: id::<RequestId>("request-snapshot"),
        protocol_session_id: hello.protocol_session_id,
        negotiation_token: hello.negotiation_token,
        params: EmptyRequest {},
    });
    write_rpc_request_for_version(&mut client, &snapshot, hello.selected_version)
        .expect("versioned snapshot request writes");
    assert!(matches!(
        read_rpc_response_for_version(&mut client, hello.selected_version),
        Ok(Some(RpcResponse::SnapshotSuccess(_)))
    ));
    client
        .shutdown(std::net::Shutdown::Write)
        .expect("client write side closes");

    let (result, backend, remaining_sessions) = server.join().expect("server thread joins");
    assert_eq!(
        result,
        Ok(hfx_bridge::ConnectionServeReport {
            requests_served: 2,
            selected_version: Some(hello.selected_version),
        })
    );
    assert_eq!(backend.snapshot_calls, 1);
    assert_eq!(backend.disconnect_calls, 1);
    assert_eq!(remaining_sessions, 0);
}

#[test]
fn framing_and_cleanup_failures_are_both_preserved() {
    let (mut client, server) = UnixStream::pair().expect("Unix stream pair creates");
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("timeout config succeeds");
    let server = spawn_server(server, true);

    let _hello = negotiate(&mut client);
    client
        .write_all(&3_u32.to_be_bytes())
        .expect("malformed frame length writes");
    client
        .write_all(b"{")
        .expect("partial malformed payload writes");
    client
        .shutdown(std::net::Shutdown::Write)
        .expect("client write side closes");

    let (result, backend, remaining_sessions) = server.join().expect("server thread joins");
    assert!(matches!(
        result,
        Err(ConnectionServeError::FrameAndCleanup {
            frame: hfx_bridge::FrameError::TruncatedPayload {
                declared: 3,
                received: 1,
            },
            cleanup,
        }) if cleanup == RpcFailure::internal()
    ));
    assert_eq!(backend.disconnect_calls, 1);
    assert_eq!(remaining_sessions, 0);
}
