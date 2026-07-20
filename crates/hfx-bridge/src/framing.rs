// SPDX-License-Identifier: GPL-2.0-only

use hfx_protocol::{
    MAX_WIRE_MESSAGE_BYTES, ProtocolWireError, RpcRequest, RpcResponse, decode_rpc_request,
    validate_rpc_response,
};
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
    ResponseEncoding,
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
            Self::ResponseEncoding => formatter.write_str("RPC response encoding failed"),
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
pub fn read_rpc_request<R: Read>(reader: &mut R) -> Result<Option<RpcRequest>, FrameError> {
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
    decode_rpc_request(&payload)
        .map(Some)
        .map_err(FrameError::InvalidRequest)
}

struct BoundedBuffer {
    bytes: Vec<u8>,
    exceeded: bool,
}

impl BoundedBuffer {
    fn new() -> Self {
        Self {
            bytes: Vec::with_capacity(4096),
            exceeded: false,
        }
    }
}

impl Write for BoundedBuffer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let Some(next_length) = self.bytes.len().checked_add(bytes.len()) else {
            self.exceeded = true;
            return Err(io::Error::other("bounded response buffer overflow"));
        };
        if next_length > MAX_WIRE_MESSAGE_BYTES {
            self.exceeded = true;
            return Err(io::Error::other("bounded response buffer exceeded"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
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
    validate_rpc_response(response).map_err(FrameError::InvalidResponse)?;
    let mut encoded = BoundedBuffer::new();
    if serde_json::to_writer(&mut encoded, response).is_err() {
        if encoded.exceeded {
            return Err(FrameError::PayloadTooLarge {
                declared: MAX_WIRE_MESSAGE_BYTES.saturating_add(1),
                maximum: MAX_WIRE_MESSAGE_BYTES,
            });
        }
        return Err(FrameError::ResponseEncoding);
    }
    if encoded.bytes.is_empty() {
        return Err(FrameError::ResponseEncoding);
    }
    let length = u32::try_from(encoded.bytes.len()).map_err(|_| FrameError::PayloadTooLarge {
        declared: encoded.bytes.len(),
        maximum: MAX_WIRE_MESSAGE_BYTES,
    })?;
    write_all_at(writer, &length.to_be_bytes(), FrameIoStage::WriteLength)?;
    write_all_at(writer, &encoded.bytes, FrameIoStage::WritePayload)?;
    writer.flush().map_err(|error| FrameError::Io {
        stage: FrameIoStage::Flush,
        kind: error.kind(),
    })
}
