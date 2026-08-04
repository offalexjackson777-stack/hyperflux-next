// SPDX-License-Identifier: GPL-2.0-only

//! Session, resource-authority, and lease-expiry state transitions.

use super::CoreBridgeBackend;
use super::failures::{
    event_failure, generation_failure, ownership_failure, profile_failure, request_failure,
    transport_unavailable_failure,
};
use crate::{BackendRequestContext, RestorationRuntime, RpcFailure};
use hfx_core::{
    Clock, EventDraft, EventSink, ProfileRegistry, ReceiverTransport, SessionAuthority, WallClock,
};
use hfx_domain::{ClientId, EventKind, LeaseId, NativeRouteAccess, NativeRouteState, ResourceKind};
use hfx_protocol::{LeaseRequest, validate_lease_request};

impl<C, W, T, R, S> CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
    S: EventSink,
{
    pub(super) fn authorize(context: BackendRequestContext<'_>) -> Result<(), RpcFailure> {
        let session = context.session();
        if context
            .sessions()
            .authorizes(&session.session_id, session.authorization_epoch)
        {
            Ok(())
        } else {
            Err(ownership_failure())
        }
    }

    pub(super) fn authorize_client(
        context: BackendRequestContext<'_>,
        client_id: &ClientId,
    ) -> Result<(), RpcFailure> {
        Self::authorize(context)?;
        if &context.session().client_id == client_id {
            Ok(())
        } else {
            Err(request_failure())
        }
    }

    pub(super) fn qualify_resources(&mut self, request: &LeaseRequest) -> Result<(), RpcFailure> {
        validate_lease_request(request).map_err(|_| request_failure())?;
        if request
            .resources
            .iter()
            .any(|resource| resource.kind == ResourceKind::NativeControl)
        {
            self.synchronize_native_routes()?;
        }
        let profiles = self.profiles.view(&self.receivers);
        for resource in &request.resources {
            if self.transport.current_generation(&resource.receiver_id)
                != Some(resource.generation_id)
            {
                return Err(generation_failure());
            }
            match resource.kind {
                ResourceKind::Lighting => {
                    if !self
                        .transport
                        .write_available(&resource.receiver_id, resource.generation_id)
                    {
                        return Err(transport_unavailable_failure());
                    }
                    if !profiles.supports(resource) {
                        return Err(profile_failure());
                    }
                }
                ResourceKind::NativeControl => {
                    let route = self
                        .native_routes
                        .route_for_resource(resource)
                        .ok_or_else(profile_failure)?;
                    if route.access != NativeRouteAccess::Qualified
                        || route.route_state != NativeRouteState::Available
                    {
                        return Err(profile_failure());
                    }
                    if !self
                        .transport
                        .native_exchange_available(&resource.receiver_id, resource.generation_id)
                    {
                        return Err(transport_unavailable_failure());
                    }
                }
                ResourceKind::Settings | ResourceKind::Pairing => {
                    return Err(profile_failure());
                }
            }
        }
        Ok(())
    }

    pub(super) fn record_ownership_change(&mut self, lease_id: LeaseId) -> Result<(), RpcFailure> {
        let event = self
            .events
            .append(EventDraft {
                kind: EventKind::OwnershipChanged,
                receiver_id: None,
                generation_id: None,
                device_id: None,
                lease_id: Some(lease_id),
                transaction_id: None,
                finding_id: None,
            })
            .map_err(|error| event_failure(&error))?;
        let _ = self.event_sink.try_emit(&event);
        Ok(())
    }

    pub(super) fn expire_leases(&mut self) -> Result<(), RpcFailure> {
        let mut next_leases = self.leases.clone();
        let expired = next_leases.expire(self.clock.now());
        if expired.is_empty() {
            return Ok(());
        }

        let mut next_events = self.events.clone();
        let mut emitted = Vec::with_capacity(expired.len());
        for grant in &expired {
            emitted.push(
                next_events
                    .append(EventDraft {
                        kind: EventKind::OwnershipChanged,
                        receiver_id: None,
                        generation_id: None,
                        device_id: None,
                        lease_id: Some(grant.lease_id.clone()),
                        transaction_id: None,
                        finding_id: None,
                    })
                    .map_err(|error| event_failure(&error))?,
            );
        }

        self.leases = next_leases;
        self.events = next_events;
        for grant in expired {
            self.desired_effects.remove_lease(&grant.lease_id);
        }
        for event in emitted {
            let _ = self.event_sink.try_emit(&event);
        }
        Ok(())
    }
}
