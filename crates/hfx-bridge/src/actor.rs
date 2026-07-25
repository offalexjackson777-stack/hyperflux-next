// SPDX-License-Identifier: GPL-2.0-only

use crate::native_scheduler::{FairRouteQueue, FairRouteQueueError};
use crate::{
    BridgeRpcBackend, BridgeSessionConfig, ConnectionDispatcher, ConnectionServeReport,
    NativeExchangeRejection, RpcFailure, SessionIdentitySource, SessionRegistry,
};
use hfx_domain::{NativeExchangeTimeoutMs, NativeRouteToken, ProtocolVersion, QueueCapacity};
use hfx_protocol::{
    FrameError, RpcRequest, RpcResponse, read_rpc_request, read_rpc_request_for_version,
    write_rpc_response, write_rpc_response_for_version,
};
use std::collections::BTreeMap;
use std::convert::Infallible;
use std::fmt;
use std::io::{Read, Write};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TryRecvError, TrySendError};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct BridgeConnectionId(u64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BridgeActorLimits {
    pub command_capacity: QueueCapacity,
    pub connection_capacity: QueueCapacity,
    pub native_transaction_capacity: QueueCapacity,
    pub response_timeout: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeActorStartError {
    ZeroResponseTimeout,
    InvalidNativeTransactionCapacity,
}

impl fmt::Display for BridgeActorStartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::ZeroResponseTimeout => "bridge actor response timeout must be positive",
            Self::InvalidNativeTransactionCapacity => {
                "bridge actor native transaction capacity is invalid"
            }
        })
    }
}

impl std::error::Error for BridgeActorStartError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeActorError {
    Busy,
    Unavailable,
    TimedOut,
    ConnectionCapacityExhausted,
    UnknownConnection,
    Cleanup(RpcFailure),
}

impl fmt::Display for BridgeActorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Busy => "bridge actor command queue is full",
            Self::Unavailable => "bridge actor is unavailable",
            Self::TimedOut => "bridge actor response timed out",
            Self::ConnectionCapacityExhausted => "bridge connection capacity is exhausted",
            Self::UnknownConnection => "bridge connection is no longer active",
            Self::Cleanup(_) => "bridge connection cleanup failed",
        })
    }
}

impl std::error::Error for BridgeActorError {}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BridgeActorExit {
    pub connections_cleaned: usize,
    pub cleanup_failures: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BridgeActorTickFailure<E> {
    pub error: E,
    pub cleanup: BridgeActorExit,
}

enum BridgeActorCommand {
    Open {
        reply: SyncSender<Result<BridgeConnectionId, BridgeActorError>>,
    },
    Request {
        connection_id: BridgeConnectionId,
        request: Box<RpcRequest>,
        reply: SyncSender<Result<RpcResponse, BridgeActorError>>,
    },
    Disconnect {
        connection_id: BridgeConnectionId,
        reply: SyncSender<Result<(), BridgeActorError>>,
    },
    Shutdown {
        reply: SyncSender<BridgeActorExit>,
    },
}

struct PendingNativeCommand {
    connection_id: BridgeConnectionId,
    route_token: NativeRouteToken,
    request: Box<RpcRequest>,
    reply: SyncSender<Result<RpcResponse, BridgeActorError>>,
    deadline: Instant,
}

#[derive(Clone)]
pub struct BridgeActorHandle {
    sender: SyncSender<BridgeActorCommand>,
    response_timeout: Duration,
}

impl BridgeActorHandle {
    /// Opens one independent protocol connection in the shared actor.
    ///
    /// # Errors
    ///
    /// Returns a bounded queue, capacity, timeout, or availability failure.
    pub fn open_connection(&self) -> Result<BridgeConnectionId, BridgeActorError> {
        let (reply, response) = mpsc::sync_channel(1);
        self.send(BridgeActorCommand::Open { reply })?;
        response
            .recv_timeout(self.response_timeout)
            .map_err(map_receive_error)?
    }

    /// Dispatches one already-framed request through the state-owning actor.
    ///
    /// # Errors
    ///
    /// Returns a bounded actor failure. Protocol failures remain typed error
    /// responses.
    pub fn request(
        &self,
        connection_id: BridgeConnectionId,
        request: RpcRequest,
    ) -> Result<RpcResponse, BridgeActorError> {
        let (reply, response) = mpsc::sync_channel(1);
        self.send(BridgeActorCommand::Request {
            connection_id,
            request: Box::new(request),
            reply,
        })?;
        response
            .recv_timeout(self.response_timeout)
            .map_err(map_receive_error)?
    }

    /// Revokes one connection before releasing backend-owned resources.
    ///
    /// # Errors
    ///
    /// Returns a bounded actor or backend cleanup failure.
    pub fn disconnect(&self, connection_id: BridgeConnectionId) -> Result<(), BridgeActorError> {
        let (reply, response) = mpsc::sync_channel(1);
        self.send(BridgeActorCommand::Disconnect {
            connection_id,
            reply,
        })?;
        response
            .recv_timeout(self.response_timeout)
            .map_err(map_receive_error)?
    }

    /// Requests graceful actor shutdown and waits for bounded cleanup.
    ///
    /// # Errors
    ///
    /// Returns a queue, timeout, or availability failure.
    pub fn shutdown(&self) -> Result<BridgeActorExit, BridgeActorError> {
        let (reply, response) = mpsc::sync_channel(1);
        self.send(BridgeActorCommand::Shutdown { reply })?;
        response
            .recv_timeout(self.response_timeout)
            .map_err(map_receive_error)
    }

    fn send(&self, command: BridgeActorCommand) -> Result<(), BridgeActorError> {
        self.sender.try_send(command).map_err(|error| match error {
            TrySendError::Full(_) => BridgeActorError::Busy,
            TrySendError::Disconnected(_) => BridgeActorError::Unavailable,
        })
    }
}

pub struct BridgeActor<I, B> {
    config: BridgeSessionConfig,
    identities: I,
    sessions: SessionRegistry,
    backend: B,
    commands: Receiver<BridgeActorCommand>,
    dispatchers: BTreeMap<BridgeConnectionId, ConnectionDispatcher>,
    native_pending: FairRouteQueue<NativeRouteToken, PendingNativeCommand>,
    command_batch_capacity: usize,
    connection_capacity: usize,
    next_connection_id: u64,
}

impl<I, B> BridgeActor<I, B>
where
    I: SessionIdentitySource,
    B: BridgeRpcBackend,
{
    /// Creates a bounded state-owning actor and its clonable command handle.
    ///
    /// # Errors
    ///
    /// Rejects an unbounded zero response timeout.
    pub fn new(
        config: BridgeSessionConfig,
        identities: I,
        sessions: SessionRegistry,
        backend: B,
        limits: BridgeActorLimits,
    ) -> Result<(Self, BridgeActorHandle), BridgeActorStartError> {
        if limits.response_timeout.is_zero() {
            return Err(BridgeActorStartError::ZeroResponseTimeout);
        }
        let native_pending =
            FairRouteQueue::new(usize::from(limits.native_transaction_capacity.get()))
                .map_err(|_| BridgeActorStartError::InvalidNativeTransactionCapacity)?;
        let (sender, commands) = mpsc::sync_channel(usize::from(limits.command_capacity.get()));
        Ok((
            Self {
                config,
                identities,
                sessions,
                backend,
                commands,
                dispatchers: BTreeMap::new(),
                native_pending,
                command_batch_capacity: usize::from(limits.command_capacity.get()),
                connection_capacity: usize::from(limits.connection_capacity.get()),
                next_connection_id: 1,
            },
            BridgeActorHandle {
                sender,
                response_timeout: limits.response_timeout,
            },
        ))
    }

    /// Runs until explicit shutdown or every command handle is gone.
    #[must_use]
    pub fn run(self) -> BridgeActorExit {
        self.run_with_tick(Duration::from_secs(1), |_| true)
    }

    /// Runs with one bounded callback for discovery and lifecycle work.
    /// Returning `false` from the callback performs the same cleanup as a
    /// graceful shutdown.
    #[must_use]
    pub fn run_with_tick<F>(self, interval: Duration, mut tick: F) -> BridgeActorExit
    where
        F: FnMut(&mut B) -> bool,
    {
        match self
            .run_with_runtime_tick(interval, |backend, _| Ok::<bool, Infallible>(tick(backend)))
        {
            Ok(exit) => exit,
            Err(failure) => match failure.error {},
        }
    }

    /// Runs bounded runtime work after each command and on idle intervals.
    /// The callback receives the shared session registry so queued transport
    /// work can recheck exact authority without duplicating it elsewhere.
    ///
    /// # Errors
    ///
    /// A callback failure first performs complete connection cleanup and then
    /// returns both the original error and cleanup report.
    pub fn run_with_runtime_tick<F, E>(
        mut self,
        interval: Duration,
        mut tick: F,
    ) -> Result<BridgeActorExit, BridgeActorTickFailure<E>>
    where
        F: FnMut(&mut B, &SessionRegistry) -> Result<bool, E>,
    {
        let interval = if interval.is_zero() {
            Duration::from_millis(1)
        } else {
            interval
        };
        loop {
            let command = if self.native_pending.is_empty() {
                match self.commands.recv_timeout(interval) {
                    Ok(command) => Some(command),
                    Err(RecvTimeoutError::Timeout) => None,
                    Err(RecvTimeoutError::Disconnected) => return Ok(self.cleanup_all()),
                }
            } else {
                match self.commands.try_recv() {
                    Ok(command) => Some(command),
                    Err(TryRecvError::Empty) => None,
                    Err(TryRecvError::Disconnected) => return Ok(self.cleanup_all()),
                }
            };

            let mut processed_command = false;
            if let Some(mut command) = command {
                for batch_index in 0..self.command_batch_capacity {
                    if let Some(reply) = self.process(command) {
                        let exit = self.cleanup_all();
                        let _ = reply.send(exit);
                        return Ok(exit);
                    }
                    processed_command = true;
                    match tick(&mut self.backend, &self.sessions) {
                        Ok(true) => self.cancel_invalidated_native(),
                        Ok(false) => return Ok(self.cleanup_all()),
                        Err(error) => {
                            let cleanup = self.cleanup_all();
                            return Err(BridgeActorTickFailure { error, cleanup });
                        }
                    }
                    if batch_index + 1 == self.command_batch_capacity {
                        break;
                    }
                    match self.commands.try_recv() {
                        Ok(next) => command = next,
                        Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
                    }
                }
            }
            self.cancel_invalidated_native();
            self.dispatch_next_native();
            self.cancel_invalidated_native();
            if !processed_command {
                match tick(&mut self.backend, &self.sessions) {
                    Ok(true) => self.cancel_invalidated_native(),
                    Ok(false) => return Ok(self.cleanup_all()),
                    Err(error) => {
                        let cleanup = self.cleanup_all();
                        return Err(BridgeActorTickFailure { error, cleanup });
                    }
                }
            }
        }
    }

    fn process(&mut self, command: BridgeActorCommand) -> Option<SyncSender<BridgeActorExit>> {
        match command {
            BridgeActorCommand::Open { reply } => {
                let _ = reply.send(self.open());
            }
            BridgeActorCommand::Request {
                connection_id,
                request,
                reply,
            } => {
                self.process_request(connection_id, request, reply);
            }
            BridgeActorCommand::Disconnect {
                connection_id,
                reply,
            } => {
                let _ = reply.send(self.close(connection_id));
            }
            BridgeActorCommand::Shutdown { reply } => return Some(reply),
        }
        None
    }

    fn process_request(
        &mut self,
        connection_id: BridgeConnectionId,
        request: Box<RpcRequest>,
        reply: SyncSender<Result<RpcResponse, BridgeActorError>>,
    ) {
        let Some(dispatcher) = self.dispatchers.get_mut(&connection_id) else {
            let _ = reply.send(Err(BridgeActorError::UnknownConnection));
            return;
        };
        let RpcRequest::NativeTransact(envelope) = request.as_ref() else {
            let response = dispatcher.dispatch(
                *request,
                &mut self.identities,
                &mut self.sessions,
                &mut self.backend,
            );
            let _ = reply.send(Ok(response));
            return;
        };
        if let Err(failure) = dispatcher.deferred_native_authorized(&request, &self.sessions) {
            let response = dispatcher.failure_response(request.request_id().clone(), failure);
            let _ = reply.send(Ok(response));
            return;
        }

        let route_token = envelope.params.route_token.clone();
        let deadline = Instant::now()
            .checked_add(Duration::from_millis(u64::from(
                envelope.params.timeout_ms.get(),
            )))
            .expect("canonical native timeout fits Instant");
        let pending = PendingNativeCommand {
            connection_id,
            route_token: route_token.clone(),
            request,
            reply,
            deadline,
        };
        match self.native_pending.admit(route_token, pending) {
            Ok(()) => {}
            Err((FairRouteQueueError::Full, pending)) => {
                self.reject_pending_native(pending, NativeExchangeRejection::QueueFull);
            }
            Err((FairRouteQueueError::InvalidCapacity, pending)) => {
                debug_assert!(false, "validated native queue became invalid");
                self.reject_pending_native(pending, NativeExchangeRejection::QueueFull);
            }
        }
    }

    fn dispatch_next_native(&mut self) {
        let Some(mut pending) = self.native_pending.pop_next() else {
            return;
        };
        let now = Instant::now();
        let remaining = pending.deadline.saturating_duration_since(now);
        if remaining < Duration::from_millis(1) {
            self.reject_pending_native(pending, NativeExchangeRejection::DeadlineExceeded);
            return;
        }
        let remaining_ms = u16::try_from(remaining.as_millis().min(1_000))
            .expect("bounded native timeout fits u16");
        let RpcRequest::NativeTransact(envelope) = pending.request.as_mut() else {
            unreachable!("native scheduler accepts only native requests");
        };
        envelope.params.timeout_ms = NativeExchangeTimeoutMs::try_from(remaining_ms)
            .expect("remaining native timeout is canonical");

        let response = self.dispatchers.get_mut(&pending.connection_id).map_or(
            Err(BridgeActorError::UnknownConnection),
            |dispatcher| {
                Ok(dispatcher.dispatch(
                    *pending.request,
                    &mut self.identities,
                    &mut self.sessions,
                    &mut self.backend,
                ))
            },
        );
        let _ = pending.reply.send(response);
    }

    fn reject_pending_native(
        &mut self,
        pending: PendingNativeCommand,
        rejection: NativeExchangeRejection,
    ) {
        let response = self.dispatchers.get_mut(&pending.connection_id).map_or(
            Err(BridgeActorError::UnknownConnection),
            |dispatcher| {
                Ok(dispatcher.dispatch_native_rejection(
                    *pending.request,
                    &self.sessions,
                    &mut self.backend,
                    rejection,
                ))
            },
        );
        let _ = pending.reply.send(response);
    }

    fn cancel_connection_native(&mut self, connection_id: BridgeConnectionId) {
        let cancelled = self
            .native_pending
            .remove_where(|pending| pending.connection_id == connection_id);
        for pending in cancelled {
            self.reject_pending_native(pending, NativeExchangeRejection::SessionEnded);
        }
    }

    fn cancel_invalidated_native(&mut self) {
        let invalidations = self.backend.take_native_route_invalidations();
        if invalidations.is_empty() {
            return;
        }
        let invalidations = invalidations.into_iter().collect::<BTreeMap<_, _>>();
        let cancelled = self
            .native_pending
            .remove_where(|pending| invalidations.contains_key(&pending.route_token));
        for pending in cancelled {
            let rejection = invalidations
                .get(&pending.route_token)
                .copied()
                .expect("cancelled route owns an invalidation");
            self.reject_pending_native(pending, rejection);
        }
    }

    fn cancel_all_native(&mut self) {
        for pending in self.native_pending.drain() {
            self.reject_pending_native(pending, NativeExchangeRejection::SessionEnded);
        }
    }

    fn open(&mut self) -> Result<BridgeConnectionId, BridgeActorError> {
        if self.dispatchers.len() >= self.connection_capacity {
            return Err(BridgeActorError::ConnectionCapacityExhausted);
        }
        let connection_id = BridgeConnectionId(self.next_connection_id);
        self.next_connection_id = self
            .next_connection_id
            .checked_add(1)
            .ok_or(BridgeActorError::ConnectionCapacityExhausted)?;
        self.dispatchers.insert(
            connection_id,
            ConnectionDispatcher::new(self.config.clone()),
        );
        Ok(connection_id)
    }

    fn close(&mut self, connection_id: BridgeConnectionId) -> Result<(), BridgeActorError> {
        self.cancel_connection_native(connection_id);
        let Some(mut dispatcher) = self.dispatchers.remove(&connection_id) else {
            return Err(BridgeActorError::UnknownConnection);
        };
        dispatcher
            .disconnect(&mut self.sessions, &mut self.backend)
            .map_err(BridgeActorError::Cleanup)
    }

    fn cleanup_all(&mut self) -> BridgeActorExit {
        self.cancel_all_native();
        let dispatchers = std::mem::take(&mut self.dispatchers);
        let mut exit = BridgeActorExit::default();
        for (_, mut dispatcher) in dispatchers {
            exit.connections_cleaned += 1;
            if dispatcher
                .disconnect(&mut self.sessions, &mut self.backend)
                .is_err()
            {
                exit.cleanup_failures += 1;
            }
        }
        exit
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ActorConnectionServeError {
    Frame(FrameError),
    Actor(BridgeActorError),
    Cleanup(BridgeActorError),
    FrameAndCleanup {
        frame: FrameError,
        cleanup: BridgeActorError,
    },
    ActorAndCleanup {
        actor: BridgeActorError,
        cleanup: BridgeActorError,
    },
}

impl fmt::Display for ActorConnectionServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Frame(error) => write!(formatter, "local RPC connection failed: {error}"),
            Self::Actor(error) => write!(formatter, "local RPC actor failed: {error}"),
            Self::Cleanup(error) => write!(formatter, "local RPC cleanup failed: {error}"),
            Self::FrameAndCleanup { frame, cleanup } => write!(
                formatter,
                "local RPC connection failed: {frame}; cleanup also failed: {cleanup}"
            ),
            Self::ActorAndCleanup { actor, cleanup } => write!(
                formatter,
                "local RPC actor failed: {actor}; cleanup also failed: {cleanup}"
            ),
        }
    }
}

impl std::error::Error for ActorConnectionServeError {}

/// Serves one local stream through a shared bridge actor.
///
/// Framing remains per connection while every policy mutation is serialized
/// by the actor. Cleanup is attempted on EOF and every failure path.
///
/// # Errors
///
/// Returns typed framing, actor, cleanup, or combined failures.
pub fn serve_actor_connection<S: Read + Write>(
    stream: &mut S,
    actor: &BridgeActorHandle,
) -> Result<ConnectionServeReport, ActorConnectionServeError> {
    let connection_id = actor
        .open_connection()
        .map_err(ActorConnectionServeError::Actor)?;
    let result = serve_actor_requests(stream, actor, connection_id);
    let cleanup = actor.disconnect(connection_id);
    match (result, cleanup) {
        (Ok(report), Ok(())) => Ok(report),
        (Ok(_), Err(cleanup)) => Err(ActorConnectionServeError::Cleanup(cleanup)),
        (Err(ActorRequestFailure::Frame(frame)), Ok(())) => {
            Err(ActorConnectionServeError::Frame(frame))
        }
        (Err(ActorRequestFailure::Actor(actor)), Ok(())) => {
            Err(ActorConnectionServeError::Actor(actor))
        }
        (Err(ActorRequestFailure::Frame(frame)), Err(cleanup)) => {
            Err(ActorConnectionServeError::FrameAndCleanup { frame, cleanup })
        }
        (Err(ActorRequestFailure::Actor(actor)), Err(cleanup)) => {
            Err(ActorConnectionServeError::ActorAndCleanup { actor, cleanup })
        }
    }
}

enum ActorRequestFailure {
    Frame(FrameError),
    Actor(BridgeActorError),
}

fn serve_actor_requests<S: Read + Write>(
    stream: &mut S,
    actor: &BridgeActorHandle,
    connection_id: BridgeConnectionId,
) -> Result<ConnectionServeReport, ActorRequestFailure> {
    let mut requests_served = 0;
    let mut selected_version: Option<ProtocolVersion> = None;
    loop {
        let request = match selected_version {
            Some(version) => read_rpc_request_for_version(stream, version),
            None => read_rpc_request(stream),
        }
        .map_err(ActorRequestFailure::Frame)?;
        let Some(request) = request else {
            return Ok(ConnectionServeReport {
                requests_served,
                selected_version,
            });
        };

        let response = actor
            .request(connection_id, request)
            .map_err(ActorRequestFailure::Actor)?;
        let response_version = match &response {
            RpcResponse::NegotiateSuccess(envelope) => Some(envelope.result.selected_version),
            _ => selected_version,
        };
        match response_version {
            Some(version) => write_rpc_response_for_version(stream, &response, version),
            None => write_rpc_response(stream, &response),
        }
        .map_err(ActorRequestFailure::Frame)?;
        requests_served += 1;
        selected_version = response_version;
    }
}

fn map_receive_error(error: RecvTimeoutError) -> BridgeActorError {
    match error {
        RecvTimeoutError::Timeout => BridgeActorError::TimedOut,
        RecvTimeoutError::Disconnected => BridgeActorError::Unavailable,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{AuthorizedSession, BackendRequestContext, SessionIdentityError};
    use hfx_domain::{
        ClientId, ClientName, ComponentVersion, FindingId, LeaseId, NativeReportHex,
        ProtocolFeatureId, RequestId, ServerInstanceId,
    };
    use hfx_protocol::{
        BridgeSnapshot, ClientHello, DiagnosticSnapshot, EventBatch, IntegrationView, LeaseRequest,
        LeaseResult, NativeExchangeMatchedResponse, NativeExchangeNotSent, NativeExchangeOutcome,
        NativeExchangeRequest, NativeExchangeResult, NegotiationRequestEnvelope,
        ReleaseLeaseRequest, RenewLeaseRequest, SessionRequestEnvelope, SubscriptionRequest,
        TransactionLookup, TransactionRequest, TransactionResult,
    };
    #[derive(Debug)]
    struct DeterministicIdentities(u8);

    impl SessionIdentitySource for DeterministicIdentities {
        fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
            for byte in destination {
                *byte = self.0;
                self.0 = self.0.wrapping_add(1);
            }
            Ok(())
        }
    }

    #[derive(Debug, Default)]
    struct NativeBackend {
        dispatched: Vec<NativeRouteToken>,
        rejected: Vec<NativeExchangeRejection>,
        invalidations: Vec<(NativeRouteToken, NativeExchangeRejection)>,
    }

    impl BridgeRpcBackend for NativeBackend {
        fn snapshot(
            &mut self,
            _context: BackendRequestContext<'_>,
        ) -> Result<BridgeSnapshot, RpcFailure> {
            Err(RpcFailure::internal())
        }

        fn integration_view(
            &mut self,
            _context: BackendRequestContext<'_>,
        ) -> Result<IntegrationView, RpcFailure> {
            Err(RpcFailure::internal())
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

        fn native_transact(
            &mut self,
            _context: BackendRequestContext<'_>,
            request: NativeExchangeRequest,
        ) -> Result<NativeExchangeOutcome, RpcFailure> {
            self.dispatched.push(request.route_token.clone());
            Ok(NativeExchangeOutcome {
                request_id: request.request_id,
                route_token: request.route_token,
                result: NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                    response: request.report,
                }),
            })
        }

        fn reject_native_exchange(
            &mut self,
            _context: BackendRequestContext<'_>,
            request: NativeExchangeRequest,
            rejection: NativeExchangeRejection,
        ) -> Result<NativeExchangeOutcome, RpcFailure> {
            self.rejected.push(rejection);
            Ok(NativeExchangeOutcome {
                request_id: request.request_id,
                route_token: request.route_token,
                result: NativeExchangeResult::NotSent(NativeExchangeNotSent {
                    error_kind: rejection.error_kind(),
                    finding_id: FindingId::try_from(rejection.error_code().as_str())
                        .expect("catalog code is a finding identity"),
                }),
            })
        }

        fn take_native_route_invalidations(
            &mut self,
        ) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
            std::mem::take(&mut self.invalidations)
        }

        fn disconnect(&mut self, _session: &AuthorizedSession) -> Result<(), RpcFailure> {
            Ok(())
        }
    }

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is canonical")
    }

    fn actor(native_capacity: u16) -> BridgeActor<DeterministicIdentities, NativeBackend> {
        let capacity = QueueCapacity::try_from(8_u16).expect("capacity is canonical");
        let (actor, _handle) = BridgeActor::new(
            BridgeSessionConfig {
                server_instance_id: text::<ServerInstanceId>("server-native-scheduler"),
                bridge_version: text::<ComponentVersion>("0.0.0-test"),
                event_buffer_capacity: capacity,
            },
            DeterministicIdentities(1),
            SessionRegistry::new(capacity),
            NativeBackend::default(),
            BridgeActorLimits {
                command_capacity: capacity,
                connection_capacity: capacity,
                native_transaction_capacity: QueueCapacity::try_from(native_capacity)
                    .expect("native capacity is canonical"),
                response_timeout: Duration::from_secs(2),
            },
        )
        .expect("actor starts");
        actor
    }

    fn open_and_negotiate(
        actor: &mut BridgeActor<DeterministicIdentities, NativeBackend>,
    ) -> (BridgeConnectionId, hfx_protocol::ServerHello) {
        let (open_reply, open_response) = mpsc::sync_channel(1);
        assert!(
            actor
                .process(BridgeActorCommand::Open { reply: open_reply })
                .is_none()
        );
        let connection_id = open_response
            .recv()
            .expect("open response is sent")
            .expect("connection opens");

        let (reply, response) = mpsc::sync_channel(1);
        let negotiation = RpcRequest::Negotiate(NegotiationRequestEnvelope {
            request_id: text::<RequestId>("request-negotiate-native-scheduler"),
            params: ClientHello {
                client_id: text::<ClientId>("client-native-scheduler"),
                client_name: text::<ClientName>("Native scheduler test"),
                minimum_version: ProtocolVersion::try_from(7_u16).expect("version is canonical"),
                maximum_version: ProtocolVersion::try_from(7_u16).expect("version is canonical"),
                required_features: vec![text::<ProtocolFeatureId>("native-receiver-tunnel")],
                optional_features: Vec::new(),
            },
        });
        assert!(
            actor
                .process(BridgeActorCommand::Request {
                    connection_id,
                    request: Box::new(negotiation),
                    reply,
                })
                .is_none()
        );
        let response = response
            .recv()
            .expect("negotiation response is sent")
            .expect("actor remains available");
        let RpcResponse::NegotiateSuccess(envelope) = response else {
            panic!("native negotiation succeeds: {response:?}");
        };
        (connection_id, envelope.result)
    }

    fn native_request(
        hello: &hfx_protocol::ServerHello,
        request_id: &str,
        route_fill: char,
        timeout_ms: u16,
    ) -> RpcRequest {
        RpcRequest::NativeTransact(SessionRequestEnvelope {
            request_id: text::<RequestId>(request_id),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: NativeExchangeRequest {
                request_id: text::<RequestId>(request_id),
                client_id: text::<ClientId>("client-native-scheduler"),
                lease_id: text::<LeaseId>("lease-native-scheduler"),
                route_token: text::<NativeRouteToken>(&route_fill.to_string().repeat(64)),
                report: text::<NativeReportHex>(&"00".repeat(90)),
                timeout_ms: NativeExchangeTimeoutMs::try_from(timeout_ms)
                    .expect("timeout is canonical"),
            },
        })
    }

    fn enqueue(
        actor: &mut BridgeActor<DeterministicIdentities, NativeBackend>,
        connection_id: BridgeConnectionId,
        request: RpcRequest,
    ) -> Receiver<Result<RpcResponse, BridgeActorError>> {
        let (reply, response) = mpsc::sync_channel(1);
        assert!(
            actor
                .process(BridgeActorCommand::Request {
                    connection_id,
                    request: Box::new(request),
                    reply,
                })
                .is_none()
        );
        response
    }

    #[test]
    fn actor_dispatches_route_local_fifo_with_fair_sibling_turns() {
        let mut actor = actor(8);
        let (connection_id, hello) = open_and_negotiate(&mut actor);
        let responses = [
            native_request(&hello, "request-mouse-1", 'a', 1_000),
            native_request(&hello, "request-mouse-2", 'a', 1_000),
            native_request(&hello, "request-keyboard-1", 'b', 1_000),
            native_request(&hello, "request-mouse-3", 'a', 1_000),
            native_request(&hello, "request-keyboard-2", 'b', 1_000),
        ]
        .into_iter()
        .map(|request| enqueue(&mut actor, connection_id, request))
        .collect::<Vec<_>>();

        for _ in 0..responses.len() {
            actor.dispatch_next_native();
        }
        assert_eq!(
            actor
                .backend
                .dispatched
                .iter()
                .map(|token| token.as_str().chars().next().expect("token is nonempty"))
                .collect::<Vec<_>>(),
            vec!['a', 'b', 'a', 'b', 'a']
        );
        for response in responses {
            assert!(matches!(
                response.recv().expect("response is sent"),
                Ok(RpcResponse::NativeTransactSuccess(_))
            ));
        }
    }

    #[test]
    fn saturation_and_expiry_are_retained_not_sent_without_dispatch() {
        let mut actor = actor(1);
        let (connection_id, hello) = open_and_negotiate(&mut actor);
        let first = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-first", 'a', 1_000),
        );
        let saturated = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-saturated", 'b', 1_000),
        );
        let saturated = saturated.recv().expect("saturation response is sent");
        assert!(matches!(
            saturated,
            Ok(RpcResponse::NativeTransactSuccess(envelope))
                if matches!(
                    envelope.result.result,
                    NativeExchangeResult::NotSent(NativeExchangeNotSent {
                        error_kind: hfx_domain::ProtocolErrorKind::QueueFull,
                        ..
                    })
                )
        ));
        assert!(actor.backend.dispatched.is_empty());

        actor.dispatch_next_native();
        assert!(matches!(
            first.recv().expect("first response is sent"),
            Ok(RpcResponse::NativeTransactSuccess(_))
        ));
        let expired = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-expired", 'c', 1),
        );
        std::thread::sleep(Duration::from_millis(3));
        actor.dispatch_next_native();
        let expired = expired.recv().expect("expiry response is sent");
        assert!(matches!(
            expired,
            Ok(RpcResponse::NativeTransactSuccess(envelope))
                if matches!(
                    envelope.result.result,
                    NativeExchangeResult::NotSent(NativeExchangeNotSent {
                        error_kind: hfx_domain::ProtocolErrorKind::DeadlineExceeded,
                        ..
                    })
                )
        ));
        assert_eq!(actor.backend.dispatched.len(), 1);
        assert_eq!(
            actor.backend.rejected,
            vec![
                NativeExchangeRejection::QueueFull,
                NativeExchangeRejection::DeadlineExceeded,
            ]
        );
    }

    #[test]
    fn route_invalidation_cancels_exact_lane_and_preserves_sibling_service() {
        let mut actor = actor(8);
        let (connection_id, hello) = open_and_negotiate(&mut actor);
        let mouse_one = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-invalidated-1", 'a', 1_000),
        );
        let mouse_two = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-invalidated-2", 'a', 1_000),
        );
        let keyboard = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-sibling", 'b', 1_000),
        );
        actor.backend.invalidations.push((
            text::<NativeRouteToken>(&"a".repeat(64)),
            NativeExchangeRejection::StaleGeneration,
        ));

        actor.cancel_invalidated_native();
        for response in [mouse_one, mouse_two] {
            assert!(matches!(
                response.recv().expect("cancellation response is sent"),
                Ok(RpcResponse::NativeTransactSuccess(envelope))
                    if matches!(
                        envelope.result.result,
                        NativeExchangeResult::NotSent(NativeExchangeNotSent {
                            error_kind: hfx_domain::ProtocolErrorKind::StaleGeneration,
                            ..
                        })
                    )
            ));
        }
        assert!(actor.backend.dispatched.is_empty());

        actor.dispatch_next_native();
        assert!(matches!(
            keyboard.recv().expect("sibling response is sent"),
            Ok(RpcResponse::NativeTransactSuccess(_))
        ));
        assert_eq!(
            actor.backend.dispatched,
            vec![text::<NativeRouteToken>(&"b".repeat(64))]
        );
        assert_eq!(
            actor.backend.rejected,
            vec![
                NativeExchangeRejection::StaleGeneration,
                NativeExchangeRejection::StaleGeneration,
            ]
        );
    }

    #[test]
    fn disconnect_cancels_queued_native_work_before_session_revocation() {
        let mut actor = actor(8);
        let (connection_id, hello) = open_and_negotiate(&mut actor);
        let pending = enqueue(
            &mut actor,
            connection_id,
            native_request(&hello, "request-disconnect", 'a', 1_000),
        );

        actor.close(connection_id).expect("connection closes");
        assert!(matches!(
            pending.recv().expect("cancellation response is sent"),
            Ok(RpcResponse::NativeTransactSuccess(envelope))
                if matches!(
                    envelope.result.result,
                    NativeExchangeResult::NotSent(NativeExchangeNotSent {
                        error_kind: hfx_domain::ProtocolErrorKind::OwnershipConflict,
                        ..
                    })
                )
        ));
        assert!(actor.backend.dispatched.is_empty());
        assert_eq!(
            actor.backend.rejected,
            vec![NativeExchangeRejection::SessionEnded]
        );
    }
}
