// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod generated;
mod negotiation;
mod validation;

pub use generated::*;
pub use negotiation::{NegotiationError, negotiate};
pub use validation::{ProtocolValidationError, validate_lease_request, validate_transaction};
