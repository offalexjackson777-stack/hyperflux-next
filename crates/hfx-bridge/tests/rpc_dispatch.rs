// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    AuthorizedSession, BridgeRpcBackend, BridgeSessionConfig, ConnectionDispatcher, RpcFailure,
    SessionIdentityError, SessionIdentitySource, SessionRegistry,
};
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, NegotiationToken, ProjectionRevision,
    ProtocolErrorKind, ProtocolFeatureId, ProtocolSessionId, ProtocolVersion, QueueCapacity,
    RequestId, SequenceNumber, ServerInstanceId, StreamEpoch, StreamId,
};
use hfx_errors::ErrorCode;
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EmptyRequest, EventBatch, EventCursor, LeaseRequest,
    LeaseResult, NegotiationRequestEnvelope, ReleaseLeaseRequest, RenewLeaseRequest, RpcRequest,
    RpcResponse, SessionRequestEnvelope, SubscriptionRequest, TransactionLookup,
    TransactionRequest, TransactionResult,
};

#[derive(Debug)]
struct DeterministicIdentities {
    next: u8,
    calls: usize,
}

impl DeterministicIdentities {
    const fn new() -> Self {
        Self { next: 1, calls: 0 }
    }
}

impl SessionIdentitySource for DeterministicIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        self.calls += 1;
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
    fail_snapshot: bool,
}

impl BridgeRpcBackend for FakeBackend {
    fn snapshot(&mut self, _session: &AuthorizedSession) -> Result<BridgeSnapshot, RpcFailure> {
        self.snapshot_calls += 1;
        if self.fail_snapshot {
            return Err(RpcFailure::new(
                ErrorCode::HfxQueue001,
                ProtocolErrorKind::QueueFull,
            ));
        }
        Ok(empty_snapshot())
    }

    fn acquire_lease(
        &mut self,
        _session: &AuthorizedSession,
        _request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn renew_lease(
        &mut self,
        _session: &AuthorizedSession,
        _request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn release_lease(
        &mut self,
        _session: &AuthorizedSession,
        _request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn submit_transaction(
        &mut self,
        _session: &AuthorizedSession,
        _request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn transaction_outcome(
        &mut self,
        _session: &AuthorizedSession,
        _request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn subscribe(
        &mut self,
        _session: &AuthorizedSession,
        _request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn diagnostics(
        &mut self,
        _session: &AuthorizedSession,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
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

fn config() -> BridgeSessionConfig {
    BridgeSessionConfig {
        server_instance_id: id::<ServerInstanceId>("server-dispatch-test"),
        bridge_version: id::<ComponentVersion>("0.0.0-test"),
        event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity must be valid"),
    }
}

fn registry() -> SessionRegistry {
    SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity must be valid"))
}

fn negotiation(client: &str, features: &[&str]) -> RpcRequest {
    RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: id::<RequestId>("request-negotiate"),
        params: hfx_protocol::ClientHello {
            client_id: id::<ClientId>(client),
            client_name: id::<ClientName>("Dispatch test"),
            minimum_version: ProtocolVersion::try_from(1).expect("version must be valid"),
            maximum_version: ProtocolVersion::try_from(2).expect("version must be valid"),
            required_features: Vec::new(),
            optional_features: features
                .iter()
                .map(|feature| id::<ProtocolFeatureId>(feature))
                .collect(),
        },
    })
}

fn snapshot_request(
    protocol_session_id: ProtocolSessionId,
    negotiation_token: NegotiationToken,
) -> RpcRequest {
    RpcRequest::Snapshot(SessionRequestEnvelope {
        request_id: id::<RequestId>("request-snapshot"),
        protocol_session_id,
        negotiation_token,
        params: EmptyRequest {},
    })
}

fn diagnostics_request(
    protocol_session_id: ProtocolSessionId,
    negotiation_token: NegotiationToken,
) -> RpcRequest {
    RpcRequest::Diagnostics(SessionRequestEnvelope {
        request_id: id::<RequestId>("request-diagnostics"),
        protocol_session_id,
        negotiation_token,
        params: EmptyRequest {},
    })
}

fn empty_snapshot() -> BridgeSnapshot {
    BridgeSnapshot {
        cursor: EventCursor {
            stream_id: id::<StreamId>("stream-test"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch must be valid"),
            projection_revision: ProjectionRevision::try_from(1_u32)
                .expect("revision must be valid"),
            sequence: SequenceNumber::try_from(0_u64).expect("sequence must be valid"),
        },
        receivers: Vec::new(),
    }
}

fn hello(response: &RpcResponse) -> hfx_protocol::ServerHello {
    let RpcResponse::NegotiateSuccess(envelope) = response else {
        panic!("response must be a negotiation success");
    };
    envelope.result.clone()
}

fn assert_error(response: &RpcResponse, code: &str, kind: ProtocolErrorKind) {
    let RpcResponse::Error(envelope) = response else {
        panic!("response must be an error");
    };
    assert_eq!(envelope.error.finding_id.as_str(), code);
    assert_eq!(envelope.error.kind, kind);
    assert_eq!(envelope.error.request_id, envelope.request_id);
}

#[test]
fn negotiation_registers_once_and_exact_replay_returns_the_same_hello() {
    let mut dispatcher = ConnectionDispatcher::new(config());
    let mut identities = DeterministicIdentities::new();
    let mut sessions = registry();
    let mut backend = FakeBackend::default();
    let request = negotiation("client-a", &[]);

    let first = dispatcher.dispatch(
        request.clone(),
        &mut identities,
        &mut sessions,
        &mut backend,
    );
    let calls = identities.calls;
    let replay = dispatcher.dispatch(request, &mut identities, &mut sessions, &mut backend);

    assert_eq!(first, replay);
    assert_eq!(identities.calls, calls);
    assert_eq!(sessions.len(), 1);
}

#[test]
fn request_before_negotiation_and_tampered_credentials_never_reach_backend() {
    let mut dispatcher = ConnectionDispatcher::new(config());
    let mut identities = DeterministicIdentities::new();
    let mut sessions = registry();
    let mut backend = FakeBackend::default();
    let before = snapshot_request(
        id::<ProtocolSessionId>("wrong-session"),
        id::<NegotiationToken>("wrong-token"),
    );
    let response = dispatcher.dispatch(before, &mut identities, &mut sessions, &mut backend);
    assert_error(
        &response,
        "HFX-REQUEST-001",
        ProtocolErrorKind::InvalidRequest,
    );

    let negotiated = dispatcher.dispatch(
        negotiation("client-a", &[]),
        &mut identities,
        &mut sessions,
        &mut backend,
    );
    let negotiated = hello(&negotiated);
    let tampered = snapshot_request(
        negotiated.protocol_session_id,
        id::<NegotiationToken>("tampered-token"),
    );
    let response = dispatcher.dispatch(tampered, &mut identities, &mut sessions, &mut backend);
    assert_error(
        &response,
        "HFX-REQUEST-001",
        ProtocolErrorKind::InvalidRequest,
    );
    assert_eq!(backend.snapshot_calls, 0);
}

#[test]
fn authorized_method_is_routed_and_backend_failure_uses_catalog_error() {
    let mut dispatcher = ConnectionDispatcher::new(config());
    let mut identities = DeterministicIdentities::new();
    let mut sessions = registry();
    let mut backend = FakeBackend::default();
    let negotiated = dispatcher.dispatch(
        negotiation("client-a", &[]),
        &mut identities,
        &mut sessions,
        &mut backend,
    );
    let negotiated = hello(&negotiated);
    let request = snapshot_request(
        negotiated.protocol_session_id.clone(),
        negotiated.negotiation_token.clone(),
    );
    let response = dispatcher.dispatch(
        request.clone(),
        &mut identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(response, RpcResponse::SnapshotSuccess(_)));

    backend.fail_snapshot = true;
    let response = dispatcher.dispatch(request, &mut identities, &mut sessions, &mut backend);
    assert_error(&response, "HFX-QUEUE-001", ProtocolErrorKind::QueueFull);
    assert_eq!(backend.snapshot_calls, 2);
}

#[test]
fn unnegotiated_feature_and_duplicate_client_fail_without_backend_access() {
    let mut first = ConnectionDispatcher::new(config());
    let mut second = ConnectionDispatcher::new(config());
    let mut first_identities = DeterministicIdentities::new();
    let mut second_identities = DeterministicIdentities {
        next: 100,
        calls: 0,
    };
    let mut sessions = registry();
    let mut backend = FakeBackend::default();
    let first_hello = first.dispatch(
        negotiation("client-a", &[]),
        &mut first_identities,
        &mut sessions,
        &mut backend,
    );
    let first_hello = hello(&first_hello);

    let diagnostics = diagnostics_request(
        first_hello.protocol_session_id,
        first_hello.negotiation_token,
    );
    let response = first.dispatch(
        diagnostics,
        &mut first_identities,
        &mut sessions,
        &mut backend,
    );
    assert_error(
        &response,
        "HFX-PROTOCOL-002",
        ProtocolErrorKind::UnsupportedFeature,
    );

    let response = second.dispatch(
        negotiation("client-a", &[]),
        &mut second_identities,
        &mut sessions,
        &mut backend,
    );
    assert_error(
        &response,
        "HFX-OWNERSHIP-001",
        ProtocolErrorKind::OwnershipConflict,
    );
    assert_eq!(sessions.len(), 1);
}

#[test]
fn disconnect_revokes_before_cleanup_and_is_idempotent() {
    let mut dispatcher = ConnectionDispatcher::new(config());
    let mut identities = DeterministicIdentities::new();
    let mut sessions = registry();
    let mut backend = FakeBackend::default();
    let negotiated = dispatcher.dispatch(
        negotiation("client-a", &[]),
        &mut identities,
        &mut sessions,
        &mut backend,
    );
    let negotiated = hello(&negotiated);
    assert_eq!(sessions.len(), 1);

    dispatcher
        .disconnect(&mut sessions, &mut backend)
        .expect("disconnect cleanup must pass");
    assert!(sessions.is_empty());
    assert_eq!(backend.disconnect_calls, 1);

    let stale = snapshot_request(negotiated.protocol_session_id, negotiated.negotiation_token);
    let response = dispatcher.dispatch(stale, &mut identities, &mut sessions, &mut backend);
    assert_error(
        &response,
        "HFX-OWNERSHIP-002",
        ProtocolErrorKind::OwnershipConflict,
    );
    dispatcher
        .disconnect(&mut sessions, &mut backend)
        .expect("second disconnect must be a no-op");
    assert_eq!(backend.disconnect_calls, 1);
}
