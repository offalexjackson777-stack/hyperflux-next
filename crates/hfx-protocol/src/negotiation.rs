// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    ClientHello, MAXIMUM_PROTOCOL_VERSION, MINIMUM_PROTOCOL_VERSION, SUPPORTED_FEATURES,
    ServerHello,
};
use hfx_domain::{ComponentVersion, ProtocolVersion, QueueCapacity};
use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NegotiationError {
    InvalidClientRange,
    IncompatibleVersion,
    TooManyFeatures,
    DuplicateFeature,
    InvalidBridgeContract,
}

impl fmt::Display for NegotiationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidClientRange => "client protocol range is reversed",
            Self::IncompatibleVersion => "client and bridge protocol ranges do not overlap",
            Self::TooManyFeatures => "client feature offer exceeds the protocol bound",
            Self::DuplicateFeature => "client feature offer contains duplicates",
            Self::InvalidBridgeContract => "generated bridge protocol contract is invalid",
        })
    }
}

impl std::error::Error for NegotiationError {}

/// Negotiates one protocol version and the intersection of optional features.
///
/// # Errors
///
/// Returns an error for a reversed or incompatible protocol range, or for an
/// unbounded or duplicate feature offer.
pub fn negotiate(
    hello: &ClientHello,
    bridge_version: ComponentVersion,
    event_buffer_capacity: QueueCapacity,
) -> Result<ServerHello, NegotiationError> {
    if hello.minimum_version > hello.maximum_version {
        return Err(NegotiationError::InvalidClientRange);
    }
    if hello.requested_features.len() > 64 {
        return Err(NegotiationError::TooManyFeatures);
    }
    let requested = hello
        .requested_features
        .iter()
        .map(hfx_domain::CapabilityId::as_str)
        .collect::<BTreeSet<_>>();
    if requested.len() != hello.requested_features.len() {
        return Err(NegotiationError::DuplicateFeature);
    }
    let lower = hello.minimum_version.get().max(MINIMUM_PROTOCOL_VERSION);
    let upper = hello.maximum_version.get().min(MAXIMUM_PROTOCOL_VERSION);
    if lower > upper {
        return Err(NegotiationError::IncompatibleVersion);
    }
    let selected_version =
        ProtocolVersion::try_from(upper).map_err(|_| NegotiationError::InvalidBridgeContract)?;
    let enabled_features = SUPPORTED_FEATURES
        .iter()
        .filter(|feature| requested.contains(**feature))
        .map(|feature| {
            hfx_domain::CapabilityId::try_from(*feature)
                .map_err(|_| NegotiationError::InvalidBridgeContract)
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ServerHello {
        selected_version,
        bridge_version,
        enabled_features,
        event_buffer_capacity,
    })
}
