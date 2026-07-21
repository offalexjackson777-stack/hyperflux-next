// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod catalog;
mod generated;

pub use catalog::{
    ProfileCatalogError, RuntimeCapability, RuntimeLightingTopology, RuntimeProfile,
    RuntimeProfileCatalog,
};
pub use generated::*;
