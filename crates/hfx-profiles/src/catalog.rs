// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    LightingTopology, PROFILES, PassiveTelemetryRecord, PresentationRecord, ProfileRecord,
};
use hfx_domain::{
    CapabilityId, CarrierIndex, DeviceKind, DomainValueError, LedCount, ProductId, ProfileDigest,
    ProfileId, ProfileKind, ProfileRevision, RouteKind, SupportLevel, VendorId,
};
use std::collections::BTreeMap;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProfileCatalogError {
    InvalidGeneratedValue(DomainValueError),
    DuplicateProfile(ProfileId),
    DuplicateReceiverIdentity(VendorId, ProductId),
    DuplicateChildIdentity(ProductId),
    MissingUsbIdentity(ProfileId),
    MissingPresentation(ProfileId),
    UnexpectedPresentation(ProfileId),
    InvalidProfileKind(ProfileId),
}

impl fmt::Display for ProfileCatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidGeneratedValue(error) => write!(formatter, "{error}"),
            Self::DuplicateProfile(profile_id) => {
                write!(formatter, "profile identity is duplicated: {profile_id}")
            }
            Self::DuplicateReceiverIdentity(vendor_id, product_id) => write!(
                formatter,
                "receiver USB identity is duplicated: {vendor_id}:{product_id}"
            ),
            Self::DuplicateChildIdentity(product_id) => {
                write!(
                    formatter,
                    "child product identity is duplicated: {product_id}"
                )
            }
            Self::MissingUsbIdentity(profile_id) => {
                write!(
                    formatter,
                    "hardware profile lacks its required identity: {profile_id}"
                )
            }
            Self::MissingPresentation(profile_id) => {
                write!(
                    formatter,
                    "child profile lacks presentation metadata: {profile_id}"
                )
            }
            Self::UnexpectedPresentation(profile_id) => write!(
                formatter,
                "non-child profile contains application presentation metadata: {profile_id}"
            ),
            Self::InvalidProfileKind(profile_id) => {
                write!(
                    formatter,
                    "profile kind and device kind disagree: {profile_id}"
                )
            }
        }
    }
}

impl std::error::Error for ProfileCatalogError {}

impl From<DomainValueError> for ProfileCatalogError {
    fn from(value: DomainValueError) -> Self {
        Self::InvalidGeneratedValue(value)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeCapability {
    pub id: CapabilityId,
    pub support_level: SupportLevel,
    pub writable: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeLightingTopology {
    pub physical_led_count: LedCount,
    pub application_slot_count: LedCount,
    pub carrier_count: LedCount,
    pub rows: u16,
    pub columns: u16,
    pub application_index_to_carrier: Vec<CarrierIndex>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimePresentation {
    pub upstream_id: &'static str,
    pub owner: &'static str,
    pub project_version: &'static str,
    pub source_commit: &'static str,
    pub model_key: &'static str,
    pub layout_key: Option<&'static str>,
    pub transport_variant: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeProfile {
    pub profile_id: ProfileId,
    pub runtime_digest: ProfileDigest,
    pub revision: ProfileRevision,
    pub profile_kind: ProfileKind,
    pub device_kind: DeviceKind,
    pub vendor_id: Option<VendorId>,
    pub product_id: Option<ProductId>,
    pub model_name: &'static str,
    pub transport_backend_id: Option<u32>,
    pub protocol_family: Option<&'static str>,
    pub receiver_protocols: &'static [&'static str],
    pub routes: &'static [RouteKind],
    pub supported_child_kinds: &'static [DeviceKind],
    pub required_sibling_kinds: &'static [DeviceKind],
    pub exact_child_combinations: bool,
    pub capabilities: Vec<RuntimeCapability>,
    pub lighting: Option<RuntimeLightingTopology>,
    pub passive: Option<PassiveTelemetryRecord>,
    pub presentation: Option<RuntimePresentation>,
}

impl RuntimeProfile {
    #[must_use]
    pub fn support_level(&self) -> SupportLevel {
        self.capabilities
            .iter()
            .map(|capability| capability.support_level)
            .max()
            .unwrap_or(SupportLevel::Candidate)
    }
}

/// Validated typed view over the generated, immutable profile catalog.
#[derive(Clone, Debug)]
pub struct RuntimeProfileCatalog {
    profiles: BTreeMap<ProfileId, RuntimeProfile>,
    receivers: BTreeMap<(VendorId, ProductId), ProfileId>,
    children: BTreeMap<ProductId, ProfileId>,
}

impl RuntimeProfileCatalog {
    /// Validates and converts the generated catalog once at service startup.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed generated values, duplicate identities,
    /// missing hardware identities, or contradictory profile kinds.
    pub fn load() -> Result<Self, ProfileCatalogError> {
        Self::from_records(PROFILES)
    }

    fn from_records(records: &[ProfileRecord]) -> Result<Self, ProfileCatalogError> {
        let mut catalog = Self {
            profiles: BTreeMap::new(),
            receivers: BTreeMap::new(),
            children: BTreeMap::new(),
        };
        for record in records {
            let profile = convert_profile(record)?;
            validate_kind(&profile)?;
            let profile_id = profile.profile_id.clone();
            match profile.profile_kind {
                ProfileKind::Receiver => {
                    let Some(identity) = profile.vendor_id.zip(profile.product_id) else {
                        return Err(ProfileCatalogError::MissingUsbIdentity(profile_id));
                    };
                    if catalog
                        .receivers
                        .insert(identity, profile_id.clone())
                        .is_some()
                    {
                        return Err(ProfileCatalogError::DuplicateReceiverIdentity(
                            identity.0, identity.1,
                        ));
                    }
                }
                ProfileKind::Child => {
                    let Some(product_id) = profile.product_id else {
                        return Err(ProfileCatalogError::MissingUsbIdentity(profile_id));
                    };
                    if catalog
                        .children
                        .insert(product_id, profile_id.clone())
                        .is_some()
                    {
                        return Err(ProfileCatalogError::DuplicateChildIdentity(product_id));
                    }
                }
                ProfileKind::Surface => {}
            }
            if catalog
                .profiles
                .insert(profile_id.clone(), profile)
                .is_some()
            {
                return Err(ProfileCatalogError::DuplicateProfile(profile_id));
            }
        }
        Ok(catalog)
    }

    #[must_use]
    pub fn profile(&self, profile_id: &ProfileId) -> Option<&RuntimeProfile> {
        self.profiles.get(profile_id)
    }

    #[must_use]
    pub fn receiver(&self, vendor_id: VendorId, product_id: ProductId) -> Option<&RuntimeProfile> {
        self.receivers
            .get(&(vendor_id, product_id))
            .and_then(|profile_id| self.profiles.get(profile_id))
    }

    #[must_use]
    pub fn child(&self, product_id: ProductId) -> Option<&RuntimeProfile> {
        self.children
            .get(&product_id)
            .and_then(|profile_id| self.profiles.get(profile_id))
    }

    #[must_use]
    pub fn iter(&self) -> impl ExactSizeIterator<Item = &RuntimeProfile> {
        self.profiles.values()
    }
}

fn convert_profile(record: &ProfileRecord) -> Result<RuntimeProfile, ProfileCatalogError> {
    Ok(RuntimeProfile {
        profile_id: ProfileId::try_from(record.id)?,
        runtime_digest: ProfileDigest::try_from(record.runtime_sha256)?,
        revision: ProfileRevision::try_from(record.revision)?,
        profile_kind: record.profile_kind,
        device_kind: record.device_kind,
        vendor_id: record.vendor_id.map(VendorId::try_from).transpose()?,
        product_id: record.product_id.map(ProductId::try_from).transpose()?,
        model_name: record.model_name,
        transport_backend_id: record.transport_backend_id,
        protocol_family: record.protocol_family,
        receiver_protocols: record.receiver_protocols,
        routes: record.routes,
        supported_child_kinds: record.supported_child_kinds,
        required_sibling_kinds: record.required_sibling_kinds,
        exact_child_combinations: record.exact_child_combinations,
        capabilities: record
            .capabilities
            .iter()
            .map(|capability| {
                Ok(RuntimeCapability {
                    id: CapabilityId::try_from(capability.id)?,
                    support_level: capability.support_level,
                    writable: capability.writable,
                })
            })
            .collect::<Result<Vec<_>, ProfileCatalogError>>()?,
        lighting: record.lighting.map(convert_lighting).transpose()?,
        passive: record.passive,
        presentation: record.presentation.map(convert_presentation),
    })
}

fn convert_presentation(presentation: PresentationRecord) -> RuntimePresentation {
    RuntimePresentation {
        upstream_id: presentation.upstream_id,
        owner: presentation.owner,
        project_version: presentation.project_version,
        source_commit: presentation.source_commit,
        model_key: presentation.model_key,
        layout_key: presentation.layout_key,
        transport_variant: presentation.transport_variant,
    }
}

fn convert_lighting(
    lighting: LightingTopology,
) -> Result<RuntimeLightingTopology, ProfileCatalogError> {
    Ok(RuntimeLightingTopology {
        physical_led_count: LedCount::try_from(lighting.physical_led_count)?,
        application_slot_count: LedCount::try_from(lighting.application_slot_count)?,
        carrier_count: LedCount::try_from(lighting.carrier_count)?,
        rows: lighting.rows,
        columns: lighting.columns,
        application_index_to_carrier: lighting
            .application_index_to_carrier
            .iter()
            .copied()
            .map(CarrierIndex::try_from)
            .collect::<Result<Vec<_>, _>>()?,
    })
}

fn validate_kind(profile: &RuntimeProfile) -> Result<(), ProfileCatalogError> {
    let valid = matches!(
        (profile.profile_kind, profile.device_kind),
        (ProfileKind::Receiver, DeviceKind::Receiver)
            | (
                ProfileKind::Child,
                DeviceKind::Mouse | DeviceKind::Keyboard | DeviceKind::Unknown
            )
            | (ProfileKind::Surface, DeviceKind::Mat)
    );
    if !valid {
        return Err(ProfileCatalogError::InvalidProfileKind(
            profile.profile_id.clone(),
        ));
    }
    match (profile.profile_kind, profile.presentation.is_some()) {
        (ProfileKind::Child, false) => Err(ProfileCatalogError::MissingPresentation(
            profile.profile_id.clone(),
        )),
        (ProfileKind::Receiver | ProfileKind::Surface, true) => Err(
            ProfileCatalogError::UnexpectedPresentation(profile.profile_id.clone()),
        ),
        _ => Ok(()),
    }
}
