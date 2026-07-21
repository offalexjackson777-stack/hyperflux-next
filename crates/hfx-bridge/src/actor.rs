// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    BridgeRpcBackend, BridgeSessionConfig, ConnectionDispatcher, ConnectionServeReport, RpcFailure,
    SessionIdentitySource, SessionRegistry,
};
use hfx_domain::{ProtocolVersion, QueueCapacity};
use hfx_protocol::{
    FrameError, RpcRequest, RpcResponse, read_rpc_request, read_rpc_request_for_version,
    write_rpc_response, write_rpc_response_for_version,
};
use std::collections::BTreeMap;
use std::convert::Infallible;
use std::fmt;
use std::io::{Read, Write};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError};
use std::time::Duration;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct BridgeConnectionId(u64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BridgeActorLimits {
    pub command_capacity: QueueCapacity,
    pub connection_capacity: QueueCapacity,
    pub response_timeout: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeActorStartError {
    ZeroResponseTimeout,
}

impl fmt::Display for BridgeActorStartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("bridge actor response timeout must be positive")
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
        let (sender, commands) = mpsc::sync_channel(usize::from(limits.command_capacity.get()));
        Ok((
            Self {
                config,
                identities,
                sessions,
                backend,
                commands,
                dispatchers: BTreeMap::new(),
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
            match self.commands.recv_timeout(interval) {
                Ok(BridgeActorCommand::Shutdown { reply }) => {
                    let exit = self.cleanup_all();
                    let _ = reply.send(exit);
                    return Ok(exit);
                }
                Ok(command) => {
                    self.process(command);
                    match tick(&mut self.backend, &self.sessions) {
                        Ok(true) => {}
                        Ok(false) => return Ok(self.cleanup_all()),
                        Err(error) => {
                            let cleanup = self.cleanup_all();
                            return Err(BridgeActorTickFailure { error, cleanup });
                        }
                    }
                }
                Err(RecvTimeoutError::Timeout) => match tick(&mut self.backend, &self.sessions) {
                    Ok(true) => {}
                    Ok(false) => return Ok(self.cleanup_all()),
                    Err(error) => {
                        let cleanup = self.cleanup_all();
                        return Err(BridgeActorTickFailure { error, cleanup });
                    }
                },
                Err(RecvTimeoutError::Disconnected) => return Ok(self.cleanup_all()),
            }
        }
    }

    fn process(&mut self, command: BridgeActorCommand) {
        match command {
            BridgeActorCommand::Open { reply } => {
                let _ = reply.send(self.open());
            }
            BridgeActorCommand::Request {
                connection_id,
                request,
                reply,
            } => {
                let response = self.dispatchers.get_mut(&connection_id).map_or(
                    Err(BridgeActorError::UnknownConnection),
                    |dispatcher| {
                        Ok(dispatcher.dispatch(
                            *request,
                            &mut self.identities,
                            &mut self.sessions,
                            &mut self.backend,
                        ))
                    },
                );
                let _ = reply.send(response);
            }
            BridgeActorCommand::Disconnect {
                connection_id,
                reply,
            } => {
                let _ = reply.send(self.close(connection_id));
            }
            BridgeActorCommand::Shutdown { .. } => {
                unreachable!("shutdown is handled by the actor loop")
            }
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
        let Some(mut dispatcher) = self.dispatchers.remove(&connection_id) else {
            return Err(BridgeActorError::UnknownConnection);
        };
        dispatcher
            .disconnect(&mut self.sessions, &mut self.backend)
            .map_err(BridgeActorError::Cleanup)
    }

    fn cleanup_all(&mut self) -> BridgeActorExit {
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
