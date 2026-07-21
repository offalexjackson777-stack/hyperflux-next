// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    FrameError, FrameIoStage, read_rpc_request, read_rpc_request_for_version, read_rpc_response,
    read_rpc_response_for_version, write_rpc_request, write_rpc_request_for_version,
    write_rpc_response, write_rpc_response_for_version,
};
use hfx_domain::{
    ClientId, ClientName, ColorChannel, ComponentVersion, NegotiationToken, ProtocolFeatureId,
    ProtocolSessionId, ProtocolVersion, QueueCapacity, RequestId, ServerInstanceId,
    StableLightingMode,
};
use hfx_protocol::{
    ClientHello, NegotiationRequestEnvelope, RpcRequest, RpcResponse, ServerHello, SuccessEnvelope,
};
use serde_json::{Value, json};
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
    framed_payload(&payload)
}

fn framed_payload(payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(
        &u32::try_from(payload.len())
            .expect("test payload must fit")
            .to_be_bytes(),
    );
    frame.extend_from_slice(payload);
    frame
}

fn framed_transaction_value() -> Value {
    let transaction: Value = serde_json::from_str(include_str!(
        "../../../protocol/v2/fixtures/transaction-request-canonical.json"
    ))
    .expect("frozen v2 fixture is JSON");
    json!({
        "method": "submit-transaction",
        "request": {
            "request_id": "request-digest",
            "protocol_session_id": "protocol-session-1",
            "negotiation_token": "negotiation-1",
            "params": transaction
        }
    })
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

#[test]
fn versioned_framing_uses_only_the_exact_negotiated_wire_shape() {
    let v2_payload =
        serde_json::to_vec(&framed_transaction_value()).expect("v2 request serializes");
    let v2_frame = framed_payload(&v2_payload);
    let normalized = read_rpc_request_for_version(
        &mut Cursor::new(&v2_frame),
        ProtocolVersion::try_from(2_u16).expect("v2 is canonical"),
    )
    .expect("v2 frame decodes")
    .expect("v2 frame contains a request");
    assert!(matches!(normalized, RpcRequest::SubmitTransaction(_)));
    assert!(matches!(
        read_rpc_request_for_version(
            &mut Cursor::new(&v2_frame),
            ProtocolVersion::try_from(3_u16).expect("v3 is canonical"),
        ),
        Err(FrameError::InvalidRequest(
            hfx_protocol::ProtocolWireError::MalformedJson
        ))
    ));

    let v3_payload = serde_json::to_vec(&normalized).expect("normalized v3 request serializes");
    let v3_frame = framed_payload(&v3_payload);
    assert_eq!(
        read_rpc_request_for_version(
            &mut Cursor::new(&v3_frame),
            ProtocolVersion::try_from(3_u16).expect("v3 is canonical"),
        ),
        Ok(Some(normalized))
    );
    assert!(matches!(
        read_rpc_request_for_version(
            &mut Cursor::new(&v3_frame),
            ProtocolVersion::try_from(2_u16).expect("v2 is canonical"),
        ),
        Err(FrameError::InvalidRequest(
            hfx_protocol::ProtocolWireError::MalformedJson
        ))
    ));
}

#[test]
fn unsupported_response_version_emits_no_partial_frame() {
    let mut output = Vec::new();
    assert!(matches!(
        write_rpc_response_for_version(
            &mut output,
            &negotiation_response(),
            ProtocolVersion::try_from(6_u16).expect("version number is canonical"),
        ),
        Err(FrameError::InvalidResponse(
            hfx_protocol::ProtocolWireError::UnsupportedProtocolVersion
        ))
    ));
    assert!(output.is_empty());
}

#[test]
fn shared_framing_round_trips_both_rpc_directions() {
    let request = negotiation_request();
    let mut client_output = ChunkedWriter {
        chunk: 3,
        ..ChunkedWriter::default()
    };
    write_rpc_request(&mut client_output, &request).expect("client request write must pass");
    assert_eq!(client_output.flushes, 1);
    assert_eq!(
        read_rpc_request(&mut Cursor::new(client_output.bytes)),
        Ok(Some(request))
    );

    let response = negotiation_response();
    let version = ProtocolVersion::try_from(2_u16).expect("v2 is canonical");
    let mut server_output = ChunkedWriter {
        chunk: 3,
        ..ChunkedWriter::default()
    };
    write_rpc_response_for_version(&mut server_output, &response, version)
        .expect("server response write must pass");
    assert_eq!(server_output.flushes, 1);
    assert_eq!(
        read_rpc_response_for_version(&mut Cursor::new(server_output.bytes), version),
        Ok(Some(response))
    );
}

#[test]
fn current_response_reader_accepts_current_framing() {
    let response = negotiation_response();
    let mut output = Vec::new();
    write_rpc_response(&mut output, &response).expect("current response write must pass");
    assert_eq!(
        read_rpc_response(&mut Cursor::new(output)),
        Ok(Some(response))
    );
}

#[test]
fn unsafe_request_downgrade_emits_no_partial_frame() {
    let payload = serde_json::to_vec(&framed_transaction_value()).expect("v2 request serializes");
    let mut current = read_rpc_request_for_version(
        &mut Cursor::new(framed_payload(&payload)),
        ProtocolVersion::try_from(2_u16).expect("v2 is canonical"),
    )
    .expect("v2 frame decodes")
    .expect("v2 frame contains a request");
    let RpcRequest::SubmitTransaction(envelope) = &mut current else {
        panic!("fixture must remain a transaction");
    };
    envelope.params.stable_intents[0].mode = StableLightingMode::Off;
    envelope.params.frames[0].colors[0].red = ColorChannel::try_from(0_u8).expect("black red");
    envelope.params.frames[0].colors[0].green = ColorChannel::try_from(0_u8).expect("black green");
    envelope.params.frames[0].colors[0].blue = ColorChannel::try_from(0_u8).expect("black blue");

    let mut output = Vec::new();
    assert_eq!(
        write_rpc_request_for_version(
            &mut output,
            &current,
            ProtocolVersion::try_from(2_u16).expect("v2 is canonical"),
        ),
        Err(FrameError::InvalidRequest(
            hfx_protocol::ProtocolWireError::VersionTranslation
        ))
    );
    assert!(output.is_empty());
}
