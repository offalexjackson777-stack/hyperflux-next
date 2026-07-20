// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{SessionAuthority, SubmissionBinding};
use hfx_domain::{
    AuthorizationEpoch, ClientId, ComponentVersion, DispatchNonce, NegotiationToken,
    ProtocolSessionId, ProtocolVersion, QueueCapacity, ServerInstanceId, SessionId,
};
use hfx_protocol::{
    NegotiationContext, NegotiationError, NegotiationRequestEnvelope, RpcRequest, ServerHello,
    negotiate,
};
use rustix::io::Errno;
use rustix::rand::{GetRandomFlags, getrandom};
use std::fmt;

const SHORT_ID_BYTES: usize = 16;
const TOKEN_BYTES: usize = 32;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BridgeSessionConfig {
    pub server_instance_id: ServerInstanceId,
    pub bridge_version: ComponentVersion,
    pub event_buffer_capacity: QueueCapacity,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionIdentityError {
    EntropyUnavailable,
    InvalidGeneratedIdentity,
}

impl fmt::Display for SessionIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::EntropyUnavailable => "operating-system identity entropy is unavailable",
            Self::InvalidGeneratedIdentity => "generated session identity is invalid",
        })
    }
}

impl std::error::Error for SessionIdentityError {}

pub trait SessionIdentitySource {
    /// Fills the complete destination or returns an error without claiming that
    /// any identity was issued.
    ///
    /// # Errors
    ///
    /// Returns an error when strong identity material cannot be obtained.
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError>;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct KernelSessionIdentitySource;

impl SessionIdentitySource for KernelSessionIdentitySource {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        let mut filled = 0;
        while filled < destination.len() {
            match getrandom(&mut destination[filled..], GetRandomFlags::empty()) {
                Ok(0) => return Err(SessionIdentityError::EntropyUnavailable),
                Ok(initialized) => filled += initialized,
                Err(Errno::INTR) => {}
                Err(_) => return Err(SessionIdentityError::EntropyUnavailable),
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionError {
    Identity(SessionIdentityError),
    Negotiation(NegotiationError),
    ConflictingNegotiation,
    NegotiationRequired,
    NegotiationMethodRequired,
    SessionRevoked,
    ProtocolSessionMismatch,
    NegotiationTokenMismatch,
    FeatureNotNegotiated,
    RequestIdMismatch,
    ClientIdMismatch,
}

impl fmt::Display for SessionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Identity(_) => "bridge session identity generation failed",
            Self::Negotiation(_) => "bridge protocol negotiation failed",
            Self::ConflictingNegotiation => "connection was renegotiated with a different request",
            Self::NegotiationRequired => "connection must negotiate before this request",
            Self::NegotiationMethodRequired => "the negotiation API requires a negotiate request",
            Self::SessionRevoked => "bridge session is no longer authorized",
            Self::ProtocolSessionMismatch => "request belongs to another protocol session",
            Self::NegotiationTokenMismatch => "request carries an invalid negotiation credential",
            Self::FeatureNotNegotiated => "request requires a feature not enabled for this session",
            Self::RequestIdMismatch => "request envelope and method payload identities differ",
            Self::ClientIdMismatch => "request belongs to another client",
        })
    }
}

impl std::error::Error for SessionError {}

impl From<SessionIdentityError> for SessionError {
    fn from(error: SessionIdentityError) -> Self {
        Self::Identity(error)
    }
}

impl From<NegotiationError> for SessionError {
    fn from(error: NegotiationError) -> Self {
        Self::Negotiation(error)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthorizedSession {
    pub client_id: ClientId,
    pub selected_version: ProtocolVersion,
    pub session_id: SessionId,
    pub authorization_epoch: AuthorizationEpoch,
}

impl AuthorizedSession {
    #[must_use]
    pub fn submission_binding(&self, dispatch_nonce: DispatchNonce) -> SubmissionBinding {
        SubmissionBinding {
            session_id: self.session_id.clone(),
            authorization_epoch: self.authorization_epoch,
            dispatch_nonce,
        }
    }
}

#[derive(Clone, Debug)]
struct NegotiatedSession {
    request: NegotiationRequestEnvelope,
    hello: ServerHello,
    authorization: AuthorizedSession,
    active: bool,
}

#[derive(Clone, Debug)]
pub struct BridgeSession {
    config: BridgeSessionConfig,
    negotiated: Option<NegotiatedSession>,
}

impl BridgeSession {
    #[must_use]
    pub const fn new(config: BridgeSessionConfig) -> Self {
        Self {
            config,
            negotiated: None,
        }
    }

    /// Negotiates this connection once. Repeating the exact same request is an
    /// idempotent replay; any different second request is rejected.
    ///
    /// # Errors
    ///
    /// Returns a negotiation, identity-generation, conflict, or method error.
    pub fn negotiate_request<S: SessionIdentitySource>(
        &mut self,
        request: &RpcRequest,
        identities: &mut S,
    ) -> Result<ServerHello, SessionError> {
        let RpcRequest::Negotiate(envelope) = request else {
            return Err(SessionError::NegotiationMethodRequired);
        };
        if let Some(existing) = &self.negotiated {
            if !existing.active {
                return Err(SessionError::SessionRevoked);
            }
            if existing.request == *envelope {
                return Ok(existing.hello.clone());
            }
            return Err(SessionError::ConflictingNegotiation);
        }

        let protocol_session_id = generated_string_identity::<ProtocolSessionId, _>(
            "protocol-session-",
            SHORT_ID_BYTES,
            identities,
        )?;
        let negotiation_token = generated_string_identity::<NegotiationToken, _>(
            "negotiation-",
            TOKEN_BYTES,
            identities,
        )?;
        let internal_session_id = generated_string_identity::<SessionId, _>(
            "bridge-session-",
            SHORT_ID_BYTES,
            identities,
        )?;
        let authorization_epoch = generated_epoch(identities)?;
        let hello = negotiate(
            &envelope.params,
            NegotiationContext {
                server_instance_id: self.config.server_instance_id.clone(),
                protocol_session_id,
                negotiation_token,
                bridge_version: self.config.bridge_version.clone(),
                event_buffer_capacity: self.config.event_buffer_capacity,
            },
        )?;
        let authorization = AuthorizedSession {
            client_id: envelope.params.client_id.clone(),
            selected_version: hello.selected_version,
            session_id: internal_session_id,
            authorization_epoch,
        };
        self.negotiated = Some(NegotiatedSession {
            request: envelope.clone(),
            hello: hello.clone(),
            authorization,
            active: true,
        });
        Ok(hello)
    }

    /// Verifies protocol credentials, negotiated feature access, request
    /// identity, and client ownership for one session-bound request.
    ///
    /// # Errors
    ///
    /// Returns a fail-closed session binding error.
    pub fn authorize_request(
        &self,
        request: &RpcRequest,
    ) -> Result<AuthorizedSession, SessionError> {
        let negotiated = self
            .negotiated
            .as_ref()
            .ok_or(SessionError::NegotiationRequired)?;
        if !negotiated.active {
            return Err(SessionError::SessionRevoked);
        }
        let Some((protocol_session_id, token)) = request.session_credentials() else {
            return Err(SessionError::ConflictingNegotiation);
        };
        if protocol_session_id != &negotiated.hello.protocol_session_id {
            return Err(SessionError::ProtocolSessionMismatch);
        }
        if !constant_time_equal(
            token.as_str().as_bytes(),
            negotiated.hello.negotiation_token.as_str().as_bytes(),
        ) {
            return Err(SessionError::NegotiationTokenMismatch);
        }
        if let Some(required) = request.method_descriptor().required_feature
            && !negotiated
                .hello
                .enabled_features
                .iter()
                .any(|feature| feature.as_str() == required)
        {
            return Err(SessionError::FeatureNotNegotiated);
        }
        validate_nested_request_id(request)?;
        validate_client_id(request, &negotiated.authorization.client_id)?;
        Ok(negotiated.authorization.clone())
    }

    pub fn revoke(&mut self) {
        if let Some(negotiated) = &mut self.negotiated {
            negotiated.active = false;
        }
    }

    /// Returns the internal authority record to register with the bridge-wide
    /// session authority after successful negotiation.
    ///
    /// # Errors
    ///
    /// Returns an error before negotiation or after revocation.
    pub fn authorization(&self) -> Result<AuthorizedSession, SessionError> {
        let negotiated = self
            .negotiated
            .as_ref()
            .ok_or(SessionError::NegotiationRequired)?;
        if !negotiated.active {
            return Err(SessionError::SessionRevoked);
        }
        Ok(negotiated.authorization.clone())
    }

    #[must_use]
    pub fn is_negotiated(&self) -> bool {
        self.negotiated.as_ref().is_some_and(|state| state.active)
    }
}

impl SessionAuthority for BridgeSession {
    fn authorizes(&self, session_id: &SessionId, authorization_epoch: AuthorizationEpoch) -> bool {
        self.negotiated.as_ref().is_some_and(|state| {
            state.active
                && &state.authorization.session_id == session_id
                && state.authorization.authorization_epoch == authorization_epoch
        })
    }
}

fn validate_nested_request_id(request: &RpcRequest) -> Result<(), SessionError> {
    let nested = match request {
        RpcRequest::AcquireLease(envelope) => Some(&envelope.params.request_id),
        RpcRequest::RenewLease(envelope) => Some(&envelope.params.request_id),
        RpcRequest::ReleaseLease(envelope) => Some(&envelope.params.request_id),
        RpcRequest::SubmitTransaction(envelope) => Some(&envelope.params.request_id),
        RpcRequest::TransactionOutcome(envelope) => Some(&envelope.params.request_id),
        RpcRequest::Negotiate(_)
        | RpcRequest::Snapshot(_)
        | RpcRequest::Subscribe(_)
        | RpcRequest::Diagnostics(_) => None,
    };
    if nested.is_some_and(|request_id| request_id != request.request_id()) {
        return Err(SessionError::RequestIdMismatch);
    }
    Ok(())
}

fn validate_client_id(request: &RpcRequest, expected: &ClientId) -> Result<(), SessionError> {
    let actual = match request {
        RpcRequest::AcquireLease(envelope) => Some(&envelope.params.client_id),
        RpcRequest::RenewLease(envelope) => Some(&envelope.params.client_id),
        RpcRequest::ReleaseLease(envelope) => Some(&envelope.params.client_id),
        RpcRequest::SubmitTransaction(envelope) => Some(&envelope.params.client_id),
        RpcRequest::TransactionOutcome(envelope) => Some(&envelope.params.client_id),
        RpcRequest::Subscribe(envelope) => Some(&envelope.params.client_id),
        RpcRequest::Negotiate(_) | RpcRequest::Snapshot(_) | RpcRequest::Diagnostics(_) => None,
    };
    if actual.is_some_and(|client_id| client_id != expected) {
        return Err(SessionError::ClientIdMismatch);
    }
    Ok(())
}

fn generated_epoch<S: SessionIdentitySource>(
    identities: &mut S,
) -> Result<AuthorizationEpoch, SessionIdentityError> {
    let mut bytes = [0_u8; size_of::<u64>()];
    identities.fill_bytes(&mut bytes)?;
    let value = u64::from_be_bytes(bytes).max(AuthorizationEpoch::MIN);
    AuthorizationEpoch::try_from(value).map_err(|_| SessionIdentityError::InvalidGeneratedIdentity)
}

fn generated_string_identity<T, S>(
    prefix: &str,
    byte_count: usize,
    identities: &mut S,
) -> Result<T, SessionIdentityError>
where
    T: TryFrom<String>,
    S: SessionIdentitySource,
{
    let mut bytes = vec![0_u8; byte_count];
    identities.fill_bytes(&mut bytes)?;
    let mut value = String::with_capacity(prefix.len() + byte_count * 2);
    value.push_str(prefix);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(value, "{byte:02x}").map_err(|_| SessionIdentityError::InvalidGeneratedIdentity)?;
    }
    T::try_from(value).map_err(|_| SessionIdentityError::InvalidGeneratedIdentity)
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (left, right)| {
            difference | (left ^ right)
        })
        == 0
}
