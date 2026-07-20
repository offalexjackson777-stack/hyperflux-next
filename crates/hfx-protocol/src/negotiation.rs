// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    ClientHello, MAXIMUM_PROTOCOL_VERSION, MINIMUM_PROTOCOL_VERSION, SUPPORTED_FEATURES,
    ServerHello,
};
use hfx_domain::{
    ComponentVersion, NegotiationToken, ProtocolFeatureId, ProtocolSessionId, ProtocolVersion,
    QueueCapacity, ServerInstanceId,
};
use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProtocolContract<'a> {
    pub minimum_version: u16,
    pub maximum_version: u16,
    pub features: &'a [&'a str],
}

pub const GENERATED_CONTRACT: ProtocolContract<'static> = ProtocolContract {
    minimum_version: MINIMUM_PROTOCOL_VERSION,
    maximum_version: MAXIMUM_PROTOCOL_VERSION,
    features: SUPPORTED_FEATURES,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NegotiationContext {
    pub server_instance_id: ServerInstanceId,
    pub protocol_session_id: ProtocolSessionId,
    pub negotiation_token: NegotiationToken,
    pub bridge_version: ComponentVersion,
    pub event_buffer_capacity: QueueCapacity,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NegotiationError {
    InvalidClientRange,
    InvalidServerRange,
    IncompatibleVersion,
    TooManyFeatures,
    DuplicateFeature,
    UnsupportedRequiredFeatures(Vec<ProtocolFeatureId>),
    InvalidBridgeContract,
}

impl fmt::Display for NegotiationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidClientRange => "client protocol range is reversed",
            Self::InvalidServerRange => "server protocol range is invalid",
            Self::IncompatibleVersion => "client and bridge protocol ranges do not overlap",
            Self::TooManyFeatures => "client feature offer exceeds the protocol bound",
            Self::DuplicateFeature => "client feature offer contains duplicates",
            Self::UnsupportedRequiredFeatures(_) => "required protocol features are unsupported",
            Self::InvalidBridgeContract => "generated bridge protocol contract is invalid",
        })
    }
}

impl std::error::Error for NegotiationError {}

/// Negotiates against the generated protocol-v1 contract.
///
/// # Errors
///
/// Returns an error for incompatible versions, malformed feature offers, or
/// unsupported required features.
pub fn negotiate(
    hello: &ClientHello,
    context: NegotiationContext,
) -> Result<ServerHello, NegotiationError> {
    negotiate_with_contract(hello, context, GENERATED_CONTRACT)
}

/// Negotiates against an explicit server range for compatibility tests and
/// future version dispatch.
///
/// # Errors
///
/// Returns an error for invalid ranges, malformed feature offers, unsupported
/// required features, or invalid server contract values.
pub fn negotiate_with_contract(
    hello: &ClientHello,
    context: NegotiationContext,
    contract: ProtocolContract<'_>,
) -> Result<ServerHello, NegotiationError> {
    if hello.minimum_version > hello.maximum_version {
        return Err(NegotiationError::InvalidClientRange);
    }
    if contract.minimum_version == 0 || contract.minimum_version > contract.maximum_version {
        return Err(NegotiationError::InvalidServerRange);
    }
    if hello.required_features.len() > 64 || hello.optional_features.len() > 64 {
        return Err(NegotiationError::TooManyFeatures);
    }
    let required = hello
        .required_features
        .iter()
        .map(ProtocolFeatureId::as_str)
        .collect::<BTreeSet<_>>();
    let optional = hello
        .optional_features
        .iter()
        .map(ProtocolFeatureId::as_str)
        .collect::<BTreeSet<_>>();
    if required.len() != hello.required_features.len()
        || optional.len() != hello.optional_features.len()
        || !required.is_disjoint(&optional)
    {
        return Err(NegotiationError::DuplicateFeature);
    }
    let lower = hello.minimum_version.get().max(contract.minimum_version);
    let upper = hello.maximum_version.get().min(contract.maximum_version);
    if lower > upper {
        return Err(NegotiationError::IncompatibleVersion);
    }
    let available = contract.features.iter().copied().collect::<BTreeSet<_>>();
    let unsupported = hello
        .required_features
        .iter()
        .filter(|feature| !available.contains(feature.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    if !unsupported.is_empty() {
        return Err(NegotiationError::UnsupportedRequiredFeatures(unsupported));
    }
    let selected_version =
        ProtocolVersion::try_from(upper).map_err(|_| NegotiationError::InvalidBridgeContract)?;
    let mut enabled_features = hello
        .required_features
        .iter()
        .chain(&hello.optional_features)
        .filter(|feature| available.contains(feature.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    enabled_features.sort_unstable();
    Ok(ServerHello {
        selected_version,
        server_instance_id: context.server_instance_id,
        protocol_session_id: context.protocol_session_id,
        negotiation_token: context.negotiation_token,
        bridge_version: context.bridge_version,
        enabled_features,
        event_buffer_capacity: context.event_buffer_capacity,
    })
}
