// SPDX-License-Identifier: GPL-2.0-only

//! Strict embedded source-catalog document parsing.

use super::SourceValidatedCatalogError;
use serde::Deserialize;

pub(super) const CATALOG_JSON: &str =
    include_str!(concat!(env!("OUT_DIR"), "/source-validated.json"));

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct CatalogDocument {
    #[serde(rename = "$schema")]
    pub(super) schema_ref: String,
    pub(super) schema: String,
    pub(super) sources: SourcesDocument,
    pub(super) transport: TransportDocument,
    pub(super) devices: Vec<DeviceDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SourcesDocument {
    pub(super) openrazer: UpstreamDocument,
    pub(super) openrgb: UpstreamDocument,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct UpstreamDocument {
    pub(super) commit: String,
    pub(super) catalog_sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct TransportDocument {
    pub(super) protocol_family: String,
    pub(super) report_bytes: u16,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct DeviceDocument {
    pub(super) route_id: String,
    pub(super) identity: IdentityDocument,
    pub(super) model_name: String,
    pub(super) operational_state: String,
    pub(super) report_family: String,
    pub(super) hardware_assurance: HardwareAssuranceDocument,
    pub(super) integrations: IntegrationsDocument,
    pub(super) rules: Vec<RuleDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct IdentityDocument {
    pub(super) vendor_id: u16,
    pub(super) product_id: u16,
    pub(super) device_kind: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct HardwareAssuranceDocument {
    pub(super) state: String,
    pub(super) profile_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct IntegrationsDocument {
    pub(super) openrazer: OpenRazerDocument,
    pub(super) openrgb: OpenRgbDocument,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "state", rename_all = "kebab-case", deny_unknown_fields)]
pub(super) enum OpenRazerDocument {
    SourceValidated {
        reason: Option<String>,
        record_id: String,
        class_name: String,
        source: Box<SourceLocationDocument>,
        image_url: String,
        methods: Vec<String>,
        driver_family: String,
        event_file_regex: Option<String>,
        direct_usb_reference: Box<Option<DirectUsbDocument>>,
        publication_identity: Box<PublicationIdentityDocument>,
    },
    BackendRequired {
        reason: String,
        record_id: Option<String>,
    },
    Absent {
        reason: Option<String>,
        record_id: Option<String>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(tag = "state", rename_all = "kebab-case", deny_unknown_fields)]
pub(super) enum OpenRgbDocument {
    SourceValidated {
        reason: Option<String>,
        record_id: String,
        source_device_key: String,
        source: SourceLocationDocument,
        transaction_id: u8,
        led_selector: u8,
        lighting_topology: LightingTopologyDocument,
    },
    BackendRequired {
        reason: String,
        record_id: Option<String>,
    },
    Absent {
        reason: Option<String>,
        record_id: Option<String>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SourceLocationDocument {
    pub(super) path: String,
    pub(super) line: u32,
    pub(super) sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct DirectUsbDocument {
    pub(super) vendor_id: u16,
    pub(super) product_id: u16,
    pub(super) model_name: String,
    pub(super) device_kind: String,
    pub(super) transport_variant: String,
    pub(super) class_name: String,
    pub(super) source_path: String,
    pub(super) source_line: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PublicationIdentityDocument {
    pub(super) method: String,
    pub(super) transaction_id: u8,
    pub(super) command_class: u8,
    pub(super) command_id: u8,
    pub(super) data_size: u8,
    pub(super) arguments: Vec<u8>,
    pub(super) normalized_sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct LightingTopologyDocument {
    pub(super) matrix_type: String,
    pub(super) rows: u8,
    pub(super) columns: u8,
    pub(super) application_slot_count: u16,
    pub(super) zones: Vec<ZoneDocument>,
    pub(super) layout_key: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ZoneDocument {
    pub(super) name: String,
    #[serde(rename = "type")]
    pub(super) zone_type: String,
    pub(super) rows: u8,
    pub(super) columns: u8,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RuleDocument {
    pub(super) id: String,
    pub(super) integrations: Vec<String>,
    pub(super) methods: Vec<String>,
    pub(super) direction: String,
    pub(super) command_class: u8,
    pub(super) command_id: u8,
    pub(super) data_sizes: Vec<u8>,
    pub(super) transaction_ids: Option<Vec<u8>>,
    pub(super) matcher: serde_json::Value,
    pub(super) sources: Vec<RuleSourceDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RuleSourceDocument {
    pub(super) upstream: String,
    pub(super) path: String,
    pub(super) function: String,
    pub(super) line: u32,
    pub(super) sha256: String,
}

pub(super) fn parse_catalog() -> Result<CatalogDocument, SourceValidatedCatalogError> {
    parse_catalog_text(CATALOG_JSON)
}

fn parse_catalog_text(value: &str) -> Result<CatalogDocument, SourceValidatedCatalogError> {
    serde_json::from_str(value).map_err(|_| SourceValidatedCatalogError::InvalidJson)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn next_property_value(state: &mut u64) -> u64 {
        *state ^= *state << 13;
        *state ^= *state >> 7;
        *state ^= *state << 17;
        *state
    }

    #[test]
    fn seeded_source_catalog_mutations_never_escape_typed_parsing() {
        parse_catalog_text(CATALOG_JSON).expect("canonical source catalog parses");
        let mut state = 0x3c6e_f372_fe94_f82b_u64;
        let canonical = CATALOG_JSON.as_bytes();
        for _ in 0..1024 {
            let mut candidate = canonical.to_vec();
            let value = next_property_value(&mut state);
            let index = usize::try_from(value).expect("host usize contains u64") % candidate.len();
            candidate[index] ^=
                u8::try_from(((value >> 32) & 0xff) | 1).expect("masked byte fits u8");
            match String::from_utf8(candidate) {
                Ok(text) => {
                    let _ = parse_catalog_text(&text);
                }
                Err(_error) => {}
            }
        }

        let mut value: serde_json::Value =
            serde_json::from_str(CATALOG_JSON).expect("catalog is JSON");
        value
            .as_object_mut()
            .expect("catalog root is an object")
            .insert("unknown".to_owned(), serde_json::Value::Bool(true));
        assert!(matches!(
            parse_catalog_text(&value.to_string()),
            Err(SourceValidatedCatalogError::InvalidJson)
        ));
    }
}
