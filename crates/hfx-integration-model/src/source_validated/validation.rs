// SPDX-License-Identifier: GPL-2.0-only

//! Structural and source-authority validation for the generated catalog.

use super::document::{CatalogDocument, RuleDocument};
use super::{SourceValidatedCatalogError, SourceValidatedPublicationIdentity};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

const OPENRAZER_KNOWLEDGE_SHA256: &str = env!("HFX_OPENRAZER_KNOWLEDGE_SHA256");
const OPENRGB_KNOWLEDGE_SHA256: &str = env!("HFX_OPENRGB_KNOWLEDGE_SHA256");
const EXPECTED_SCHEMA: &str = "hyperflux-source-validated-device-catalog-v1";
const EXPECTED_SCHEMA_REF: &str = "../../schemas/source-validated-device-catalog.schema.json";
pub(super) const EXPECTED_PROTOCOL: &str = "razer-feature-report-v1";
const EXPECTED_REPORT_BYTES: u16 = 90;
pub(super) const RAZER_VENDOR_ID: u16 = 0x1532;

pub(super) fn validate_authority(
    document: &CatalogDocument,
) -> Result<(), SourceValidatedCatalogError> {
    if document.schema_ref != EXPECTED_SCHEMA_REF
        || document.schema != EXPECTED_SCHEMA
        || document.transport.protocol_family != EXPECTED_PROTOCOL
        || document.transport.report_bytes != EXPECTED_REPORT_BYTES
        || !valid_commit(&document.sources.openrazer.commit)
        || !valid_commit(&document.sources.openrgb.commit)
    {
        return Err(SourceValidatedCatalogError::InvalidSchema);
    }
    if document.sources.openrazer.catalog_sha256 != OPENRAZER_KNOWLEDGE_SHA256
        || document.sources.openrgb.catalog_sha256 != OPENRGB_KNOWLEDGE_SHA256
    {
        return Err(SourceValidatedCatalogError::SourceDrift);
    }
    Ok(())
}

pub(super) fn validate_rules(
    rules: &[RuleDocument],
    openrazer: bool,
    openrgb: bool,
) -> Result<(), SourceValidatedCatalogError> {
    if rules.len() > 512 {
        return Err(SourceValidatedCatalogError::InvalidPolicy);
    }
    let mut ids = BTreeSet::new();
    let mut previous_id: Option<&str> = None;
    for rule in rules {
        if !valid_rule_id(&rule.id)
            || previous_id.is_some_and(|previous| previous >= rule.id.as_str())
            || !ids.insert(rule.id.as_str())
            || rule.integrations.is_empty()
            || rule.integrations.len() > 2
            || !ordered_unique(&rule.integrations)
            || rule.methods.is_empty()
            || rule.methods.len() > 64
            || !ordered_unique(&rule.methods)
            || rule.data_sizes.is_empty()
            || rule.data_sizes.len() > 80
            || rule.data_sizes.windows(2).any(|pair| pair[0] >= pair[1])
            || rule.data_sizes.iter().any(|size| *size == 0 || *size > 80)
            || !matches!(rule.direction.as_str(), "read" | "write")
            || (rule.command_id >= 0x80) != (rule.direction == "read")
            || rule.transaction_ids.as_ref().is_some_and(|transactions| {
                rule.direction != "read"
                    || transactions.is_empty()
                    || transactions.len() > 16
                    || transactions.contains(&0)
                    || transactions.windows(2).any(|pair| pair[0] >= pair[1])
            })
            || rule.sources.is_empty()
            || rule.sources.len() > 64
        {
            return Err(SourceValidatedCatalogError::InvalidPolicy);
        }
        previous_id = Some(&rule.id);
        for integration in &rule.integrations {
            if (integration == "openrazer" && !openrazer)
                || (integration == "openrgb" && !openrgb)
                || !matches!(integration.as_str(), "openrazer" | "openrgb")
            {
                return Err(SourceValidatedCatalogError::InvalidPolicy);
            }
        }
        let mut source_keys = BTreeSet::new();
        for source in &rule.sources {
            let valid_path = match source.upstream.as_str() {
                "openrazer" => matches!(
                    source.path.as_str(),
                    "driver/razercommon.c"
                        | "driver/razerchromacommon.c"
                        | "driver/razerkbd_driver.c"
                        | "driver/razermouse_driver.c"
                ),
                "openrgb" => {
                    source.path == "Controllers/RazerController/RazerController/RazerController.cpp"
                }
                _ => false,
            };
            if !valid_path
                || source.function.is_empty()
                || source.line == 0
                || !valid_sha256(&source.sha256)
                || !source_keys.insert((&source.upstream, &source.path, &source.function))
            {
                return Err(SourceValidatedCatalogError::InvalidPolicy);
            }
        }
    }
    Ok(())
}

pub(super) fn validate_publication_identity_rule(
    rules: &[RuleDocument],
    query: &SourceValidatedPublicationIdentity,
) -> Result<(), SourceValidatedCatalogError> {
    let matches = rules
        .iter()
        .filter(|rule| {
            rule.integrations.iter().any(|value| value == "openrazer")
                && rule.methods.iter().any(|method| method == &query.method)
                && rule.direction == "read"
                && rule.command_class == query.command_class
                && rule.command_id == query.command_id
                && rule.data_sizes == [query.data_size]
                && rule
                    .transaction_ids
                    .as_ref()
                    .is_some_and(|values| values == &[query.transaction_id])
                && rule.matcher
                    == serde_json::json!({
                        "kind": "fields",
                        "fields": [{
                            "kind": "exact",
                            "offset": 0,
                            "bytes": query.arguments,
                        }],
                    })
        })
        .count();
    let mut normalized = [0_u8; 90];
    normalized[1] = query.transaction_id;
    normalized[5] = query.data_size;
    normalized[6] = query.command_class;
    normalized[7] = query.command_id;
    let end = 8 + query.arguments.len();
    if matches != 1 || end > 88 {
        return Err(SourceValidatedCatalogError::InvalidPolicy);
    }
    normalized[8..end].copy_from_slice(&query.arguments);
    if hex_sha256(&normalized) != query.normalized_sha256 {
        return Err(SourceValidatedCatalogError::InvalidPolicy);
    }
    Ok(())
}

fn valid_commit(value: &str) -> bool {
    value.len() == 40 && value.bytes().all(is_lower_hex)
}

pub(super) fn valid_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(is_lower_hex)
}

fn is_lower_hex(byte: u8) -> bool {
    byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
}

pub(super) fn valid_profile_id(value: &str) -> bool {
    value.starts_with("child.")
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
        })
}

fn valid_rule_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'+')
        })
}

pub(super) fn ordered_unique(values: &[String]) -> bool {
    values.iter().all(|value| {
        !value.is_empty()
            && value.len() <= 160
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    }) && values.windows(2).all(|pair| pair[0] < pair[1])
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
