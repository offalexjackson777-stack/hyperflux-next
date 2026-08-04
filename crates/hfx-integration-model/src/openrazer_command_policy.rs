// SPDX-License-Identifier: GPL-2.0-only

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

const COMMAND_CATALOG_JSON: &str =
    include_str!("../../../integrations/openrazer/native-command-catalog.json");
const COMMAND_POLICY_JSON: &str =
    include_str!("../../../integrations/openrazer/native-command-policy.json");
const METHOD_INVENTORY_JSON: &str =
    include_str!("../../../integrations/openrazer/native-method-inventory.json");
const PROBE_CATALOG_JSON: &str =
    include_str!("../../../integrations/openrazer/native-probe-catalog.json");
const EXPECTED_SCHEMA: &str = "hyperflux-openrazer-native-command-catalog-v1";
const EXPECTED_SCHEMA_REF: &str = "../../schemas/openrazer-native-command-catalog.schema.json";
const REPORT_BYTES: usize = 90;
const ARGUMENT_BYTES: usize = 80;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeCommandPolicy {
    rules: Vec<NativeCommandRule>,
}

pub(crate) struct NativePolicyRuleInput {
    pub command_class: u8,
    pub command_id: u8,
    pub data_sizes: Vec<u8>,
    pub matcher: serde_json::Value,
}

impl NativeCommandPolicy {
    /// Returns true only for a complete, source-bound `OpenRazer` request.
    #[must_use]
    pub fn allows(&self, report: &[u8; REPORT_BYTES]) -> bool {
        if report[0] != 0
            || report[1] == 0
            || report[2] != 0
            || report[3] != 0
            || report[4] != 0
            || report[89] != 0
            || report[88] != report_crc(report)
        {
            return false;
        }
        let data_size = usize::from(report[5]);
        if data_size == 0
            || data_size > ARGUMENT_BYTES
            || report[8 + data_size..88].iter().any(|byte| *byte != 0)
        {
            return false;
        }
        let arguments = &report[8..8 + data_size];
        self.rules.iter().any(|rule| {
            rule.data_sizes.binary_search(&report[5]).is_ok()
                && rule.command_class == report[6]
                && rule.command_id == report[7]
                && rule.matcher.matches(arguments)
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct NativeCommandPolicyError;

impl fmt::Display for NativeCommandPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("the source-bound OpenRazer command policy is invalid")
    }
}

impl std::error::Error for NativeCommandPolicyError {}

#[derive(Clone, Debug, Eq, PartialEq)]
struct NativeCommandRule {
    command_class: u8,
    command_id: u8,
    data_sizes: Vec<u8>,
    matcher: NativeMatcher,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum NativeMatcher {
    Fields(Vec<FieldMatcher>),
    CustomFrame {
        rows: u8,
        columns: u8,
        length_mode: CustomFrameLengthMode,
    },
    DpiStages {
        maximum_stages: u8,
        minimum_dpi: u16,
        maximum_dpi: u16,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CustomFrameLengthMode {
    Exact,
    ZeroPadded,
}

impl NativeMatcher {
    fn matches(&self, arguments: &[u8]) -> bool {
        match self {
            Self::Fields(fields) => fields.iter().all(|field| field.matches(arguments)),
            Self::CustomFrame {
                rows,
                columns,
                length_mode,
            } => {
                if arguments.len() < 8
                    || arguments[0] != 0
                    || arguments[1] != 0
                    || arguments[2] >= *rows
                {
                    return false;
                }
                let start = usize::from(arguments[3]);
                let stop = usize::from(arguments[4]);
                if start > stop || stop >= usize::from(*columns) {
                    return false;
                }
                let used = 5 + (stop - start + 1) * 3;
                match length_mode {
                    CustomFrameLengthMode::Exact => used == arguments.len(),
                    CustomFrameLengthMode::ZeroPadded => {
                        used <= arguments.len() && arguments[used..].iter().all(|byte| *byte == 0)
                    }
                }
            }
            Self::DpiStages {
                maximum_stages,
                minimum_dpi,
                maximum_dpi,
            } => {
                if arguments.len() != 38 || arguments[0] != 1 {
                    return false;
                }
                let active = arguments[1];
                let count = arguments[2];
                if count == 0 || count > *maximum_stages || active == 0 || active > count {
                    return false;
                }
                for index in 0..usize::from(count) {
                    let offset = 3 + index * 7;
                    let x = u16::from_be_bytes([arguments[offset + 1], arguments[offset + 2]]);
                    let y = u16::from_be_bytes([arguments[offset + 3], arguments[offset + 4]]);
                    if arguments[offset] != u8::try_from(index).unwrap_or(u8::MAX)
                        || x < *minimum_dpi
                        || x > *maximum_dpi
                        || y < *minimum_dpi
                        || y > *maximum_dpi
                        || arguments[offset + 5] != 0
                        || arguments[offset + 6] != 0
                    {
                        return false;
                    }
                }
                let used = 3 + usize::from(count) * 7;
                arguments[used..].iter().all(|byte| *byte == 0)
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum FieldMatcher {
    Exact {
        offset: usize,
        bytes: Vec<u8>,
    },
    OneOf {
        offset: usize,
        values: Vec<u8>,
    },
    U8Range {
        offset: usize,
        minimum: u8,
        maximum: u8,
    },
    Be16Range {
        offset: usize,
        minimum: u16,
        maximum: u16,
    },
    Rgb {
        offset: usize,
        pixels: usize,
    },
}

impl FieldMatcher {
    fn width(&self) -> usize {
        match self {
            Self::Exact { bytes, .. } => bytes.len(),
            Self::OneOf { .. } | Self::U8Range { .. } => 1,
            Self::Be16Range { .. } => 2,
            Self::Rgb { pixels, .. } => pixels * 3,
        }
    }

    fn offset(&self) -> usize {
        match self {
            Self::Exact { offset, .. }
            | Self::OneOf { offset, .. }
            | Self::U8Range { offset, .. }
            | Self::Be16Range { offset, .. }
            | Self::Rgb { offset, .. } => *offset,
        }
    }

    fn matches(&self, arguments: &[u8]) -> bool {
        match self {
            Self::Exact { offset, bytes } => {
                arguments.get(*offset..*offset + bytes.len()) == Some(bytes)
            }
            Self::OneOf { offset, values } => arguments
                .get(*offset)
                .is_some_and(|value| values.binary_search(value).is_ok()),
            Self::U8Range {
                offset,
                minimum,
                maximum,
            } => arguments
                .get(*offset)
                .is_some_and(|value| value >= minimum && value <= maximum),
            Self::Be16Range {
                offset,
                minimum,
                maximum,
            } => arguments.get(*offset..*offset + 2).is_some_and(|bytes| {
                let value = u16::from_be_bytes([bytes[0], bytes[1]]);
                value >= *minimum && value <= *maximum
            }),
            Self::Rgb { offset, pixels } => arguments.get(*offset..*offset + pixels * 3).is_some(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogDocument {
    #[serde(rename = "$schema")]
    schema_ref: String,
    schema: String,
    upstream: UpstreamDocument,
    policy_sha256: String,
    inventory_sha256: String,
    probe_catalog_sha256: String,
    report_size: u16,
    devices: Vec<DeviceDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct UpstreamDocument {
    id: String,
    repository: String,
    version: String,
    commit: String,
    license_expression: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeviceDocument {
    profile_id: String,
    rules: Vec<RuleDocument>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuleDocument {
    id: String,
    methods: Vec<String>,
    direction: DirectionDocument,
    command_class: u8,
    command_id: u8,
    data_size: u8,
    matcher: MatcherDocument,
    sources: Vec<SourceDocument>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
enum DirectionDocument {
    Read,
    Write,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
enum MatcherDocument {
    Fields {
        fields: Vec<FieldDocument>,
    },
    CustomFrame {
        rows: u8,
        columns: u8,
        #[serde(default)]
        length_mode: Option<CustomFrameLengthModeDocument>,
    },
    DpiStages {
        maximum_stages: u8,
        minimum_dpi: u16,
        maximum_dpi: u16,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "kebab-case")]
enum CustomFrameLengthModeDocument {
    Exact,
    ZeroPadded,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
enum FieldDocument {
    Exact {
        offset: usize,
        bytes: Vec<u8>,
    },
    OneOf {
        offset: usize,
        values: Vec<u8>,
    },
    U8Range {
        offset: usize,
        minimum: u8,
        maximum: u8,
    },
    Be16Range {
        offset: usize,
        minimum: u16,
        maximum: u16,
    },
    Rgb {
        offset: usize,
        pixels: usize,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceDocument {
    path: String,
    function: String,
    line: u32,
    sha256: String,
}

/// Loads the generated per-profile command policies after checking every source binding.
///
/// # Errors
///
/// Returns [`NativeCommandPolicyError`] when the embedded catalog, its source digests,
/// profile bindings, or any command constraint is invalid.
pub fn load_native_command_policies(
    expected_commit: &str,
) -> Result<BTreeMap<String, NativeCommandPolicy>, NativeCommandPolicyError> {
    let document: CatalogDocument =
        serde_json::from_str(COMMAND_CATALOG_JSON).map_err(|_| NativeCommandPolicyError)?;
    if document.schema_ref != EXPECTED_SCHEMA_REF
        || document.schema != EXPECTED_SCHEMA
        || usize::from(document.report_size) != REPORT_BYTES
        || document.upstream.id != "openrazer"
        || document.upstream.commit != expected_commit
        || document.upstream.repository.is_empty()
        || document.upstream.version.is_empty()
        || document.upstream.license_expression.is_empty()
        || document.policy_sha256 != hex_sha256(COMMAND_POLICY_JSON.as_bytes())
        || document.inventory_sha256 != hex_sha256(METHOD_INVENTORY_JSON.as_bytes())
        || document.probe_catalog_sha256 != hex_sha256(PROBE_CATALOG_JSON.as_bytes())
        || document.devices.is_empty()
        || document.devices.len() > 128
    {
        return Err(NativeCommandPolicyError);
    }

    let mut policies = BTreeMap::new();
    for device in document.devices {
        if !valid_profile_id(&device.profile_id)
            || device.rules.is_empty()
            || device.rules.len() > 256
        {
            return Err(NativeCommandPolicyError);
        }
        let mut previous_id: Option<&str> = None;
        let mut ids = BTreeSet::new();
        let mut rules = Vec::with_capacity(device.rules.len());
        for rule in &device.rules {
            if !valid_identifier(&rule.id)
                || previous_id.is_some_and(|previous| previous >= rule.id.as_str())
                || !ids.insert(rule.id.as_str())
                || rule.methods.is_empty()
                || rule.methods.len() > 32
                || !ordered_unique_identifiers(&rule.methods)
                || rule.data_size == 0
                || usize::from(rule.data_size) > ARGUMENT_BYTES
                || (rule.command_id >= 0x80) != (rule.direction == DirectionDocument::Read)
                || !valid_sources(&rule.sources)
            {
                return Err(NativeCommandPolicyError);
            }
            previous_id = Some(&rule.id);
            let matcher = bind_matcher(&rule.matcher, usize::from(rule.data_size))?;
            rules.push(NativeCommandRule {
                command_class: rule.command_class,
                command_id: rule.command_id,
                data_sizes: vec![rule.data_size],
                matcher,
            });
        }
        if policies
            .insert(device.profile_id, NativeCommandPolicy { rules })
            .is_some()
        {
            return Err(NativeCommandPolicyError);
        }
    }
    Ok(policies)
}

fn bind_matcher(
    document: &MatcherDocument,
    data_size: usize,
) -> Result<NativeMatcher, NativeCommandPolicyError> {
    bind_matcher_for_sizes(document, &[data_size])
}

fn bind_matcher_for_sizes(
    document: &MatcherDocument,
    data_sizes: &[usize],
) -> Result<NativeMatcher, NativeCommandPolicyError> {
    match document {
        MatcherDocument::Fields { fields } => {
            if data_sizes.len() != 1 {
                return Err(NativeCommandPolicyError);
            }
            bind_fields(fields, data_sizes[0])
        }
        MatcherDocument::CustomFrame {
            rows,
            columns,
            length_mode,
        } => {
            let length_mode = match length_mode.unwrap_or(CustomFrameLengthModeDocument::ZeroPadded)
            {
                CustomFrameLengthModeDocument::Exact => CustomFrameLengthMode::Exact,
                CustomFrameLengthModeDocument::ZeroPadded => CustomFrameLengthMode::ZeroPadded,
            };
            if data_sizes.is_empty()
                || data_sizes
                    .iter()
                    .any(|size| *size < 8 || *size > ARGUMENT_BYTES)
                || *rows == 0
                || *rows > 32
                || *columns == 0
                || *columns > 25
                || (length_mode == CustomFrameLengthMode::ZeroPadded && data_sizes != [71_usize])
                || (length_mode == CustomFrameLengthMode::Exact
                    && data_sizes.iter().any(|size| (size - 5) % 3 != 0))
            {
                return Err(NativeCommandPolicyError);
            }
            Ok(NativeMatcher::CustomFrame {
                rows: *rows,
                columns: *columns,
                length_mode,
            })
        }
        MatcherDocument::DpiStages {
            maximum_stages,
            minimum_dpi,
            maximum_dpi,
        } => {
            if data_sizes != [38_usize]
                || *maximum_stages == 0
                || *maximum_stages > 5
                || *minimum_dpi == 0
                || minimum_dpi > maximum_dpi
            {
                return Err(NativeCommandPolicyError);
            }
            Ok(NativeMatcher::DpiStages {
                maximum_stages: *maximum_stages,
                minimum_dpi: *minimum_dpi,
                maximum_dpi: *maximum_dpi,
            })
        }
    }
}

pub(crate) fn native_command_policy_from_rules(
    inputs: Vec<NativePolicyRuleInput>,
) -> Result<NativeCommandPolicy, NativeCommandPolicyError> {
    if inputs.is_empty() || inputs.len() > 512 {
        return Err(NativeCommandPolicyError);
    }
    let mut rules = Vec::with_capacity(inputs.len());
    for input in inputs {
        if input.data_sizes.is_empty()
            || input.data_sizes.len() > ARGUMENT_BYTES
            || input.data_sizes.windows(2).any(|pair| pair[0] >= pair[1])
            || input
                .data_sizes
                .iter()
                .any(|size| *size == 0 || usize::from(*size) > ARGUMENT_BYTES)
        {
            return Err(NativeCommandPolicyError);
        }
        let document: MatcherDocument =
            serde_json::from_value(input.matcher).map_err(|_| NativeCommandPolicyError)?;
        let sizes = input
            .data_sizes
            .iter()
            .copied()
            .map(usize::from)
            .collect::<Vec<_>>();
        let matcher = bind_matcher_for_sizes(&document, &sizes)?;
        rules.push(NativeCommandRule {
            command_class: input.command_class,
            command_id: input.command_id,
            data_sizes: input.data_sizes,
            matcher,
        });
    }
    Ok(NativeCommandPolicy { rules })
}

fn bind_fields(
    fields: &[FieldDocument],
    data_size: usize,
) -> Result<NativeMatcher, NativeCommandPolicyError> {
    if fields.is_empty() || fields.len() > ARGUMENT_BYTES {
        return Err(NativeCommandPolicyError);
    }
    let bound = fields
        .iter()
        .map(bind_field)
        .collect::<Result<Vec<_>, _>>()?;
    let mut covered = vec![false; data_size];
    let mut previous_offset = None;
    for field in &bound {
        let offset = field.offset();
        let end = offset
            .checked_add(field.width())
            .ok_or(NativeCommandPolicyError)?;
        if previous_offset.is_some_and(|previous| offset <= previous)
            || end > data_size
            || covered[offset..end].iter().any(|value| *value)
        {
            return Err(NativeCommandPolicyError);
        }
        covered[offset..end].fill(true);
        previous_offset = Some(offset);
    }
    if covered.iter().any(|value| !*value) {
        return Err(NativeCommandPolicyError);
    }
    Ok(NativeMatcher::Fields(bound))
}

fn bind_field(field: &FieldDocument) -> Result<FieldMatcher, NativeCommandPolicyError> {
    match field {
        FieldDocument::Exact { offset, bytes } => {
            if bytes.is_empty() || bytes.len() > ARGUMENT_BYTES {
                return Err(NativeCommandPolicyError);
            }
            Ok(FieldMatcher::Exact {
                offset: *offset,
                bytes: bytes.clone(),
            })
        }
        FieldDocument::OneOf { offset, values } => {
            if values.is_empty()
                || values.len() > 256
                || values.windows(2).any(|pair| pair[0] >= pair[1])
            {
                return Err(NativeCommandPolicyError);
            }
            Ok(FieldMatcher::OneOf {
                offset: *offset,
                values: values.clone(),
            })
        }
        FieldDocument::U8Range {
            offset,
            minimum,
            maximum,
        } => {
            if minimum > maximum {
                return Err(NativeCommandPolicyError);
            }
            Ok(FieldMatcher::U8Range {
                offset: *offset,
                minimum: *minimum,
                maximum: *maximum,
            })
        }
        FieldDocument::Be16Range {
            offset,
            minimum,
            maximum,
        } => {
            if minimum > maximum {
                return Err(NativeCommandPolicyError);
            }
            Ok(FieldMatcher::Be16Range {
                offset: *offset,
                minimum: *minimum,
                maximum: *maximum,
            })
        }
        FieldDocument::Rgb { offset, pixels } => {
            if *pixels == 0 || *pixels > 26 {
                return Err(NativeCommandPolicyError);
            }
            Ok(FieldMatcher::Rgb {
                offset: *offset,
                pixels: *pixels,
            })
        }
    }
}

fn valid_sources(sources: &[SourceDocument]) -> bool {
    if sources.is_empty() || sources.len() > 32 {
        return false;
    }
    let mut seen = BTreeSet::new();
    sources.iter().all(|source| {
        matches!(
            source.path.as_str(),
            "driver/razerchromacommon.c"
                | "driver/razerkbd_driver.c"
                | "driver/razermouse_driver.c"
        ) && source.function.starts_with("razer_")
            && source
                .function
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
            && source.line > 0
            && valid_sha256(&source.sha256)
            && seen.insert((&source.path, &source.function))
    })
}

fn ordered_unique_identifiers(values: &[String]) -> bool {
    values.iter().all(|value| valid_method_id(value))
        && values.windows(2).all(|pair| pair[0] < pair[1])
}

fn valid_profile_id(value: &str) -> bool {
    value.starts_with("child.")
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
        })
}

fn valid_identifier(value: &str) -> bool {
    value.len() <= 128
        && value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn valid_method_id(value: &str) -> bool {
    value.len() <= 128
        && value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn report_crc(report: &[u8; REPORT_BYTES]) -> u8 {
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

    fn request(class: u8, command: u8, arguments: &[u8]) -> [u8; REPORT_BYTES] {
        let mut report = [0_u8; REPORT_BYTES];
        report[1] = 0x1f;
        report[5] = u8::try_from(arguments.len()).expect("test payload fits");
        report[6] = class;
        report[7] = command;
        report[8..8 + arguments.len()].copy_from_slice(arguments);
        report[88] = report_crc(&report);
        report
    }

    fn policies() -> BTreeMap<String, NativeCommandPolicy> {
        load_native_command_policies("1ef8a91712906a89fd332a2ed62bbd105315d6d1")
            .expect("generated policy loads")
    }

    #[test]
    fn generated_policy_is_profile_bound_and_rejects_unknown_commands() {
        let policies = policies();
        let mouse = &policies["child.razer.basilisk-v3-pro-35k.00cd"];
        assert!(mouse.allows(&request(0x00, 0x05, &[1])));
        assert!(!mouse.allows(&request(0x7e, 0x01, &[0])));
        assert_eq!(policies.len(), 2);
    }

    #[test]
    fn custom_frames_enforce_profile_geometry_and_zero_tail() {
        let policies = policies();
        let keyboard = &policies["child.razer.deathstalker-v2-pro-tkl.0296"];
        let mut arguments = [0_u8; 71];
        arguments[2] = 5;
        arguments[3] = 16;
        arguments[4] = 16;
        arguments[5..8].copy_from_slice(&[0xff, 0x20, 0x00]);
        assert!(keyboard.allows(&request(0x0f, 0x03, &arguments)));
        arguments[4] = 17;
        assert!(!keyboard.allows(&request(0x0f, 0x03, &arguments)));
        arguments[4] = 16;
        arguments[8] = 1;
        assert!(!keyboard.allows(&request(0x0f, 0x03, &arguments)));
    }

    #[test]
    fn dpi_stage_relations_and_ranges_are_enforced() {
        let policies = policies();
        let mouse = &policies["child.razer.basilisk-v3-pro-35k.00cd"];
        let mut arguments = [0_u8; 38];
        arguments[0] = 1;
        arguments[1] = 1;
        arguments[2] = 1;
        arguments[3] = 0;
        arguments[4..6].copy_from_slice(&800_u16.to_be_bytes());
        arguments[6..8].copy_from_slice(&800_u16.to_be_bytes());
        assert!(mouse.allows(&request(0x04, 0x06, &arguments)));
        arguments[1] = 2;
        assert!(!mouse.allows(&request(0x04, 0x06, &arguments)));
        arguments[1] = 1;
        arguments[4..6].copy_from_slice(&99_u16.to_be_bytes());
        assert!(!mouse.allows(&request(0x04, 0x06, &arguments)));
    }

    #[test]
    fn source_generated_dpi_write_requires_varstore() {
        let policies = policies();
        let mouse = &policies["child.razer.basilisk-v3-pro-35k.00cd"];
        let mut arguments = [0_u8; 7];
        arguments[0] = 1;
        arguments[1..3].copy_from_slice(&1800_u16.to_be_bytes());
        arguments[3..5].copy_from_slice(&1800_u16.to_be_bytes());
        assert!(mouse.allows(&request(0x04, 0x05, &arguments)));
        arguments[0] = 0;
        assert!(!mouse.allows(&request(0x04, 0x05, &arguments)));
    }

    #[test]
    fn inherited_keyboard_brightness_requires_backlight_selector() {
        let policies = policies();
        let keyboard = &policies["child.razer.deathstalker-v2-pro-tkl.0296"];
        let mut arguments = [1_u8, 5, 128];
        assert!(keyboard.allows(&request(0x0f, 0x04, &arguments)));
        arguments[1] = 0;
        assert!(!keyboard.allows(&request(0x0f, 0x04, &arguments)));
        arguments[0] = 0;
        arguments[1] = 5;
        assert!(!keyboard.allows(&request(0x0f, 0x04, &arguments)));
    }

    #[test]
    fn malformed_headers_crc_and_fixed_bytes_are_rejected() {
        let policies = policies();
        let mouse = &policies["child.razer.basilisk-v3-pro-35k.00cd"];
        let valid = request(0x00, 0x05, &[1]);
        assert!(mouse.allows(&valid));
        for index in [0_usize, 2, 3, 4, 89] {
            let mut changed = valid;
            changed[index] = 1;
            changed[88] = report_crc(&changed);
            assert!(!mouse.allows(&changed));
        }
        let mut bad_crc = valid;
        bad_crc[88] ^= 1;
        assert!(!mouse.allows(&bad_crc));
        assert!(!mouse.allows(&request(0x00, 0x05, &[3])));
    }
}
