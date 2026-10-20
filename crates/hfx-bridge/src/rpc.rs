// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    AuthorizedSession, BridgeSession, BridgeSessionConfig, SessionError, SessionIdentitySource,
    SessionRegistry, SessionRegistryError,
};
use hfx_core::SessionAuthority;
use hfx_domain::{
    FindingId, HumanMessage, NativeRouteToken, ProtocolErrorKind, RequestId, ServerInstanceId,
};
use hfx_errors::{ErrorCode, error_by_code};
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, ErrorEnvelope, EventBatch, GENERATED_CONTRACT,
    IntegrationView, LeaseRequest, LeaseResult, NativeExchangeOutcome, NativeExchangeRequest,
    NativeRouteEventBatch, NativeRouteInventory, ProtocolContract, ReleaseLeaseRequest,
    RenewLeaseRequest, RpcError, RpcRequest, RpcResponse, SubscriptionRequest, SuccessEnvelope,
    TransactionLookup, TransactionRequest, TransactionResult,
};
use std::fmt;

/// Immutable authority available to one already-authorized backend call.
///
/// Keeping request-scoped authority in one context prevents backend method
/// signatures from drifting as connection policy gains additional metadata.
#[derive(Clone, Copy, Debug)]
pub struct BackendRequestContext<'a> {
    session: &'a AuthorizedSession,
    sessions: &'a SessionRegistry,
}

impl<'a> BackendRequestContext<'a> {
    const fn new(session: &'a AuthorizedSession, sessions: &'a SessionRegistry) -> Self {
        Self { session, sessions }
    }

    #[must_use]
    pub const fn session(self) -> &'a AuthorizedSession {
        self.session
    }

    #[must_use]
    pub const fn sessions(self) -> &'a SessionRegistry {
        self.sessions
    }
}

/// Appends the exact set of served protocol versions to the catalogued
/// incompatible-version explanation.
///
/// The frozen wire error has no detail fields, so the message is the only
/// place an old client can learn which versions to offer. Listing the set
/// instead of a minimum and maximum keeps retired versions inside that range
/// from looking served.
fn incompatible_version_explanation(explanation: &str, contract: ProtocolContract<'_>) -> String {
    let served = contract.served_versions_list().replace(',', ", ");
    format!("{explanation} Supported protocol versions: {served}.")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RpcFailure {
    code: ErrorCode,
    kind: ProtocolErrorKind,
}

impl RpcFailure {
    #[must_use]
    pub const fn new(code: ErrorCode, kind: ProtocolErrorKind) -> Self {
        Self { code, kind }
    }

    #[must_use]
    pub const fn code(self) -> ErrorCode {
        self.code
    }

    #[must_use]
    pub const fn kind(self) -> ProtocolErrorKind {
        self.kind
    }

    #[must_use]
    pub const fn internal() -> Self {
        Self::new(
            ErrorCode::HfxInternal001,
            ProtocolErrorKind::InternalFailure,
        )
    }

    fn from_session(error: &SessionError) -> Self {
        match error {
            SessionError::Negotiation(hfx_protocol::NegotiationError::IncompatibleVersion) => {
                Self::new(
                    ErrorCode::HfxProtocol001,
                    ProtocolErrorKind::IncompatibleVersion,
                )
            }
            SessionError::Negotiation(
                hfx_protocol::NegotiationError::UnsupportedRequiredFeatures(_),
            )
            | SessionError::FeatureNotNegotiated => Self::new(
                ErrorCode::HfxProtocol002,
                ProtocolErrorKind::UnsupportedFeature,
            ),
            SessionError::SessionRevoked => Self::new(
                ErrorCode::HfxOwnership002,
                ProtocolErrorKind::OwnershipConflict,
            ),
            SessionError::Identity(_) => Self::internal(),
            SessionError::Negotiation(_)
            | SessionError::ConflictingNegotiation
            | SessionError::NegotiationRequired
            | SessionError::NegotiationMethodRequired
            | SessionError::ProtocolSessionMismatch
            | SessionError::NegotiationTokenMismatch
            | SessionError::RequestIdMismatch
            | SessionError::ClientIdMismatch => {
                Self::new(ErrorCode::HfxRequest001, ProtocolErrorKind::InvalidRequest)
            }
        }
    }

    fn from_registry(error: SessionRegistryError) -> Self {
        match error {
            SessionRegistryError::CapacityExhausted => {
                Self::new(ErrorCode::HfxQueue001, ProtocolErrorKind::QueueFull)
            }
            SessionRegistryError::ClientAlreadyConnected => Self::new(
                ErrorCode::HfxOwnership001,
                ProtocolErrorKind::OwnershipConflict,
            ),
            SessionRegistryError::DuplicateSessionIdentity => Self::internal(),
        }
    }

    fn protocol_error(self, request_id: Option<RequestId>) -> RpcError {
        let descriptor = error_by_code(self.code);
        let explanation = if self.code == ErrorCode::HfxProtocol001 {
            incompatible_version_explanation(descriptor.user_explanation, GENERATED_CONTRACT)
        } else {
            descriptor.user_explanation.to_owned()
        };
        let message = HumanMessage::try_from(explanation.as_str()).unwrap_or_else(|_| {
            HumanMessage::try_from("HyperFlux rejected the request safely.")
                .expect("static fallback message satisfies the domain bound")
        });
        let finding_id = FindingId::try_from(self.code.as_str()).unwrap_or_else(|_| {
            FindingId::try_from("HFX-INTERNAL-001")
                .expect("static fallback finding satisfies the domain bound")
        });
        RpcError {
            request_id,
            kind: self.kind,
            message,
            finding_id,
        }
    }
}

impl fmt::Display for RpcFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} ({})", self.code, self.kind)
    }
}

impl std::error::Error for RpcFailure {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeExchangeRejection {
    /// The bounded actor queue had no admission capacity.
    QueueFull,
    /// The complete exchange deadline expired before SET began.
    DeadlineExceeded,
    /// The owning protocol session ended while the request was queued.
    SessionEnded,
    /// The route token belongs to a replaced receiver generation.
    StaleGeneration,
    /// The route still exists but cannot currently carry native control.
    RouteUnavailable,
    /// The route no longer has qualified native-control authority.
    RouteWithdrawn,
}

impl NativeExchangeRejection {
    #[must_use]
    pub const fn error_kind(self) -> ProtocolErrorKind {
        match self {
            Self::QueueFull => ProtocolErrorKind::QueueFull,
            Self::DeadlineExceeded => ProtocolErrorKind::DeadlineExceeded,
            Self::SessionEnded => ProtocolErrorKind::OwnershipConflict,
            Self::StaleGeneration => ProtocolErrorKind::StaleGeneration,
            Self::RouteUnavailable => ProtocolErrorKind::TransportFailure,
            Self::RouteWithdrawn => ProtocolErrorKind::UnsupportedFeature,
        }
    }

    #[must_use]
    pub const fn error_code(self) -> ErrorCode {
        match self {
            Self::QueueFull => ErrorCode::HfxQueue001,
            Self::DeadlineExceeded => ErrorCode::HfxDeadline001,
            Self::SessionEnded => ErrorCode::HfxOwnership002,
            Self::StaleGeneration => ErrorCode::HfxGeneration001,
            Self::RouteUnavailable => ErrorCode::HfxTransport001,
            Self::RouteWithdrawn => ErrorCode::HfxProfile001,
        }
    }
}

/// Application-neutral method backend used by the connection dispatcher.
///
/// Implementations own core state and transport policy. The dispatcher owns
/// protocol/session mechanics and cannot access raw receiver reports.
pub trait BridgeRpcBackend {
    /// # Errors
    ///
    /// Returns a catalog-backed failure when a snapshot cannot be projected.
    fn snapshot(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed failure when the canonical application view
    /// cannot be projected from the same snapshot and profile authority.
    fn integration_view(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed lease admission failure.
    fn acquire_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed renewal failure.
    fn renew_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed release failure.
    fn release_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed transaction admission failure.
    fn submit_transaction(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed outcome lookup failure.
    fn transaction_outcome(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed subscription failure.
    fn subscribe(
        &mut self,
        context: BackendRequestContext<'_>,
        request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure>;

    /// # Errors
    ///
    /// Returns a catalog-backed diagnostic projection failure.
    fn diagnostics(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure>;

    /// Enumerates generation-bound receiver child routes without host paths.
    ///
    /// # Errors
    ///
    /// The default rejects the additive method as unsupported.
    fn native_enumerate(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<NativeRouteInventory, RpcFailure> {
        Err(RpcFailure::new(
            ErrorCode::HfxProtocol002,
            ProtocolErrorKind::UnsupportedFeature,
        ))
    }

    /// Performs one exact lease-bound opaque receiver child exchange.
    ///
    /// The backend authenticates the local session and applies the exact
    /// device's complete safe command-policy union. Client-supplied names are
    /// diagnostic metadata, never application-specific authorization.
    ///
    /// # Errors
    ///
    /// The default rejects the additive method as unsupported.
    fn native_transact(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Err(RpcFailure::new(
            ErrorCode::HfxProtocol002,
            ProtocolErrorKind::UnsupportedFeature,
        ))
    }

    /// Retains one retry-safe native result for work rejected before SET.
    ///
    /// Actor admission, absolute deadline, and disconnect cancellation use
    /// this path so a repeated request identity cannot later reach hardware.
    ///
    /// # Errors
    ///
    /// The default rejects the additive method as unsupported.
    fn reject_native_exchange(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: NativeExchangeRequest,
        _rejection: NativeExchangeRejection,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Err(RpcFailure::new(
            ErrorCode::HfxProtocol002,
            ProtocolErrorKind::UnsupportedFeature,
        ))
    }

    /// Drains bounded route-token invalidations for the actor scheduler.
    ///
    /// Backends without a native route authority have no invalidations. The
    /// production backend keeps rejection tombstones after this notification
    /// is consumed, so draining it cannot restore old authority.
    fn take_native_route_invalidations(
        &mut self,
    ) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
        Vec::new()
    }

    /// Reads bounded native-route lifecycle changes after one cursor.
    ///
    /// # Errors
    ///
    /// The default rejects the additive method as unsupported.
    fn native_watch(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: SubscriptionRequest,
    ) -> Result<NativeRouteEventBatch, RpcFailure> {
        Err(RpcFailure::new(
            ErrorCode::HfxProtocol002,
            ProtocolErrorKind::UnsupportedFeature,
        ))
    }

    /// Releases client-owned resources and invalidates queued work after the
    /// registry has already revoked session authority.
    ///
    /// # Errors
    ///
    /// Returns a catalog-backed cleanup failure after authority is revoked.
    fn disconnect(&mut self, session: &AuthorizedSession) -> Result<(), RpcFailure>;
}

#[derive(Clone, Debug)]
pub struct ConnectionDispatcher {
    server_instance_id: ServerInstanceId,
    session: BridgeSession,
    registered: Option<AuthorizedSession>,
}

impl ConnectionDispatcher {
    #[must_use]
    pub fn new(config: BridgeSessionConfig) -> Self {
        Self {
            server_instance_id: config.server_instance_id.clone(),
            session: BridgeSession::new(config),
            registered: None,
        }
    }

    /// Routes one typed request and always returns one typed response.
    ///
    /// Session and backend failures become privacy-safe central-catalog errors;
    /// they are never exposed through debug strings or unstructured payloads.
    pub fn dispatch<S, B>(
        &mut self,
        request: RpcRequest,
        identities: &mut S,
        sessions: &mut SessionRegistry,
        backend: &mut B,
    ) -> RpcResponse
    where
        S: SessionIdentitySource,
        B: BridgeRpcBackend,
    {
        let request_id = request.request_id().clone();
        if matches!(request, RpcRequest::Negotiate(_)) {
            return self.dispatch_negotiation(&request, request_id, identities, sessions);
        }

        let authorization = match self.authorize(&request, sessions) {
            Ok(authorization) => authorization,
            Err(failure) => return self.error_response(Some(request_id), failure),
        };

        let error_request_id = request_id.clone();
        let context = BackendRequestContext::new(&authorization, sessions);
        let response = match request {
            RpcRequest::Snapshot(_) => backend
                .snapshot(context)
                .map(|result| RpcResponse::SnapshotSuccess(self.success(request_id, result))),
            RpcRequest::IntegrationView(_) => backend.integration_view(context).map(|result| {
                RpcResponse::IntegrationViewSuccess(self.success(request_id, result))
            }),
            RpcRequest::AcquireLease(envelope) => backend
                .acquire_lease(context, envelope.params)
                .map(|result| RpcResponse::AcquireLeaseSuccess(self.success(request_id, result))),
            RpcRequest::RenewLease(envelope) => backend
                .renew_lease(context, envelope.params)
                .map(|result| RpcResponse::RenewLeaseSuccess(self.success(request_id, result))),
            RpcRequest::ReleaseLease(envelope) => backend
                .release_lease(context, envelope.params)
                .map(|result| RpcResponse::ReleaseLeaseSuccess(self.success(request_id, result))),
            RpcRequest::SubmitTransaction(envelope) => backend
                .submit_transaction(context, envelope.params)
                .map(|result| {
                    RpcResponse::SubmitTransactionSuccess(self.success(request_id, result))
                }),
            RpcRequest::TransactionOutcome(envelope) => backend
                .transaction_outcome(context, envelope.params)
                .map(|result| {
                    RpcResponse::TransactionOutcomeSuccess(self.success(request_id, result))
                }),
            RpcRequest::Subscribe(envelope) => backend
                .subscribe(context, envelope.params)
                .map(|result| RpcResponse::SubscribeSuccess(self.success(request_id, result))),
            RpcRequest::Diagnostics(_) => backend
                .diagnostics(context)
                .map(|result| RpcResponse::DiagnosticsSuccess(self.success(request_id, result))),
            RpcRequest::NativeEnumerate(_) => backend.native_enumerate(context).map(|result| {
                RpcResponse::NativeEnumerateSuccess(self.success(request_id, result))
            }),
            RpcRequest::NativeTransact(envelope) => backend
                .native_transact(context, envelope.params)
                .map(|result| RpcResponse::NativeTransactSuccess(self.success(request_id, result))),
            RpcRequest::NativeWatch(envelope) => backend
                .native_watch(context, envelope.params)
                .map(|result| RpcResponse::NativeWatchSuccess(self.success(request_id, result))),
            RpcRequest::Negotiate(_) => unreachable!("negotiation returned before method routing"),
        };
        response.unwrap_or_else(|failure| self.error_response(Some(error_request_id), failure))
    }

    pub(crate) fn deferred_native_authorized(
        &self,
        request: &RpcRequest,
        sessions: &SessionRegistry,
    ) -> Result<(), RpcFailure> {
        if !matches!(request, RpcRequest::NativeTransact(_)) {
            return Err(RpcFailure::new(
                ErrorCode::HfxRequest001,
                ProtocolErrorKind::InvalidRequest,
            ));
        }
        self.authorize(request, sessions).map(|_| ())
    }

    pub(crate) fn failure_response(
        &self,
        request_id: RequestId,
        failure: RpcFailure,
    ) -> RpcResponse {
        self.error_response(Some(request_id), failure)
    }

    pub(crate) fn dispatch_native_rejection<B>(
        &mut self,
        request: RpcRequest,
        sessions: &SessionRegistry,
        backend: &mut B,
        rejection: NativeExchangeRejection,
    ) -> RpcResponse
    where
        B: BridgeRpcBackend,
    {
        let request_id = request.request_id().clone();
        let authorization = match self.authorize(&request, sessions) {
            Ok(authorization) => authorization,
            Err(failure) => return self.error_response(Some(request_id), failure),
        };
        let RpcRequest::NativeTransact(envelope) = request else {
            return self.error_response(
                Some(request_id),
                RpcFailure::new(ErrorCode::HfxRequest001, ProtocolErrorKind::InvalidRequest),
            );
        };
        let context = BackendRequestContext::new(&authorization, sessions);
        match backend.reject_native_exchange(context, envelope.params, rejection) {
            Ok(result) => RpcResponse::NativeTransactSuccess(self.success(request_id, result)),
            Err(failure) => self.error_response(Some(request_id), failure),
        }
    }

    fn authorize(
        &self,
        request: &RpcRequest,
        sessions: &SessionRegistry,
    ) -> Result<AuthorizedSession, RpcFailure> {
        let authorization = self
            .session
            .authorize_request(request)
            .map_err(|error| RpcFailure::from_session(&error))?;
        if !sessions.authorizes(&authorization.session_id, authorization.authorization_epoch) {
            return Err(RpcFailure::from_session(&SessionError::SessionRevoked));
        }
        Ok(authorization)
    }

    fn dispatch_negotiation<S: SessionIdentitySource>(
        &mut self,
        request: &RpcRequest,
        request_id: RequestId,
        identities: &mut S,
        sessions: &mut SessionRegistry,
    ) -> RpcResponse {
        let hello = match self.session.negotiate_request(request, identities) {
            Ok(hello) => hello,
            Err(error) => {
                return self.error_response(Some(request_id), RpcFailure::from_session(&error));
            }
        };

        if let Some(registered) = &self.registered {
            if !sessions.authorizes(&registered.session_id, registered.authorization_epoch) {
                self.session.revoke();
                return self.error_response(
                    Some(request_id),
                    RpcFailure::from_session(&SessionError::SessionRevoked),
                );
            }
        } else {
            let authorization = match self.session.authorization() {
                Ok(authorization) => authorization,
                Err(error) => {
                    return self.error_response(Some(request_id), RpcFailure::from_session(&error));
                }
            };
            if let Err(error) = sessions.register(authorization.clone()) {
                self.session.revoke();
                return self.error_response(Some(request_id), RpcFailure::from_registry(error));
            }
            self.registered = Some(authorization);
        }

        RpcResponse::NegotiateSuccess(self.success(request_id, hello))
    }

    /// Revokes authority first, then asks the backend to release leases and
    /// terminally account for queued work. Calling this more than once is safe.
    ///
    /// # Errors
    ///
    /// Returns a backend cleanup failure after authority has already ended.
    pub fn disconnect<B: BridgeRpcBackend>(
        &mut self,
        sessions: &mut SessionRegistry,
        backend: &mut B,
    ) -> Result<(), RpcFailure> {
        self.session.revoke();
        let Some(authorization) = self.registered.take() else {
            return Ok(());
        };
        let _ = sessions.revoke(&authorization.session_id);
        backend.disconnect(&authorization)
    }

    fn success<T>(&self, request_id: RequestId, result: T) -> SuccessEnvelope<T> {
        SuccessEnvelope {
            request_id,
            server_instance_id: self.server_instance_id.clone(),
            result,
        }
    }

    fn error_response(&self, request_id: Option<RequestId>, failure: RpcFailure) -> RpcResponse {
        RpcResponse::Error(ErrorEnvelope {
            request_id: request_id.clone(),
            server_instance_id: self.server_instance_id.clone(),
            error: failure.protocol_error(request_id),
        })
    }
}
