// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    ClientId, ComponentVersion, ControllerAvailability, FreshnessState, InventoryAvailability,
    LedCount, LogicalDeviceId, ModelName, PairingState, PowerState, PresenceState, PresentationKey,
    ProfileId, ProfileKind, ReceiverLifecycleState, ResourceKind, RouteKind, RouteState,
    SleepState, SourceRevision, SupportLevel, TransportVariant, UpstreamId, UpstreamOwner,
};
use hfx_profiles::{RuntimeProfile, RuntimeProfileCatalog};
use hfx_protocol::{
    BridgeSnapshot, ControllerActions, ControllerOwnership, ControllerView, DeviceInventoryView,
    EndpointSnapshot, IntegrationReceiverView, IntegrationView, LightingTopologyView,
    LogicalDeviceSnapshot, OtherOwnedController, PresentationView, ProfileBindingView,
    ReceiverSnapshot, ResourceKey, SnapshotValidationError, UnownedController,
    ViewerOwnedController, validate_bridge_snapshot,
};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ViewModelError {
    InvalidSnapshot(SnapshotValidationError),
    UnknownReceiverProfile(ProfileId),
    ReceiverProfileMismatch(ProfileId),
    IncompleteDeviceProfileBinding(LogicalDeviceId),
    QualifiedChildWithoutReceiverProfile(LogicalDeviceId),
    UnknownDeviceProfile {
        device_id: LogicalDeviceId,
        profile_id: ProfileId,
    },
    DeviceProfileMismatch {
        device_id: LogicalDeviceId,
        profile_id: ProfileId,
    },
    DeviceCapabilitiesMismatch(LogicalDeviceId),
    DeviceSupportMismatch(LogicalDeviceId),
    UnqualifiedDeviceClaimsCapabilities(LogicalDeviceId),
    AmbiguousHyperfluxRoute(LogicalDeviceId),
    InvalidProfilePresentation(ProfileId),
}

impl fmt::Display for ViewModelError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSnapshot(error) => write!(formatter, "invalid bridge snapshot: {error}"),
            Self::UnknownReceiverProfile(profile_id) => {
                write!(
                    formatter,
                    "snapshot names an unknown receiver profile: {profile_id}"
                )
            }
            Self::ReceiverProfileMismatch(profile_id) => {
                write!(
                    formatter,
                    "receiver profile binding does not match the catalog: {profile_id}"
                )
            }
            Self::IncompleteDeviceProfileBinding(device_id) => {
                write!(
                    formatter,
                    "device profile binding is incomplete: {device_id}"
                )
            }
            Self::QualifiedChildWithoutReceiverProfile(device_id) => write!(
                formatter,
                "qualified child lacks an exact receiver profile binding: {device_id}"
            ),
            Self::UnknownDeviceProfile {
                device_id,
                profile_id,
            } => write!(
                formatter,
                "device {device_id} names an unknown profile: {profile_id}"
            ),
            Self::DeviceProfileMismatch {
                device_id,
                profile_id,
            } => write!(
                formatter,
                "device {device_id} does not match profile {profile_id}"
            ),
            Self::DeviceCapabilitiesMismatch(device_id) => {
                write!(
                    formatter,
                    "device capabilities drift from its profile: {device_id}"
                )
            }
            Self::DeviceSupportMismatch(device_id) => {
                write!(
                    formatter,
                    "device support level drifts from its profile: {device_id}"
                )
            }
            Self::UnqualifiedDeviceClaimsCapabilities(device_id) => write!(
                formatter,
                "unqualified device claims qualified capabilities: {device_id}"
            ),
            Self::AmbiguousHyperfluxRoute(device_id) => {
                write!(
                    formatter,
                    "device has multiple usable HyperFlux routes: {device_id}"
                )
            }
            Self::InvalidProfilePresentation(profile_id) => write!(
                formatter,
                "profile presentation cannot be represented by the integration protocol: {profile_id}"
            ),
        }
    }
}

impl std::error::Error for ViewModelError {}

/// Projects one validated bridge snapshot into application-facing inventory
/// and exact receiver-backed controllers.
///
/// Unknown hardware remains visible in inventory. A controller is emitted only
/// for a complete profile binding and one fresh, usable `HyperFlux` route.
///
/// # Errors
///
/// Returns an error when the snapshot is malformed, profile authority drifts,
/// or more than one receiver route could own the same controller.
pub fn project_integration_view(
    snapshot: &BridgeSnapshot,
    catalog: &RuntimeProfileCatalog,
    viewer: Option<&ClientId>,
) -> Result<IntegrationView, ViewModelError> {
    validate_bridge_snapshot(snapshot).map_err(ViewModelError::InvalidSnapshot)?;
    let receivers = snapshot
        .receivers
        .iter()
        .map(|receiver| project_receiver(receiver, catalog, viewer))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(IntegrationView {
        cursor: snapshot.cursor.clone(),
        receivers,
    })
}

fn project_receiver(
    receiver: &ReceiverSnapshot,
    catalog: &RuntimeProfileCatalog,
    viewer: Option<&ClientId>,
) -> Result<IntegrationReceiverView, ViewModelError> {
    let receiver_profile = resolve_receiver_profile(receiver, catalog)?;
    let receiver_binding = receiver_profile.map(|profile| ProfileBindingView {
        profile_id: profile.profile_id.clone(),
        profile_digest: profile.runtime_digest.clone(),
    });
    let mut inventory = Vec::with_capacity(receiver.devices.len());
    let mut controllers = Vec::with_capacity(receiver.devices.len());
    for device in &receiver.devices {
        let profile = resolve_device_profile(device, receiver_profile, catalog)?;
        inventory.push(project_inventory(receiver, device, profile)?);
        if let Some(controller) =
            project_controller(receiver, device, receiver_profile, profile, viewer)?
        {
            controllers.push(controller);
        }
    }
    Ok(IntegrationReceiverView {
        receiver_id: receiver.receiver_id.clone(),
        generation_id: receiver.generation_id,
        profile: receiver_binding,
        model_name: receiver_profile.map(model_name).transpose()?,
        lifecycle: receiver.lifecycle,
        stable_restore_enabled: receiver.stable_restore_enabled,
        restore_state: receiver.restore_state,
        inventory,
        controllers,
    })
}

fn resolve_receiver_profile<'a>(
    receiver: &ReceiverSnapshot,
    catalog: &'a RuntimeProfileCatalog,
) -> Result<Option<&'a RuntimeProfile>, ViewModelError> {
    let (Some(profile_id), Some(profile_digest)) = (
        receiver.profile_id.as_ref(),
        receiver.profile_digest.as_ref(),
    ) else {
        return Ok(None);
    };
    let profile = catalog
        .profile(profile_id)
        .ok_or_else(|| ViewModelError::UnknownReceiverProfile(profile_id.clone()))?;
    if profile.profile_kind != ProfileKind::Receiver || profile.runtime_digest != *profile_digest {
        return Err(ViewModelError::ReceiverProfileMismatch(profile_id.clone()));
    }
    Ok(Some(profile))
}

fn resolve_device_profile<'a>(
    device: &LogicalDeviceSnapshot,
    receiver_profile: Option<&RuntimeProfile>,
    catalog: &'a RuntimeProfileCatalog,
) -> Result<Option<&'a RuntimeProfile>, ViewModelError> {
    match (&device.profile_id, &device.profile_digest) {
        (None, None) => {
            if !device.capabilities.is_empty() || device.support_level != SupportLevel::ReadOnly {
                return Err(ViewModelError::UnqualifiedDeviceClaimsCapabilities(
                    device.device_id.clone(),
                ));
            }
            Ok(None)
        }
        (Some(_), None) | (None, Some(_)) => Err(ViewModelError::IncompleteDeviceProfileBinding(
            device.device_id.clone(),
        )),
        (Some(profile_id), Some(profile_digest)) => {
            let Some(receiver_profile) = receiver_profile else {
                return Err(ViewModelError::QualifiedChildWithoutReceiverProfile(
                    device.device_id.clone(),
                ));
            };
            let profile = catalog.profile(profile_id).ok_or_else(|| {
                ViewModelError::UnknownDeviceProfile {
                    device_id: device.device_id.clone(),
                    profile_id: profile_id.clone(),
                }
            })?;
            let exact_child = catalog.child(device.product_id);
            if profile.profile_kind != ProfileKind::Child
                || profile.device_kind != device.device_kind
                || profile.product_id != Some(device.product_id)
                || profile.runtime_digest != *profile_digest
                || exact_child.is_none_or(|child| child.profile_id != profile.profile_id)
                || !receiver_profile
                    .supported_child_kinds
                    .contains(&device.device_kind)
                || receiver_profile
                    .protocol_family
                    .is_none_or(|family| !profile.receiver_protocols.contains(&family))
                || !device
                    .endpoints
                    .iter()
                    .any(|endpoint| profile.routes.contains(&endpoint.route_kind))
            {
                return Err(ViewModelError::DeviceProfileMismatch {
                    device_id: device.device_id.clone(),
                    profile_id: profile_id.clone(),
                });
            }
            let expected_capabilities = profile
                .capabilities
                .iter()
                .map(|capability| capability.id.clone())
                .collect::<Vec<_>>();
            if device.capabilities != expected_capabilities {
                return Err(ViewModelError::DeviceCapabilitiesMismatch(
                    device.device_id.clone(),
                ));
            }
            if device.support_level != profile.support_level() {
                return Err(ViewModelError::DeviceSupportMismatch(
                    device.device_id.clone(),
                ));
            }
            Ok(Some(profile))
        }
    }
}

fn project_inventory(
    receiver: &ReceiverSnapshot,
    device: &LogicalDeviceSnapshot,
    profile: Option<&RuntimeProfile>,
) -> Result<DeviceInventoryView, ViewModelError> {
    Ok(DeviceInventoryView {
        device_id: device.device_id.clone(),
        device_kind: device.device_kind,
        product_id: device.product_id,
        profile: profile.map(|profile| ProfileBindingView {
            profile_id: profile.profile_id.clone(),
            profile_digest: profile.runtime_digest.clone(),
        }),
        model_name: profile.map(model_name).transpose()?,
        pairing: device.pairing,
        presence: device.presence,
        availability: inventory_availability(receiver.lifecycle, device.pairing, device.presence),
        support_level: device.support_level,
        endpoints: device.endpoints.clone(),
        battery: device.battery.clone(),
        capabilities: device.capabilities.clone(),
    })
}

fn model_name(profile: &RuntimeProfile) -> Result<ModelName, ViewModelError> {
    ModelName::try_from(profile.model_name)
        .map_err(|_| ViewModelError::InvalidProfilePresentation(profile.profile_id.clone()))
}

const fn inventory_availability(
    lifecycle: ReceiverLifecycleState,
    pairing: PairingState,
    presence: PresenceState,
) -> InventoryAvailability {
    if !matches!(lifecycle, ReceiverLifecycleState::Active) {
        return InventoryAvailability::ReceiverUnavailable;
    }
    match pairing {
        PairingState::Unpaired => InventoryAvailability::Unpaired,
        PairingState::Unknown => InventoryAvailability::PairingUnknown,
        PairingState::Paired => match presence {
            PresenceState::Available => InventoryAvailability::Available,
            PresenceState::Sleeping => InventoryAvailability::Sleeping,
            PresenceState::Unavailable => InventoryAvailability::Unavailable,
            PresenceState::Unknown => InventoryAvailability::Unknown,
        },
    }
}

fn project_controller(
    receiver: &ReceiverSnapshot,
    device: &LogicalDeviceSnapshot,
    receiver_profile: Option<&RuntimeProfile>,
    device_profile: Option<&RuntimeProfile>,
    viewer: Option<&ClientId>,
) -> Result<Option<ControllerView>, ViewModelError> {
    let (Some(receiver_profile), Some(device_profile)) = (receiver_profile, device_profile) else {
        return Ok(None);
    };
    if receiver.lifecycle != ReceiverLifecycleState::Active
        || device.pairing != PairingState::Paired
        || !device_profile
            .routes
            .contains(&RouteKind::HyperfluxWireless)
        || !device_profile.capabilities.iter().any(|capability| {
            capability.writable && capability.id.as_str() == "lighting.direct-frame"
        })
    {
        return Ok(None);
    }
    let Some(lighting) = device_profile.lighting.as_ref() else {
        return Ok(None);
    };
    let Some(presentation) = device_profile.presentation.as_ref() else {
        return Ok(None);
    };
    let availability = match device.presence {
        PresenceState::Available => ControllerAvailability::Ready,
        PresenceState::Sleeping => ControllerAvailability::Sleeping,
        PresenceState::Unavailable | PresenceState::Unknown => return Ok(None),
    };
    let routes = device
        .endpoints
        .iter()
        .filter(|endpoint| usable_hyperflux_endpoint(endpoint, availability))
        .collect::<Vec<_>>();
    let [endpoint] = routes.as_slice() else {
        if routes.len() > 1 {
            return Err(ViewModelError::AmbiguousHyperfluxRoute(
                device.device_id.clone(),
            ));
        }
        return Ok(None);
    };
    let resource = ResourceKey {
        receiver_id: receiver.receiver_id.clone(),
        generation_id: receiver.generation_id,
        device_id: device.device_id.clone(),
        kind: ResourceKind::Lighting,
    };
    let ownership = controller_ownership(receiver, &resource, viewer);
    let actions = controller_actions(availability, &ownership, viewer.is_some());
    Ok(Some(ControllerView {
        receiver_id: receiver.receiver_id.clone(),
        generation_id: receiver.generation_id,
        device_id: device.device_id.clone(),
        endpoint_id: endpoint.endpoint_id.clone(),
        device_kind: device.device_kind,
        product_id: device.product_id,
        receiver_profile: ProfileBindingView {
            profile_id: receiver_profile.profile_id.clone(),
            profile_digest: receiver_profile.runtime_digest.clone(),
        },
        device_profile: ProfileBindingView {
            profile_id: device_profile.profile_id.clone(),
            profile_digest: device_profile.runtime_digest.clone(),
        },
        model_name: model_name(device_profile)?,
        presentation: presentation_view(device_profile, presentation)?,
        availability,
        battery: device.battery.clone(),
        capabilities: device.capabilities.clone(),
        lighting: lighting_topology_view(device_profile, lighting)?,
        resource,
        ownership,
        actions,
    }))
}

fn presentation_view(
    profile: &RuntimeProfile,
    presentation: &hfx_profiles::RuntimePresentation,
) -> Result<PresentationView, ViewModelError> {
    let invalid = || ViewModelError::InvalidProfilePresentation(profile.profile_id.clone());
    Ok(PresentationView {
        upstream_id: UpstreamId::try_from(presentation.upstream_id).map_err(|_| invalid())?,
        owner: UpstreamOwner::try_from(presentation.owner).map_err(|_| invalid())?,
        project_version: ComponentVersion::try_from(presentation.project_version)
            .map_err(|_| invalid())?,
        source_revision: SourceRevision::try_from(presentation.source_commit)
            .map_err(|_| invalid())?,
        model_key: PresentationKey::try_from(presentation.model_key).map_err(|_| invalid())?,
        layout_key: presentation
            .layout_key
            .map(PresentationKey::try_from)
            .transpose()
            .map_err(|_| invalid())?,
        transport_variant: TransportVariant::try_from(presentation.transport_variant)
            .map_err(|_| invalid())?,
    })
}

fn lighting_topology_view(
    profile: &RuntimeProfile,
    lighting: &hfx_profiles::RuntimeLightingTopology,
) -> Result<LightingTopologyView, ViewModelError> {
    let invalid = || ViewModelError::InvalidProfilePresentation(profile.profile_id.clone());
    Ok(LightingTopologyView {
        physical_led_count: lighting.physical_led_count,
        application_slot_count: lighting.application_slot_count,
        rows: LedCount::try_from(lighting.rows).map_err(|_| invalid())?,
        columns: LedCount::try_from(lighting.columns).map_err(|_| invalid())?,
    })
}

fn usable_hyperflux_endpoint(
    endpoint: &EndpointSnapshot,
    availability: ControllerAvailability,
) -> bool {
    endpoint.route_kind == RouteKind::HyperfluxWireless
        && endpoint.connection_mode == hfx_domain::ConnectionMode::Hyperflux24ghz
        && endpoint.route_state == RouteState::Available
        && endpoint.freshness == FreshnessState::Fresh
        && endpoint.power_state != PowerState::Off
        && match availability {
            ControllerAvailability::Ready => endpoint.sleep_state != SleepState::Asleep,
            ControllerAvailability::Sleeping => endpoint.sleep_state == SleepState::Asleep,
        }
}

fn controller_ownership(
    receiver: &ReceiverSnapshot,
    resource: &ResourceKey,
    viewer: Option<&ClientId>,
) -> ControllerOwnership {
    let Some(ownership) = receiver
        .ownership
        .iter()
        .find(|ownership| ownership.resource == *resource)
    else {
        return ControllerOwnership::Unowned(UnownedController {});
    };
    if viewer.is_some_and(|client_id| *client_id == ownership.client_id) {
        ControllerOwnership::OwnedByViewer(ViewerOwnedController {
            lease_id: ownership.lease_id.clone(),
            expires_at_ms: ownership.expires_at_ms,
        })
    } else {
        ControllerOwnership::OwnedByOther(OtherOwnedController {
            client_id: ownership.client_id.clone(),
            lease_id: ownership.lease_id.clone(),
            expires_at_ms: ownership.expires_at_ms,
        })
    }
}

const fn controller_actions(
    availability: ControllerAvailability,
    ownership: &ControllerOwnership,
    has_viewer: bool,
) -> ControllerActions {
    let owned_by_viewer = matches!(ownership, ControllerOwnership::OwnedByViewer(_));
    ControllerActions {
        can_acquire: has_viewer && matches!(ownership, ControllerOwnership::Unowned(_)),
        can_release: owned_by_viewer,
        can_submit_now: owned_by_viewer && matches!(availability, ControllerAvailability::Ready),
    }
}
