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
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeRouteCatalog {
    upstream_commit: String,
    protocol_family: NativeProtocolFamily,
    descriptor: Vec<u8>,
    phys_prefix: String,
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
                "OpenRazer serial probe no longer matches the native-route catalog"
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

struct BoundSerialProbe {
    wireless_identity: ProbeIdentityDocument,
    direct_usb_reference: ProbeIdentityDocument,
    query: SerialQuerySpec,
}

impl NativeRouteCatalog {
    /// Loads the exact generated catalog embedded in this binary.
    ///
    /// # Errors
    ///
    /// Fails closed when metadata, transport, or route identity drifts.
    pub fn load() -> Result<Self, NativeRouteCatalogError> {
        let document: CatalogDocument =
            serde_json::from_str(CATALOG_JSON).map_err(|_| NativeRouteCatalogError::InvalidJson)?;
        if document.schema != EXPECTED_SCHEMA
            || document.schema_ref != "../../../schemas/openrazer-native-route-catalog.schema.json"
            || document.upstream_commit.len() != 40
        {
            return Err(NativeRouteCatalogError::InvalidSchema);
        }
        let metadata_digest = hex_sha256(METADATA_JSON.as_bytes());
        if document.metadata_sha256 != metadata_digest {
            return Err(NativeRouteCatalogError::MetadataDrift);
        }
        let descriptor = decode_hex(&document.transport.descriptor_hex)
            .ok_or(NativeRouteCatalogError::InvalidTransport)?;
        if document.transport.report_bytes != 90
            || descriptor != EXPECTED_DESCRIPTOR
            || document.transport.descriptor_sha256 != hex_sha256(&descriptor)
            || document.transport.phys_prefix != "hyperflux-next/native/v1/"
        {
            return Err(NativeRouteCatalogError::InvalidTransport);
        }
        let protocol_family = NativeProtocolFamily::try_from(document.transport.protocol_family)
            .map_err(|_| NativeRouteCatalogError::InvalidTransport)?;
        let serial_probes = load_serial_probes(&document.upstream_commit)?;
        let mut identities = BTreeSet::new();
        let mut profiles = BTreeSet::new();
        let mut routes = Vec::with_capacity(document.routes.len());
        for route in document.routes {
            let device_kind = match route.device_kind.as_str() {
                "mouse" if route.driver_family == "razermouse" => DeviceKind::Mouse,
                "keyboard" if route.driver_family == "razerkbd" => DeviceKind::Keyboard,
                _ => return Err(NativeRouteCatalogError::InvalidRoute),
            };
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
            if !identities.insert((route.vendor_id, route.wireless_product_id, device_kind))
                || !profiles.insert(route.profile_id.clone())
            {
                return Err(NativeRouteCatalogError::DuplicateRoute);
            }
            let vendor_id = VendorId::try_from(route.vendor_id)
                .map_err(|_| NativeRouteCatalogError::InvalidRoute)?;
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
            let serial_probe = serial_probes
                .get(&route.profile_id)
                .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
            validate_probe_identity(&route, device_kind, &direct_data_product_ids, serial_probe)?;
            routes.push(NativeRouteRecord {
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
                serial_query: serial_probe.query.clone(),
            });
        }
        if routes.len() != serial_probes.len() {
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
            upstream_commit: document.upstream_commit,
            protocol_family,
            descriptor,
            phys_prefix: document.transport.phys_prefix,
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
}

fn load_serial_probes(
    expected_commit: &str,
) -> Result<BTreeMap<String, BoundSerialProbe>, NativeRouteCatalogError> {
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
    let mut probes = BTreeMap::new();
    for device in document.devices {
        let mut serials = device
            .probes
            .into_iter()
            .filter(|probe| probe.method == "get_serial");
        let serial = serials
            .next()
            .ok_or(NativeRouteCatalogError::ProbeCatalogDrift)?;
        if serials.next().is_some()
            || serial.privacy != "redacted-equality-only"
            || serial.driver.attribute != "device_serial"
            || serial.driver.reader != "razer_attr_read_device_serial"
            || serial.driver.request_owner != "razer_attr_read_device_serial"
            || serial.response_summary != "redacted-serial-equality"
            || serial.comparison != "redacted-equality"
            || !serial.request.transaction_id_from_upstream
            || !serial.request.crc_recomputed_by_upstream
        {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
        let query = SerialQuerySpec::new(
            serial.request.command_class,
            serial.request.command_id,
            serial.request.data_size,
            serial.request.arguments,
            serial.request.normalized_sha256,
        )
        .map_err(|_| NativeRouteCatalogError::ProbeCatalogDrift)?;
        if probes
            .insert(
                device.profile_id,
                BoundSerialProbe {
                    wireless_identity: device.wireless_identity,
                    direct_usb_reference: device.direct_usb_reference,
                    query,
                },
            )
            .is_some()
        {
            return Err(NativeRouteCatalogError::ProbeCatalogDrift);
        }
    }
    Ok(probes)
}

fn validate_probe_identity(
    route: &RouteDocument,
    device_kind: DeviceKind,
    direct_products: &[ProductId],
    probe: &BoundSerialProbe,
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
