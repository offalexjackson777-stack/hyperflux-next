// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    DiagnosticSnapshot, EventBatch, LeaseResult, MAX_WIRE_MESSAGE_BYTES, ProtocolValidationError,
    RpcRequest, RpcResponse, SnapshotValidationError, TransactionResult, TransactionTerminal,
    TransactionUnavailable, validate_bridge_snapshot, validate_lease_request, validate_transaction,
};
use hfx_domain::{ProtocolErrorKind, SideEffectCertainty, TransactionState};
use serde::de::DeserializeOwned;
use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProtocolWireError {
    MessageTooLarge,
    MalformedJson,
    RequestBoundExceeded,
    RequestNotCanonical,
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
    let request = decode(bytes)?;
    validate_rpc_request(&request)?;
    Ok(request)
}

/// Decodes one bounded typed response and enforces projection invariants.
///
/// # Errors
///
/// Returns an error before parsing oversized input, or after parsing malformed
/// and semantically invalid input.
pub fn decode_rpc_response(bytes: &[u8]) -> Result<RpcResponse, ProtocolWireError> {
    let response = decode(bytes)?;
    validate_rpc_response(&response)?;
    Ok(response)
}
