// SPDX-License-Identifier: GPL-2.0-only

use crate::{Assessment, SystemSnapshot, assess_system};
use hfx_runtime::{
    MAX_RECEIVER_GENERATIONS, MAX_STRUCTURED_EVENTS, MAX_SUPPORT_BUNDLE_BYTES, PRODUCT_VERSION,
    SUPPORT_BUNDLE_PREFIX,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fmt;
use std::io;
use std::path::Path;

pub const SUPPORT_PREVIEW_SCHEMA: &str = "hyperflux-support-bundle-preview-v1";
pub const SUPPORT_BUNDLE_SCHEMA: &str = "hyperflux-support-bundle-v1";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SupportBundlePreview {
    pub schema: String,
    pub bundle_id: String,
    pub assessment_state: String,
    pub receiver_generations: usize,
    pub structured_findings: usize,
    pub transaction_outcomes: usize,
    pub included: Vec<String>,
    pub excluded: Vec<String>,
    pub output: SupportOutputDeclaration,
    pub side_effects: SupportSideEffectDeclaration,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SupportOutputDeclaration {
    pub mode: String,
    pub file_written: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SupportSideEffectDeclaration {
    pub network_upload_executed: bool,
    pub active_information_query_executed: bool,
    pub hardware_write_executed: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SupportReceiver {
    pub generation: u64,
    pub profile: Option<String>,
    pub lifecycle: String,
    pub logical_devices: usize,
    pub stable_restore_enabled: bool,
    pub restore_state: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SupportBundle {
    pub schema: String,
    pub product_version: String,
    pub preview: SupportBundlePreview,
    pub assessment: Assessment,
    pub receivers: Vec<SupportReceiver>,
}

#[derive(Debug)]
pub enum SupportBundleError {
    Io(io::Error),
    Serialization,
    SizeBoundExceeded,
}

impl fmt::Display for SupportBundleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Io(_) => "the support bundle could not be written safely",
            Self::Serialization => "the support bundle could not be serialized",
            Self::SizeBoundExceeded => "the support bundle exceeds its configured size bound",
        })
    }
}

impl std::error::Error for SupportBundleError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for SupportBundleError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[must_use]
pub fn preview_support_bundle(snapshot: &SystemSnapshot) -> SupportBundlePreview {
    let assessment = assess_system(snapshot);
    let receiver_generations = snapshot
        .bridge
        .as_ref()
        .map_or(0, |bridge| bridge.snapshot.receivers.len())
        .min(MAX_RECEIVER_GENERATIONS);
    let structured_findings = snapshot
        .bridge
        .as_ref()
        .map_or(0, |bridge| bridge.diagnostics.findings.len())
        .min(MAX_STRUCTURED_EVENTS);
    let seed = format!(
        "{}|{:?}|{}|{}|{}",
        PRODUCT_VERSION,
        assessment.state,
        receiver_generations,
        structured_findings,
        snapshot
            .installed_module_identity
            .as_deref()
            .unwrap_or("none")
    );
    let digest = Sha256::digest(seed.as_bytes());
    let bundle_id = format!("hfx-{}", hex_prefix(&digest, 16));
    SupportBundlePreview {
        schema: SUPPORT_PREVIEW_SCHEMA.to_owned(),
        bundle_id,
        assessment_state: match assessment.state {
            crate::AssessmentState::Ready => "ready",
            crate::AssessmentState::NeedsAttention => "needs-attention",
        }
        .to_owned(),
        receiver_generations,
        structured_findings,
        transaction_outcomes: 0,
        included: vec![
            "package and kernel activation state".to_owned(),
            "typed system assessment and safe remediation".to_owned(),
            "bounded receiver generation summaries".to_owned(),
            "bounded structured diagnostic findings".to_owned(),
            "explicit privacy and side-effect declaration".to_owned(),
        ],
        excluded: vec![
            "hardware serials and stable host identifiers".to_owned(),
            "private filesystem paths".to_owned(),
            "raw HID or USB transport payloads".to_owned(),
            "arbitrary terminal and journal text".to_owned(),
            "captures and memory dumps".to_owned(),
            "active information-query responses".to_owned(),
        ],
        output: SupportOutputDeclaration {
            mode: "0600".to_owned(),
            file_written: false,
        },
        side_effects: SupportSideEffectDeclaration {
            network_upload_executed: false,
            active_information_query_executed: false,
            hardware_write_executed: false,
        },
    }
}

#[must_use]
pub fn build_support_bundle(snapshot: &SystemSnapshot) -> SupportBundle {
    let mut preview = preview_support_bundle(snapshot);
    preview.output.file_written = true;
    let receivers = snapshot
        .bridge
        .as_ref()
        .into_iter()
        .flat_map(|bridge| bridge.snapshot.receivers.iter())
        .take(MAX_RECEIVER_GENERATIONS)
        .map(|receiver| SupportReceiver {
            generation: receiver.generation_id.get(),
            profile: receiver.profile_id.as_ref().map(ToString::to_string),
            lifecycle: receiver.lifecycle.to_string(),
            logical_devices: receiver.devices.len(),
            stable_restore_enabled: receiver.stable_restore_enabled,
            restore_state: receiver.restore_state.to_string(),
        })
        .collect();
    SupportBundle {
        schema: SUPPORT_BUNDLE_SCHEMA.to_owned(),
        product_version: PRODUCT_VERSION.to_owned(),
        preview,
        assessment: assess_system(snapshot),
        receivers,
    }
}

/// Writes one privacy-safe bundle with exclusive creation and mode 0600.
///
/// # Errors
///
/// Returns without overwriting an existing path or exceeding the byte bound.
pub fn write_support_bundle(path: &Path, bundle: &SupportBundle) -> Result<(), SupportBundleError> {
    let payload =
        serde_json::to_vec_pretty(bundle).map_err(|_| SupportBundleError::Serialization)?;
    if payload.len() > MAX_SUPPORT_BUNDLE_BYTES {
        return Err(SupportBundleError::SizeBoundExceeded);
    }
    crate::probe::write_private_file(path, &payload)?;
    Ok(())
}

#[must_use]
pub fn suggested_support_name(bundle: &SupportBundle) -> String {
    format!("{SUPPORT_BUNDLE_PREFIX}-{}.json", bundle.preview.bundle_id)
}

fn hex_prefix(bytes: &[u8], length: usize) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(length);
    for byte in bytes {
        if output.len() >= length {
            break;
        }
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        if output.len() < length {
            output.push(char::from(HEX[usize::from(byte & 0x0f)]));
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ServiceState, SystemSnapshot};
    use std::fs;
    use std::os::unix::fs::PermissionsExt as _;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn empty_snapshot() -> SystemSnapshot {
        SystemSnapshot {
            package_version: PRODUCT_VERSION.to_owned(),
            installed_module_identity: Some("ABC".to_owned()),
            loaded_module_identity: None,
            service_state: ServiceState::Inactive,
            bridge: None,
            legacy_v2_stack_detected: false,
        }
    }

    #[test]
    fn preview_is_side_effect_free_and_names_exclusions() {
        let preview = preview_support_bundle(&empty_snapshot());
        assert!(!preview.output.file_written);
        assert!(!preview.side_effects.network_upload_executed);
        assert!(!preview.side_effects.active_information_query_executed);
        assert!(!preview.side_effects.hardware_write_executed);
        assert!(preview.excluded.iter().any(|item| item.contains("serials")));
        assert!(
            preview
                .excluded
                .iter()
                .any(|item| item.contains("private filesystem"))
        );
    }

    #[test]
    fn bundle_is_private_and_never_overwrites() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("hfx-support-{unique}.json"));
        let bundle = build_support_bundle(&empty_snapshot());
        write_support_bundle(&path, &bundle).expect("write");
        assert_eq!(
            fs::metadata(&path).expect("metadata").permissions().mode() & 0o777,
            0o600
        );
        assert!(write_support_bundle(&path, &bundle).is_err());
        fs::remove_file(path).expect("cleanup");
    }
}
