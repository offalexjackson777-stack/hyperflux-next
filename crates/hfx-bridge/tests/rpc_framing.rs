// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{FrameError, FrameIoStage, read_rpc_request, write_rpc_response};
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, NegotiationToken, ProtocolFeatureId, ProtocolSessionId,
    ProtocolVersion, QueueCapacity, RequestId, ServerInstanceId,
};
use hfx_protocol::{
    ClientHello, NegotiationRequestEnvelope, RpcRequest, RpcResponse, ServerHello, SuccessEnvelope,
};
use std::io::{self, Cursor, Read, Write};

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity must be valid")
}

fn negotiation_request() -> RpcRequest {
    RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: id::<RequestId>("request-negotiate"),
        params: ClientHello {
            client_id: id::<ClientId>("client-test"),
            client_name: id::<ClientName>("Framing test"),
            minimum_version: ProtocolVersion::try_from(1).expect("version must be valid"),
            maximum_version: ProtocolVersion::try_from(2).expect("version must be valid"),
            required_features: Vec::new(),
            optional_features: vec![id::<ProtocolFeatureId>("structured-diagnostics")],
        },
    })
}

fn negotiation_response() -> RpcResponse {
    RpcResponse::NegotiateSuccess(SuccessEnvelope {
        request_id: id::<RequestId>("request-negotiate"),
        server_instance_id: id::<ServerInstanceId>("server-test"),
        result: ServerHello {
            selected_version: ProtocolVersion::try_from(2).expect("version must be valid"),
            server_instance_id: id::<ServerInstanceId>("server-test"),
            protocol_session_id: id::<ProtocolSessionId>("protocol-session-test"),
            negotiation_token: id::<NegotiationToken>("negotiation-token-test"),
            bridge_version: id::<ComponentVersion>("0.0.0-test"),
            enabled_features: vec![id::<ProtocolFeatureId>("structured-diagnostics")],
            event_buffer_capacity: QueueCapacity::try_from(128).expect("capacity must be valid"),
        },
    })
}

fn framed(request: &RpcRequest) -> Vec<u8> {
    let payload = serde_json::to_vec(request).expect("request must serialize");
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(
        &u32::try_from(payload.len())
            .expect("test payload must fit")
            .to_be_bytes(),
    );
    frame.extend_from_slice(&payload);
    frame
}

struct ChunkedReader<R> {
    inner: R,
    chunk: usize,
}

impl<R: Read> Read for ChunkedReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let count = buffer.len().min(self.chunk);
        self.inner.read(&mut buffer[..count])
    }
}

#[derive(Default)]
struct ChunkedWriter {
    bytes: Vec<u8>,
    chunk: usize,
    flushes: usize,
}

impl Write for ChunkedWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let count = bytes.len().min(self.chunk);
        self.bytes.extend_from_slice(&bytes[..count]);
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.flushes += 1;
        Ok(())
    }
}

struct FailingWriter {
    writes_before_failure: usize,
}

impl Write for FailingWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.writes_before_failure == 0 {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe, "injected"));
        }
        self.writes_before_failure -= 1;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn clean_eof_and_chunked_frame_have_distinct_results() {
    assert_eq!(
        read_rpc_request(&mut Cursor::new(Vec::<u8>::new())),
        Ok(None)
    );

    let request = negotiation_request();
    let mut reader = ChunkedReader {
        inner: Cursor::new(framed(&request)),
        chunk: 1,
    };
    assert_eq!(read_rpc_request(&mut reader), Ok(Some(request)));
}

#[test]
fn torn_and_unbounded_declarations_are_rejected_before_decode() {
    assert_eq!(
        read_rpc_request(&mut Cursor::new(vec![0, 0, 1])),
        Err(FrameError::TruncatedLength { received: 3 })
    );
    assert_eq!(
        read_rpc_request(&mut Cursor::new(0_u32.to_be_bytes())),
        Err(FrameError::EmptyPayload)
    );

    let oversized = u32::try_from(hfx_protocol::MAX_WIRE_MESSAGE_BYTES + 1)
        .expect("protocol bound must fit u32")
        .to_be_bytes();
    assert_eq!(
        read_rpc_request(&mut Cursor::new(oversized)),
        Err(FrameError::PayloadTooLarge {
            declared: hfx_protocol::MAX_WIRE_MESSAGE_BYTES + 1,
            maximum: hfx_protocol::MAX_WIRE_MESSAGE_BYTES,
        })
    );

    let mut truncated = 10_u32.to_be_bytes().to_vec();
    truncated.extend_from_slice(b"short");
    assert_eq!(
        read_rpc_request(&mut Cursor::new(truncated)),
        Err(FrameError::TruncatedPayload {
            declared: 10,
            received: 5,
        })
    );
}

#[test]
fn malformed_and_unknown_methods_remain_protocol_errors() {
    let malformed_payload = b"{";
    let mut malformed = u32::try_from(malformed_payload.len())
        .expect("length must fit")
        .to_be_bytes()
        .to_vec();
    malformed.extend_from_slice(malformed_payload);
    assert!(matches!(
        read_rpc_request(&mut Cursor::new(malformed)),
        Err(FrameError::InvalidRequest(
            hfx_protocol::ProtocolWireError::MalformedJson
        ))
    ));

    let unknown_payload = br#"{"method":"invented","request":{}}"#;
    let mut unknown = u32::try_from(unknown_payload.len())
        .expect("length must fit")
        .to_be_bytes()
        .to_vec();
    unknown.extend_from_slice(unknown_payload);
    assert!(matches!(
        read_rpc_request(&mut Cursor::new(unknown)),
        Err(FrameError::InvalidRequest(
            hfx_protocol::ProtocolWireError::MalformedJson
        ))
    ));
}

#[test]
fn response_writer_handles_partial_writes_and_flushes_once() {
    let response = negotiation_response();
    let mut writer = ChunkedWriter {
        chunk: 2,
        ..ChunkedWriter::default()
    };
    write_rpc_response(&mut writer, &response).expect("response write must pass");
    assert_eq!(writer.flushes, 1);

    let declared = u32::from_be_bytes(
        writer.bytes[..4]
            .try_into()
            .expect("frame must have a complete prefix"),
    ) as usize;
    assert_eq!(declared, writer.bytes.len() - 4);
    assert_eq!(
        hfx_protocol::decode_rpc_response(&writer.bytes[4..]),
        Ok(response)
    );
}

#[test]
fn response_validation_precedes_io_and_io_stage_is_preserved() {
    let mut invalid = negotiation_response();
    let RpcResponse::NegotiateSuccess(envelope) = &mut invalid else {
        panic!("fixture must be a negotiation response");
    };
    envelope
        .result
        .enabled_features
        .push(id::<ProtocolFeatureId>("structured-diagnostics"));
    let mut untouched = Vec::new();
    assert!(matches!(
        write_rpc_response(&mut untouched, &invalid),
        Err(FrameError::InvalidResponse(_))
    ));
    assert!(untouched.is_empty());

    let mut failing = FailingWriter {
        writes_before_failure: 1,
    };
    assert_eq!(
        write_rpc_response(&mut failing, &negotiation_response()),
        Err(FrameError::Io {
            stage: FrameIoStage::WritePayload,
            kind: io::ErrorKind::BrokenPipe,
        })
    );
}
