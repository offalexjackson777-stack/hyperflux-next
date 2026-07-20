// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod generated;
mod negotiation;
mod snapshot;
#[path = "generated_v1.rs"]
pub mod v1;
#[path = "generated_v2.rs"]
pub mod v2;
mod validation;
mod wire;

pub use generated::*;
pub use negotiation::{
    GENERATED_CONTRACT, NegotiationContext, NegotiationError, ProtocolContract, negotiate,
    negotiate_with_contract,
};
pub use snapshot::{SnapshotValidationError, validate_bridge_snapshot};
pub use validation::{ProtocolValidationError, validate_lease_request, validate_transaction};
pub use wire::{
    ProtocolWireError, decode_rpc_request, decode_rpc_response, validate_rpc_request,
    validate_rpc_response,
};
