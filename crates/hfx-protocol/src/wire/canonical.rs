// SPDX-License-Identifier: GPL-2.0-only

//! Canonical request and response invariant validation.

use super::ProtocolWireError;
use crate::{
    ControllerOwnership, ControllerView, DeviceInventoryView, DiagnosticSnapshot, EndpointSnapshot,
    EventBatch, IntegrationReceiverView, IntegrationView, LeaseResult, NativeExchangeOutcome,
    NativeExchangeRequest, NativeExchangeResult, NativeRoute, NativeRouteEventBatch,
    NativeRouteInventory, RpcRequest, RpcResponse, TransactionResult, TransactionTerminal,
    TransactionUnavailable, validate_bridge_snapshot, validate_lease_request, validate_transaction,
};
use hfx_domain::{
    ConnectionMode, ControllerAvailability, FreshnessState, InventoryAvailability,
    NativeRouteAccess, NativeRouteEventKind, NativeRouteState, PairingState, PowerState,
    PresenceState, ProtocolErrorKind, ReceiverLifecycleState, ResourceKind, RouteKind, RouteState,
    SideEffectCertainty, SleepState, TransactionState,
};
use std::collections::BTreeSet;

fn strictly_ordered<T: Ord>(values: &[T]) -> bool {
    values.windows(2).all(|pair| pair[0] < pair[1])
}

fn native_report_is_canonical(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn native_route_identity_precedes(left: &NativeRoute, right: &NativeRoute) -> bool {
    (
        &left.receiver_id,
        left.generation_id,
        &left.device_id,
        &left.endpoint_id,
    ) < (
        &right.receiver_id,
        right.generation_id,
        &right.device_id,
        &right.endpoint_id,
    )
}

fn validate_native_route(route: &NativeRoute) -> Result<(), ProtocolWireError> {
    let writable = route.access == NativeRouteAccess::Qualified
        && route.route_state == NativeRouteState::Available;
    if writable != route.route_token.is_some() || writable != route.resource.is_some() {
        return Err(ProtocolWireError::InvalidResponse(
            "native route authority contradicts access or availability",
        ));
    }
    if !writable {
        return Ok(());
    }

    let Some(resource) = &route.resource else {
        return Err(ProtocolWireError::InvalidResponse(
            "writable native route lacks its ownership resource",
        ));
    };
    if resource.receiver_id != route.receiver_id
        || resource.generation_id != route.generation_id
        || resource.device_id != route.device_id
        || resource.kind != ResourceKind::NativeControl
        || usize::from(route.report_bytes.get()).saturating_mul(2)
            != hfx_domain::NativeReportHex::MIN_LENGTH
    {
        return Err(ProtocolWireError::InvalidResponse(
            "writable native route contradicts identity, resource, or report shape",
        ));
    }
    Ok(())
}

fn validate_native_route_inventory(
    inventory: &NativeRouteInventory,
) -> Result<(), ProtocolWireError> {
    if inventory.routes.len() > 32
        || !inventory
            .routes
            .windows(2)
            .all(|pair| native_route_identity_precedes(&pair[0], &pair[1]))
    {
        return Err(ProtocolWireError::InvalidResponse(
            "native routes are oversized, duplicated, or unordered",
        ));
    }
    for route in &inventory.routes {
        validate_native_route(route)?;
    }
    Ok(())
}

fn validate_native_exchange_request(
    envelope_request_id: &hfx_domain::RequestId,
    request: &NativeExchangeRequest,
) -> Result<(), ProtocolWireError> {
    if envelope_request_id != &request.request_id {
        return Err(ProtocolWireError::RequestNotCanonical);
    }
    if !native_report_is_canonical(request.report.as_str()) {
        return Err(ProtocolWireError::RequestNotCanonical);
    }
    Ok(())
}

fn validate_native_exchange_outcome(
    outcome: &NativeExchangeOutcome,
) -> Result<(), ProtocolWireError> {
    if let NativeExchangeResult::MatchedResponse(response) = &outcome.result
        && !native_report_is_canonical(response.response.as_str())
    {
        return Err(ProtocolWireError::InvalidResponse(
            "native response report is not canonical lowercase hexadecimal",
        ));
    }
    Ok(())
}

fn validate_native_route_event_batch(
    batch: &NativeRouteEventBatch,
) -> Result<(), ProtocolWireError> {
    if batch.events.len() > 256
        || batch.oldest_available > batch.latest_available
        || !batch
            .events
            .windows(2)
            .all(|pair| pair[0].sequence < pair[1].sequence)
        || batch.events.iter().any(|event| {
            event.sequence < batch.oldest_available
                || event.sequence > batch.latest_available
                || !native_event_matches_route(event.kind, event.route.route_state)
        })
        || batch
            .events
            .last()
            .is_some_and(|event| event.sequence != batch.next_cursor.sequence)
        || (batch.cursor_gap && (!batch.events.is_empty() || batch.has_more))
        || (batch.has_more && batch.next_cursor.sequence >= batch.latest_available)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "native route event cursor, sequence, or transition facts contradict",
        ));
    }
    for event in &batch.events {
        validate_native_route(&event.route)?;
    }
    Ok(())
}

fn native_event_matches_route(kind: NativeRouteEventKind, state: NativeRouteState) -> bool {
    match kind {
        NativeRouteEventKind::Available => state == NativeRouteState::Available,
        NativeRouteEventKind::TemporarilyUnavailable => {
            state == NativeRouteState::TemporarilyUnavailable
        }
        NativeRouteEventKind::Withdrawn => state == NativeRouteState::Withdrawn,
        NativeRouteEventKind::GenerationReplaced => true,
    }
}

/// Validates method-specific request bounds after bounded decoding.
///
/// # Errors
///
/// Returns an error for oversized feature offers, duplicate features, invalid
/// lease sets, or invalid transactions.
pub fn validate_rpc_request(request: &RpcRequest) -> Result<(), ProtocolWireError> {
    match request {
        RpcRequest::Negotiate(envelope) => {
            let hello = &envelope.params;
            if hello.required_features.len() > 64 || hello.optional_features.len() > 64 {
                return Err(ProtocolWireError::RequestBoundExceeded);
            }
            let required = hello.required_features.iter().collect::<BTreeSet<_>>();
            let optional = hello.optional_features.iter().collect::<BTreeSet<_>>();
            if required.len() != hello.required_features.len()
                || optional.len() != hello.optional_features.len()
                || !required.is_disjoint(&optional)
            {
                return Err(ProtocolWireError::RequestNotCanonical);
            }
            Ok(())
        }
        RpcRequest::AcquireLease(envelope) => {
            validate_lease_request(&envelope.params).map_err(ProtocolWireError::InvalidRequest)
        }
        RpcRequest::SubmitTransaction(envelope) => {
            validate_transaction(&envelope.params).map_err(ProtocolWireError::InvalidRequest)
        }
        RpcRequest::NativeTransact(envelope) => {
            validate_native_exchange_request(&envelope.request_id, &envelope.params)
        }
        RpcRequest::Snapshot(_)
        | RpcRequest::IntegrationView(_)
        | RpcRequest::RenewLease(_)
        | RpcRequest::ReleaseLease(_)
        | RpcRequest::TransactionOutcome(_)
        | RpcRequest::Subscribe(_)
        | RpcRequest::Diagnostics(_)
        | RpcRequest::NativeEnumerate(_)
        | RpcRequest::NativeWatch(_) => Ok(()),
    }
}

fn validate_lease_result(result: &LeaseResult) -> Result<(), ProtocolWireError> {
    if let LeaseResult::Granted(grant) = result {
        if grant.resources.is_empty() || grant.resources.len() > 32 {
            return Err(ProtocolWireError::InvalidResponse(
                "lease grant has an invalid resource count",
            ));
        }
        if !strictly_ordered(&grant.resources) {
            return Err(ProtocolWireError::InvalidResponse(
                "lease grant resources are duplicated or unordered",
            ));
        }
    }
    Ok(())
}

fn validate_transaction_terminal(terminal: &TransactionTerminal) -> Result<(), ProtocolWireError> {
    if terminal.delivered_frames.get() > terminal.declared_frames.get() {
        return Err(ProtocolWireError::InvalidResponse(
            "transaction delivered more frames than declared",
        ));
    }
    if !terminal.live_write_executed
        && (terminal.delivered_frames.get() != 0
            || terminal.side_effect_certainty != SideEffectCertainty::None)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "transaction side-effect facts contradict live-write state",
        ));
    }
    if terminal.automatic_retry
        && (terminal.live_write_executed
            || terminal.side_effect_certainty != SideEffectCertainty::None)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "uncertain hardware transaction permits automatic retry",
        ));
    }
    match terminal.state {
        TransactionState::Succeeded => {
            if terminal.error_kind.is_some() || terminal.superseded_by.is_some() {
                return Err(ProtocolWireError::InvalidResponse(
                    "successful transaction carries failure or supersession detail",
                ));
            }
        }
        TransactionState::Failed | TransactionState::Revoked => {
            if terminal.error_kind.is_none() || terminal.superseded_by.is_some() {
                return Err(ProtocolWireError::InvalidResponse(
                    "failed transaction lacks exactly one failure category",
                ));
            }
        }
        TransactionState::Superseded => {
            if terminal.superseded_by.is_none()
                || terminal.error_kind.is_some()
                || terminal.live_write_executed
            {
                return Err(ProtocolWireError::InvalidResponse(
                    "superseded transaction has contradictory terminal detail",
                ));
            }
        }
        TransactionState::Created
        | TransactionState::Validated
        | TransactionState::OwnershipBound
        | TransactionState::GenerationBound
        | TransactionState::Queued
        | TransactionState::Sent
        | TransactionState::HealthPending => {
            return Err(ProtocolWireError::InvalidResponse(
                "terminal transaction carries a nonterminal state",
            ));
        }
    }
    Ok(())
}

fn validate_transaction_result(result: &TransactionResult) -> Result<(), ProtocolWireError> {
    match result {
        TransactionResult::Progress(progress) => {
            if progress.delivered_frames.get() > progress.declared_frames.get()
                || matches!(
                    progress.state,
                    TransactionState::Succeeded
                        | TransactionState::Failed
                        | TransactionState::Revoked
                        | TransactionState::Superseded
                )
            {
                return Err(ProtocolWireError::InvalidResponse(
                    "transaction progress carries impossible frame or state facts",
                ));
            }
            if !progress.live_write_executed
                && (progress.delivered_frames.get() != 0
                    || progress.side_effect_certainty != SideEffectCertainty::None)
            {
                return Err(ProtocolWireError::InvalidResponse(
                    "transaction progress contradicts live-write state",
                ));
            }
            Ok(())
        }
        TransactionResult::Terminal(terminal) => validate_transaction_terminal(terminal),
        TransactionResult::Unavailable(TransactionUnavailable { error_kind, .. }) => {
            if matches!(
                error_kind,
                ProtocolErrorKind::OutcomeUnknown | ProtocolErrorKind::OutcomeEvicted
            ) {
                Ok(())
            } else {
                Err(ProtocolWireError::InvalidResponse(
                    "unavailable transaction has an unrelated error category",
                ))
            }
        }
    }
}

fn validate_event_batch(batch: &EventBatch) -> Result<(), ProtocolWireError> {
    if batch.events.len() > 256 {
        return Err(ProtocolWireError::InvalidResponse(
            "event batch exceeds the event bound",
        ));
    }
    if batch.oldest_available > batch.latest_available
        || !batch
            .events
            .windows(2)
            .all(|pair| pair[0].sequence < pair[1].sequence)
        || batch.events.iter().any(|event| {
            event.sequence < batch.oldest_available || event.sequence > batch.latest_available
        })
        || batch
            .events
            .last()
            .is_some_and(|event| event.sequence != batch.next_cursor.sequence)
        || (batch.cursor_gap && (!batch.events.is_empty() || batch.has_more))
        || (batch.has_more && batch.next_cursor.sequence >= batch.latest_available)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "event batch cursor and sequence facts contradict",
        ));
    }
    Ok(())
}

fn validate_diagnostics(snapshot: &DiagnosticSnapshot) -> Result<(), ProtocolWireError> {
    if snapshot.findings.len() > 128 {
        return Err(ProtocolWireError::InvalidResponse(
            "diagnostic snapshot exceeds the finding bound",
        ));
    }
    if !snapshot
        .findings
        .windows(2)
        .all(|pair| pair[0].finding_id < pair[1].finding_id)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "diagnostic findings are duplicated or unordered",
        ));
    }
    Ok(())
}

fn validate_integration_view(view: &IntegrationView) -> Result<(), ProtocolWireError> {
    if view.receivers.len() > 16
        || !view
            .receivers
            .windows(2)
            .all(|pair| pair[0].receiver_id < pair[1].receiver_id)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "integration receivers are oversized, duplicated, or unordered",
        ));
    }
    for receiver in &view.receivers {
        validate_integration_receiver(receiver)?;
    }
    Ok(())
}

fn validate_integration_receiver(
    receiver: &IntegrationReceiverView,
) -> Result<(), ProtocolWireError> {
    if receiver.profile.is_some() != receiver.model_name.is_some()
        || receiver.inventory.len() > 32
        || receiver.controllers.len() > 32
        || !receiver
            .inventory
            .windows(2)
            .all(|pair| pair[0].device_id < pair[1].device_id)
        || !receiver
            .controllers
            .windows(2)
            .all(|pair| pair[0].device_id < pair[1].device_id)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "integration receiver contents are contradictory or noncanonical",
        ));
    }
    for inventory in &receiver.inventory {
        validate_integration_inventory(receiver.lifecycle, inventory)?;
    }
    for controller in &receiver.controllers {
        validate_integration_controller(receiver, controller)?;
    }
    Ok(())
}

fn validate_integration_inventory(
    lifecycle: ReceiverLifecycleState,
    inventory: &DeviceInventoryView,
) -> Result<(), ProtocolWireError> {
    if inventory.profile.is_some() != inventory.model_name.is_some()
        || inventory.endpoints.len() > 8
        || inventory.capabilities.len() > 128
        || !strictly_ordered(&inventory.capabilities)
        || !inventory
            .endpoints
            .windows(2)
            .all(|pair| pair[0].endpoint_id < pair[1].endpoint_id)
        || inventory.availability
            != expected_inventory_availability(lifecycle, inventory.pairing, inventory.presence)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "integration inventory is contradictory or noncanonical",
        ));
    }
    Ok(())
}

fn validate_integration_controller(
    receiver: &IntegrationReceiverView,
    controller: &ControllerView,
) -> Result<(), ProtocolWireError> {
    let Some(inventory) = receiver
        .inventory
        .iter()
        .find(|inventory| inventory.device_id == controller.device_id)
    else {
        return Err(ProtocolWireError::InvalidResponse(
            "integration controller is absent from receiver inventory",
        ));
    };
    let expected_availability = match controller.availability {
        ControllerAvailability::Ready => InventoryAvailability::Available,
        ControllerAvailability::Sleeping => InventoryAvailability::Sleeping,
    };
    let endpoint = inventory
        .endpoints
        .iter()
        .find(|endpoint| endpoint.endpoint_id == controller.endpoint_id);
    let lighting_slots = u32::from(controller.lighting.rows.get())
        .saturating_mul(u32::from(controller.lighting.columns.get()));
    if controller.receiver_id != receiver.receiver_id
        || controller.generation_id != receiver.generation_id
        || receiver.lifecycle != ReceiverLifecycleState::Active
        || receiver.profile.as_ref() != Some(&controller.receiver_profile)
        || inventory.profile.as_ref() != Some(&controller.device_profile)
        || inventory.model_name.as_ref() != Some(&controller.model_name)
        || inventory.pairing != PairingState::Paired
        || inventory.availability != expected_availability
        || inventory.device_kind != controller.device_kind
        || inventory.product_id != controller.product_id
        || inventory.battery != controller.battery
        || inventory.capabilities != controller.capabilities
        || controller.capabilities.len() > 128
        || controller.resource.receiver_id != receiver.receiver_id
        || controller.resource.generation_id != receiver.generation_id
        || controller.resource.device_id != controller.device_id
        || controller.resource.kind != ResourceKind::Lighting
        || controller.lighting.physical_led_count > controller.lighting.application_slot_count
        || u32::from(controller.lighting.application_slot_count.get()) > lighting_slots
        || !endpoint_is_controller_route(endpoint, controller.availability)
        || !controller_actions_are_consistent(controller)
    {
        return Err(ProtocolWireError::InvalidResponse(
            "integration controller contradicts inventory, route, or ownership authority",
        ));
    }
    Ok(())
}

fn endpoint_is_controller_route(
    endpoint: Option<&EndpointSnapshot>,
    availability: ControllerAvailability,
) -> bool {
    endpoint.is_some_and(|endpoint| {
        endpoint.route_kind == RouteKind::HyperfluxWireless
            && endpoint.route_state == RouteState::Available
            && endpoint.connection_mode == ConnectionMode::Hyperflux24ghz
            && endpoint.freshness == FreshnessState::Fresh
            && endpoint.power_state != PowerState::Off
            && match availability {
                ControllerAvailability::Ready => endpoint.sleep_state != SleepState::Asleep,
                ControllerAvailability::Sleeping => endpoint.sleep_state == SleepState::Asleep,
            }
    })
}

const fn expected_inventory_availability(
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

fn controller_actions_are_consistent(controller: &crate::ControllerView) -> bool {
    match &controller.ownership {
        ControllerOwnership::Unowned(_) => {
            controller.actions.can_acquire
                && !controller.actions.can_release
                && !controller.actions.can_submit_now
        }
        ControllerOwnership::OwnedByViewer(_) => {
            !controller.actions.can_acquire
                && controller.actions.can_release
                && controller.actions.can_submit_now
                    == (controller.availability == ControllerAvailability::Ready)
        }
        ControllerOwnership::OwnedByOther(_) => {
            !controller.actions.can_acquire
                && !controller.actions.can_release
                && !controller.actions.can_submit_now
        }
    }
}

/// Validates generated response invariants after bounded decoding.
///
/// # Errors
///
/// Returns an error for contradictory outcomes, oversized projections, or
/// noncanonical generated collections.
pub fn validate_rpc_response(response: &RpcResponse) -> Result<(), ProtocolWireError> {
    match response {
        RpcResponse::NegotiateSuccess(envelope) => {
            let features = &envelope.result.enabled_features;
            if features.len() > 64 || !strictly_ordered(features) {
                return Err(ProtocolWireError::InvalidResponse(
                    "negotiated features are oversized, duplicated, or unordered",
                ));
            }
            Ok(())
        }
        RpcResponse::SnapshotSuccess(envelope) => {
            validate_bridge_snapshot(&envelope.result).map_err(ProtocolWireError::InvalidSnapshot)
        }
        RpcResponse::IntegrationViewSuccess(envelope) => {
            validate_integration_view(&envelope.result)
        }
        RpcResponse::AcquireLeaseSuccess(envelope)
        | RpcResponse::RenewLeaseSuccess(envelope)
        | RpcResponse::ReleaseLeaseSuccess(envelope) => validate_lease_result(&envelope.result),
        RpcResponse::SubmitTransactionSuccess(envelope)
        | RpcResponse::TransactionOutcomeSuccess(envelope) => {
            validate_transaction_result(&envelope.result)
        }
        RpcResponse::SubscribeSuccess(envelope) => validate_event_batch(&envelope.result),
        RpcResponse::DiagnosticsSuccess(envelope) => validate_diagnostics(&envelope.result),
        RpcResponse::NativeEnumerateSuccess(envelope) => {
            validate_native_route_inventory(&envelope.result)
        }
        RpcResponse::NativeTransactSuccess(envelope) => {
            validate_native_exchange_outcome(&envelope.result)
        }
        RpcResponse::NativeWatchSuccess(envelope) => {
            validate_native_route_event_batch(&envelope.result)
        }
        RpcResponse::Error(_) => Ok(()),
    }
}
