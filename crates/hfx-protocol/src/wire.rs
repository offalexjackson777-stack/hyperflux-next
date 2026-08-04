// SPDX-License-Identifier: GPL-2.0-only

mod canonical;
mod codec;
mod versions;

pub use self::canonical::{validate_rpc_request, validate_rpc_response};
pub use self::versions::{
    decode_rpc_request, decode_rpc_request_for_version, decode_rpc_response,
    decode_rpc_response_for_version, encode_rpc_request, encode_rpc_request_for_version,
    encode_rpc_response, encode_rpc_response_for_version, validate_rpc_response_for_version,
};
use crate::{ProtocolValidationError, SnapshotValidationError};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProtocolWireError {
    MessageTooLarge,
    MalformedJson,
    RequestBoundExceeded,
    RequestNotCanonical,
    UnsupportedProtocolVersion,
    UnsupportedVersionMethod,
    VersionTranslation,
    InvalidRequest(ProtocolValidationError),
    InvalidSnapshot(SnapshotValidationError),
    InvalidResponse(&'static str),
}

impl fmt::Display for ProtocolWireError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::MessageTooLarge => "protocol message exceeds the encoded byte bound",
            Self::MalformedJson => "protocol message is malformed or exceeds the JSON depth bound",
            Self::RequestBoundExceeded => "protocol request exceeds a collection bound",
            Self::RequestNotCanonical => "protocol request contains duplicate or unordered values",
            Self::UnsupportedProtocolVersion => "protocol version is not registered",
            Self::UnsupportedVersionMethod => {
                "method is not safely served by the negotiated protocol version"
            }
            Self::VersionTranslation => "versioned protocol value cannot be normalized safely",
            Self::InvalidRequest(_) => "protocol request violates a method invariant",
            Self::InvalidSnapshot(_) => "protocol snapshot violates a projection invariant",
            Self::InvalidResponse(reason) => reason,
        })
    }
}

impl std::error::Error for ProtocolWireError {}
