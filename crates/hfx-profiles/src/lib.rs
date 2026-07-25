// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod catalog;
mod generated;
mod native_report;

pub use catalog::{
    ProfileCatalogError, RuntimeCapability, RuntimeLightingTopology, RuntimePresentation,
    RuntimeProfile, RuntimeProfileCatalog,
};
pub use generated::*;
pub use native_report::{
    NativeReportValidationError, native_response_matches, validate_native_exchange_record,
    validate_native_request, validate_native_response,
};
