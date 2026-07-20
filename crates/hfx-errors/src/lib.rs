// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod generated;
mod validation;

pub use generated::*;
pub use hfx_domain::{ErrorSeverity, PrivacyClass};
pub use validation::{
    SafeDetail, SafeDetailValidationError, SafeDetailValue, validate_safe_details,
};
