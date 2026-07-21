// SPDX-License-Identifier: GPL-2.0-only

use crate::SdkError;
use hfx_domain::ProtocolVersion;
use hfx_protocol::{
    RpcRequest, RpcResponse, read_rpc_response, read_rpc_response_for_version, write_rpc_request,
    write_rpc_request_for_version,
};
use std::io::{Read, Write};

#[derive(Debug)]
pub struct FramedIoChannel<S> {
    stream: S,
}

impl<S> FramedIoChannel<S> {
    #[must_use]
    pub const fn new(stream: S) -> Self {
        Self { stream }
    }

    #[must_use]
    pub fn into_inner(self) -> S {
        self.stream
    }
}

impl<S: Read + Write> FramedIoChannel<S> {
    /// Performs one blocking request-response exchange.
    ///
    /// `None` selects the stable negotiation handshake. A selected version
    /// makes both directions use that exact frozen wire schema.
    ///
    /// # Errors
    ///
    /// Returns a framing, validation, translation, I/O, or clean-EOF error.
    pub fn exchange(
        &mut self,
        request: &RpcRequest,
        version: Option<ProtocolVersion>,
    ) -> Result<RpcResponse, SdkError> {
        match version {
            Some(version) => write_rpc_request_for_version(&mut self.stream, request, version)?,
            None => write_rpc_request(&mut self.stream, request)?,
        }
        let response = match version {
            Some(version) => read_rpc_response_for_version(&mut self.stream, version)?,
            None => read_rpc_response(&mut self.stream)?,
        };
        response.ok_or(SdkError::ConnectionClosed)
    }
}
