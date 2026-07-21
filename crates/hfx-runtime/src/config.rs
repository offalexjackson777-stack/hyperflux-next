// SPDX-License-Identifier: GPL-2.0-only

use serde::{Deserialize, Serialize};
use std::fmt;

use crate::BRIDGE_CLIENT_GROUP;

pub const BRIDGE_CONFIG_SCHEMA: &str = "hyperflux-bridge-config-v1";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum BridgeMode {
    ReadOnly,
    QualifiedLive,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestorationConfig {
    pub enabled: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SocketConfig {
    pub group: String,
    pub mode: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeConfig {
    #[serde(rename = "$schema")]
    pub schema_path: String,
    pub schema: String,
    pub mode: BridgeMode,
    pub restoration: RestorationConfig,
    pub socket: SocketConfig,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeConfigError {
    UnsupportedSchema,
    InvalidSchemaPath,
    RestorationRequiresQualifiedLive,
    InvalidSocketGroup,
    InvalidSocketMode,
}

impl fmt::Display for BridgeConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::UnsupportedSchema => "bridge configuration schema is unsupported",
            Self::InvalidSchemaPath => "bridge configuration schema path is invalid",
            Self::RestorationRequiresQualifiedLive => {
                "stable restoration requires qualified-live bridge mode"
            }
            Self::InvalidSocketGroup => "bridge socket group does not match runtime authority",
            Self::InvalidSocketMode => "bridge socket mode must remain 0660",
        })
    }
}

impl std::error::Error for BridgeConfigError {}

impl BridgeConfig {
    /// Validates the persisted configuration against cross-file runtime policy.
    ///
    /// # Errors
    ///
    /// Returns a bounded error when the schema or socket authority differs.
    pub fn validate(&self) -> Result<(), BridgeConfigError> {
        if self.schema != BRIDGE_CONFIG_SCHEMA {
            return Err(BridgeConfigError::UnsupportedSchema);
        }
        if self.schema_path != "/usr/share/hyperflux-next/schemas/bridge-config.schema.json" {
            return Err(BridgeConfigError::InvalidSchemaPath);
        }
        if self.restoration.enabled && self.mode != BridgeMode::QualifiedLive {
            return Err(BridgeConfigError::RestorationRequiresQualifiedLive);
        }
        if self.socket.group != BRIDGE_CLIENT_GROUP {
            return Err(BridgeConfigError::InvalidSocketGroup);
        }
        if self.socket.mode != "0660" {
            return Err(BridgeConfigError::InvalidSocketMode);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT_CONFIG: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../packaging/generated/bridge.json"
    ));

    #[test]
    fn generated_default_configuration_is_typed_and_read_only() {
        let config: BridgeConfig = serde_json::from_str(DEFAULT_CONFIG).expect("default config");
        config.validate().expect("valid default config");
        assert_eq!(config.mode, BridgeMode::ReadOnly);
        assert!(!config.restoration.enabled);
    }

    #[test]
    fn unknown_fields_and_unsafe_socket_modes_are_rejected() {
        let unknown = DEFAULT_CONFIG.replace(
            "\"mode\": \"read-only\",",
            "\"mode\": \"read-only\", \"surprise\": true,",
        );
        assert!(serde_json::from_str::<BridgeConfig>(&unknown).is_err());

        let mut config: BridgeConfig =
            serde_json::from_str(DEFAULT_CONFIG).expect("default config");
        config.socket.mode = "0666".to_owned();
        assert_eq!(config.validate(), Err(BridgeConfigError::InvalidSocketMode));
    }

    #[test]
    fn restoration_cannot_turn_a_read_only_service_into_a_writer() {
        let mut config: BridgeConfig =
            serde_json::from_str(DEFAULT_CONFIG).expect("default config");
        config.restoration.enabled = true;
        assert_eq!(
            config.validate(),
            Err(BridgeConfigError::RestorationRequiresQualifiedLive)
        );
        config.mode = BridgeMode::QualifiedLive;
        config
            .validate()
            .expect("qualified live restoration is valid");
    }
}
