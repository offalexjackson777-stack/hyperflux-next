// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    MAX_WIRE_MESSAGE_BYTES, ProtocolWireError, RpcRequest, RpcResponse, decode_rpc_request,
    decode_rpc_request_for_version, decode_rpc_response, decode_rpc_response_for_version,
    encode_rpc_request, encode_rpc_request_for_version, encode_rpc_response,
    encode_rpc_response_for_version,
};
use hfx_domain::ProtocolVersion;
use std::fmt;
use std::io::{self, Read, Write};

pub const FRAME_LENGTH_BYTES: usize = size_of::<u32>();

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FrameIoStage {
    ReadLength,
    ReadPayload,
    WriteLength,
    WritePayload,
    Flush,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FrameError {
    Io {
        stage: FrameIoStage,
        kind: io::ErrorKind,
    },
    TruncatedLength {
        received: usize,
    },
    EmptyPayload,
    PayloadTooLarge {
        declared: usize,
        maximum: usize,
    },
    TruncatedPayload {
        declared: usize,
        received: usize,
    },
    InvalidRequest(ProtocolWireError),
    InvalidResponse(ProtocolWireError),
}

impl fmt::Display for FrameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { stage, kind } => {
                write!(formatter, "RPC frame I/O failed during {stage:?}: {kind}")
            }
            Self::TruncatedLength { received } => {
                write!(formatter, "RPC frame length ended after {received} bytes")
            }
            Self::EmptyPayload => formatter.write_str("RPC frame declares an empty payload"),
            Self::PayloadTooLarge { declared, maximum } => write!(
                formatter,
                "RPC frame declares {declared} bytes, above the {maximum}-byte bound"
            ),
            Self::TruncatedPayload { declared, received } => write!(
                formatter,
                "RPC frame payload ended after {received} of {declared} bytes"
            ),
            Self::InvalidRequest(error) => write!(formatter, "invalid RPC request: {error}"),
            Self::InvalidResponse(error) => write!(formatter, "invalid RPC response: {error}"),
        }
    }
}

impl std::error::Error for FrameError {}

fn read_into<R: Read>(
    reader: &mut R,
    buffer: &mut [u8],
    stage: FrameIoStage,
) -> Result<usize, FrameError> {
    let mut received = 0;
    while received < buffer.len() {
        match reader.read(&mut buffer[received..]) {
            Ok(0) => break,
            Ok(count) => received += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => {
                return Err(FrameError::Io {
                    stage,
                    kind: error.kind(),
                });
            }
        }
    }
    Ok(received)
}

/// Reads one length-delimited request without allocating from an untrusted
/// length until the shared protocol bound has been enforced.
///
/// A clean EOF before any length byte returns `Ok(None)`. Any partial prefix or
/// payload is a framing error, so callers cannot silently accept torn requests.
///
/// # Errors
///
/// Returns a typed framing, I/O, decoding, or protocol-validation error.
fn read_rpc_payload<R: Read>(reader: &mut R) -> Result<Option<Vec<u8>>, FrameError> {
    let mut length_bytes = [0_u8; FRAME_LENGTH_BYTES];
    let received = read_into(reader, &mut length_bytes, FrameIoStage::ReadLength)?;
    if received == 0 {
        return Ok(None);
    }
    if received != FRAME_LENGTH_BYTES {
        return Err(FrameError::TruncatedLength { received });
    }

    let declared = u32::from_be_bytes(length_bytes) as usize;
    if declared == 0 {
        return Err(FrameError::EmptyPayload);
    }
    if declared > MAX_WIRE_MESSAGE_BYTES {
        return Err(FrameError::PayloadTooLarge {
            declared,
            maximum: MAX_WIRE_MESSAGE_BYTES,
        });
    }

    let mut payload = vec![0_u8; declared];
    let received = read_into(reader, &mut payload, FrameIoStage::ReadPayload)?;
    if received != declared {
        return Err(FrameError::TruncatedPayload { declared, received });
    }
    Ok(Some(payload))
}

/// Reads one request using the current protocol schema.
///
/// # Errors
///
/// Returns a typed framing, I/O, decoding, or protocol-validation error.
pub fn read_rpc_request<R: Read>(reader: &mut R) -> Result<Option<RpcRequest>, FrameError> {
    read_rpc_payload(reader)?
        .map(|payload| decode_rpc_request(&payload).map_err(FrameError::InvalidRequest))
        .transpose()
}

/// Reads one request using the exact frozen schema selected by negotiation.
///
/// # Errors
///
/// Returns a typed framing, I/O, version-decoding, or protocol-validation error.
pub fn read_rpc_request_for_version<R: Read>(
    reader: &mut R,
    version: ProtocolVersion,
) -> Result<Option<RpcRequest>, FrameError> {
    read_rpc_payload(reader)?
        .map(|payload| {
            decode_rpc_request_for_version(&payload, version).map_err(FrameError::InvalidRequest)
        })
        .transpose()
}

/// Reads one response using the current protocol schema.
///
/// # Errors
///
/// Returns a typed framing, I/O, decoding, or protocol-validation error.
pub fn read_rpc_response<R: Read>(reader: &mut R) -> Result<Option<RpcResponse>, FrameError> {
    read_rpc_payload(reader)?
        .map(|payload| decode_rpc_response(&payload).map_err(FrameError::InvalidResponse))
        .transpose()
}

/// Reads one response using the exact frozen schema selected by negotiation.
///
/// # Errors
///
/// Returns a typed framing, I/O, version-decoding, or protocol-validation error.
pub fn read_rpc_response_for_version<R: Read>(
    reader: &mut R,
    version: ProtocolVersion,
) -> Result<Option<RpcResponse>, FrameError> {
    read_rpc_payload(reader)?
        .map(|payload| {
            decode_rpc_response_for_version(&payload, version).map_err(FrameError::InvalidResponse)
        })
        .transpose()
}

fn write_all_at<W: Write>(
    writer: &mut W,
    bytes: &[u8],
    stage: FrameIoStage,
) -> Result<(), FrameError> {
    writer.write_all(bytes).map_err(|error| FrameError::Io {
        stage,
        kind: error.kind(),
    })
}

/// Validates and writes one bounded length-delimited response.
///
/// Encoding uses a bounded writer, preventing a malformed in-memory response
/// from growing an unbounded temporary allocation before its size is checked.
///
/// # Errors
///
/// Returns a typed validation, encoding, or I/O error.
pub fn write_rpc_response<W: Write>(
    writer: &mut W,
    response: &RpcResponse,
) -> Result<(), FrameError> {
    let encoded = encode_rpc_response(response).map_err(FrameError::InvalidResponse)?;
    write_rpc_payload(writer, &encoded)
}

/// Writes one response only after proving it is representable by the exact
/// frozen schema selected for the connection.
///
/// # Errors
///
/// Returns a typed version-validation, encoding, or I/O error.
pub fn write_rpc_response_for_version<W: Write>(
    writer: &mut W,
    response: &RpcResponse,
    version: ProtocolVersion,
) -> Result<(), FrameError> {
    let encoded =
        encode_rpc_response_for_version(response, version).map_err(FrameError::InvalidResponse)?;
    write_rpc_payload(writer, &encoded)
}

/// Validates and writes one bounded length-delimited request.
///
/// # Errors
///
/// Returns a typed validation, encoding, or I/O error.
pub fn write_rpc_request<W: Write>(writer: &mut W, request: &RpcRequest) -> Result<(), FrameError> {
    let encoded = encode_rpc_request(request).map_err(FrameError::InvalidRequest)?;
    write_rpc_payload(writer, &encoded)
}

/// Writes one request using the exact frozen schema selected for the
/// connection.
///
/// # Errors
///
/// Returns a typed version-validation, semantic-translation, encoding, or I/O
/// error before emitting bytes when the request is not safely representable.
pub fn write_rpc_request_for_version<W: Write>(
    writer: &mut W,
    request: &RpcRequest,
    version: ProtocolVersion,
) -> Result<(), FrameError> {
    let encoded =
        encode_rpc_request_for_version(request, version).map_err(FrameError::InvalidRequest)?;
    write_rpc_payload(writer, &encoded)
}

fn write_rpc_payload<W: Write>(writer: &mut W, encoded: &[u8]) -> Result<(), FrameError> {
    let length = u32::try_from(encoded.len()).map_err(|_| FrameError::PayloadTooLarge {
        declared: encoded.len(),
        maximum: MAX_WIRE_MESSAGE_BYTES,
    })?;
    write_all_at(writer, &length.to_be_bytes(), FrameIoStage::WriteLength)?;
    write_all_at(writer, encoded, FrameIoStage::WritePayload)?;
    writer.flush().map_err(|error| FrameError::Io {
        stage: FrameIoStage::Flush,
        kind: error.kind(),
    })
}
