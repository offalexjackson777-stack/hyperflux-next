// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{
    DeviceLifecycle, DeviceStateAuthority, ProfileRegistry, QualifiedDeviceProfile,
    QualifiedReceiverProfile, ReceiverGenerationLifecycle, ReceiverLifecycleRegistry,
};
use hfx_domain::{
    DeviceWriteReadiness, GenerationId, PresenceState, ProductId, ProfileDigest, ProfileId,
    ProfileKind, ReceiverId, ReceiverLifecycleState, ResourceKind, VendorId,
};
use hfx_profiles::{ProfileCatalogError, RuntimeProfile, RuntimeProfileCatalog};
use hfx_protocol::ResourceKey;
use std::collections::BTreeMap;
use std::fmt;

pub const DEFAULT_MAX_PROFILE_BINDINGS: usize = hfx_core::DEFAULT_MAX_RECEIVERS;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiverProfileBinding {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
    pub protocol_family: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProfileBindingOutcome {
    Bound,
    Unchanged,
    Replaced { previous_generation: GenerationId },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RuntimeProfileAuthorityError {
    InvalidCapacity,
    Catalog(ProfileCatalogError),
    UnsupportedReceiver(VendorId, ProductId),
    MissingProtocolFamily(ProfileId),
    CapacityExhausted,
    ConflictingBinding {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    },
    StaleGeneration {
        receiver_id: ReceiverId,
        active_generation: GenerationId,
        requested_generation: GenerationId,
    },
}

impl fmt::Display for RuntimeProfileAuthorityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCapacity => formatter.write_str("profile binding capacity is invalid"),
            Self::Catalog(error) => write!(formatter, "{error}"),
            Self::UnsupportedReceiver(vendor_id, product_id) => {
                write!(
                    formatter,
                    "receiver profile is not qualified: {vendor_id}:{product_id}"
                )
            }
            Self::MissingProtocolFamily(profile_id) => {
                write!(
                    formatter,
                    "receiver profile has no protocol family: {profile_id}"
                )
            }
            Self::CapacityExhausted => formatter.write_str("profile binding capacity is exhausted"),
            Self::ConflictingBinding {
                receiver_id,
                generation_id,
            } => write!(
                formatter,
                "profile binding conflicts for {receiver_id} generation {generation_id}"
            ),
            Self::StaleGeneration {
                receiver_id,
                active_generation,
                requested_generation,
            } => write!(
                formatter,
                "profile binding is stale for {receiver_id}: active {active_generation}, requested {requested_generation}"
            ),
        }
    }
}

impl std::error::Error for RuntimeProfileAuthorityError {}

impl From<ProfileCatalogError> for RuntimeProfileAuthorityError {
    fn from(error: ProfileCatalogError) -> Self {
        Self::Catalog(error)
    }
}

/// Generation-bound runtime profile authority derived from the generated catalog.
#[derive(Clone, Debug)]
pub struct RuntimeProfileAuthority {
    catalog: RuntimeProfileCatalog,
    capacity: usize,
    bindings: BTreeMap<ReceiverId, ReceiverProfileBinding>,
}

impl RuntimeProfileAuthority {
    /// Loads the generated catalog and creates a bounded binding registry.
    ///
    /// # Errors
    ///
    /// Returns an error for an invalid bound or invalid generated catalog.
    pub fn load(capacity: usize) -> Result<Self, RuntimeProfileAuthorityError> {
        Self::new(RuntimeProfileCatalog::load()?, capacity)
    }

    /// Creates an authority from one already validated catalog.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is zero or exceeds the public receiver bound.
    pub fn new(
        catalog: RuntimeProfileCatalog,
        capacity: usize,
    ) -> Result<Self, RuntimeProfileAuthorityError> {
        if !(1..=DEFAULT_MAX_PROFILE_BINDINGS).contains(&capacity) {
            return Err(RuntimeProfileAuthorityError::InvalidCapacity);
        }
        Ok(Self {
            catalog,
            capacity,
            bindings: BTreeMap::new(),
        })
    }

    /// Binds one observed receiver USB identity to one exact generation.
    ///
    /// Replaying the same binding is idempotent. A strictly newer generation
    /// replaces the old binding; an older generation is rejected without mutation.
    ///
    /// # Errors
    ///
    /// Returns an error for unknown hardware, missing generated compatibility,
    /// exhausted capacity, or stale generation evidence.
    pub fn bind_receiver(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        vendor_id: VendorId,
        product_id: ProductId,
    ) -> Result<ProfileBindingOutcome, RuntimeProfileAuthorityError> {
        let profile = self.catalog.receiver(vendor_id, product_id).ok_or(
            RuntimeProfileAuthorityError::UnsupportedReceiver(vendor_id, product_id),
        )?;
        let protocol_family = profile.protocol_family.ok_or_else(|| {
            RuntimeProfileAuthorityError::MissingProtocolFamily(profile.profile_id.clone())
        })?;
        let next = ReceiverProfileBinding {
            receiver_id: receiver_id.clone(),
            generation_id,
            profile_id: profile.profile_id.clone(),
            profile_digest: profile.runtime_digest.clone(),
            protocol_family,
        };
        if let Some(current) = self.bindings.get(&receiver_id) {
            if generation_id < current.generation_id {
                return Err(RuntimeProfileAuthorityError::StaleGeneration {
                    receiver_id,
                    active_generation: current.generation_id,
                    requested_generation: generation_id,
                });
            }
            if generation_id == current.generation_id {
                if current.profile_id == next.profile_id
                    && current.profile_digest == next.profile_digest
                    && current.protocol_family == next.protocol_family
                {
                    return Ok(ProfileBindingOutcome::Unchanged);
                }
                return Err(RuntimeProfileAuthorityError::ConflictingBinding {
                    receiver_id,
                    generation_id,
                });
            }
            let previous_generation = current.generation_id;
            self.bindings.insert(receiver_id, next);
            return Ok(ProfileBindingOutcome::Replaced {
                previous_generation,
            });
        }
        if self.bindings.len() == self.capacity {
            return Err(RuntimeProfileAuthorityError::CapacityExhausted);
        }
        self.bindings.insert(receiver_id, next);
        Ok(ProfileBindingOutcome::Bound)
    }

    /// Removes only the exact active generation binding.
    pub fn retire(&mut self, receiver_id: &ReceiverId, generation_id: GenerationId) -> bool {
        if self
            .bindings
            .get(receiver_id)
            .is_some_and(|binding| binding.generation_id == generation_id)
        {
            self.bindings.remove(receiver_id);
            true
        } else {
            false
        }
    }

    #[must_use]
    pub fn binding(&self, receiver_id: &ReceiverId) -> Option<&ReceiverProfileBinding> {
        self.bindings.get(receiver_id)
    }

    #[must_use]
    pub const fn catalog(&self) -> &RuntimeProfileCatalog {
        &self.catalog
    }

    #[must_use]
    pub const fn view<'a>(
        &'a self,
        receivers: &'a ReceiverLifecycleRegistry,
    ) -> RuntimeProfileView<'a> {
        RuntimeProfileView {
            authority: self,
            receivers,
        }
    }
}

/// Read-only profile and capability view over current lifecycle state.
#[derive(Clone, Copy, Debug)]
pub struct RuntimeProfileView<'a> {
    authority: &'a RuntimeProfileAuthority,
    receivers: &'a ReceiverLifecycleRegistry,
}

impl<'a> RuntimeProfileView<'a> {
    #[must_use]
    pub fn profile_for_device(
        self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        device: &DeviceLifecycle,
    ) -> Option<&'a RuntimeProfile> {
        let (binding, generation, receiver_profile) =
            self.active_generation(receiver_id, generation_id)?;
        let device = generation.device(device.identity().device_id())?;
        let profile = self
            .authority
            .catalog
            .child(device.identity().product_id())?;
        let kind = device.identity().device_kind();
        if profile.profile_kind != ProfileKind::Child
            || profile.device_kind != kind
            || !receiver_profile.supported_child_kinds.contains(&kind)
            || !profile
                .receiver_protocols
                .contains(&binding.protocol_family)
            || !profile.required_sibling_kinds.iter().all(|required| {
                generation
                    .devices()
                    .any(|candidate| candidate.identity().device_kind() == *required)
            })
            || !device
                .endpoints()
                .any(|endpoint| profile.routes.contains(&endpoint.identity().route_kind()))
        {
            return None;
        }
        Some(profile)
    }

    fn active_generation(
        self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Option<(
        &'a ReceiverProfileBinding,
        &'a ReceiverGenerationLifecycle,
        &'a RuntimeProfile,
    )> {
        let binding = self.authority.bindings.get(receiver_id)?;
        if binding.generation_id != generation_id {
            return None;
        }
        let generation = self.receivers.get(receiver_id)?.current()?;
        if generation.generation_id() != generation_id {
            return None;
        }
        let receiver_profile = self.authority.catalog.profile(&binding.profile_id)?;
        if receiver_profile.profile_kind != ProfileKind::Receiver
            || receiver_profile.runtime_digest != binding.profile_digest
            || receiver_profile.protocol_family != Some(binding.protocol_family)
        {
            return None;
        }
        Some((binding, generation, receiver_profile))
    }
}

impl ProfileRegistry for RuntimeProfileView<'_> {
    fn supports(&self, resource: &ResourceKey) -> bool {
        <Self as ProfileRegistry>::device_profile(self, resource).is_some()
    }

    fn receiver_profile(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Option<QualifiedReceiverProfile> {
        let (binding, _, _) = (*self).active_generation(receiver_id, generation_id)?;
        Some(QualifiedReceiverProfile {
            profile_id: binding.profile_id.clone(),
            profile_digest: binding.profile_digest.clone(),
        })
    }

    fn device_profile(&self, resource: &ResourceKey) -> Option<QualifiedDeviceProfile> {
        if resource.kind != ResourceKind::Lighting {
            return None;
        }
        let (_, generation, _) =
            (*self).active_generation(&resource.receiver_id, resource.generation_id)?;
        let device = generation.device(&resource.device_id)?;
        let profile = RuntimeProfileView::profile_for_device(
            *self,
            &resource.receiver_id,
            resource.generation_id,
            device,
        )?;
        let lighting = profile.lighting.as_ref()?;
        if !profile.capabilities.iter().any(|capability| {
            capability.writable && capability.id.as_str() == "lighting.direct-frame"
        }) {
            return None;
        }
        Some(QualifiedDeviceProfile {
            profile_id: profile.profile_id.clone(),
            profile_digest: profile.runtime_digest.clone(),
            application_slot_count: lighting.application_slot_count,
        })
    }
}

impl DeviceStateAuthority for RuntimeProfileView<'_> {
    fn write_readiness(&self, resource: &ResourceKey) -> DeviceWriteReadiness {
        if resource.kind != ResourceKind::Lighting {
            return DeviceWriteReadiness::Unknown;
        }
        let Some((_, generation, _)) =
            (*self).active_generation(&resource.receiver_id, resource.generation_id)
        else {
            return DeviceWriteReadiness::Unknown;
        };
        match generation.lifecycle().value() {
            ReceiverLifecycleState::Active => {}
            ReceiverLifecycleState::Suspended => return DeviceWriteReadiness::Sleeping,
            ReceiverLifecycleState::PartiallySuspended | ReceiverLifecycleState::Disconnecting => {
                return DeviceWriteReadiness::Unavailable;
            }
            ReceiverLifecycleState::Unknown => return DeviceWriteReadiness::Unknown,
        }
        let Some(device) = generation.device(&resource.device_id) else {
            return DeviceWriteReadiness::Unknown;
        };
        match device.presence() {
            PresenceState::Available => DeviceWriteReadiness::Ready,
            PresenceState::Sleeping => DeviceWriteReadiness::Sleeping,
            PresenceState::Unavailable => DeviceWriteReadiness::Unavailable,
            PresenceState::Unknown => DeviceWriteReadiness::Unknown,
        }
    }
}
