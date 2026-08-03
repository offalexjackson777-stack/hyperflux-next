// SPDX-License-Identifier: GPL-2.0-only

//! Bounded dispatch identity and retained kernel-result reconciliation.

use super::{KernelReceiverTransport, io_error, uapi_size};
use crate::{
    AlignedU64, EncodedTransaction, HFX_UAPI_ABI_VERSION,
    HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE, HFX_UAPI_RESULT_FLAG_WRITE_STARTED,
    HFX_UAPI_TRANSPORT_STATUS_CONFLICT, HFX_UAPI_TRANSPORT_STATUS_EVICTED,
    HFX_UAPI_TRANSPORT_STATUS_FAILED, HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED,
    HFX_UAPI_TRANSPORT_STATUS_RESERVED, HFX_UAPI_TRANSPORT_STATUS_REVOKED,
    HFX_UAPI_TRANSPORT_STATUS_STARTED, HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED,
    HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE, HfxUapiSubmit, HfxUapiTransactionResult, KernelIo,
    KernelTransportError, KernelTransportErrorKind,
};
use hfx_core::{
    TransportDispatch, TransportFailureFacts, TransportReceipt, TransportReconciliation,
    TransportTerminal,
};
use hfx_domain::{DeliveredFrameCount, DeviceApplicationState, SideEffectCertainty};
use serde_json::to_vec;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::mem::size_of;

const DISPATCH_BINDING_CAPACITY: usize = 128;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DispatchBinding {
    fingerprint: [u8; 32],
    authorization_epoch: u64,
}

#[derive(Debug, Default)]
pub(super) struct DispatchHistory {
    maximum_nonce: u64,
    bindings: BTreeMap<u64, DispatchBinding>,
    order: VecDeque<u64>,
}

pub(super) enum DispatchLookup {
    Known(u64),
    New(u64),
    Forgotten,
    Conflict,
}

impl<I: KernelIo> KernelReceiverTransport<I> {
    pub(super) fn lookup_dispatch(
        &self,
        nonce: u64,
        fingerprint: [u8; 32],
    ) -> Result<DispatchLookup, KernelTransportError> {
        let history = self.history.lock().map_err(|_| {
            KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeUnavailable)
        })?;
        if let Some(binding) = history.bindings.get(&nonce) {
            return Ok(if binding.fingerprint == fingerprint {
                DispatchLookup::Known(binding.authorization_epoch)
            } else {
                DispatchLookup::Conflict
            });
        }
        if nonce <= history.maximum_nonce {
            return Ok(DispatchLookup::Forgotten);
        }
        drop(history);
        self.ensure_session().map(DispatchLookup::New)
    }

    pub(super) fn remember_dispatch(
        &self,
        nonce: u64,
        fingerprint: [u8; 32],
        authorization_epoch: u64,
    ) -> Result<(), KernelTransportError> {
        let mut history = self.history.lock().map_err(|_| {
            KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeUnavailable)
        })?;
        if let Some(existing) = history.bindings.get(&nonce) {
            return if existing.fingerprint == fingerprint
                && existing.authorization_epoch == authorization_epoch
            {
                Ok(())
            } else {
                Err(KernelTransportError::uncertain(
                    KernelTransportErrorKind::OutcomeConflict,
                ))
            };
        }
        if nonce <= history.maximum_nonce {
            return Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeUnavailable,
            ));
        }
        history.maximum_nonce = nonce;
        history.bindings.insert(
            nonce,
            DispatchBinding {
                fingerprint,
                authorization_epoch,
            },
        );
        history.order.push_back(nonce);
        while history.order.len() > DISPATCH_BINDING_CAPACITY {
            if let Some(expired) = history.order.pop_front() {
                history.bindings.remove(&expired);
            }
        }
        Ok(())
    }

    pub(super) fn reconcile_exact(
        &self,
        dispatch: &TransportDispatch,
        encoded: &EncodedTransaction,
        fingerprint: [u8; 32],
    ) -> TransportReconciliation {
        let Ok(lookup) = self.lookup_dispatch(dispatch.dispatch_nonce.get(), fingerprint) else {
            return TransportReconciliation::Unavailable;
        };
        let authorization_epoch = match lookup {
            DispatchLookup::Known(epoch) | DispatchLookup::New(epoch) => epoch,
            DispatchLookup::Forgotten => return TransportReconciliation::Unavailable,
            DispatchLookup::Conflict => return TransportReconciliation::Conflict,
        };
        self.query_result(dispatch, encoded, fingerprint, authorization_epoch)
            .unwrap_or(TransportReconciliation::Unavailable)
    }

    fn query_result(
        &self,
        dispatch: &TransportDispatch,
        encoded: &EncodedTransaction,
        fingerprint: [u8; 32],
        authorization_epoch: u64,
    ) -> Result<TransportReconciliation, KernelTransportError> {
        let mut query = HfxUapiTransactionResult {
            version: HFX_UAPI_ABI_VERSION,
            size: uapi_size::<HfxUapiTransactionResult>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            authorization_epoch: AlignedU64(authorization_epoch),
            dispatch_nonce: AlignedU64(dispatch.dispatch_nonce.get()),
            request_digest: fingerprint,
            ..HfxUapiTransactionResult::default()
        };
        self.io
            .get_transaction_result(&mut query)
            .map_err(io_error)?;
        validate_result_key(dispatch, fingerprint, authorization_epoch, &query)?;
        decode_result(encoded, &query)
    }

    pub(super) fn submit_new(
        &self,
        dispatch: &TransportDispatch,
        encoded: &EncodedTransaction,
        fingerprint: [u8; 32],
    ) -> Result<TransportReceipt, KernelTransportError> {
        let authorization_epoch = self.ensure_session()?;
        self.remember_dispatch(
            dispatch.dispatch_nonce.get(),
            fingerprint,
            authorization_epoch,
        )?;
        let mut submit = HfxUapiSubmit {
            version: HFX_UAPI_ABI_VERSION,
            size: uapi_size::<HfxUapiSubmit>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            authorization_epoch: AlignedU64(authorization_epoch),
            dispatch_nonce: AlignedU64(dispatch.dispatch_nonce.get()),
            request_digest: fingerprint,
            frame_count: u32::try_from(encoded.frames().len())
                .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::Encoding))?,
            ..HfxUapiSubmit::default()
        };
        for (destination, source) in submit.frames.iter_mut().zip(encoded.frames()) {
            *destination = *source;
        }
        let submit_result = self.io.submit(&mut submit);
        let reconciliation = self.query_result(dispatch, encoded, fingerprint, authorization_epoch);
        match reconciliation {
            Ok(TransportReconciliation::Retained(receipt)) => Ok(receipt),
            Ok(TransportReconciliation::RetainedFailure(facts)) => {
                Err(KernelTransportError::retained(
                    KernelTransportErrorKind::OutcomeRetainedFailure,
                    facts,
                ))
            }
            Ok(TransportReconciliation::NotObserved) if submit_result.is_err() => Err(
                KernelTransportError::safe(KernelTransportErrorKind::KernelRejected),
            ),
            Ok(TransportReconciliation::Evicted) => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeEvicted,
            )),
            Ok(TransportReconciliation::Conflict) => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeConflict,
            )),
            Ok(TransportReconciliation::Unavailable | TransportReconciliation::NotObserved)
            | Err(_) => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeUnavailable,
            )),
        }
    }
}

fn validate_result_key(
    dispatch: &TransportDispatch,
    fingerprint: [u8; 32],
    authorization_epoch: u64,
    result: &HfxUapiTransactionResult,
) -> Result<(), KernelTransportError> {
    if result.version != HFX_UAPI_ABI_VERSION
        || usize::try_from(result.size).ok() != Some(size_of::<HfxUapiTransactionResult>())
    {
        return Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::AbiMismatch,
        ));
    }
    if result.receiver_generation.0 != dispatch.generation_id.get()
        || result.dispatch_nonce.0 != dispatch.dispatch_nonce.get()
        || result.request_digest != fingerprint
    {
        return Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::OutcomeConflict,
        ));
    }
    if matches!(
        result.status,
        HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED
            | HFX_UAPI_TRANSPORT_STATUS_RESERVED
            | HFX_UAPI_TRANSPORT_STATUS_STARTED
            | HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE
            | HFX_UAPI_TRANSPORT_STATUS_CONFLICT
    ) && result.authorization_epoch.0 != authorization_epoch
    {
        return Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::OutcomeConflict,
        ));
    }
    Ok(())
}

fn decode_result(
    encoded: &EncodedTransaction,
    result: &HfxUapiTransactionResult,
) -> Result<TransportReconciliation, KernelTransportError> {
    let known_flags =
        HFX_UAPI_RESULT_FLAG_WRITE_STARTED | HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE;
    if result.flags & !known_flags != 0 {
        return Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::AbiMismatch,
        ));
    }
    match result.status {
        HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED => {
            if result.kernel_sequence.0 != 0
                || result.frames_planned != 0
                || result.frames_completed != 0
                || result.flags != HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE
            {
                return Err(KernelTransportError::uncertain(
                    KernelTransportErrorKind::OutcomeConflict,
                ));
            }
            Ok(TransportReconciliation::NotObserved)
        }
        HFX_UAPI_TRANSPORT_STATUS_RESERVED | HFX_UAPI_TRANSPORT_STATUS_STARTED => {
            Ok(TransportReconciliation::Unavailable)
        }
        HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED => {
            validate_terminal_counts(encoded, result)?;
            if usize::try_from(result.frames_completed).ok() != Some(encoded.frames().len())
                || result.flags & HFX_UAPI_RESULT_FLAG_WRITE_STARTED == 0
                || result.flags & HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE != 0
            {
                return Err(KernelTransportError::uncertain(
                    KernelTransportErrorKind::OutcomeConflict,
                ));
            }
            Ok(TransportReconciliation::Retained(TransportReceipt {
                terminal: TransportTerminal::Delivered,
                delivered_frames: semantic_count(encoded.semantic_frame_count())?,
                side_effect_certainty: SideEffectCertainty::Committed,
                live_write_executed: true,
                automatic_retry_safe: false,
                device_application: DeviceApplicationState::Unverified,
            }))
        }
        HFX_UAPI_TRANSPORT_STATUS_FAILED | HFX_UAPI_TRANSPORT_STATUS_REVOKED => {
            validate_terminal_counts(encoded, result)?;
            let facts = failure_facts(encoded, result)?;
            if result.status == HFX_UAPI_TRANSPORT_STATUS_REVOKED {
                Ok(TransportReconciliation::Retained(TransportReceipt {
                    terminal: TransportTerminal::Revoked,
                    delivered_frames: facts.delivered_frames,
                    side_effect_certainty: facts.side_effect_certainty,
                    live_write_executed: facts.live_write_executed,
                    automatic_retry_safe: facts.automatic_retry_safe,
                    device_application: facts.device_application,
                }))
            } else {
                Ok(TransportReconciliation::RetainedFailure(facts))
            }
        }
        HFX_UAPI_TRANSPORT_STATUS_EVICTED => Ok(TransportReconciliation::Evicted),
        HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE => Ok(TransportReconciliation::Unavailable),
        HFX_UAPI_TRANSPORT_STATUS_CONFLICT => Ok(TransportReconciliation::Conflict),
        _ => Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::AbiMismatch,
        )),
    }
}

fn validate_terminal_counts(
    encoded: &EncodedTransaction,
    result: &HfxUapiTransactionResult,
) -> Result<(), KernelTransportError> {
    if result.kernel_sequence.0 == 0
        || usize::try_from(result.frames_planned).ok() != Some(encoded.frames().len())
        || result.frames_completed > result.frames_planned
    {
        return Err(KernelTransportError::uncertain(
            KernelTransportErrorKind::OutcomeConflict,
        ));
    }
    Ok(())
}

fn failure_facts(
    encoded: &EncodedTransaction,
    result: &HfxUapiTransactionResult,
) -> Result<TransportFailureFacts, KernelTransportError> {
    let physical_completed = usize::try_from(result.frames_completed)
        .map_err(|_| KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeConflict))?;
    let semantic_completed = encoded.semantic_frames_completed(physical_completed);
    let write_started = result.flags & HFX_UAPI_RESULT_FLAG_WRITE_STARTED != 0;
    let live_write_executed = write_started || physical_completed > 0;
    let side_effect_certainty = if physical_completed > 0 {
        SideEffectCertainty::Partial
    } else if write_started {
        SideEffectCertainty::Possible
    } else {
        SideEffectCertainty::None
    };
    let automatic_retry_safe = result.flags & HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE != 0
        && !live_write_executed
        && side_effect_certainty == SideEffectCertainty::None;
    Ok(TransportFailureFacts {
        delivered_frames: semantic_count(semantic_completed)?,
        side_effect_certainty,
        live_write_executed,
        automatic_retry_safe,
        device_application: DeviceApplicationState::Unverified,
    })
}

pub(super) fn dispatch_fingerprint(
    dispatch: &TransportDispatch,
    encoded: &EncodedTransaction,
) -> Result<[u8; 32], KernelTransportError> {
    let semantic = to_vec(dispatch)
        .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::Encoding))?;
    let mut digest = Sha256::new();
    digest.update(b"hyperflux-kernel-dispatch-v1\0");
    digest.update(
        u64::try_from(semantic.len())
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::Encoding))?
            .to_be_bytes(),
    );
    digest.update(&semantic);
    for frame in encoded.frames() {
        digest.update(frame.backend_id.to_be_bytes());
        digest.update(frame.kind.to_be_bytes());
        digest.update(frame.payload_length.to_be_bytes());
        digest.update(frame.delay_after_us.to_be_bytes());
        digest.update(frame.flags.to_be_bytes());
        digest.update(frame.payload);
    }
    Ok(digest.finalize().into())
}

fn semantic_count(value: usize) -> Result<DeliveredFrameCount, KernelTransportError> {
    let value = u16::try_from(value)
        .map_err(|_| KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeConflict))?;
    DeliveredFrameCount::try_from(value)
        .map_err(|_| KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeConflict))
}
