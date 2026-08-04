// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    LifecyclePolicyRecord, LightingTopology, NativeExchangeRecord, PROFILES,
    PassiveTelemetryRecord, PresentationRecord, ProfileRecord, QualificationMigrationRecord,
    ReturnEvidenceSource,
};
use crate::{NativeReportValidationError, validate_native_exchange_record};
use hfx_domain::{
    CapabilityId, CarrierIndex, DeviceKind, DomainValueError, LedCount, ProductId, ProfileDigest,
    ProfileId, ProfileKind, ProfileRevision, ReturnEvidence, RouteKind, SupportLevel, VendorId,
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
    InvalidNativeExchange(ProfileId, NativeReportValidationError),
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
            Self::InvalidNativeExchange(profile_id, error) => {
                write!(
                    formatter,
                    "native exchange profile is invalid for {profile_id}: {error}"
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
pub struct RuntimeQualificationMigration {
    pub from_runtime_digest: ProfileDigest,
    pub preserve_stage_roles: &'static [&'static str],
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
    pub native_exchange: Option<NativeExchangeRecord>,
    pub lighting: Option<RuntimeLightingTopology>,
    pub passive: Option<PassiveTelemetryRecord>,
    pub lifecycle: Option<LifecyclePolicyRecord>,
    pub qualification_migrations: Vec<RuntimeQualificationMigration>,
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

impl ReturnEvidenceSource {
    /// Projects generated profile evidence into the application-neutral core
    /// lifecycle vocabulary.
    #[must_use]
    pub const fn domain(self) -> ReturnEvidence {
        match self {
            Self::BatteryReport => ReturnEvidence::BatteryReport,
            Self::ContactReport => ReturnEvidence::ContactReport,
            Self::EndpointActivity => ReturnEvidence::EndpointActivity,
            Self::RouteReturn => ReturnEvidence::RouteReturn,
        }
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
    let profile_id = ProfileId::try_from(record.id)?;
    if let Some(native_exchange) = record.native_exchange {
        validate_native_exchange_record(&native_exchange).map_err(|error| {
            ProfileCatalogError::InvalidNativeExchange(profile_id.clone(), error)
        })?;
    }
    Ok(RuntimeProfile {
        profile_id,
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
        native_exchange: record.native_exchange,
        lighting: record.lighting.map(convert_lighting).transpose()?,
        passive: record.passive,
        lifecycle: record.lifecycle,
        qualification_migrations: record
            .qualification_migrations
            .iter()
            .map(convert_qualification_migration)
            .collect::<Result<Vec<_>, _>>()?,
        presentation: record.presentation.map(convert_presentation),
    })
}

fn convert_qualification_migration(
    migration: &QualificationMigrationRecord,
) -> Result<RuntimeQualificationMigration, ProfileCatalogError> {
    Ok(RuntimeQualificationMigration {
        from_runtime_digest: ProfileDigest::try_from(migration.from_runtime_sha256)?,
        preserve_stage_roles: migration.preserve_stage_roles,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{CapabilityRecord, PresentationRecord};

    const SYNTHETIC_CAPABILITIES: &[CapabilityRecord] = &[CapabilityRecord {
        id: "native.feature-report-exchange",
        support_level: SupportLevel::ProductionQualified,
        writable: true,
    }];

    #[test]
    fn a_second_same_protocol_device_is_profile_data_only() {
        let mut records = PROFILES.to_vec();
        records.push(ProfileRecord {
            id: "child.razer.viper-ultimate.007b",
            runtime_sha256: "1111111111111111111111111111111111111111111111111111111111111111",
            revision: 1,
            profile_kind: ProfileKind::Child,
            device_kind: DeviceKind::Mouse,
            vendor_id: Some(0x1532),
            product_id: Some(0x007b),
            model_name: "Razer Viper Ultimate",
            transport_backend_id: None,
            protocol_family: None,
            receiver_protocols: &["razer-hyperflux-v2"],
            routes: &[RouteKind::HyperfluxWireless],
            supported_child_kinds: &[],
            required_sibling_kinds: &[],
            exact_child_combinations: false,
            capabilities: SYNTHETIC_CAPABILITIES,
            native_exchange: None,
            lighting: None,
            passive: None,
            lifecycle: None,
            qualification_migrations: &[],
            presentation: Some(PresentationRecord {
                upstream_id: "openrgb",
                owner: "OpenRGB",
                project_version: "fixture",
                source_commit: "1111111111111111111111111111111111111111",
                model_key: "razer_viper_ultimate",
                layout_key: None,
                transport_variant: "wireless",
            }),
        });

        let catalog = RuntimeProfileCatalog::from_records(&records)
            .expect("the synthetic profile uses the existing profile contract");
        let profile = catalog
            .child(ProductId::try_from(0x007b_u16).expect("fixture PID is valid"))
            .expect("the synthetic profile is addressable by exact PID");
        assert_eq!(profile.model_name, "Razer Viper Ultimate");
        assert_eq!(profile.routes, &[RouteKind::HyperfluxWireless]);
        assert!(profile.capabilities.iter().any(|capability| {
            capability.id.as_str() == "native.feature-report-exchange"
                && capability.writable
                && capability.support_level == SupportLevel::ProductionQualified
        }));
    }

    #[test]
    fn generated_profile_catalog_permutations_preserve_exact_identity_rules() {
        for rotation in 0..PROFILES.len() {
            let mut records = PROFILES.to_vec();
            records.rotate_left(rotation);
            let catalog = RuntimeProfileCatalog::from_records(&records)
                .expect("catalog identity does not depend on record order");
            assert_eq!(catalog.iter().len(), PROFILES.len());
        }

        for (index, profile) in PROFILES.iter().enumerate() {
            let mut records = PROFILES.to_vec();
            records.push(*profile);
            assert!(
                RuntimeProfileCatalog::from_records(&records).is_err(),
                "duplicate profile {index} must fail closed"
            );
        }
    }
}
