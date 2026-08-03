// SPDX-License-Identifier: GPL-2.0-only

mod binding;
mod document;
mod validation;

use self::binding::bind_device;
#[cfg(test)]
use self::binding::{bind_command_policy, bind_openrazer, bind_openrgb};
use self::document::parse_catalog;
#[cfg(test)]
use self::document::{CatalogDocument, OpenRazerDocument, OpenRgbDocument, RuleDocument};
use self::validation::validate_authority;
#[cfg(test)]
use self::validation::{validate_publication_identity_rule, validate_rules};
use crate::NativeCommandPolicy;
#[cfg(test)]
use crate::{NativePolicyRuleInput, native_command_policy_from_rules};
use hfx_domain::{DeviceKind, ProductId, VendorId};
use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceValidatedDeviceCatalog {
    openrazer_commit: String,
    openrgb_commit: String,
    devices: Vec<SourceValidatedDevice>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceValidatedDevice {
    pub route_id: String,
    pub vendor_id: VendorId,
    pub product_id: ProductId,
    pub device_kind: DeviceKind,
    pub model_name: String,
    pub hardware_profile_id: Option<String>,
    pub openrazer: Option<SourceValidatedOpenRazer>,
    pub openrgb: Option<SourceValidatedOpenRgb>,
    /// `OpenRazer`'s source-proven subset, used for adapter-local preflight.
    pub openrazer_command_policy: Option<NativeCommandPolicy>,
    /// `OpenRGB`'s source-proven subset, used for adapter-local preflight.
    pub openrgb_command_policy: Option<NativeCommandPolicy>,
    /// Complete safe per-device union used by the application-neutral bridge.
    ///
    /// Native clients are authenticated and lease-bound, but application names
    /// are not an authorization claim. The bridge therefore admits this union
    /// and never selects a policy from a client-supplied application label.
    pub command_policy: Option<NativeCommandPolicy>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceValidatedOpenRazer {
    pub record_id: String,
    pub class_name: String,
    pub source_path: String,
    pub source_sha256: String,
    pub image_url: String,
    pub methods: Vec<String>,
    pub driver_family: String,
    pub event_file_regex: Option<String>,
    pub direct_usb_product_id: Option<ProductId>,
    pub publication_identity: SourceValidatedPublicationIdentity,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceValidatedPublicationIdentity {
    pub method: String,
    pub transaction_id: u8,
    pub command_class: u8,
    pub command_id: u8,
    pub data_size: u8,
    pub arguments: Vec<u8>,
    pub normalized_sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceValidatedOpenRgb {
    pub record_id: String,
    pub source_device_key: String,
    pub transaction_id: u8,
    pub led_selector: u8,
    pub rows: u8,
    pub columns: u8,
    pub led_count: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceValidatedCatalogError {
    InvalidJson,
    InvalidSchema,
    SourceDrift,
    InvalidDevice,
    DuplicateDevice,
    InvalidIntegration,
    InvalidPolicy,
}

impl fmt::Display for SourceValidatedCatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidJson => "the source-validated device catalog JSON is invalid",
            Self::InvalidSchema => "the source-validated device catalog schema is invalid",
            Self::SourceDrift => "a source-validated upstream catalog binding drifted",
            Self::InvalidDevice => "a source-validated device identity is invalid",
            Self::DuplicateDevice => "a source-validated device identity is duplicated",
            Self::InvalidIntegration => "a source-validated integration record is invalid",
            Self::InvalidPolicy => "a source-validated command policy is invalid",
        })
    }
}

impl std::error::Error for SourceValidatedCatalogError {}

impl SourceValidatedDeviceCatalog {
    /// Loads the generated exact-identity operational catalog embedded in this binary.
    ///
    /// # Errors
    ///
    /// Fails closed for malformed identities, stale upstream bindings, ambiguous records,
    /// or a command rule that cannot be represented by the strict native firewall.
    pub fn load() -> Result<Self, SourceValidatedCatalogError> {
        let document = parse_catalog()?;
        validate_authority(&document)?;
        let mut identities = BTreeSet::new();
        let mut route_ids = BTreeSet::new();
        let mut previous_route: Option<String> = None;
        let mut devices = Vec::with_capacity(document.devices.len());
        for device in document.devices {
            if previous_route
                .as_ref()
                .is_some_and(|previous| previous.as_str() >= device.route_id.as_str())
                || !route_ids.insert(device.route_id.clone())
            {
                return Err(SourceValidatedCatalogError::DuplicateDevice);
            }
            previous_route = Some(device.route_id.clone());
            let bound = bind_device(device)?;
            if !identities.insert((bound.vendor_id, bound.product_id, bound.device_kind)) {
                return Err(SourceValidatedCatalogError::DuplicateDevice);
            }
            devices.push(bound);
        }
        if devices.is_empty() || devices.len() > 256 {
            return Err(SourceValidatedCatalogError::InvalidSchema);
        }
        Ok(Self {
            openrazer_commit: document.sources.openrazer.commit,
            openrgb_commit: document.sources.openrgb.commit,
            devices,
        })
    }

    #[must_use]
    pub fn device(
        &self,
        vendor_id: VendorId,
        product_id: ProductId,
        device_kind: DeviceKind,
    ) -> Option<&SourceValidatedDevice> {
        self.devices.iter().find(|device| {
            device.vendor_id == vendor_id
                && device.product_id == product_id
                && device.device_kind == device_kind
        })
    }

    #[must_use]
    pub fn devices(&self) -> &[SourceValidatedDevice] {
        &self.devices
    }

    #[must_use]
    pub fn openrazer_commit(&self) -> &str {
        &self.openrazer_commit
    }

    #[must_use]
    pub fn openrgb_commit(&self) -> &str {
        &self.openrgb_commit
    }
}

impl SourceValidatedDevice {
    #[must_use]
    pub const fn operational(&self) -> bool {
        self.command_policy.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::document::CATALOG_JSON;
    use super::*;

    fn report(command_class: u8, command_id: u8, arguments: &[u8]) -> [u8; 90] {
        let mut report = [0_u8; 90];
        report[1] = 0x1f;
        report[5] = u8::try_from(arguments.len()).expect("bounded test arguments");
        report[6] = command_class;
        report[7] = command_id;
        report[8..8 + arguments.len()].copy_from_slice(arguments);
        report[88] = report[2..88]
            .iter()
            .fold(0_u8, |checksum, byte| checksum ^ byte);
        report
    }

    fn synthetic_rule(
        rule_id: &str,
        integration: &str,
        command_class: u8,
        command_id: u8,
        argument: u8,
    ) -> RuleDocument {
        RuleDocument {
            id: rule_id.to_owned(),
            integrations: vec![integration.to_owned()],
            methods: vec![format!("{integration}_synthetic_command")],
            direction: "write".to_owned(),
            command_class,
            command_id,
            data_sizes: vec![1],
            transaction_ids: None,
            matcher: serde_json::json!({
                "kind": "fields",
                "fields": [{
                    "kind": "exact",
                    "offset": 0,
                    "bytes": [argument]
                }]
            }),
            sources: Vec::new(),
        }
    }

    #[test]
    fn every_generated_rule_binds_independently() {
        let document: CatalogDocument =
            serde_json::from_str(CATALOG_JSON).expect("catalog JSON parses");
        for device in document.devices {
            for rule in device.rules {
                native_command_policy_from_rules(vec![NativePolicyRuleInput {
                    command_class: rule.command_class,
                    command_id: rule.command_id,
                    data_sizes: rule.data_sizes,
                    matcher: rule.matcher,
                }])
                .unwrap_or_else(|_| panic!("{} rule {} binds", device.route_id, rule.id));
            }
        }
    }

    #[test]
    fn every_generated_rule_set_and_publication_identity_is_consistent() {
        let document: CatalogDocument =
            serde_json::from_str(CATALOG_JSON).expect("catalog JSON parses");
        for device in document.devices {
            let openrazer = matches!(
                &device.integrations.openrazer,
                OpenRazerDocument::SourceValidated { .. }
            );
            let openrgb = matches!(
                &device.integrations.openrgb,
                OpenRgbDocument::SourceValidated { .. }
            );
            validate_rules(&device.rules, openrazer, openrgb)
                .unwrap_or_else(|_| panic!("{} rule set validates", device.route_id));
            if let OpenRazerDocument::SourceValidated {
                publication_identity,
                ..
            } = device.integrations.openrazer
            {
                let query = SourceValidatedPublicationIdentity {
                    method: publication_identity.method,
                    transaction_id: publication_identity.transaction_id,
                    command_class: publication_identity.command_class,
                    command_id: publication_identity.command_id,
                    data_size: publication_identity.data_size,
                    arguments: publication_identity.arguments,
                    normalized_sha256: publication_identity.normalized_sha256,
                };
                validate_publication_identity_rule(&device.rules, &query).unwrap_or_else(|_| {
                    panic!("{} publication identity validates", device.route_id)
                });
            }
        }
    }

    #[test]
    fn every_generated_integration_binding_is_consistent() {
        let document: CatalogDocument =
            serde_json::from_str(CATALOG_JSON).expect("catalog JSON parses");
        for device in document.devices {
            let kind = match device.identity.device_kind.as_str() {
                "mouse" => DeviceKind::Mouse,
                "keyboard" => DeviceKind::Keyboard,
                _ => panic!("{} has a supported kind", device.route_id),
            };
            bind_openrazer(device.integrations.openrazer, kind)
                .unwrap_or_else(|_| panic!("{} OpenRazer binding validates", device.route_id));
            bind_openrgb(device.integrations.openrgb)
                .unwrap_or_else(|_| panic!("{} OpenRGB binding validates", device.route_id));
        }
    }

    #[test]
    fn exact_upstream_intersection_loads_without_hardware_promotion() {
        let catalog = SourceValidatedDeviceCatalog::load().expect("generated catalog loads");
        let mouse = catalog
            .device(
                VendorId::try_from(0x1532_u16).expect("valid vendor"),
                ProductId::try_from(0x00ab_u16).expect("valid product"),
                DeviceKind::Mouse,
            )
            .expect("original Basilisk V3 Pro is source validated");
        assert!(mouse.hardware_profile_id.is_none());
        let openrazer = mouse.openrazer.as_ref().expect("OpenRazer is available");
        assert_eq!(openrazer.class_name, "RazerBasiliskV3ProWireless");
        assert_eq!(
            openrazer.direct_usb_product_id.map(ProductId::get),
            Some(0x00aa)
        );
        assert!(
            openrazer
                .methods
                .iter()
                .any(|method| method == "set_dpi_stages")
        );
        assert!(
            openrazer
                .methods
                .iter()
                .any(|method| method == "set_scroll_smart_reel")
        );
        let openrgb = mouse.openrgb.as_ref().expect("OpenRGB is available");
        assert_eq!(openrgb.led_count, 13);
    }

    #[test]
    fn assurance_does_not_change_operational_access() {
        let catalog = SourceValidatedDeviceCatalog::load().expect("generated catalog loads");
        let unverified = catalog
            .devices()
            .iter()
            .find(|device| device.product_id.get() == 0x00ab)
            .expect("unverified operational fixture");
        let verified = catalog
            .devices()
            .iter()
            .find(|device| device.product_id.get() == 0x00cd)
            .expect("verified operational fixture");
        assert!(unverified.hardware_profile_id.is_none());
        assert!(verified.hardware_profile_id.is_some());
        assert!(unverified.openrazer.is_some() && unverified.openrgb.is_some());
        assert!(verified.openrazer.is_some() && verified.openrgb.is_some());
    }

    #[test]
    fn exact_kind_is_part_of_identity() {
        let catalog = SourceValidatedDeviceCatalog::load().expect("generated catalog loads");
        assert!(
            catalog
                .device(
                    VendorId::try_from(0x1532_u16).expect("valid vendor"),
                    ProductId::try_from(0x00ab_u16).expect("valid product"),
                    DeviceKind::Keyboard,
                )
                .is_none()
        );
    }

    #[test]
    fn adapter_policies_are_exact_subsets_of_the_route_policy() {
        let catalog = SourceValidatedDeviceCatalog::load().expect("generated catalog loads");
        let mouse = catalog
            .device(
                VendorId::try_from(0x1532_u16).expect("valid vendor"),
                ProductId::try_from(0x00ab_u16).expect("valid product"),
                DeviceKind::Mouse,
            )
            .expect("original Basilisk V3 Pro is source validated");
        let route = mouse.command_policy.as_ref().expect("route policy exists");
        let openrazer = mouse
            .openrazer_command_policy
            .as_ref()
            .expect("OpenRazer policy exists");
        let openrgb = mouse
            .openrgb_command_policy
            .as_ref()
            .expect("OpenRGB policy exists");

        let openrazer_dpi = report(0x04, 0x05, &[1, 0x03, 0x20, 0x03, 0x20, 0, 0]);
        assert!(route.allows(&openrazer_dpi));
        assert!(openrazer.allows(&openrazer_dpi));
        assert!(!openrgb.allows(&openrazer_dpi));

        let openrgb_breathing = report(0x0f, 0x02, &[0, 0, 2, 2, 0, 2, 0xff, 0, 0, 0, 0, 0xff]);
        assert!(route.allows(&openrgb_breathing));
        assert!(!openrazer.allows(&openrgb_breathing));
        assert!(openrgb.allows(&openrgb_breathing));
    }

    #[test]
    fn synthetic_device_uses_one_client_neutral_union_policy() {
        let rules = vec![
            synthetic_rule("synthetic-openrazer-a", "openrazer", 0x10, 0x01, 0xa1),
            synthetic_rule("synthetic-openrgb-b", "openrgb", 0x20, 0x02, 0xb2),
        ];
        let union = bind_command_policy(&rules, None)
            .expect("synthetic union binds")
            .expect("synthetic union exists");
        let openrazer = bind_command_policy(&rules, Some("openrazer"))
            .expect("OpenRazer subset binds")
            .expect("OpenRazer subset exists");
        let openrgb = bind_command_policy(&rules, Some("openrgb"))
            .expect("OpenRGB subset binds")
            .expect("OpenRGB subset exists");

        let command_a = report(0x10, 0x01, &[0xa1]);
        let command_b = report(0x20, 0x02, &[0xb2]);
        let command_c = report(0x30, 0x03, &[0xc3]);

        assert!(openrazer.allows(&command_a));
        assert!(!openrazer.allows(&command_b));
        assert!(!openrazer.allows(&command_c));
        assert!(!openrgb.allows(&command_a));
        assert!(openrgb.allows(&command_b));
        assert!(!openrgb.allows(&command_c));
        assert!(union.allows(&command_a));
        assert!(union.allows(&command_b));
        assert!(!union.allows(&command_c));
    }
}
