// SPDX-License-Identifier: GPL-2.0-only

//! Frozen protocol-version adapters and current wire entry points.

use super::codec::{decode, encode};
use super::{ProtocolWireError, validate_rpc_request, validate_rpc_response};
use crate::{CURRENT_PROTOCOL_VERSION, LeaseResult, RpcRequest, RpcResponse, v5, v7, v8};
use hfx_domain::{ProtocolVersion, ResourceKind};
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
/// Only the served versions 5, 7 and 8 have an encoder; retired version numbers
/// fail before any request field is translated.
///
/// # Errors
///
/// Returns an error for an invalid request, unknown or retired version,
/// unsupported legacy method, or bounded encoding failure.
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
    let encoder: fn(Value) -> Result<Vec<u8>, ProtocolWireError> = match version {
        5 => frozen_encoding::<v5::RpcRequest>,
        7 => frozen_encoding::<v7::RpcRequest>,
        8 => frozen_encoding::<v8::RpcRequest>,
        _ => return Err(ProtocolWireError::UnsupportedProtocolVersion),
    };
    validate_rpc_request(request)?;
    if version < 7 && request_requires_v7(request) {
        return Err(ProtocolWireError::UnsupportedVersionMethod);
    }
    let value = serde_json::to_value(request).map_err(|_| ProtocolWireError::VersionTranslation)?;
    encoder(value)
}

/// Decodes a request using the exact frozen schema selected for the connection.
///
/// Only the served versions 5, 7 and 8 decode; retired version numbers are
/// rejected without parsing.
///
/// # Errors
///
/// Returns an error for an unknown or retired version, an unsupported legacy
/// method, or any frozen-schema or current-core invariant violation.
pub fn decode_rpc_request_for_version(
    bytes: &[u8],
    version: ProtocolVersion,
) -> Result<RpcRequest, ProtocolWireError> {
    decode_rpc_request_version(bytes, version.get())
}

fn decode_rpc_request_version(bytes: &[u8], version: u16) -> Result<RpcRequest, ProtocolWireError> {
    let request = match version {
        5 => transcode_request(decode::<v5::RpcRequest>(bytes)?),
        7 => transcode_request(decode::<v7::RpcRequest>(bytes)?),
        8 => transcode_request(decode::<v8::RpcRequest>(bytes)?),
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
        5 => transcode_response(decode::<v5::RpcResponse>(bytes)?),
        7 => transcode_response(decode::<v7::RpcResponse>(bytes)?),
        8 => transcode_response(decode::<v8::RpcResponse>(bytes)?),
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
/// Only the served versions 5, 7 and 8 have an encoder; retired version numbers
/// fail before any response field is translated.
///
/// # Errors
///
/// Returns an error for an unknown or retired version, unsafe translation,
/// invalid response, or bounded encoding failure.
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
    let encoder: fn(Value) -> Result<Vec<u8>, ProtocolWireError> = match version {
        5 => frozen_encoding::<v5::RpcResponse>,
        7 => frozen_encoding::<v7::RpcResponse>,
        8 => frozen_encoding::<v8::RpcResponse>,
        _ => return Err(ProtocolWireError::UnsupportedProtocolVersion),
    };
    validate_rpc_response(response)?;
    if version < 7 && response_requires_v7(response) {
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
    if version < 8 {
        remove_battery_charge(response, &mut value)?;
    }
    encoder(value)
}

/// Versions before 8 have no battery charge field, so it is dropped from
/// every device and controller battery those versions carry.
fn remove_battery_charge(
    response: &RpcResponse,
    value: &mut Value,
) -> Result<(), ProtocolWireError> {
    let collections: &[&str] = match response {
        RpcResponse::SnapshotSuccess(_) => &["devices"],
        RpcResponse::IntegrationViewSuccess(_) => &["inventory", "controllers"],
        _ => return Ok(()),
    };
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
        for collection in collections {
            let items = receiver
                .get_mut(*collection)
                .and_then(Value::as_array_mut)
                .ok_or(ProtocolWireError::VersionTranslation)?;
            for item in items {
                item.as_object_mut()
                    .and_then(|item| item.get_mut("battery"))
                    .and_then(Value::as_object_mut)
                    .ok_or(ProtocolWireError::VersionTranslation)?
                    .remove("charge");
            }
        }
    }
    Ok(())
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
