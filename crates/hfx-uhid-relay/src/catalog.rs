// SPDX-License-Identifier: GPL-2.0-only

use crate::{NativeCommandPolicy, serial::SerialQuerySpec};
use hfx_domain::{DeviceKind, NativeProtocolFamily, ProductId, VendorId};
use hfx_integration_model::{SourceValidatedDevice, SourceValidatedDeviceCatalog};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

pub(crate) const SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS: u8 = 5;

const PROBE_CATALOG_JSON: &str =
    include_str!("../../../integrations/openrazer/native-probe-catalog.json");
const EXPECTED_PROBE_SCHEMA: &str = "hyperflux-openrazer-native-probe-catalog-v1";
const EXPECTED_DESCRIPTOR: &[u8] = &[
    0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x5a,
    0xb1, 0x02, 0xc0,
];
const PROTOCOL_FAMILY: &str = "razer-feature-report-v1";
const PHYS_PREFIX: &str = "hyperflux-next/native/v1/";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeRouteRecord {
    pub profile_id: String,
    pub vendor_id: VendorId,
    pub wireless_product_id: ProductId,
    pub model_name: String,
    pub device_kind: DeviceKind,
    pub driver_family: String,
    pub source_path: String,
    pub source_class: String,
    pub source_sha256: String,
    pub direct_data_product_ids: Vec<ProductId>,
    pub event_file_regex: Option<String>,
    pub publication_identity: SerialQuerySpec,
    pub qualification_probes: Vec<NativeQualificationProbe>,
    pub command_policy: NativeCommandPolicy,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeQualificationProbe {
    pub method: String,
    pub privacy: String,
    pub attribute: String,
    pub response_summary: String,
    pub comparison: String,
    pub command_class: u8,
    pub command_id: u8,
    pub data_size: u8,
    pub arguments: Vec<u8>,
    pub normalized_sha256: String,
}

impl NativeQualificationProbe {
    #[must_use]
    pub fn matches(&self, report: &[u8; 90]) -> bool {
        if report[0] != 0
            || report[1] == 0
            || report[2] != 0
            || report[3] != 0
            || report[4] != 0
            || report[5] != self.data_size
            || report[6] != self.command_class
            || report[7] != self.command_id
            || report[89] != 0
            || report[88] != report_crc(report)
        {
            return false;
        }
        let argument_count = usize::from(self.data_size);
        report[8..8 + argument_count] == self.arguments
            && report[8 + argument_count..88].iter().all(|byte| *byte == 0)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeRouteCatalog {
    upstream_commit: String,
    protocol_family: NativeProtocolFamily,
    descriptor: Vec<u8>,
    qualification_maximum_attempts: u8,
    routes: Vec<NativeRouteRecord>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeRouteCatalogError {
    InvalidJson,
    InvalidSchema,
    InvalidTransport,
    InvalidRoute,
    DuplicateRoute,
    ProbeCatalogDrift,
    CommandCatalogDrift,
}

impl fmt::Display for NativeRouteCatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidJson => "OpenRazer native-route catalog JSON is invalid",
            Self::InvalidSchema => "OpenRazer native-route catalog schema is invalid",
            Self::InvalidTransport => "OpenRazer native Feature transport is invalid",
            Self::InvalidRoute => "OpenRazer native route is invalid",
            Self::DuplicateRoute => "OpenRazer native route identity is duplicated",
            Self::ProbeCatalogDrift => {
                "OpenRazer assurance probes no longer match their source-validated routes"
            }
            Self::CommandCatalogDrift => {
                "OpenRazer runtime command policy no longer matches its source-validated route"
            }
        })
    }
}

impl std::error::Error for NativeRouteCatalogError {}

#[derive(Debug, Deserialize)]
struct ProbeCatalogDocument {
    schema: String,
    upstream: ProbeUpstreamDocument,
    report_size: u16,
    busy_retry: ProbeBusyRetryDocument,
    devices: Vec<ProbeDeviceDocument>,
}

#[derive(Debug, Deserialize)]
struct ProbeUpstreamDocument {
    commit: String,
}

#[derive(Debug, Deserialize)]
struct ProbeBusyRetryDocument {
    status: u8,
    maximum_attempts: u8,
}

#[derive(Debug, Deserialize)]
struct ProbeDeviceDocument {
    profile_id: String,
    wireless_identity: ProbeIdentityDocument,
    direct_usb_reference: ProbeIdentityDocument,
    probes: Vec<ProbeDocument>,
}

#[derive(Debug, Deserialize)]
struct ProbeIdentityDocument {
    vendor_id: u16,
    product_id: u16,
    model_name: String,
    device_kind: String,
    transport_variant: String,
}

#[derive(Debug, Deserialize)]
struct ProbeDocument {
    method: String,
    privacy: String,
    driver: ProbeDriverDocument,
    request: ProbeRequestDocument,
    response_summary: String,
    comparison: String,
}

#[derive(Debug, Deserialize)]
struct ProbeDriverDocument {
    attribute: String,
    reader: String,
    request_owner: String,
}

#[derive(Debug, Deserialize)]
struct ProbeRequestDocument {
    command_class: u8,
    command_id: u8,
    data_size: u8,
    arguments: Vec<u8>,
    normalized_sha256: String,
    transaction_id_from_upstream: bool,
    crc_recomputed_by_upstream: bool,
}

struct BoundProbeDevice {
    wireless_identity: ProbeIdentityDocument,
    direct_usb_reference: ProbeIdentityDocument,
    probes: Vec<NativeQualificationProbe>,
}

impl NativeRouteCatalog {
    /// Loads every OpenRazer-operational exact identity from the generated catalog.
    /// Hardware qualification probes are optional assurance attached to known records.
    ///
    /// # Errors
    ///
    /// Fails closed when source bindings, transport shape, exact identities, command
    /// policies, or retained assurance evidence disagree.
    pub fn load() -> Result<Self, NativeRouteCatalogError> {
        let operational = SourceValidatedDeviceCatalog::load()
            .map_err(|_| NativeRouteCatalogError::InvalidSchema)?;
        let protocol_family = NativeProtocolFamily::try_from(PROTOCOL_FAMILY.to_owned())
            .map_err(|_| NativeRouteCatalogError::InvalidTransport)?;
        let (mut probes, qualification_maximum_attempts) =
            load_qualification_probes(operational.openrazer_commit())?;
        let mut identities = BTreeSet::new();
        let mut profiles = BTreeSet::new();
        let mut routes = Vec::new();
        for device in operational.devices() {
            let Some(openrazer) = &device.openrazer else {
                continue;
            };
            if !device.operational()
                || !identities.insert((device.vendor_id, device.product_id, device.device_kind))
            {
                return Err(NativeRouteCatalogError::DuplicateRoute);
            }
            let profile_id = device
                .hardware_profile_id
                .clone()
                .unwrap_or_else(|| device.route_id.clone());
            if !profiles.insert(profile_id.clone()) {
                return Err(NativeRouteCatalogError::DuplicateRoute);
            }
            let qualification_probes = match device.hardware_profile_id.as_ref() {
                Some(profile_id) => {
                    let bound = probes
                        .remove(profile_id)
                        .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
                    validate_probe_identity(device, &bound)?;
                    bound.probes
                }
                None => Vec::new(),
            };
            let direct_data_product_ids = openrazer
                .direct_usb_product_id
                .iter()
                .copied()
                .collect::<Vec<_>>();
            let publication_identity = SerialQuerySpec::new(
                openrazer.publication_identity.transaction_id,
                openrazer.publication_identity.command_class,
                openrazer.publication_identity.command_id,
                openrazer.publication_identity.data_size,
                openrazer.publication_identity.arguments.clone(),
                openrazer.publication_identity.normalized_sha256.clone(),
            )
            .map_err(|_| NativeRouteCatalogError::InvalidRoute)?;
            let command_policy = device
                .openrazer_command_policy
                .clone()
                .ok_or(NativeRouteCatalogError::CommandCatalogDrift)?;
            routes.push(NativeRouteRecord {
                profile_id,
                vendor_id: device.vendor_id,
                wireless_product_id: device.product_id,
                model_name: device.model_name.clone(),
                device_kind: device.device_kind,
                driver_family: openrazer.driver_family.clone(),
                source_path: openrazer.source_path.clone(),
                source_class: openrazer.class_name.clone(),
                source_sha256: openrazer.source_sha256.clone(),
                direct_data_product_ids,
                event_file_regex: openrazer.event_file_regex.clone(),
                publication_identity,
                qualification_probes,
                command_policy,
            });
        }
        if routes.is_empty() || routes.len() > 256 || !probes.is_empty() {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
        routes.sort_by_key(|route| {
            (
                route.vendor_id,
                route.wireless_product_id,
                route.device_kind,
            )
        });
        Ok(Self {
            upstream_commit: operational.openrazer_commit().to_owned(),
            protocol_family,
            descriptor: EXPECTED_DESCRIPTOR.to_vec(),
            qualification_maximum_attempts,
            routes,
        })
    }

    #[must_use]
    pub fn route(
        &self,
        vendor_id: VendorId,
        product_id: ProductId,
        device_kind: DeviceKind,
    ) -> Option<&NativeRouteRecord> {
        self.routes.iter().find(|route| {
            route.vendor_id == vendor_id
                && route.wireless_product_id == product_id
                && route.device_kind == device_kind
        })
    }

    #[must_use]
    pub fn routes(&self) -> &[NativeRouteRecord] {
        &self.routes
    }

    #[must_use]
    pub fn profile(&self, profile_id: &str) -> Option<&NativeRouteRecord> {
        self.routes
            .iter()
            .find(|route| route.profile_id == profile_id)
    }

    #[must_use]
    pub fn descriptor(&self) -> &[u8] {
        &self.descriptor
    }

    #[must_use]
    pub fn protocol_family(&self) -> &NativeProtocolFamily {
        &self.protocol_family
    }

    #[must_use]
    pub const fn phys_prefix(&self) -> &str {
        PHYS_PREFIX
    }

    #[must_use]
    pub fn upstream_commit(&self) -> &str {
        &self.upstream_commit
    }

    #[must_use]
    pub const fn qualification_maximum_attempts(&self) -> u8 {
        self.qualification_maximum_attempts
    }
}

fn load_qualification_probes(
    expected_commit: &str,
) -> Result<(BTreeMap<String, BoundProbeDevice>, u8), NativeRouteCatalogError> {
    let document: ProbeCatalogDocument = serde_json::from_str(PROBE_CATALOG_JSON)
        .map_err(|_| NativeRouteCatalogError::InvalidJson)?;
    if document.schema != EXPECTED_PROBE_SCHEMA
        || document.upstream.commit != expected_commit
        || document.report_size != 90
        || document.busy_retry.status != 1
        || document.busy_retry.maximum_attempts != SOURCE_BUSY_RETRY_MAXIMUM_ATTEMPTS
    {
        return Err(NativeRouteCatalogError::ProbeCatalogDrift);
    }
    let mut devices = BTreeMap::new();
    for device in document.devices {
        let mut methods = BTreeSet::new();
        let mut attributes = BTreeSet::new();
        let mut qualification_probes = Vec::with_capacity(device.probes.len());
        for probe in device.probes {
            if probe.method.is_empty()
                || probe.method.len() > 128
                || !methods.insert(probe.method.clone())
                || probe.privacy.is_empty()
                || probe.driver.attribute.is_empty()
                || !attributes.insert(probe.driver.attribute.clone())
                || probe.driver.reader.is_empty()
                || probe.driver.request_owner.is_empty()
                || probe.response_summary.is_empty()
                || !matches!(
                    probe.comparison.as_str(),
                    "exact-semantic" | "domain-valid" | "redacted-equality"
                )
                || probe.request.command_id < 0x80
                || usize::from(probe.request.data_size) != probe.request.arguments.len()
                || probe.request.data_size > 80
                || !valid_sha256(&probe.request.normalized_sha256)
                || !probe.request.transaction_id_from_upstream
                || !probe.request.crc_recomputed_by_upstream
            {
                return Err(NativeRouteCatalogError::ProbeCatalogDrift);
            }
            let mut normalized = [0_u8; 90];
            normalized[5] = probe.request.data_size;
            normalized[6] = probe.request.command_class;
            normalized[7] = probe.request.command_id;
            normalized[8..8 + probe.request.arguments.len()]
                .copy_from_slice(&probe.request.arguments);
            if probe.request.normalized_sha256 != hex_sha256(&normalized) {
                return Err(NativeRouteCatalogError::ProbeCatalogDrift);
            }
            qualification_probes.push(NativeQualificationProbe {
                method: probe.method,
                privacy: probe.privacy,
                attribute: probe.driver.attribute,
                response_summary: probe.response_summary,
                comparison: probe.comparison,
                command_class: probe.request.command_class,
                command_id: probe.request.command_id,
                data_size: probe.request.data_size,
                arguments: probe.request.arguments,
                normalized_sha256: probe.request.normalized_sha256,
            });
        }
        if qualification_probes.is_empty() {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
        let serials = qualification_probes
            .iter()
            .filter(|probe| probe.method == "get_serial")
            .collect::<Vec<_>>();
        if serials.len() != 1
            || serials[0].privacy != "redacted-equality-only"
            || serials[0].attribute != "device_serial"
            || serials[0].response_summary != "redacted-serial-equality"
            || serials[0].comparison != "redacted-equality"
        {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
        if devices
            .insert(
                device.profile_id,
                BoundProbeDevice {
                    wireless_identity: device.wireless_identity,
                    direct_usb_reference: device.direct_usb_reference,
                    probes: qualification_probes,
                },
            )
            .is_some()
        {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
    }
    Ok((devices, document.busy_retry.maximum_attempts))
}

fn validate_probe_identity(
    device: &SourceValidatedDevice,
    probe: &BoundProbeDevice,
) -> Result<(), NativeRouteCatalogError> {
    let openrazer = device
        .openrazer
        .as_ref()
        .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
    let expected_kind = match device.device_kind {
        DeviceKind::Mouse => "mouse",
        DeviceKind::Keyboard => "keyboard",
        _ => return Err(NativeRouteCatalogError::ProbeCatalogDrift),
    };
    let wireless = &probe.wireless_identity;
    let direct = &probe.direct_usb_reference;
    // Assurance keeps the source presentation captured during qualification.
    // Exact transport identities and kind bind it; a later canonical display
    // name cannot grant, revoke, or otherwise change operational access.
    if wireless.vendor_id != device.vendor_id.get()
        || wireless.product_id != device.product_id.get()
        || wireless.model_name.is_empty()
        || wireless.device_kind != expected_kind
        || wireless.transport_variant != "wireless"
        || direct.vendor_id != device.vendor_id.get()
        || direct.device_kind != expected_kind
        || direct.transport_variant != "wired"
        || direct.model_name.is_empty()
        || openrazer.direct_usb_product_id.map(ProductId::get) != Some(direct.product_id)
    {
        return Err(NativeRouteCatalogError::ProbeCatalogDrift);
    }
    Ok(())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn report_crc(report: &[u8; 90]) -> u8 {
    report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte)
}

fn hex_sha256(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    let mut output = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    fn upstream_request(probe: &NativeQualificationProbe, transaction_id: u8) -> [u8; 90] {
        let mut report = [0_u8; 90];
        report[1] = transaction_id;
        report[5] = probe.data_size;
        report[6] = probe.command_class;
        report[7] = probe.command_id;
        report[8..8 + probe.arguments.len()].copy_from_slice(&probe.arguments);
        report[88] = report_crc(&report);
        report
    }

    #[test]
    fn generated_catalog_exposes_operational_devices_beyond_assurance_profiles() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let original_basilisk = catalog
            .route(
                VendorId::try_from(0x1532_u16).expect("vendor"),
                ProductId::try_from(0x00ab_u16).expect("product"),
                DeviceKind::Mouse,
            )
            .expect("original Basilisk V3 Pro exists automatically");
        assert_eq!(original_basilisk.source_class, "RazerBasiliskV3ProWireless");
        assert_eq!(
            original_basilisk
                .direct_data_product_ids
                .iter()
                .map(|product| product.get())
                .collect::<Vec<_>>(),
            vec![0x00aa]
        );
        assert!(original_basilisk.qualification_probes.is_empty());
        assert!(
            catalog
                .route(
                    VendorId::try_from(0x1532_u16).expect("vendor"),
                    ProductId::try_from(0x02ba_u16).expect("product"),
                    DeviceKind::Keyboard,
                )
                .is_some(),
            "official OpenRazer-only source intersection is exposed"
        );
        assert!(
            catalog
                .route(
                    VendorId::try_from(0x1532_u16).expect("vendor"),
                    ProductId::try_from(0x02d2_u16).expect("product"),
                    DeviceKind::Keyboard,
                )
                .is_none(),
            "official OpenRGB-only source intersection is not exposed through OpenRazer"
        );
        assert!(
            catalog
                .route(
                    VendorId::try_from(0x1532_u16).expect("vendor"),
                    ProductId::try_from(0x0062_u16).expect("product"),
                    DeviceKind::Mouse,
                )
                .is_none(),
            "non-official upstream support is outside receiver authority"
        );
        assert!(
            catalog
                .route(
                    VendorId::try_from(0x1532_u16).expect("vendor"),
                    ProductId::try_from(0x00cf_u16).expect("product"),
                    DeviceKind::Mouse,
                )
                .is_none(),
            "receiver transport is never exposed as a child device"
        );
        assert!(catalog.routes().len() > 2);
        for route in catalog.routes() {
            assert!(
                route
                    .command_policy
                    .allows(&route.publication_identity.request()),
                "the publication identity remains inside the source-generated route policy"
            );
        }
    }

    #[test]
    fn retained_hardware_assurance_probes_remain_attached() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mouse = catalog
            .profile("child.razer.basilisk-v3-pro-35k.00cd")
            .expect("mouse exists");
        let keyboard = catalog
            .profile("child.razer.deathstalker-v2-pro-tkl.0296")
            .expect("keyboard exists");
        assert_eq!(mouse.qualification_probes.len(), 16);
        assert_eq!(keyboard.qualification_probes.len(), 10);
        assert!(keyboard.qualification_probes.iter().any(|probe| {
            probe.method == "get_brightness"
                && probe.command_class == 0x0f
                && probe.command_id == 0x84
                && probe.arguments == [0x01, 0x05, 0x00]
        }));
    }

    #[test]
    fn matcher_accepts_only_the_exact_upstream_read_signature() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let probe = catalog
            .routes()
            .iter()
            .find_map(|route| route.qualification_probes.first())
            .expect("at least one assurance probe exists");
        let request = upstream_request(probe, 0x1f);
        assert!(probe.matches(&request));

        let mut zero_transaction = request;
        zero_transaction[1] = 0;
        zero_transaction[88] = report_crc(&zero_transaction);
        assert!(!probe.matches(&zero_transaction));

        let mut changed_argument = request;
        changed_argument[8] ^= 1;
        changed_argument[88] = report_crc(&changed_argument);
        assert!(!probe.matches(&changed_argument));

        let mut bad_crc = request;
        bad_crc[88] ^= 1;
        assert!(!probe.matches(&bad_crc));
    }
}
