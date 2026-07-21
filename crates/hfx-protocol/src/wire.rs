// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    CURRENT_PROTOCOL_VERSION, ControllerOwnership, ControllerView, DeviceInventoryView,
    DiagnosticSnapshot, EndpointSnapshot, EventBatch, IntegrationReceiverView, IntegrationView,
    LeaseResult, MAX_WIRE_MESSAGE_BYTES, ProtocolValidationError, RpcRequest, RpcResponse,
    SnapshotValidationError, StableLightingIntent, TransactionResult, TransactionTerminal,
    TransactionUnavailable, v1, v2, v3, v4, v5, validate_bridge_snapshot, validate_lease_request,
    validate_transaction,
};
use hfx_domain::{
    ConnectionMode, ControllerAvailability, FreshnessState, InventoryAvailability, PairingState,
    PowerState, PresenceState, ProtocolErrorKind, ProtocolVersion, ReceiverLifecycleState,
    ResourceKind, RouteKind, RouteState, SideEffectCertainty, SleepState, StableLightingMode,
    TransactionClass, TransactionState,
};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::Value;
use std::collections::BTreeSet;
use std::fmt;
use std::io::{self, Write};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProtocolWireError {
    MessageTooLarge,
    MalformedJson,
    RequestBoundExceeded,
    RequestNotCanonical,
    UnsupportedProtocolVersion,
    UnsupportedVersionMethod,
    VersionTranslation,
    InvalidRequest(ProtocolValidationError),
    InvalidSnapshot(SnapshotValidationError),
    InvalidResponse(&'static str),
}

impl fmt::Display for ProtocolWireError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::MessageTooLarge => "protocol message exceeds the encoded byte bound",
            Self::MalformedJson => "protocol message is malformed or exceeds the JSON depth bound",
            Self::RequestBoundExceeded => "protocol request exceeds a collection bound",
            Self::RequestNotCanonical => "protocol request contains duplicate or unordered values",
            Self::UnsupportedProtocolVersion => "protocol version is not registered",
            Self::UnsupportedVersionMethod => {
                "method is not safely served by the negotiated protocol version"
            }
            Self::VersionTranslation => "versioned protocol value cannot be normalized safely",
            Self::InvalidRequest(_) => "protocol request violates a method invariant",
            Self::InvalidSnapshot(_) => "protocol snapshot violates a projection invariant",
            Self::InvalidResponse(reason) => reason,
        })
    }
}

impl std::error::Error for ProtocolWireError {}

fn decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, ProtocolWireError> {
    if bytes.len() > MAX_WIRE_MESSAGE_BYTES {
        return Err(ProtocolWireError::MessageTooLarge);
    }
    serde_json::from_slice(bytes).map_err(|_| ProtocolWireError::MalformedJson)
}

struct BoundedEncoder {
    bytes: Vec<u8>,
    exceeded: bool,
}

impl BoundedEncoder {
    fn new() -> Self {
        Self {
            bytes: Vec::with_capacity(4096),
            exceeded: false,
        }
    }
}

impl Write for BoundedEncoder {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let Some(next_length) = self.bytes.len().checked_add(bytes.len()) else {
            self.exceeded = true;
            return Err(io::Error::other("bounded protocol encoding overflow"));
        };
        if next_length > MAX_WIRE_MESSAGE_BYTES {
            self.exceeded = true;
            return Err(io::Error::other("bounded protocol encoding exceeded"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolWireError> {
    let mut encoded = BoundedEncoder::new();
    if serde_json::to_writer(&mut encoded, value).is_err() {
        return Err(if encoded.exceeded {
            ProtocolWireError::MessageTooLarge
        } else {
            ProtocolWireError::VersionTranslation
        });
    }
    if encoded.bytes.is_empty() {
        return Err(ProtocolWireError::VersionTranslation);
    }
    Ok(encoded.bytes)
}

fn transcode_request<T: Serialize>(request: T) -> Result<RpcRequest, ProtocolWireError> {
    let value = serde_json::to_value(request).map_err(|_| ProtocolWireError::VersionTranslation)?;
    let request =
        serde_json::from_value(value).map_err(|_| ProtocolWireError::VersionTranslation)?;
    validate_rpc_request(&request)?;
    Ok(request)
}

fn transcode_response<T: Serialize>(response: T) -> Result<RpcResponse, ProtocolWireError> {
    let value =
        serde_json::to_value(response).map_err(|_| ProtocolWireError::VersionTranslation)?;
    let response =
        serde_json::from_value(value).map_err(|_| ProtocolWireError::VersionTranslation)?;
    validate_rpc_response(&response)?;
    Ok(response)
}

fn frozen_encoding<T>(value: Value) -> Result<Vec<u8>, ProtocolWireError>
where
    T: DeserializeOwned + Serialize,
{
    let frozen =
        serde_json::from_value::<T>(value).map_err(|_| ProtocolWireError::VersionTranslation)?;
    encode(&frozen)
}

fn normalize_v1_request(request: v1::RpcRequest) -> Result<RpcRequest, ProtocolWireError> {
    if matches!(request, v1::RpcRequest::SubmitTransaction(_)) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    transcode_request(request)
}

fn normalize_v2_request(request: v2::RpcRequest) -> Result<RpcRequest, ProtocolWireError> {
    let stable_intents = match &request {
        v2::RpcRequest::SubmitTransaction(envelope) => {
            let mut intents =
                if envelope.params.transaction_class == TransactionClass::StaticLighting {
                    envelope
                        .params
                        .frames
                        .iter()
                        .map(|frame| StableLightingIntent {
                            device_id: frame.device_id.clone(),
                            mode: StableLightingMode::Static,
                        })
                        .collect::<Vec<_>>()
                } else {
                    Vec::new()
                };
            intents.sort_unstable_by(|left, right| left.device_id.cmp(&right.device_id));
            Some(intents)
        }
        _ => None,
    };
    let mut value =
        serde_json::to_value(request).map_err(|_| ProtocolWireError::VersionTranslation)?;
    if let Some(stable_intents) = stable_intents {
        insert_stable_intents(&mut value, stable_intents)?;
    }
    let request =
        serde_json::from_value(value).map_err(|_| ProtocolWireError::VersionTranslation)?;
    validate_rpc_request(&request)?;
    Ok(request)
}

fn insert_stable_intents(
    value: &mut Value,
    stable_intents: Vec<StableLightingIntent>,
) -> Result<(), ProtocolWireError> {
    let params = value
        .as_object_mut()
        .and_then(|root| root.get_mut("request"))
        .and_then(Value::as_object_mut)
        .and_then(|request| request.get_mut("params"))
        .and_then(Value::as_object_mut)
        .ok_or(ProtocolWireError::VersionTranslation)?;
    params.insert(
        "stable_intents".to_owned(),
        serde_json::to_value(stable_intents).map_err(|_| ProtocolWireError::VersionTranslation)?,
    );
    Ok(())
}

fn remove_stable_intents(value: &mut Value) -> Result<(), ProtocolWireError> {
    let params = value
        .as_object_mut()
        .and_then(|root| root.get_mut("request"))
        .and_then(Value::as_object_mut)
        .and_then(|request| request.get_mut("params"))
        .and_then(Value::as_object_mut)
        .ok_or(ProtocolWireError::VersionTranslation)?;
    if params.remove("stable_intents").is_none() {
        return Err(ProtocolWireError::VersionTranslation);
    }
    Ok(())
}

fn strictly_ordered<T: Ord>(values: &[T]) -> bool {
    values.windows(2).all(|pair| pair[0] < pair[1])
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
        RpcRequest::Snapshot(_)
        | RpcRequest::IntegrationView(_)
        | RpcRequest::RenewLease(_)
        | RpcRequest::ReleaseLease(_)
        | RpcRequest::TransactionOutcome(_)
        | RpcRequest::Subscribe(_)
        | RpcRequest::Diagnostics(_) => Ok(()),
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
        RpcResponse::Error(_) => Ok(()),
    }
}

/// Decodes one bounded typed request and enforces method invariants.
///
/// # Errors
///
/// Returns an error before parsing oversized input, or after parsing malformed
/// and semantically invalid input.
pub fn decode_rpc_request(bytes: &[u8]) -> Result<RpcRequest, ProtocolWireError> {
    decode_rpc_request_version(bytes, CURRENT_PROTOCOL_VERSION)
}

/// Encodes one current request after enforcing current protocol invariants.
///
/// # Errors
///
/// Returns an error for an invalid request or bounded encoding failure.
pub fn encode_rpc_request(request: &RpcRequest) -> Result<Vec<u8>, ProtocolWireError> {
    encode_rpc_request_version(request, CURRENT_PROTOCOL_VERSION)
}

/// Encodes one current request into the exact frozen schema selected for the
/// connection.
///
/// Version 1 writes are unavailable. Version 3 Off semantics cannot be
/// represented by version 2 and fail before encoding.
///
/// # Errors
///
/// Returns an error for an invalid request, unknown version, unsupported
/// legacy method, unsafe semantic downgrade, or bounded encoding failure.
pub fn encode_rpc_request_for_version(
    request: &RpcRequest,
    version: ProtocolVersion,
) -> Result<Vec<u8>, ProtocolWireError> {
    encode_rpc_request_version(request, version.get())
}

fn encode_rpc_request_version(
    request: &RpcRequest,
    version: u16,
) -> Result<Vec<u8>, ProtocolWireError> {
    validate_rpc_request(request)?;
    if version < 5 && matches!(request, RpcRequest::IntegrationView(_)) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    let mut value =
        serde_json::to_value(request).map_err(|_| ProtocolWireError::VersionTranslation)?;
    match version {
        1 => {
            if matches!(request, RpcRequest::SubmitTransaction(_)) {
                return Err(ProtocolWireError::UnsupportedVersionMethod);
            }
            frozen_encoding::<v1::RpcRequest>(value)
        }
        2 => {
            if let RpcRequest::SubmitTransaction(envelope) = request {
                if envelope
                    .params
                    .stable_intents
                    .iter()
                    .any(|intent| intent.mode == StableLightingMode::Off)
                {
                    return Err(ProtocolWireError::VersionTranslation);
                }
                remove_stable_intents(&mut value)?;
            }
            frozen_encoding::<v2::RpcRequest>(value)
        }
        3 => frozen_encoding::<v3::RpcRequest>(value),
        4 => frozen_encoding::<v4::RpcRequest>(value),
        5 => frozen_encoding::<v5::RpcRequest>(value),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }
}

/// Decodes a request using the exact frozen schema selected for the connection.
///
/// Version 2 static transactions are conservatively normalized to semantic
/// `Static`; version 1 writes fail closed because they lack profile bindings.
///
/// # Errors
///
/// Returns an error for an unknown version, an unsupported legacy method, or
/// any frozen-schema, normalization, or current-core invariant violation.
pub fn decode_rpc_request_for_version(
    bytes: &[u8],
    version: ProtocolVersion,
) -> Result<RpcRequest, ProtocolWireError> {
    decode_rpc_request_version(bytes, version.get())
}

fn decode_rpc_request_version(bytes: &[u8], version: u16) -> Result<RpcRequest, ProtocolWireError> {
    match version {
        1 => normalize_v1_request(decode::<v1::RpcRequest>(bytes)?),
        2 => normalize_v2_request(decode::<v2::RpcRequest>(bytes)?),
        3 => transcode_request(decode::<v3::RpcRequest>(bytes)?),
        4 => transcode_request(decode::<v4::RpcRequest>(bytes)?),
        5 => transcode_request(decode::<v5::RpcRequest>(bytes)?),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }
}

/// Decodes one bounded typed response and enforces projection invariants.
///
/// # Errors
///
/// Returns an error before parsing oversized input, or after parsing malformed
/// and semantically invalid input.
pub fn decode_rpc_response(bytes: &[u8]) -> Result<RpcResponse, ProtocolWireError> {
    decode_rpc_response_version(bytes, CURRENT_PROTOCOL_VERSION)
}

/// Decodes one response using the exact frozen schema selected for the
/// connection and normalizes it to the current core representation.
///
/// # Errors
///
/// Returns an error for an unknown version, malformed response, unsafe
/// translation, or current response invariant violation.
pub fn decode_rpc_response_for_version(
    bytes: &[u8],
    version: ProtocolVersion,
) -> Result<RpcResponse, ProtocolWireError> {
    decode_rpc_response_version(bytes, version.get())
}

fn decode_rpc_response_version(
    bytes: &[u8],
    version: u16,
) -> Result<RpcResponse, ProtocolWireError> {
    match version {
        1 => transcode_response(decode::<v1::RpcResponse>(bytes)?),
        2 => transcode_response(decode::<v2::RpcResponse>(bytes)?),
        3 => transcode_response(decode::<v3::RpcResponse>(bytes)?),
        4 => transcode_response(decode::<v4::RpcResponse>(bytes)?),
        5 => transcode_response(decode::<v5::RpcResponse>(bytes)?),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }
}

/// Encodes one current response after enforcing response invariants.
///
/// # Errors
///
/// Returns an error for an invalid response or bounded encoding failure.
pub fn encode_rpc_response(response: &RpcResponse) -> Result<Vec<u8>, ProtocolWireError> {
    encode_rpc_response_version(response, CURRENT_PROTOCOL_VERSION)
}

/// Encodes one current response into the exact frozen schema selected for the
/// connection.
///
/// # Errors
///
/// Returns an error for an unknown version, unsafe translation, invalid
/// response, or bounded encoding failure.
pub fn encode_rpc_response_for_version(
    response: &RpcResponse,
    version: ProtocolVersion,
) -> Result<Vec<u8>, ProtocolWireError> {
    encode_rpc_response_version(response, version.get())
}

fn encode_rpc_response_version(
    response: &RpcResponse,
    version: u16,
) -> Result<Vec<u8>, ProtocolWireError> {
    validate_rpc_response(response)?;
    if version < 5 && matches!(response, RpcResponse::IntegrationViewSuccess(_)) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    let mut value =
        serde_json::to_value(response).map_err(|_| ProtocolWireError::VersionTranslation)?;
    match version {
        1 => {
            remove_snapshot_profile_bindings(response, &mut value)?;
            frozen_encoding::<v1::RpcResponse>(value)
        }
        2 => {
            remove_snapshot_profile_bindings(response, &mut value)?;
            frozen_encoding::<v2::RpcResponse>(value)
        }
        3 => {
            remove_snapshot_profile_bindings(response, &mut value)?;
            frozen_encoding::<v3::RpcResponse>(value)
        }
        4 => frozen_encoding::<v4::RpcResponse>(value),
        5 => frozen_encoding::<v5::RpcResponse>(value),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }
}

fn remove_snapshot_profile_bindings(
    response: &RpcResponse,
    value: &mut Value,
) -> Result<(), ProtocolWireError> {
    if !matches!(response, RpcResponse::SnapshotSuccess(_)) {
        return Ok(());
    }
    let receivers = value
        .as_object_mut()
        .and_then(|root| root.get_mut("response"))
        .and_then(Value::as_object_mut)
        .and_then(|response| response.get_mut("result"))
        .and_then(Value::as_object_mut)
        .and_then(|result| result.get_mut("receivers"))
        .and_then(Value::as_array_mut)
        .ok_or(ProtocolWireError::VersionTranslation)?;
    for receiver in receivers {
        let receiver = receiver
            .as_object_mut()
            .ok_or(ProtocolWireError::VersionTranslation)?;
        receiver.remove("profile_id");
        receiver.remove("profile_digest");
        let devices = receiver
            .get_mut("devices")
            .and_then(Value::as_array_mut)
            .ok_or(ProtocolWireError::VersionTranslation)?;
        for device in devices {
            device
                .as_object_mut()
                .ok_or(ProtocolWireError::VersionTranslation)?
                .remove("profile_digest");
        }
    }
    Ok(())
}

/// Verifies that a current-core response is exactly encodable by one frozen
/// negotiated response schema.
///
/// # Errors
///
/// Returns an error for an unknown version or any response field that is not
/// representable by the selected frozen schema.
pub fn validate_rpc_response_for_version(
    response: &RpcResponse,
    version: ProtocolVersion,
) -> Result<(), ProtocolWireError> {
    encode_rpc_response_for_version(response, version).map(|_| ())
}
