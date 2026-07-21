// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    CapabilityId, ClientId, DeviceKind, EndpointId, FreshnessState, GenerationId, LeaseId,
    LogicalDeviceId, MonotonicMs, PairingState, PowerState, PresenceState, ProductId,
    ProfileDigest, ProfileId, ProfileKind, ReceiverId, ReceiverLifecycleState, ResourceKind,
    RestoreState, RouteKind, RouteState, SleepState, SupportLevel,
};
use hfx_profiles::{RuntimeProfile, RuntimeProfileCatalog};
use hfx_protocol::{
    BatteryObservation, BridgeSnapshot, EndpointSnapshot, EventCursor, LogicalDeviceSnapshot,
    ReceiverSnapshot, ResourceKey, SnapshotValidationError, validate_bridge_snapshot,
};
use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IntegrationView {
    pub cursor: EventCursor,
    pub receivers: Vec<ReceiverView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiverView {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub profile: Option<ProfileBindingView>,
    pub model_name: Option<String>,
    pub lifecycle: ReceiverLifecycleState,
    pub stable_restore_enabled: bool,
    pub restore_state: RestoreState,
    pub inventory: Vec<DeviceInventoryView>,
    pub controllers: Vec<ControllerView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileBindingView {
    pub profile_id: ProfileId,
    pub profile_digest: ProfileDigest,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum InventoryAvailability {
    Available,
    Sleeping,
    Unavailable,
    Unknown,
    Unpaired,
    PairingUnknown,
    ReceiverUnavailable,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceInventoryView {
    pub device_id: LogicalDeviceId,
    pub device_kind: DeviceKind,
    pub product_id: ProductId,
    pub profile: Option<ProfileBindingView>,
    pub model_name: Option<String>,
    pub pairing: PairingState,
    pub presence: PresenceState,
    pub availability: InventoryAvailability,
    pub support_level: SupportLevel,
    pub endpoints: Vec<EndpointSnapshot>,
    pub battery: BatteryObservation,
    pub capabilities: Vec<CapabilityId>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ControllerAvailability {
    Ready,
    Sleeping,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PresentationView {
    pub upstream_id: String,
    pub owner: String,
    pub project_version: String,
    pub source_commit: String,
    pub model_key: String,
    pub layout_key: Option<String>,
    pub transport_variant: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LightingTopologyView {
    pub physical_led_count: hfx_domain::LedCount,
    pub application_slot_count: hfx_domain::LedCount,
    pub rows: u16,
    pub columns: u16,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "kebab-case")]
pub enum ControllerOwnership {
    Unowned,
    OwnedByViewer {
        lease_id: LeaseId,
        expires_at_ms: MonotonicMs,
    },
    OwnedByOther {
        client_id: ClientId,
        lease_id: LeaseId,
        expires_at_ms: MonotonicMs,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ControllerActions {
    pub can_acquire: bool,
    pub can_release: bool,
    pub can_submit_now: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ControllerView {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub device_id: LogicalDeviceId,
    pub endpoint_id: EndpointId,
    pub device_kind: DeviceKind,
    pub product_id: ProductId,
    pub receiver_profile: ProfileBindingView,
    pub device_profile: ProfileBindingView,
    pub model_name: String,
    pub presentation: PresentationView,
    pub availability: ControllerAvailability,
    pub battery: BatteryObservation,
    pub capabilities: Vec<CapabilityId>,
    pub lighting: LightingTopologyView,
    pub resource: ResourceKey,
    pub ownership: ControllerOwnership,
    pub actions: ControllerActions,
}

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
) -> Result<ReceiverView, ViewModelError> {
    let receiver_profile = resolve_receiver_profile(receiver, catalog)?;
    let receiver_binding = receiver_profile.map(|profile| ProfileBindingView {
        profile_id: profile.profile_id.clone(),
        profile_digest: profile.runtime_digest.clone(),
    });
    let mut inventory = Vec::with_capacity(receiver.devices.len());
    let mut controllers = Vec::with_capacity(receiver.devices.len());
    for device in &receiver.devices {
        let profile = resolve_device_profile(device, receiver_profile, catalog)?;
        inventory.push(project_inventory(receiver, device, profile));
        if let Some(controller) =
            project_controller(receiver, device, receiver_profile, profile, viewer)?
        {
            controllers.push(controller);
        }
    }
    Ok(ReceiverView {
        receiver_id: receiver.receiver_id.clone(),
        generation_id: receiver.generation_id,
        profile: receiver_binding,
        model_name: receiver_profile.map(|profile| profile.model_name.to_owned()),
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
) -> DeviceInventoryView {
    DeviceInventoryView {
        device_id: device.device_id.clone(),
        device_kind: device.device_kind,
        product_id: device.product_id,
        profile: profile.map(|profile| ProfileBindingView {
            profile_id: profile.profile_id.clone(),
            profile_digest: profile.runtime_digest.clone(),
        }),
        model_name: profile.map(|profile| profile.model_name.to_owned()),
        pairing: device.pairing,
        presence: device.presence,
        availability: inventory_availability(receiver.lifecycle, device.pairing, device.presence),
        support_level: device.support_level,
        endpoints: device.endpoints.clone(),
        battery: device.battery.clone(),
        capabilities: device.capabilities.clone(),
    }
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
        model_name: device_profile.model_name.to_owned(),
        presentation: PresentationView {
            upstream_id: presentation.upstream_id.to_owned(),
            owner: presentation.owner.to_owned(),
            project_version: presentation.project_version.to_owned(),
            source_commit: presentation.source_commit.to_owned(),
            model_key: presentation.model_key.to_owned(),
            layout_key: presentation.layout_key.map(str::to_owned),
            transport_variant: presentation.transport_variant.to_owned(),
        },
        availability,
        battery: device.battery.clone(),
        capabilities: device.capabilities.clone(),
        lighting: LightingTopologyView {
            physical_led_count: lighting.physical_led_count,
            application_slot_count: lighting.application_slot_count,
            rows: lighting.rows,
            columns: lighting.columns,
        },
        resource,
        ownership,
        actions,
    }))
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
        return ControllerOwnership::Unowned;
    };
    if viewer.is_some_and(|client_id| *client_id == ownership.client_id) {
        ControllerOwnership::OwnedByViewer {
            lease_id: ownership.lease_id.clone(),
            expires_at_ms: ownership.expires_at_ms,
        }
    } else {
        ControllerOwnership::OwnedByOther {
            client_id: ownership.client_id.clone(),
            lease_id: ownership.lease_id.clone(),
            expires_at_ms: ownership.expires_at_ms,
        }
    }
}

const fn controller_actions(
    availability: ControllerAvailability,
    ownership: &ControllerOwnership,
    has_viewer: bool,
) -> ControllerActions {
    let owned_by_viewer = matches!(ownership, ControllerOwnership::OwnedByViewer { .. });
    ControllerActions {
        can_acquire: has_viewer && matches!(ownership, ControllerOwnership::Unowned),
        can_release: owned_by_viewer,
        can_submit_now: owned_by_viewer && matches!(availability, ControllerAvailability::Ready),
    }
}
