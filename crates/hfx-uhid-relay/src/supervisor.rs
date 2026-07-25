// SPDX-License-Identifier: GPL-2.0-only

use crate::catalog::{NativeRouteCatalog, NativeRouteRecord};
use crate::uhid::VirtualDeviceSpec;
use hfx_domain::{
    EndpointId, GenerationId, LogicalDeviceId, NativeRouteAccess, NativeRouteEventKind, ReceiverId,
    ResourceKind, RouteState, SequenceNumber,
};
use hfx_protocol::{NativeRoute, NativeRouteEvent, NativeRouteInventory};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

#[allow(clippy::struct_field_names)]
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct RouteIdentity {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub device_id: LogicalDeviceId,
    pub endpoint_id: EndpointId,
}

impl From<&NativeRoute> for RouteIdentity {
    fn from(route: &NativeRoute) -> Self {
        Self {
            receiver_id: route.receiver_id.clone(),
            generation_id: route.generation_id,
            device_id: route.device_id.clone(),
            endpoint_id: route.endpoint_id.clone(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RouteAction {
    Start {
        identity: RouteIdentity,
        route: Box<NativeRoute>,
        record: Box<NativeRouteRecord>,
        device: Box<VirtualDeviceSpec>,
    },
    Stop {
        identity: RouteIdentity,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RouteSupervisorError {
    DuplicateRoute,
    InvalidQualifiedRoute,
    InvalidDeviceSpec,
    StaleEvent,
}

impl fmt::Display for RouteSupervisorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::DuplicateRoute => "native route inventory contains a duplicate identity",
            Self::InvalidQualifiedRoute => "qualified native route has invalid write authority",
            Self::InvalidDeviceSpec => "native route cannot produce a trusted UHID identity",
            Self::StaleEvent => "native route event is stale or non-contiguous",
        })
    }
}

impl std::error::Error for RouteSupervisorError {}

#[derive(Clone, Debug, Default)]
pub struct RouteSupervisor {
    active: BTreeMap<RouteIdentity, NativeRoute>,
    last_sequence: Option<SequenceNumber>,
}

impl RouteSupervisor {
    #[must_use]
    pub fn active_identities(&self) -> Vec<RouteIdentity> {
        self.active.keys().cloned().collect()
    }

    /// Reconciles one complete route inventory into deterministic stop/start
    /// actions. Candidate and unknown routes remain visible to the bridge but
    /// never become UHID endpoints.
    ///
    /// # Errors
    ///
    /// Rejects duplicate identities and malformed qualified write authority.
    pub fn reconcile(
        &mut self,
        inventory: &NativeRouteInventory,
        catalog: &NativeRouteCatalog,
    ) -> Result<Vec<RouteAction>, RouteSupervisorError> {
        let mut seen = BTreeSet::new();
        let mut desired = BTreeMap::new();
        for route in &inventory.routes {
            let identity = RouteIdentity::from(route);
            if !seen.insert(identity.clone()) {
                return Err(RouteSupervisorError::DuplicateRoute);
            }
            if let Some((record, device)) = eligible_route(route, catalog)? {
                desired.insert(identity, (route.clone(), record.clone(), device));
            }
        }

        let mut actions = Vec::new();
        for (identity, current) in &self.active {
            if desired
                .get(identity)
                .is_none_or(|(next, _, _)| next != current)
            {
                actions.push(RouteAction::Stop {
                    identity: identity.clone(),
                });
            }
        }
        for (identity, (route, record, device)) in &desired {
            if self.active.get(identity) != Some(route) {
                actions.push(RouteAction::Start {
                    identity: identity.clone(),
                    route: Box::new(route.clone()),
                    record: Box::new(record.clone()),
                    device: Box::new(device.clone()),
                });
            }
        }
        self.active = desired
            .into_iter()
            .map(|(identity, (route, _, _))| (identity, route))
            .collect();
        self.last_sequence = Some(inventory.cursor.sequence);
        Ok(actions)
    }

    /// Applies one contiguous route event. Generation replacement removes the
    /// older endpoint before considering the new route.
    ///
    /// # Errors
    ///
    /// Rejects stale or skipped event sequences and malformed qualified routes.
    pub fn apply_event(
        &mut self,
        event: &NativeRouteEvent,
        catalog: &NativeRouteCatalog,
    ) -> Result<Vec<RouteAction>, RouteSupervisorError> {
        if let Some(last) = self.last_sequence {
            let expected = last
                .get()
                .checked_add(1)
                .ok_or(RouteSupervisorError::StaleEvent)?;
            if event.sequence.get() != expected {
                return Err(RouteSupervisorError::StaleEvent);
            }
        }
        let identity = RouteIdentity::from(&event.route);
        let mut actions = Vec::new();
        match event.kind {
            NativeRouteEventKind::Unavailable => {
                stop_route_family(&mut self.active, &event.route, &mut actions);
            }
            NativeRouteEventKind::GenerationReplaced => {
                stop_route_family(&mut self.active, &event.route, &mut actions);
                start_if_eligible(
                    &mut self.active,
                    &event.route,
                    identity,
                    catalog,
                    &mut actions,
                )?;
            }
            NativeRouteEventKind::Available => {
                if let Some(previous) = self.active.get(&identity)
                    && previous != &event.route
                {
                    self.active.remove(&identity);
                    actions.push(RouteAction::Stop {
                        identity: identity.clone(),
                    });
                }
                start_if_eligible(
                    &mut self.active,
                    &event.route,
                    identity,
                    catalog,
                    &mut actions,
                )?;
            }
        }
        self.last_sequence = Some(event.sequence);
        Ok(actions)
    }
}

fn eligible_route<'a>(
    route: &NativeRoute,
    catalog: &'a NativeRouteCatalog,
) -> Result<Option<(&'a NativeRouteRecord, VirtualDeviceSpec)>, RouteSupervisorError> {
    let Some(record) = catalog.route(route.vendor_id, route.product_id, route.device_kind) else {
        return Ok(None);
    };
    if route.access != NativeRouteAccess::Qualified || route.route_state != RouteState::Available {
        return Ok(None);
    }
    let Some(token) = &route.route_token else {
        return Err(RouteSupervisorError::InvalidQualifiedRoute);
    };
    let Some(resource) = &route.resource else {
        return Err(RouteSupervisorError::InvalidQualifiedRoute);
    };
    if token.as_str().len() != 64
        || resource.receiver_id != route.receiver_id
        || resource.generation_id != route.generation_id
        || resource.device_id != route.device_id
        || resource.kind != ResourceKind::NativeControl
        || route.report_bytes.get() != 90
        || &route.protocol_family != catalog.protocol_family()
    {
        return Err(RouteSupervisorError::InvalidQualifiedRoute);
    }
    let device = VirtualDeviceSpec::new(
        record.model_name.clone(),
        route.vendor_id.get(),
        route.product_id.get(),
        catalog.descriptor().to_vec(),
        &instance_label(route),
    )
    .map_err(|_| RouteSupervisorError::InvalidDeviceSpec)?;
    Ok(Some((record, device)))
}

fn start_if_eligible(
    active: &mut BTreeMap<RouteIdentity, NativeRoute>,
    route: &NativeRoute,
    identity: RouteIdentity,
    catalog: &NativeRouteCatalog,
    actions: &mut Vec<RouteAction>,
) -> Result<(), RouteSupervisorError> {
    if let Some((record, device)) = eligible_route(route, catalog)? {
        active.insert(identity.clone(), route.clone());
        actions.push(RouteAction::Start {
            identity,
            route: Box::new(route.clone()),
            record: Box::new(record.clone()),
            device: Box::new(device),
        });
    }
    Ok(())
}

fn stop_route_family(
    active: &mut BTreeMap<RouteIdentity, NativeRoute>,
    route: &NativeRoute,
    actions: &mut Vec<RouteAction>,
) {
    let identities = active
        .keys()
        .filter(|identity| {
            identity.receiver_id == route.receiver_id
                && identity.device_id == route.device_id
                && identity.endpoint_id == route.endpoint_id
        })
        .cloned()
        .collect::<Vec<_>>();
    for identity in identities {
        active.remove(&identity);
        actions.push(RouteAction::Stop { identity });
    }
}

fn instance_label(route: &NativeRoute) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"hyperflux-openrazer-uhid-v1\0");
    hasher.update(route.receiver_id.as_str().as_bytes());
    hasher.update(route.generation_id.get().to_le_bytes());
    hasher.update(route.device_id.as_str().as_bytes());
    hasher.update(route.endpoint_id.as_str().as_bytes());
    let digest = hasher.finalize();
    let mut label = String::from("native-");
    for byte in &digest[..8] {
        use std::fmt::Write as _;
        let _ = write!(label, "{byte:02x}");
    }
    label
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        DeviceKind, EventBatchLimit, NativeProtocolFamily, NativeReportByteCount, NativeRouteToken,
        ProductId, ProjectionRevision, StreamEpoch, StreamId, VendorId,
    };
    use hfx_protocol::{EventCursor, ResourceKey};

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test text is valid")
    }

    fn cursor(sequence: u64) -> EventCursor {
        EventCursor {
            stream_id: text::<StreamId>("native-routes-test"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
            projection_revision: ProjectionRevision::try_from(1_u32).expect("revision is valid"),
            sequence: SequenceNumber::try_from(sequence).expect("sequence is valid"),
        }
    }

    fn route(
        receiver: &str,
        device: &str,
        generation: u64,
        product: u16,
        kind: DeviceKind,
        access: NativeRouteAccess,
        state: RouteState,
    ) -> NativeRoute {
        let receiver_id = text::<ReceiverId>(receiver);
        let generation_id = GenerationId::try_from(generation).expect("generation is valid");
        let device_id = text::<LogicalDeviceId>(device);
        let writable = access == NativeRouteAccess::Qualified && state == RouteState::Available;
        NativeRoute {
            receiver_id: receiver_id.clone(),
            generation_id,
            device_id: device_id.clone(),
            endpoint_id: text::<EndpointId>("native-control"),
            device_kind: kind,
            vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is valid"),
            product_id: ProductId::try_from(product).expect("product is valid"),
            route_state: state,
            access,
            protocol_family: text::<NativeProtocolFamily>("razer-feature-report-v1"),
            report_bytes: NativeReportByteCount::try_from(90_u16).expect("size is valid"),
            route_token: writable
                .then(|| NativeRouteToken::try_from("a".repeat(64)).expect("token is valid")),
            resource: writable.then_some(ResourceKey {
                receiver_id,
                generation_id,
                device_id,
                kind: ResourceKind::NativeControl,
            }),
        }
    }

    fn inventory(sequence: u64, routes: Vec<NativeRoute>) -> NativeRouteInventory {
        NativeRouteInventory {
            cursor: cursor(sequence),
            routes,
        }
    }

    #[test]
    fn candidates_and_unknown_profiles_never_create_endpoints() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mut supervisor = RouteSupervisor::default();
        let actions = supervisor
            .reconcile(
                &inventory(
                    0,
                    vec![
                        route(
                            "receiver-1",
                            "mouse-1",
                            1,
                            0x00cd,
                            DeviceKind::Mouse,
                            NativeRouteAccess::Candidate,
                            RouteState::Available,
                        ),
                        route(
                            "receiver-1",
                            "unknown-1",
                            1,
                            0xffff,
                            DeviceKind::Mouse,
                            NativeRouteAccess::Qualified,
                            RouteState::Available,
                        ),
                    ],
                ),
                &catalog,
            )
            .expect("inventory reconciles");
        assert!(actions.is_empty());
        assert!(supervisor.active_identities().is_empty());
    }

    #[test]
    fn identical_models_on_distinct_routes_are_not_deduplicated() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mut supervisor = RouteSupervisor::default();
        let actions = supervisor
            .reconcile(
                &inventory(
                    1,
                    vec![
                        route(
                            "receiver-1",
                            "mouse-1",
                            1,
                            0x00cd,
                            DeviceKind::Mouse,
                            NativeRouteAccess::Qualified,
                            RouteState::Available,
                        ),
                        route(
                            "receiver-2",
                            "mouse-2",
                            7,
                            0x00cd,
                            DeviceKind::Mouse,
                            NativeRouteAccess::Qualified,
                            RouteState::Available,
                        ),
                    ],
                ),
                &catalog,
            )
            .expect("inventory reconciles");
        assert_eq!(actions.len(), 2);
        assert_eq!(supervisor.active_identities().len(), 2);
    }

    #[test]
    fn generation_replacement_stops_old_before_starting_new() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mut supervisor = RouteSupervisor::default();
        supervisor
            .reconcile(
                &inventory(
                    4,
                    vec![route(
                        "receiver-1",
                        "keyboard-1",
                        1,
                        0x0296,
                        DeviceKind::Keyboard,
                        NativeRouteAccess::Qualified,
                        RouteState::Available,
                    )],
                ),
                &catalog,
            )
            .expect("initial inventory reconciles");
        let replacement = route(
            "receiver-1",
            "keyboard-1",
            2,
            0x0296,
            DeviceKind::Keyboard,
            NativeRouteAccess::Qualified,
            RouteState::Available,
        );
        let actions = supervisor
            .apply_event(
                &NativeRouteEvent {
                    sequence: SequenceNumber::try_from(5_u64).expect("sequence is valid"),
                    kind: NativeRouteEventKind::GenerationReplaced,
                    route: replacement,
                },
                &catalog,
            )
            .expect("event applies");
        assert!(matches!(actions.first(), Some(RouteAction::Stop { .. })));
        assert!(matches!(actions.get(1), Some(RouteAction::Start { .. })));
        assert_eq!(supervisor.active_identities()[0].generation_id.get(), 2);
    }

    #[test]
    fn malformed_authority_and_noncontiguous_events_fail_closed() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let mut supervisor = RouteSupervisor::default();
        let mut malformed = route(
            "receiver-1",
            "mouse-1",
            1,
            0x00cd,
            DeviceKind::Mouse,
            NativeRouteAccess::Qualified,
            RouteState::Available,
        );
        malformed.route_token = None;
        assert_eq!(
            supervisor.reconcile(&inventory(2, vec![malformed]), &catalog),
            Err(RouteSupervisorError::InvalidQualifiedRoute)
        );

        supervisor
            .reconcile(&inventory(2, Vec::new()), &catalog)
            .expect("empty inventory reconciles");
        let event = NativeRouteEvent {
            sequence: SequenceNumber::try_from(4_u64).expect("sequence is valid"),
            kind: NativeRouteEventKind::Available,
            route: route(
                "receiver-1",
                "mouse-1",
                1,
                0x00cd,
                DeviceKind::Mouse,
                NativeRouteAccess::Qualified,
                RouteState::Available,
            ),
        };
        assert_eq!(
            supervisor.apply_event(&event, &catalog),
            Err(RouteSupervisorError::StaleEvent)
        );
    }

    #[test]
    fn feature_descriptor_has_no_input_or_output_main_items() {
        let catalog = NativeRouteCatalog::load().expect("catalog loads");
        let descriptor = catalog.descriptor();
        assert!(descriptor.windows(2).any(|item| item == [0xb1, 0x02]));
        assert!(!descriptor.iter().any(|item| matches!(*item, 0x81 | 0x91)));
    }

    #[test]
    fn event_batch_limit_type_remains_bounded_for_runtime_use() {
        assert!(EventBatchLimit::try_from(32_u16).is_ok());
        assert!(EventBatchLimit::try_from(0_u16).is_err());
    }
}
