// SPDX-License-Identifier: GPL-2.0-only

use crate::serial::SerialQuerySpec;
use hfx_domain::{DeviceKind, NativeProtocolFamily, ProductId, VendorId};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

const CATALOG_JSON: &str =
    include_str!("../../../integrations/openrazer/native-route/catalog.json");
const METADATA_JSON: &str = include_str!("../../../integrations/openrazer/metadata.json");
const PROBE_CATALOG_JSON: &str =
    include_str!("../../../integrations/openrazer/native-probe-catalog.json");
const EXPECTED_SCHEMA: &str = "hyperflux-openrazer-native-route-catalog-v1";
const EXPECTED_PROBE_SCHEMA: &str = "hyperflux-openrazer-native-probe-catalog-v1";
const EXPECTED_DESCRIPTOR: &[u8] = &[
    0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x5a,
    0xb1, 0x02, 0xc0,
];

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
    pub event_file_regex: String,
    pub serial_query: SerialQuerySpec,
    pub qualification_probes: Vec<NativeQualificationProbe>,
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
    phys_prefix: String,
    qualification_maximum_attempts: u8,
    routes: Vec<NativeRouteRecord>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeRouteCatalogError {
    InvalidJson,
    InvalidSchema,
    MetadataDrift,
    InvalidTransport,
    InvalidRoute,
    DuplicateRoute,
    ProbeCatalogDrift,
}

impl fmt::Display for NativeRouteCatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidJson => "OpenRazer native-route catalog JSON is invalid",
            Self::InvalidSchema => "OpenRazer native-route catalog schema is invalid",
            Self::MetadataDrift => "OpenRazer metadata no longer matches the native-route catalog",
            Self::InvalidTransport => "OpenRazer native Feature transport is invalid",
            Self::InvalidRoute => "OpenRazer native route is invalid",
            Self::DuplicateRoute => "OpenRazer native route identity is duplicated",
            Self::ProbeCatalogDrift => {
                "OpenRazer qualification probes no longer match the native-route catalog"
            }
        })
    }
}

impl std::error::Error for NativeRouteCatalogError {}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogDocument {
    #[serde(rename = "$schema")]
    schema_ref: String,
    schema: String,
    upstream_commit: String,
    metadata_sha256: String,
    transport: TransportDocument,
    routes: Vec<RouteDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TransportDocument {
    protocol_family: String,
    report_bytes: u16,
    phys_prefix: String,
    descriptor_hex: String,
    descriptor_sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RouteDocument {
    profile_id: String,
    vendor_id: u16,
    wireless_product_id: u16,
    model_name: String,
    device_kind: String,
    driver_family: String,
    source: SourceDocument,
    direct_data_product_ids: Vec<u16>,
    event_file_regex: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceDocument {
    path: String,
    class_name: String,
    sha256: String,
}

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
    /// Loads the exact generated catalog embedded in this binary.
    ///
    /// # Errors
    ///
    /// Fails closed when metadata, transport, or route identity drifts.
    pub fn load() -> Result<Self, NativeRouteCatalogError> {
        let document = load_catalog_document()?;
        let (descriptor, protocol_family) = validate_transport(&document.transport)?;
        let (bound_probes, qualification_maximum_attempts) =
            load_qualification_probes(&document.upstream_commit)?;
        let routes = bind_routes(document.routes, &bound_probes)?;
        Ok(Self {
            upstream_commit: document.upstream_commit,
            protocol_family,
            descriptor,
            phys_prefix: document.transport.phys_prefix,
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
    pub fn phys_prefix(&self) -> &str {
        &self.phys_prefix
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

fn load_catalog_document() -> Result<CatalogDocument, NativeRouteCatalogError> {
    let document: CatalogDocument =
        serde_json::from_str(CATALOG_JSON).map_err(|_| NativeRouteCatalogError::InvalidJson)?;
    if document.schema != EXPECTED_SCHEMA
        || document.schema_ref != "../../../schemas/openrazer-native-route-catalog.schema.json"
        || document.upstream_commit.len() != 40
    {
        return Err(NativeRouteCatalogError::InvalidSchema);
    }
    if document.metadata_sha256 != hex_sha256(METADATA_JSON.as_bytes()) {
        return Err(NativeRouteCatalogError::MetadataDrift);
    }
    Ok(document)
}

fn validate_transport(
    transport: &TransportDocument,
) -> Result<(Vec<u8>, NativeProtocolFamily), NativeRouteCatalogError> {
    let descriptor =
        decode_hex(&transport.descriptor_hex).ok_or(NativeRouteCatalogError::InvalidTransport)?;
    if transport.report_bytes != 90
        || descriptor != EXPECTED_DESCRIPTOR
        || transport.descriptor_sha256 != hex_sha256(&descriptor)
        || transport.phys_prefix != "hyperflux-next/native/v1/"
    {
        return Err(NativeRouteCatalogError::InvalidTransport);
    }
    let protocol_family = NativeProtocolFamily::try_from(transport.protocol_family.clone())
        .map_err(|_| NativeRouteCatalogError::InvalidTransport)?;
    Ok((descriptor, protocol_family))
}

fn bind_routes(
    route_documents: Vec<RouteDocument>,
    bound_probes: &BTreeMap<String, BoundProbeDevice>,
) -> Result<Vec<NativeRouteRecord>, NativeRouteCatalogError> {
    let mut identities = BTreeSet::new();
    let mut profiles = BTreeSet::new();
    let mut routes = Vec::with_capacity(route_documents.len());
    for route in route_documents {
        let device_kind = route_device_kind(&route)?;
        if !identities.insert((route.vendor_id, route.wireless_product_id, device_kind))
            || !profiles.insert(route.profile_id.clone())
        {
            return Err(NativeRouteCatalogError::DuplicateRoute);
        }
        let bound = bound_probes
            .get(&route.profile_id)
            .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
        routes.push(bind_route(route, device_kind, bound)?);
    }
    if routes.len() != bound_probes.len() {
        return Err(NativeRouteCatalogError::ProbeCatalogDrift);
    }
    routes.sort_by_key(|route| {
        (
            route.vendor_id,
            route.wireless_product_id,
            route.device_kind,
        )
    });
    Ok(routes)
}

fn route_device_kind(route: &RouteDocument) -> Result<DeviceKind, NativeRouteCatalogError> {
    match route.device_kind.as_str() {
        "mouse" if route.driver_family == "razermouse" => Ok(DeviceKind::Mouse),
        "keyboard" if route.driver_family == "razerkbd" => Ok(DeviceKind::Keyboard),
        _ => Err(NativeRouteCatalogError::InvalidRoute),
    }
}

fn bind_route(
    route: RouteDocument,
    device_kind: DeviceKind,
    bound: &BoundProbeDevice,
) -> Result<NativeRouteRecord, NativeRouteCatalogError> {
    if route.profile_id.is_empty()
        || route.model_name.is_empty()
        || route.model_name.len() >= 128
        || route.source.path.is_empty()
        || route.source.class_name.is_empty()
        || route.source.sha256.len() != 64
        || route.direct_data_product_ids.is_empty()
        || route.event_file_regex.is_empty()
    {
        return Err(NativeRouteCatalogError::InvalidRoute);
    }
    let vendor_id =
        VendorId::try_from(route.vendor_id).map_err(|_| NativeRouteCatalogError::InvalidRoute)?;
    let wireless_product_id = ProductId::try_from(route.wireless_product_id)
        .map_err(|_| NativeRouteCatalogError::InvalidRoute)?;
    let direct_data_product_ids = route
        .direct_data_product_ids
        .iter()
        .copied()
        .map(|product| {
            ProductId::try_from(product).map_err(|_| NativeRouteCatalogError::InvalidRoute)
        })
        .collect::<Result<Vec<_>, _>>()?;
    validate_probe_identity(&route, device_kind, &direct_data_product_ids, bound)?;
    let serial = bound
        .probes
        .iter()
        .find(|probe| probe.method == "get_serial")
        .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
    let serial_query = SerialQuerySpec::new(
        serial.command_class,
        serial.command_id,
        serial.data_size,
        serial.arguments.clone(),
        serial.normalized_sha256.clone(),
    )
    .map_err(|_| NativeRouteCatalogError::ProbeCatalogDrift)?;
    Ok(NativeRouteRecord {
        profile_id: route.profile_id,
        vendor_id,
        wireless_product_id,
        model_name: route.model_name,
        device_kind,
        driver_family: route.driver_family,
        source_path: route.source.path,
        source_class: route.source.class_name,
        source_sha256: route.source.sha256,
        direct_data_product_ids,
        event_file_regex: route.event_file_regex,
        serial_query,
        qualification_probes: bound.probes.clone(),
    })
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
        || !(1..=5).contains(&document.busy_retry.maximum_attempts)
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
                || probe.request.normalized_sha256.len() != 64
                || !probe
                    .request
                    .normalized_sha256
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
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
    route: &RouteDocument,
    device_kind: DeviceKind,
    direct_products: &[ProductId],
    probe: &BoundProbeDevice,
) -> Result<(), NativeRouteCatalogError> {
    let expected_kind = match device_kind {
        DeviceKind::Mouse => "mouse",
        DeviceKind::Keyboard => "keyboard",
        _ => return Err(NativeRouteCatalogError::ProbeCatalogDrift),
    };
    let wireless = &probe.wireless_identity;
    let direct = &probe.direct_usb_reference;
    if wireless.vendor_id != route.vendor_id
        || wireless.product_id != route.wireless_product_id
        || wireless.model_name != route.model_name
        || wireless.device_kind != expected_kind
        || wireless.transport_variant != "wireless"
        || direct.vendor_id != route.vendor_id
        || direct.device_kind != expected_kind
        || direct.transport_variant != "wired"
        || direct.model_name.is_empty()
        || !direct_products
            .iter()
            .any(|product| product.get() == direct.product_id)
    {
        return Err(NativeRouteCatalogError::ProbeCatalogDrift);
    }
    Ok(())
}

fn report_crc(report: &[u8; 90]) -> u8 {
    report[2..88]
        .iter()
        .fold(0_u8, |checksum, byte| checksum ^ byte)
}

fn decode_hex(value: &str) -> Option<Vec<u8>> {
    if !value.len().is_multiple_of(2) || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let high = hex_nibble(pair[0])?;
            let low = hex_nibble(pair[1])?;
            Some((high << 4) | low)
        })
        .collect()
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
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
    fn generated_catalog_exposes_every_bounded_read_probe() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mouse = catalog
            .profile("child.razer.basilisk-v3-pro-35k.00cd")
            .expect("mouse exists");
        let keyboard = catalog
            .profile("child.razer.deathstalker-v2-pro-tkl.0296")
            .expect("keyboard exists");
        assert_eq!(mouse.qualification_probes.len(), 16);
        assert_eq!(keyboard.qualification_probes.len(), 9);
        assert!(
            mouse
                .qualification_probes
                .iter()
                .all(|probe| probe.command_id >= 0x80)
        );
        assert!(
            keyboard
                .qualification_probes
                .iter()
                .all(|probe| probe.command_id >= 0x80)
        );
    }

    #[test]
    fn matcher_accepts_only_the_exact_upstream_read_signature() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let probe = &catalog.routes()[0].qualification_probes[0];
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

        let mut write_direction = request;
        write_direction[7] &= 0x7f;
        write_direction[88] = report_crc(&write_direction);
        assert!(!probe.matches(&write_direction));

        let mut bad_crc = request;
        bad_crc[88] ^= 1;
        assert!(!probe.matches(&bad_crc));
    }
}
