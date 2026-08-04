// SPDX-License-Identifier: GPL-2.0-only

//! Frozen protocol-version adapters and current wire entry points.

use super::codec::{decode, encode};
use super::{ProtocolWireError, validate_rpc_request, validate_rpc_response};
use crate::{
    CURRENT_PROTOCOL_VERSION, LeaseResult, RpcRequest, RpcResponse, StableLightingIntent, v1, v2,
    v3, v4, v5, v6, v7,
};
use hfx_domain::{ProtocolVersion, ResourceKind, StableLightingMode, TransactionClass};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::Value;

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

fn request_requires_v7(request: &RpcRequest) -> bool {
    match request {
        RpcRequest::NativeEnumerate(_)
        | RpcRequest::NativeTransact(_)
        | RpcRequest::NativeWatch(_) => true,
        RpcRequest::AcquireLease(envelope) => envelope
            .params
            .resources
            .iter()
            .any(|resource| resource.kind == ResourceKind::NativeControl),
        RpcRequest::SubmitTransaction(envelope) => envelope
            .params
            .resources
            .iter()
            .any(|resource| resource.kind == ResourceKind::NativeControl),
        RpcRequest::Negotiate(_)
        | RpcRequest::Snapshot(_)
        | RpcRequest::IntegrationView(_)
        | RpcRequest::RenewLease(_)
        | RpcRequest::ReleaseLease(_)
        | RpcRequest::TransactionOutcome(_)
        | RpcRequest::Subscribe(_)
        | RpcRequest::Diagnostics(_) => false,
    }
}

fn lease_result_uses_native_control(result: &LeaseResult) -> bool {
    match result {
        LeaseResult::Granted(grant) => grant
            .resources
            .iter()
            .any(|resource| resource.kind == ResourceKind::NativeControl),
        LeaseResult::Conflict(conflict) => {
            conflict.conflicting_resource.kind == ResourceKind::NativeControl
        }
        LeaseResult::Rejected(_) => false,
    }
}

fn response_requires_v7(response: &RpcResponse) -> bool {
    match response {
        RpcResponse::NativeEnumerateSuccess(_)
        | RpcResponse::NativeTransactSuccess(_)
        | RpcResponse::NativeWatchSuccess(_) => true,
        RpcResponse::AcquireLeaseSuccess(envelope)
        | RpcResponse::RenewLeaseSuccess(envelope)
        | RpcResponse::ReleaseLeaseSuccess(envelope) => {
            lease_result_uses_native_control(&envelope.result)
        }
        RpcResponse::NegotiateSuccess(_)
        | RpcResponse::SnapshotSuccess(_)
        | RpcResponse::IntegrationViewSuccess(_)
        | RpcResponse::SubmitTransactionSuccess(_)
        | RpcResponse::TransactionOutcomeSuccess(_)
        | RpcResponse::SubscribeSuccess(_)
        | RpcResponse::DiagnosticsSuccess(_)
        | RpcResponse::Error(_) => false,
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
    if version < 7 && request_requires_v7(request) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
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
        6 => frozen_encoding::<v6::RpcRequest>(value),
        7 => frozen_encoding::<v7::RpcRequest>(value),
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
    let request = match version {
        1 => normalize_v1_request(decode::<v1::RpcRequest>(bytes)?),
        2 => normalize_v2_request(decode::<v2::RpcRequest>(bytes)?),
        3 => transcode_request(decode::<v3::RpcRequest>(bytes)?),
        4 => transcode_request(decode::<v4::RpcRequest>(bytes)?),
        5 => transcode_request(decode::<v5::RpcRequest>(bytes)?),
        6 => transcode_request(decode::<v6::RpcRequest>(bytes)?),
        7 => transcode_request(decode::<v7::RpcRequest>(bytes)?),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }?;
    if version < 7 && request_requires_v7(&request) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    Ok(request)
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
    let response = match version {
        1 => transcode_response(decode::<v1::RpcResponse>(bytes)?),
        2 => transcode_response(decode::<v2::RpcResponse>(bytes)?),
        3 => transcode_response(decode::<v3::RpcResponse>(bytes)?),
        4 => transcode_response(decode::<v4::RpcResponse>(bytes)?),
        5 => transcode_response(decode::<v5::RpcResponse>(bytes)?),
        6 => transcode_response(decode::<v6::RpcResponse>(bytes)?),
        7 => transcode_response(decode::<v7::RpcResponse>(bytes)?),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }?;
    if version < 7 && response_requires_v7(&response) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    Ok(response)
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
    if version < 7 && response_requires_v7(response) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    if version < 5 && matches!(response, RpcResponse::IntegrationViewSuccess(_)) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    let mut value =
        serde_json::to_value(response).map_err(|_| ProtocolWireError::VersionTranslation)?;
    if version < 6 {
        remove_return_epochs(response, &mut value)?;
    }
    if version < 7 {
        remove_native_control_snapshot_ownership(response, &mut value)?;
    }
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
        6 => frozen_encoding::<v6::RpcResponse>(value),
        7 => frozen_encoding::<v7::RpcResponse>(value),
        _ => Err(ProtocolWireError::UnsupportedProtocolVersion),
    }
}

fn remove_native_control_snapshot_ownership(
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
        let ownership = receiver
            .as_object_mut()
            .and_then(|receiver| receiver.get_mut("ownership"))
            .and_then(Value::as_array_mut)
            .ok_or(ProtocolWireError::VersionTranslation)?;
        ownership.retain(|entry| {
            entry
                .as_object()
                .and_then(|entry| entry.get("resource"))
                .and_then(Value::as_object)
                .and_then(|resource| resource.get("kind"))
                .and_then(Value::as_str)
                != Some("native-control")
        });
    }
    Ok(())
}

fn remove_return_epochs(
    response: &RpcResponse,
    value: &mut Value,
) -> Result<(), ProtocolWireError> {
    let receivers = value
        .as_object_mut()
        .and_then(|root| root.get_mut("response"))
        .and_then(Value::as_object_mut)
        .and_then(|response| response.get_mut("result"))
        .and_then(Value::as_object_mut)
        .and_then(|result| result.get_mut("receivers"))
        .and_then(Value::as_array_mut);

    let Some(receivers) = receivers else {
        return if matches!(
            response,
            RpcResponse::SnapshotSuccess(_) | RpcResponse::IntegrationViewSuccess(_)
        ) {
            Err(ProtocolWireError::VersionTranslation)
        } else {
            Ok(())
        };
    };

    for receiver in receivers {
        let receiver = receiver
            .as_object_mut()
            .ok_or(ProtocolWireError::VersionTranslation)?;
        let collections: &mut Vec<Value> = match response {
            RpcResponse::SnapshotSuccess(_) => receiver
                .get_mut("devices")
                .and_then(Value::as_array_mut)
                .ok_or(ProtocolWireError::VersionTranslation)?,
            RpcResponse::IntegrationViewSuccess(_) => receiver
                .get_mut("inventory")
                .and_then(Value::as_array_mut)
                .ok_or(ProtocolWireError::VersionTranslation)?,
            _ => return Ok(()),
        };
        for item in collections {
            let endpoints = item
                .as_object_mut()
                .and_then(|item| item.get_mut("endpoints"))
                .and_then(Value::as_array_mut)
                .ok_or(ProtocolWireError::VersionTranslation)?;
            for endpoint in endpoints {
                endpoint
                    .as_object_mut()
                    .ok_or(ProtocolWireError::VersionTranslation)?
                    .remove("return_epoch");
            }
        }
    }
    Ok(())
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
