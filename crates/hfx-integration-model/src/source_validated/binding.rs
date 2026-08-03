// SPDX-License-Identifier: GPL-2.0-only

//! Binding of validated source documents into runtime integration authority.

use super::document::{
    DeviceDocument, DirectUsbDocument, OpenRazerDocument, OpenRgbDocument, RuleDocument,
};
use super::validation::{
    EXPECTED_PROTOCOL, RAZER_VENDOR_ID, ordered_unique, valid_profile_id, valid_sha256,
    validate_publication_identity_rule, validate_rules,
};
use super::{
    SourceValidatedCatalogError, SourceValidatedDevice, SourceValidatedOpenRazer,
    SourceValidatedOpenRgb, SourceValidatedPublicationIdentity,
};
use crate::{
    NativeCommandPolicy, NativeCommandPolicyError, NativePolicyRuleInput,
    native_command_policy_from_rules,
};
use hfx_domain::{DeviceKind, ProductId, VendorId};

pub(super) fn bind_device(
    document: DeviceDocument,
) -> Result<SourceValidatedDevice, SourceValidatedCatalogError> {
    let device_kind = match document.identity.device_kind.as_str() {
        "mouse" => DeviceKind::Mouse,
        "keyboard" => DeviceKind::Keyboard,
        _ => return Err(SourceValidatedCatalogError::InvalidDevice),
    };
    if document.identity.vendor_id != RAZER_VENDOR_ID
        || document.model_name.is_empty()
        || document.model_name.len() > 127
        || document.report_family != EXPECTED_PROTOCOL
        || document.route_id
            != format!(
                "razer.{:04x}.{:04x}.{}",
                document.identity.vendor_id,
                document.identity.product_id,
                document.identity.device_kind
            )
    {
        return Err(SourceValidatedCatalogError::InvalidDevice);
    }
    let vendor_id = VendorId::try_from(document.identity.vendor_id)
        .map_err(|_| SourceValidatedCatalogError::InvalidDevice)?;
    let product_id = ProductId::try_from(document.identity.product_id)
        .map_err(|_| SourceValidatedCatalogError::InvalidDevice)?;
    let hardware_profile_id = match (
        document.hardware_assurance.state.as_str(),
        document.hardware_assurance.profile_id,
    ) {
        ("hardware-verified", Some(profile)) if valid_profile_id(&profile) => Some(profile),
        ("not-hardware-verified", None) => None,
        _ => return Err(SourceValidatedCatalogError::InvalidDevice),
    };
    let openrazer = bind_openrazer(document.integrations.openrazer, device_kind)?;
    let openrgb = bind_openrgb(document.integrations.openrgb)?;
    let operational = openrazer.is_some() || openrgb.is_some();
    if (document.operational_state == "source-validated") != operational
        || (!operational && document.operational_state != "backend-required")
        || (operational && document.rules.is_empty())
        || (!operational && !document.rules.is_empty())
    {
        return Err(SourceValidatedCatalogError::InvalidDevice);
    }
    validate_rules(&document.rules, openrazer.is_some(), openrgb.is_some())?;
    if let Some(integration) = &openrazer {
        validate_publication_identity_rule(&document.rules, &integration.publication_identity)?;
    }
    let command_policy = bind_command_policy(&document.rules, None)?;
    let openrazer_command_policy = bind_command_policy(&document.rules, Some("openrazer"))?;
    let openrgb_command_policy = bind_command_policy(&document.rules, Some("openrgb"))?;
    if openrazer.is_some() != openrazer_command_policy.is_some()
        || openrgb.is_some() != openrgb_command_policy.is_some()
        || operational != command_policy.is_some()
    {
        return Err(SourceValidatedCatalogError::InvalidPolicy);
    }
    Ok(SourceValidatedDevice {
        route_id: document.route_id,
        vendor_id,
        product_id,
        device_kind,
        model_name: document.model_name,
        hardware_profile_id,
        openrazer,
        openrgb,
        openrazer_command_policy,
        openrgb_command_policy,
        command_policy,
    })
}

pub(super) fn bind_command_policy(
    rules: &[RuleDocument],
    integration: Option<&str>,
) -> Result<Option<NativeCommandPolicy>, SourceValidatedCatalogError> {
    let inputs = rules
        .iter()
        .filter(|rule| {
            integration.is_none_or(|name| rule.integrations.iter().any(|value| value == name))
        })
        .map(|rule| NativePolicyRuleInput {
            command_class: rule.command_class,
            command_id: rule.command_id,
            data_sizes: rule.data_sizes.clone(),
            matcher: rule.matcher.clone(),
        })
        .collect::<Vec<_>>();
    if inputs.is_empty() {
        return Ok(None);
    }
    native_command_policy_from_rules(inputs)
        .map(Some)
        .map_err(|NativeCommandPolicyError| SourceValidatedCatalogError::InvalidPolicy)
}

pub(super) fn bind_openrazer(
    document: OpenRazerDocument,
    device_kind: DeviceKind,
) -> Result<Option<SourceValidatedOpenRazer>, SourceValidatedCatalogError> {
    match document {
        OpenRazerDocument::SourceValidated {
            reason,
            record_id,
            class_name,
            source,
            image_url,
            methods,
            driver_family,
            event_file_regex,
            direct_usb_reference,
            publication_identity,
        } => {
            let source = *source;
            if reason.is_some()
                || !record_id.starts_with("openrazer:")
                || class_name.is_empty()
                || source.path
                    != match device_kind {
                        DeviceKind::Mouse => "daemon/openrazer_daemon/hardware/mouse.py",
                        DeviceKind::Keyboard => "daemon/openrazer_daemon/hardware/keyboards.py",
                        _ => return Err(SourceValidatedCatalogError::InvalidIntegration),
                    }
                || source.line == 0
                || !valid_sha256(&source.sha256)
                || !image_url.starts_with("https://")
                || methods.is_empty()
                || methods.len() > 128
                || !ordered_unique(&methods)
                || driver_family
                    != if device_kind == DeviceKind::Mouse {
                        "razermouse"
                    } else {
                        "razerkbd"
                    }
                || event_file_regex.as_ref().is_some_and(String::is_empty)
                || publication_identity.method != "get_serial"
                || publication_identity.transaction_id == 0
                || publication_identity.command_id < 0x80
                || usize::from(publication_identity.data_size)
                    != publication_identity.arguments.len()
                || !valid_sha256(&publication_identity.normalized_sha256)
            {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            let direct_usb_product_id =
                bind_direct_usb_reference(*direct_usb_reference, device_kind)?;
            Ok(Some(SourceValidatedOpenRazer {
                record_id,
                class_name,
                source_path: source.path,
                source_sha256: source.sha256,
                image_url,
                methods,
                driver_family,
                event_file_regex,
                direct_usb_product_id,
                publication_identity: SourceValidatedPublicationIdentity {
                    method: publication_identity.method,
                    transaction_id: publication_identity.transaction_id,
                    command_class: publication_identity.command_class,
                    command_id: publication_identity.command_id,
                    data_size: publication_identity.data_size,
                    arguments: publication_identity.arguments,
                    normalized_sha256: publication_identity.normalized_sha256,
                },
            }))
        }
        OpenRazerDocument::BackendRequired { reason, record_id } => {
            if reason.is_empty()
                || record_id
                    .as_ref()
                    .is_some_and(|value| !value.starts_with("openrazer:"))
            {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            Ok(None)
        }
        OpenRazerDocument::Absent { reason, record_id } => {
            if reason.is_some() || record_id.is_some() {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            Ok(None)
        }
    }
}

fn bind_direct_usb_reference(
    document: Option<DirectUsbDocument>,
    device_kind: DeviceKind,
) -> Result<Option<ProductId>, SourceValidatedCatalogError> {
    let expected_kind = match device_kind {
        DeviceKind::Mouse => "mouse",
        DeviceKind::Keyboard => "keyboard",
        _ => return Err(SourceValidatedCatalogError::InvalidIntegration),
    };
    match document {
        Some(direct)
            if direct.vendor_id == RAZER_VENDOR_ID
                && direct.device_kind == expected_kind
                && direct.transport_variant == "wired"
                && !direct.model_name.is_empty()
                && !direct.class_name.is_empty()
                && !direct.source_path.is_empty()
                && direct.source_line > 0 =>
        {
            ProductId::try_from(direct.product_id)
                .map(Some)
                .map_err(|_| SourceValidatedCatalogError::InvalidIntegration)
        }
        None => Ok(None),
        Some(_) => Err(SourceValidatedCatalogError::InvalidIntegration),
    }
}

pub(super) fn bind_openrgb(
    document: OpenRgbDocument,
) -> Result<Option<SourceValidatedOpenRgb>, SourceValidatedCatalogError> {
    match document {
        OpenRgbDocument::SourceValidated {
            reason,
            record_id,
            source_device_key,
            source,
            transaction_id,
            led_selector,
            lighting_topology,
        } => {
            let zone_slots = lighting_topology.zones.iter().try_fold(0_u16, |sum, zone| {
                if zone.name.is_empty()
                    || !matches!(
                        zone.zone_type.as_str(),
                        "ZONE_TYPE_SINGLE" | "ZONE_TYPE_LINEAR" | "ZONE_TYPE_MATRIX"
                    )
                    || zone.rows == 0
                    || zone.columns == 0
                {
                    return None;
                }
                sum.checked_add(u16::from(zone.rows) * u16::from(zone.columns))
            });
            if reason.is_some()
                || !record_id.starts_with("openrgb:")
                || source_device_key.is_empty()
                || source.path != "Controllers/RazerController/RazerDevices.cpp"
                || source.line == 0
                || !valid_sha256(&source.sha256)
                || transaction_id == 0
                || !matches!(led_selector, 0 | 5)
                || lighting_topology.matrix_type != "RAZER_MATRIX_TYPE_EXTENDED"
                || lighting_topology.rows == 0
                || lighting_topology.rows > 32
                || lighting_topology.columns == 0
                || lighting_topology.columns > 25
                || lighting_topology.application_slot_count
                    != u16::from(lighting_topology.rows) * u16::from(lighting_topology.columns)
                || !zone_slots.is_some_and(|slots| {
                    slots > 0 && slots <= lighting_topology.application_slot_count
                })
                || lighting_topology
                    .layout_key
                    .as_ref()
                    .is_some_and(String::is_empty)
            {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            Ok(Some(SourceValidatedOpenRgb {
                record_id,
                source_device_key,
                transaction_id,
                led_selector,
                rows: lighting_topology.rows,
                columns: lighting_topology.columns,
                led_count: lighting_topology.application_slot_count,
            }))
        }
        OpenRgbDocument::BackendRequired { reason, record_id } => {
            if reason.is_empty()
                || record_id
                    .as_ref()
                    .is_some_and(|value| !value.starts_with("openrgb:"))
            {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            Ok(None)
        }
        OpenRgbDocument::Absent { reason, record_id } => {
            if reason.is_some() || record_id.is_some() {
                return Err(SourceValidatedCatalogError::InvalidIntegration);
            }
            Ok(None)
        }
    }
}
