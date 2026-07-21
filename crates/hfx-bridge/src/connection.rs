// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    BridgeRpcBackend, BridgeSessionConfig, ConnectionDispatcher, RpcFailure, SessionIdentitySource,
    SessionRegistry,
};
use hfx_domain::ProtocolVersion;
use hfx_protocol::{
    FrameError, RpcResponse, read_rpc_request, read_rpc_request_for_version, write_rpc_response,
    write_rpc_response_for_version,
};
use std::fmt;
use std::io::{Read, Write};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConnectionServeReport {
    pub requests_served: usize,
    pub selected_version: Option<ProtocolVersion>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ConnectionServeError {
    Frame(FrameError),
    Cleanup(RpcFailure),
    FrameAndCleanup {
        frame: FrameError,
        cleanup: RpcFailure,
    },
}

impl fmt::Display for ConnectionServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Frame(error) => write!(formatter, "local RPC connection failed: {error}"),
            Self::Cleanup(error) => write!(formatter, "local RPC cleanup failed: {error}"),
            Self::FrameAndCleanup { frame, cleanup } => write!(
                formatter,
                "local RPC connection failed: {frame}; cleanup also failed: {cleanup}"
            ),
        }
    }
}

impl std::error::Error for ConnectionServeError {}

/// Serves one already-accepted local byte stream until clean EOF or a framing
/// failure.
///
/// Negotiation uses the stable base handshake. Every request and response
/// after negotiation uses the exact selected frozen protocol schema. Session
/// authority is revoked and backend resources are released on every exit path.
///
/// # Errors
///
/// Returns a typed framing failure, cleanup failure, or both when they occur
/// during the same connection lifetime.
pub fn serve_connection<S, I, B>(
    stream: &mut S,
    config: BridgeSessionConfig,
    identities: &mut I,
    sessions: &mut SessionRegistry,
    backend: &mut B,
) -> Result<ConnectionServeReport, ConnectionServeError>
where
    S: Read + Write,
    I: SessionIdentitySource,
    B: BridgeRpcBackend,
{
    let mut dispatcher = ConnectionDispatcher::new(config);
    let result = serve_requests(stream, identities, sessions, backend, &mut dispatcher);
    let cleanup = dispatcher.disconnect(sessions, backend);

    match (result, cleanup) {
        (Ok(report), Ok(())) => Ok(report),
        (Ok(_), Err(cleanup)) => Err(ConnectionServeError::Cleanup(cleanup)),
        (Err(frame), Ok(())) => Err(ConnectionServeError::Frame(frame)),
        (Err(frame), Err(cleanup)) => Err(ConnectionServeError::FrameAndCleanup { frame, cleanup }),
    }
}

fn serve_requests<S, I, B>(
    stream: &mut S,
    identities: &mut I,
    sessions: &mut SessionRegistry,
    backend: &mut B,
    dispatcher: &mut ConnectionDispatcher,
) -> Result<ConnectionServeReport, FrameError>
where
    S: Read + Write,
    I: SessionIdentitySource,
    B: BridgeRpcBackend,
{
    let mut requests_served = 0;
    let mut selected_version = None;

    loop {
        let request = match selected_version {
            Some(version) => read_rpc_request_for_version(stream, version)?,
            None => read_rpc_request(stream)?,
        };
        let Some(request) = request else {
            return Ok(ConnectionServeReport {
                requests_served,
                selected_version,
            });
        };

        let response = dispatcher.dispatch(request, identities, sessions, backend);
        let response_version = match &response {
            RpcResponse::NegotiateSuccess(envelope) => Some(envelope.result.selected_version),
            _ => selected_version,
        };
        match response_version {
            Some(version) => write_rpc_response_for_version(stream, &response, version)?,
            None => write_rpc_response(stream, &response)?,
        }
        requests_served += 1;
        selected_version = response_version;
    }
}
