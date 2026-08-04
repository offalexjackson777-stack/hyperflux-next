// SPDX-License-Identifier: GPL-2.0-only

//! Native child feature-report exchange and core outcome mapping.

use super::history::DispatchLookup;
use super::{KernelReceiverTransport, io_error, uapi_size};
use crate::{
    AlignedU64, HFX_UAPI_NATIVE_ABI_VERSION, HFX_UAPI_NATIVE_REPORT_BYTES,
    HfxUapiNativeExchangeResult, HfxUapiNativeExchangeSubmit, KernelIo, KernelTransportError,
    KernelTransportErrorKind, NativeExchangeOutcome, NativeExchangeRequest,
};
use hfx_core::{NativeTransportDispatch, NativeTransportOutcome, NativeTransportResult};
use hfx_domain::ProtocolErrorKind;
use std::mem::size_of;

impl<I: KernelIo> KernelReceiverTransport<I> {
    /// Reconciles an exact native child exchange without sending a report.
    ///
    /// # Errors
    ///
    /// Returns a typed error for invalid request material, forgotten or
    /// contradictory nonce bindings, or a malformed kernel result.
    pub fn reconcile_native_exchange(
        &self,
        request: &NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, KernelTransportError> {
        Self::validate_native_request(request)?;
        let request_digest = request.request_digest();
        let authorization_epoch =
            match self.lookup_dispatch(request.dispatch_nonce, request_digest)? {
                DispatchLookup::Known(epoch) | DispatchLookup::New(epoch) => epoch,
                DispatchLookup::Forgotten => {
                    return Err(KernelTransportError::uncertain(
                        KernelTransportErrorKind::NativeOutcomeUnavailable,
                    ));
                }
                DispatchLookup::Conflict => {
                    return Err(KernelTransportError::uncertain(
                        KernelTransportErrorKind::NativeOutcomeConflict,
                    ));
                }
            };
        self.query_native_exchange(request, request_digest, authorization_epoch, 0)
    }

    /// Performs one generation-bound native child feature-report exchange.
    ///
    /// The kernel submits `SET_REPORT` at most once for a new nonce. This method
    /// always reconciles the exact retained result after the ioctl returns and
    /// never repeats an uncertain submit.
    ///
    /// # Errors
    ///
    /// Returns a typed error for invalid request material, session loss,
    /// contradictory nonce reuse, or an unavailable/malformed retained result.
    pub fn native_exchange(
        &mut self,
        request: &NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, KernelTransportError> {
        Self::validate_native_request(request)?;
        let request_digest = request.request_digest();
        match self.lookup_dispatch(request.dispatch_nonce, request_digest)? {
            DispatchLookup::Known(epoch) => {
                self.query_native_exchange(request, request_digest, epoch, 0)
            }
            DispatchLookup::New(epoch) => {
                self.submit_native_exchange(request, request_digest, epoch)
            }
            DispatchLookup::Forgotten => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::NativeOutcomeUnavailable,
            )),
            DispatchLookup::Conflict => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::NativeOutcomeConflict,
            )),
        }
    }

    fn validate_native_request(
        request: &NativeExchangeRequest,
    ) -> Result<(), KernelTransportError> {
        if request.validate() {
            Ok(())
        } else {
            Err(KernelTransportError::safe(
                KernelTransportErrorKind::InvalidNativeExchange,
            ))
        }
    }

    fn query_native_exchange(
        &self,
        request: &NativeExchangeRequest,
        request_digest: [u8; 32],
        authorization_epoch: u64,
        kernel_sequence: u64,
    ) -> Result<NativeExchangeOutcome, KernelTransportError> {
        let mut query = HfxUapiNativeExchangeResult {
            version: HFX_UAPI_NATIVE_ABI_VERSION,
            size: uapi_size::<HfxUapiNativeExchangeResult>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            authorization_epoch: AlignedU64(authorization_epoch),
            kernel_sequence: AlignedU64(kernel_sequence),
            dispatch_nonce: AlignedU64(request.dispatch_nonce),
            endpoint_slot: request.endpoint.as_uapi(),
            route_token_digest: request.route_token_digest,
            request_digest,
            ..HfxUapiNativeExchangeResult::default()
        };
        self.io
            .get_native_exchange_result(&mut query)
            .map_err(|_| {
                KernelTransportError::uncertain(KernelTransportErrorKind::NativeOutcomeUnavailable)
            })?;
        if query.version != HFX_UAPI_NATIVE_ABI_VERSION
            || usize::try_from(query.size).ok() != Some(size_of::<HfxUapiNativeExchangeResult>())
            || query.receiver_generation.0 != self.material.generation_id.get()
            || query.authorization_epoch.0 != authorization_epoch
            || query.dispatch_nonce.0 != request.dispatch_nonce
            || query.endpoint_slot != request.endpoint.as_uapi()
            || query.route_token_digest != request.route_token_digest
            || query.request_digest != request_digest
            || query.reserved != 0
        {
            return Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::NativeOutcomeConflict,
            ));
        }
        NativeExchangeOutcome::decode(&query).ok_or_else(|| {
            KernelTransportError::uncertain(KernelTransportErrorKind::NativeOutcomeConflict)
        })
    }

    fn submit_native_exchange(
        &self,
        request: &NativeExchangeRequest,
        request_digest: [u8; 32],
        authorization_epoch: u64,
    ) -> Result<NativeExchangeOutcome, KernelTransportError> {
        self.remember_dispatch(request.dispatch_nonce, request_digest, authorization_epoch)?;
        let timeout_ns = u64::try_from(request.timeout.as_nanos()).map_err(|_| {
            KernelTransportError::safe(KernelTransportErrorKind::InvalidNativeExchange)
        })?;
        let now = self.io.boottime_ns().map_err(io_error)?;
        let deadline = now.checked_add(timeout_ns).ok_or_else(|| {
            KernelTransportError::safe(KernelTransportErrorKind::InvalidNativeExchange)
        })?;
        let mut submit = HfxUapiNativeExchangeSubmit {
            version: HFX_UAPI_NATIVE_ABI_VERSION,
            size: uapi_size::<HfxUapiNativeExchangeSubmit>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            authorization_epoch: AlignedU64(authorization_epoch),
            dispatch_nonce: AlignedU64(request.dispatch_nonce),
            deadline_boottime_ns: AlignedU64(deadline),
            endpoint_slot: request.endpoint.as_uapi(),
            report_length: u32::try_from(HFX_UAPI_NATIVE_REPORT_BYTES).map_err(|_| {
                KernelTransportError::safe(KernelTransportErrorKind::InvalidNativeExchange)
            })?,
            route_token_digest: request.route_token_digest,
            request_digest,
            report: request.report,
            kernel_sequence: AlignedU64(0),
        };
        let submit_result = self.io.submit_native_exchange(&mut submit);
        match self.query_native_exchange(
            request,
            request_digest,
            authorization_epoch,
            submit.kernel_sequence.0,
        ) {
            Ok(outcome) => Ok(outcome),
            Err(error) if submit_result.is_err() => Err(error),
            Err(error) => Err(error),
        }
    }
}

pub(super) fn core_native_failure(
    dispatch: &NativeTransportDispatch,
    error_kind: ProtocolErrorKind,
    retry_safe: bool,
) -> NativeTransportOutcome {
    NativeTransportOutcome {
        dispatch_nonce: dispatch.dispatch_nonce,
        route_token_digest: dispatch.route_token_digest,
        result: if retry_safe {
            NativeTransportResult::NotSent { error_kind }
        } else {
            NativeTransportResult::MayHaveBeenSent { error_kind }
        },
    }
}

pub(super) fn native_transport_error_kind(transport_errno: i32) -> ProtocolErrorKind {
    if transport_errno == 0 {
        ProtocolErrorKind::OutcomeUnknown
    } else {
        ProtocolErrorKind::TransportFailure
    }
}

pub(super) const fn kernel_native_error_kind(kind: KernelTransportErrorKind) -> ProtocolErrorKind {
    match kind {
        KernelTransportErrorKind::InvalidNativeExchange
        | KernelTransportErrorKind::InvalidDispatch
        | KernelTransportErrorKind::Encoding => ProtocolErrorKind::InvalidRequest,
        KernelTransportErrorKind::GenerationMismatch
        | KernelTransportErrorKind::ReceiverMismatch => ProtocolErrorKind::StaleGeneration,
        KernelTransportErrorKind::OutcomeEvicted => ProtocolErrorKind::OutcomeEvicted,
        KernelTransportErrorKind::NativeOutcomeUnavailable
        | KernelTransportErrorKind::NativeOutcomeConflict
        | KernelTransportErrorKind::OutcomeUnavailable
        | KernelTransportErrorKind::OutcomeConflict => ProtocolErrorKind::OutcomeUnknown,
        KernelTransportErrorKind::Io
        | KernelTransportErrorKind::AbiMismatch
        | KernelTransportErrorKind::ProfileMismatch
        | KernelTransportErrorKind::UnsupportedBackend
        | KernelTransportErrorKind::InvalidSessionMaterial
        | KernelTransportErrorKind::SessionUnavailable
        | KernelTransportErrorKind::OutcomeRetainedFailure
        | KernelTransportErrorKind::KernelRejected => ProtocolErrorKind::TransportFailure,
    }
}
