// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod config;
mod generated;

pub use config::{
    BRIDGE_CONFIG_SCHEMA, BridgeConfig, BridgeConfigError, BridgeMode, RestorationConfig,
    SocketConfig,
};
pub use generated::*;
