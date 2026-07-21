// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod framing;
mod generated;
mod generated_versions;
mod negotiation;
mod snapshot;
mod validation;
mod wire;

pub use framing::{
    FRAME_LENGTH_BYTES, FrameError, FrameIoStage, read_rpc_request, read_rpc_request_for_version,
    read_rpc_response, read_rpc_response_for_version, write_rpc_request,
    write_rpc_request_for_version, write_rpc_response, write_rpc_response_for_version,
};
pub use generated::*;
pub use generated_versions::{
    CURRENT_PROTOCOL_VERSION, GENERATED_PROTOCOL_VERSIONS, ProtocolVersionDescriptor, v1, v2, v3,
    v4,
};
pub use negotiation::{
    GENERATED_CONTRACT, NegotiationContext, NegotiationError, ProtocolContract, negotiate,
    negotiate_with_contract,
};
pub use snapshot::{SnapshotValidationError, validate_bridge_snapshot};
pub use validation::{ProtocolValidationError, validate_lease_request, validate_transaction};
pub use wire::{
    ProtocolWireError, decode_rpc_request, decode_rpc_request_for_version, decode_rpc_response,
    decode_rpc_response_for_version, encode_rpc_request, encode_rpc_request_for_version,
    encode_rpc_response, encode_rpc_response_for_version, validate_rpc_request,
    validate_rpc_response, validate_rpc_response_for_version,
};
