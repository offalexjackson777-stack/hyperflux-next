// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    BridgeSession, BridgeSessionConfig, SessionError, SessionIdentityError, SessionIdentitySource,
};
use hfx_core::SessionAuthority;
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, LeaseDurationMs, LeaseId, NegotiationToken,
    ProtocolFeatureId, ProtocolSessionId, ProtocolVersion, QueueCapacity, RequestId,
    ServerInstanceId,
};
use hfx_protocol::{
    ClientHello, EmptyRequest, NegotiationRequestEnvelope, RenewLeaseRequest, RpcRequest,
    SessionRequestEnvelope,
};

#[derive(Debug)]
struct DeterministicIdentities {
    next: u8,
    calls: usize,
    fail: bool,
}

impl DeterministicIdentities {
    const fn new() -> Self {
        Self {
            next: 1,
            calls: 0,
            fail: false,
        }
    }
}

impl SessionIdentitySource for DeterministicIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        self.calls += 1;
        if self.fail {
            return Err(SessionIdentityError::EntropyUnavailable);
        }
        for byte in destination {
            *byte = self.next;
            self.next = self.next.wrapping_add(1);
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
        server_instance_id: id::<ServerInstanceId>("server-instance-test"),
        bridge_version: id::<ComponentVersion>("0.0.0-test"),
        event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity must be valid"),
    }
}

fn negotiation(features: &[&str]) -> RpcRequest {
    RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: id::<RequestId>("request-negotiate"),
        params: ClientHello {
            client_id: id::<ClientId>("client-openrgb"),
            client_name: id::<ClientName>("OpenRGB test"),
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

fn snapshot(
    request_id: &str,
    protocol_session_id: ProtocolSessionId,
    negotiation_token: NegotiationToken,
) -> RpcRequest {
    RpcRequest::Snapshot(SessionRequestEnvelope {
        request_id: id::<RequestId>(request_id),
        protocol_session_id,
        negotiation_token,
        params: EmptyRequest {},
    })
}

fn diagnostics(
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

fn renew(
    envelope_request_id: &str,
    nested_request_id: &str,
    client_id: &str,
    protocol_session_id: ProtocolSessionId,
    negotiation_token: NegotiationToken,
) -> RpcRequest {
    RpcRequest::RenewLease(SessionRequestEnvelope {
        request_id: id::<RequestId>(envelope_request_id),
        protocol_session_id,
        negotiation_token,
        params: RenewLeaseRequest {
            request_id: id::<RequestId>(nested_request_id),
            client_id: id::<ClientId>(client_id),
            lease_id: id::<LeaseId>("lease-test"),
            duration_ms: LeaseDurationMs::try_from(1_000).expect("duration must be valid"),
        },
    })
}

#[test]
fn exact_negotiation_replay_is_idempotent_but_conflict_is_rejected() {
    let mut session = BridgeSession::new(config());
    let mut identities = DeterministicIdentities::new();
    let request = negotiation(&["ownership-leases"]);

    let first = session
        .negotiate_request(&request, &mut identities)
        .expect("first negotiation must pass");
    let calls_after_first = identities.calls;
    let replay = session
        .negotiate_request(&request, &mut identities)
        .expect("exact replay must pass");

    assert_eq!(first, replay);
    assert_eq!(identities.calls, calls_after_first);

    let conflict = negotiation(&["structured-diagnostics"]);
    assert_eq!(
        session.negotiate_request(&conflict, &mut identities),
        Err(SessionError::ConflictingNegotiation)
    );
}

#[test]
fn session_methods_require_negotiation_and_exact_credentials() {
    let mut session = BridgeSession::new(config());
    let mut identities = DeterministicIdentities::new();
    let placeholder = snapshot(
        "request-snapshot",
        id::<ProtocolSessionId>("wrong-session"),
        id::<NegotiationToken>("wrong-token"),
    );
    assert_eq!(
        session.authorize_request(&placeholder),
        Err(SessionError::NegotiationRequired)
    );

    let hello = session
        .negotiate_request(&negotiation(&[]), &mut identities)
        .expect("negotiation must pass");
    let wrong_session = snapshot(
        "request-snapshot",
        id::<ProtocolSessionId>("wrong-session"),
        hello.negotiation_token.clone(),
    );
    assert_eq!(
        session.authorize_request(&wrong_session),
        Err(SessionError::ProtocolSessionMismatch)
    );
    let wrong_token = snapshot(
        "request-snapshot",
        hello.protocol_session_id.clone(),
        id::<NegotiationToken>("wrong-token"),
    );
    assert_eq!(
        session.authorize_request(&wrong_token),
        Err(SessionError::NegotiationTokenMismatch)
    );

    let valid = snapshot(
        "request-snapshot",
        hello.protocol_session_id,
        hello.negotiation_token,
    );
    let authorized = session
        .authorize_request(&valid)
        .expect("credential-bound snapshot must pass");
    assert_eq!(authorized.client_id.as_str(), "client-openrgb");
    assert_eq!(authorized.selected_version.get(), 2);
}

#[test]
fn method_feature_client_and_nested_request_bindings_fail_closed() {
    let mut session = BridgeSession::new(config());
    let mut identities = DeterministicIdentities::new();
    let hello = session
        .negotiate_request(&negotiation(&["ownership-leases"]), &mut identities)
        .expect("negotiation must pass");

    let unavailable = diagnostics(
        hello.protocol_session_id.clone(),
        hello.negotiation_token.clone(),
    );
    assert_eq!(
        session.authorize_request(&unavailable),
        Err(SessionError::FeatureNotNegotiated)
    );

    let wrong_request = renew(
        "request-renew-envelope",
        "request-renew-payload",
        "client-openrgb",
        hello.protocol_session_id.clone(),
        hello.negotiation_token.clone(),
    );
    assert_eq!(
        session.authorize_request(&wrong_request),
        Err(SessionError::RequestIdMismatch)
    );

    let wrong_client = renew(
        "request-renew",
        "request-renew",
        "client-other",
        hello.protocol_session_id.clone(),
        hello.negotiation_token.clone(),
    );
    assert_eq!(
        session.authorize_request(&wrong_client),
        Err(SessionError::ClientIdMismatch)
    );

    let valid = renew(
        "request-renew",
        "request-renew",
        "client-openrgb",
        hello.protocol_session_id,
        hello.negotiation_token,
    );
    session
        .authorize_request(&valid)
        .expect("fully bound request must pass");
}

#[test]
fn revocation_invalidates_queued_authority_and_future_requests() {
    let mut session = BridgeSession::new(config());
    let mut identities = DeterministicIdentities::new();
    let hello = session
        .negotiate_request(&negotiation(&[]), &mut identities)
        .expect("negotiation must pass");
    let request = snapshot(
        "request-snapshot",
        hello.protocol_session_id,
        hello.negotiation_token,
    );
    let authorization = session
        .authorize_request(&request)
        .expect("request must initially be authorized");
    assert!(session.authorizes(&authorization.session_id, authorization.authorization_epoch));

    session.revoke();

    assert!(!session.is_negotiated());
    assert!(!session.authorizes(&authorization.session_id, authorization.authorization_epoch));
    assert_eq!(
        session.authorize_request(&request),
        Err(SessionError::SessionRevoked)
    );
}

#[test]
fn entropy_failure_does_not_create_a_partial_session() {
    let mut session = BridgeSession::new(config());
    let mut identities = DeterministicIdentities {
        fail: true,
        ..DeterministicIdentities::new()
    };
    let request = negotiation(&[]);

    assert_eq!(
        session.negotiate_request(&request, &mut identities),
        Err(SessionError::Identity(
            SessionIdentityError::EntropyUnavailable
        ))
    );
    assert!(!session.is_negotiated());

    identities.fail = false;
    session
        .negotiate_request(&request, &mut identities)
        .expect("a later complete identity generation must pass");
    assert!(session.is_negotiated());
}
