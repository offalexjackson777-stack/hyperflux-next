// SPDX-License-Identifier: GPL-2.0-only

mod history;
mod native_exchange;
mod session;

use self::history::{DispatchHistory, dispatch_fingerprint};
use self::native_exchange::{
    core_native_failure, kernel_native_error_kind, native_transport_error_kind,
};
pub use self::session::KernelSessionMaterial;
use self::session::KernelSessionState;
use crate::observation::decode_observations;
use crate::{
    AlignedU64, ChildBatteryLane, ChildBatteryPublisher, ChildBatteryState, EncodedTransaction,
    HFX_UAPI_ABI_VERSION, HFX_UAPI_INFO_FLAG_DISCONNECTING, HFX_UAPI_REVOKE_REASON_EXPLICIT,
    HFX_UAPI_REVOKE_REASON_SERVICE_LOSS, HfxUapiReadObservations, KernelIo, KernelObservationBatch,
    KernelTransportError, KernelTransportErrorKind, LinuxKernelIo, NativeExchangeRequest,
    ReceiverFrameEncoderRegistry,
};
use hfx_core::{
    NativeEndpointLane as CoreNativeEndpointLane, NativeTransportDispatch, NativeTransportOutcome,
    NativeTransportResult, ReceiverTransport, TransportDispatch, TransportReceipt,
    TransportReconciliation,
};
use hfx_domain::{GenerationId, ProfileKind, ProtocolErrorKind, ReceiverId};
use hfx_profiles::RuntimeProfileCatalog;
use std::path::Path;
use std::sync::Mutex;
use std::time::Duration;

pub struct KernelReceiverTransport<I: KernelIo> {
    io: I,
    catalog: RuntimeProfileCatalog,
    encoder: ReceiverFrameEncoderRegistry,
    material: KernelSessionMaterial,
    profile_digest: [u8; 32],
    session_duration_ns: u64,
    receiver_vendor_id: u16,
    receiver_product_id: u16,
    receiver_protocol_family: &'static str,
    backend_id: u32,
    session: Mutex<KernelSessionState>,
    history: Mutex<DispatchHistory>,
}

impl KernelReceiverTransport<LinuxKernelIo> {
    /// Opens and authorizes one generation-scoped Linux kernel receiver.
    ///
    /// # Errors
    ///
    /// Returns a typed error without exposing the endpoint path when opening,
    /// profile validation, ABI validation, or session admission fails.
    pub fn open(
        path: &Path,
        catalog: RuntimeProfileCatalog,
        material: KernelSessionMaterial,
    ) -> Result<Self, KernelTransportError> {
        let io = LinuxKernelIo::open_read_write(path)
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::Io))?;
        Self::new(io, catalog, material)
    }
}

impl<I: KernelIo> KernelReceiverTransport<I> {
    /// Creates a kernel-backed receiver transport over an injected I/O port.
    ///
    /// # Errors
    ///
    /// Returns a typed error for invalid session material, stale receiver or
    /// profile bindings, incompatible ABI, or failed session admission.
    pub fn new(
        io: I,
        catalog: RuntimeProfileCatalog,
        material: KernelSessionMaterial,
    ) -> Result<Self, KernelTransportError> {
        let session_duration_ns = material.validate()?;
        let receiver = catalog
            .profile(&material.receiver_profile_id)
            .ok_or_else(profile_mismatch)?;
        if receiver.profile_kind != ProfileKind::Receiver
            || receiver.runtime_digest != material.receiver_profile_digest
        {
            return Err(profile_mismatch());
        }
        let receiver_vendor_id = receiver.vendor_id.ok_or_else(profile_mismatch)?.get();
        let receiver_product_id = receiver.product_id.ok_or_else(profile_mismatch)?.get();
        let receiver_protocol_family = receiver.protocol_family.ok_or_else(profile_mismatch)?;
        let backend_id = receiver.transport_backend_id.ok_or_else(|| {
            KernelTransportError::safe(KernelTransportErrorKind::UnsupportedBackend)
        })?;
        let profile_digest = decode_digest(material.receiver_profile_digest.as_str())?;
        let transport = Self {
            io,
            catalog,
            encoder: ReceiverFrameEncoderRegistry,
            material,
            profile_digest,
            session_duration_ns,
            receiver_vendor_id,
            receiver_product_id,
            receiver_protocol_family,
            backend_id,
            session: Mutex::new(KernelSessionState::default()),
            history: Mutex::new(DispatchHistory::default()),
        };
        transport.validate_writable_info(&transport.io.get_info().map_err(io_error)?)?;
        transport.ensure_session()?;
        Ok(transport)
    }

    #[must_use]
    pub const fn receiver_id(&self) -> &ReceiverId {
        &self.material.receiver_id
    }

    #[must_use]
    pub const fn generation_id(&self) -> GenerationId {
        self.material.generation_id
    }

    /// Reads bounded passive observations without issuing an active query.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the ioctl fails or the returned batch does
    /// not match the bound ABI and receiver generation.
    pub fn read_observations(
        &self,
        after_sequence: u64,
    ) -> Result<KernelObservationBatch, KernelTransportError> {
        let mut request = HfxUapiReadObservations {
            version: HFX_UAPI_ABI_VERSION,
            size: uapi_size::<HfxUapiReadObservations>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            after_sequence: AlignedU64(after_sequence),
            ..HfxUapiReadObservations::default()
        };
        self.io.read_observations(&mut request).map_err(io_error)?;
        decode_observations(self.material.generation_id, after_sequence, &request)
    }

    /// Ends the current kernel authorization session explicitly.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the session state cannot be locked or the
    /// kernel rejects the exact generation and epoch.
    pub fn shutdown(&mut self) -> Result<(), KernelTransportError> {
        self.end_active_session(HFX_UAPI_REVOKE_REASON_EXPLICIT)
    }

    fn encode_and_fingerprint(
        &self,
        dispatch: &TransportDispatch,
    ) -> Result<(EncodedTransaction, [u8; 32]), KernelTransportError> {
        if dispatch.receiver_id != self.material.receiver_id
            || dispatch.generation_id != self.material.generation_id
            || dispatch.receiver_profile_id != self.material.receiver_profile_id
            || dispatch.receiver_profile_digest != self.material.receiver_profile_digest
        {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::InvalidDispatch,
            ));
        }
        let encoded = self.encoder.encode(
            &self.catalog,
            self.receiver_protocol_family,
            self.backend_id,
            dispatch,
        )?;
        let fingerprint = dispatch_fingerprint(dispatch, &encoded)?;
        Ok((encoded, fingerprint))
    }
}

impl<I: KernelIo> ReceiverTransport for KernelReceiverTransport<I> {
    type Error = KernelTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        if receiver_id != &self.material.receiver_id {
            return None;
        }
        let info = self.io.get_info().ok()?;
        self.validate_binding_info(&info).ok()?;
        if info.flags & HFX_UAPI_INFO_FLAG_DISCONNECTING != 0 || info.bound_interfaces == 0 {
            return None;
        }
        Some(self.material.generation_id)
    }

    fn native_exchange_available(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> bool {
        receiver_id == &self.material.receiver_id
            && generation_id == self.material.generation_id
            && self
                .io
                .get_info()
                .ok()
                .is_some_and(|info| self.validate_writable_info(&info).is_ok())
    }

    fn reconcile(&self, dispatch: &TransportDispatch) -> TransportReconciliation {
        let Ok((encoded, fingerprint)) = self.encode_and_fingerprint(dispatch) else {
            return TransportReconciliation::Conflict;
        };
        self.reconcile_exact(dispatch, &encoded, fingerprint)
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        let (encoded, fingerprint) = self.encode_and_fingerprint(dispatch)?;
        match self.reconcile_exact(dispatch, &encoded, fingerprint) {
            TransportReconciliation::Retained(receipt) => Ok(receipt),
            TransportReconciliation::RetainedFailure(facts) => Err(KernelTransportError::retained(
                KernelTransportErrorKind::OutcomeRetainedFailure,
                facts,
            )),
            TransportReconciliation::NotObserved => {
                self.submit_new(dispatch, &encoded, fingerprint)
            }
            TransportReconciliation::Evicted => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeEvicted,
            )),
            TransportReconciliation::Unavailable => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeUnavailable,
            )),
            TransportReconciliation::Conflict => Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::OutcomeConflict,
            )),
        }
    }

    fn native_exchange(&mut self, dispatch: &NativeTransportDispatch) -> NativeTransportOutcome {
        if dispatch.receiver_id != self.material.receiver_id
            || dispatch.generation_id != self.material.generation_id
        {
            return core_native_failure(dispatch, ProtocolErrorKind::StaleGeneration, true);
        }
        let request = NativeExchangeRequest {
            dispatch_nonce: dispatch.dispatch_nonce.get(),
            endpoint: match dispatch.endpoint {
                CoreNativeEndpointLane::Pointer => crate::NativeEndpointLane::Pointer,
                CoreNativeEndpointLane::Keyboard => crate::NativeEndpointLane::Keyboard,
            },
            route_token_digest: dispatch.route_token_digest,
            report: dispatch.report,
            timeout: Duration::from_millis(u64::from(dispatch.timeout_ms.get())),
        };
        match KernelReceiverTransport::native_exchange(self, &request) {
            Ok(outcome)
                if outcome.dispatch_nonce == dispatch.dispatch_nonce.get()
                    && outcome.route_token_digest == dispatch.route_token_digest =>
            {
                let result = match outcome.certainty {
                    crate::NativeExchangeCertainty::NotSent => NativeTransportResult::NotSent {
                        error_kind: native_transport_error_kind(outcome.transport_errno),
                    },
                    crate::NativeExchangeCertainty::MayHaveBeenSent => {
                        NativeTransportResult::MayHaveBeenSent {
                            error_kind: native_transport_error_kind(outcome.transport_errno),
                        }
                    }
                    crate::NativeExchangeCertainty::MatchedResponse => {
                        let Some(response) = outcome.response else {
                            return core_native_failure(
                                dispatch,
                                ProtocolErrorKind::OutcomeUnknown,
                                false,
                            );
                        };
                        NativeTransportResult::MatchedResponse(response)
                    }
                };
                NativeTransportOutcome {
                    dispatch_nonce: dispatch.dispatch_nonce,
                    route_token_digest: dispatch.route_token_digest,
                    result,
                }
            }
            Ok(_) => core_native_failure(dispatch, ProtocolErrorKind::OutcomeUnknown, false),
            Err(error) => core_native_failure(
                dispatch,
                kernel_native_error_kind(error.kind()),
                error.failure_facts().automatic_retry_safe,
            ),
        }
    }
}

impl<I: KernelIo> ChildBatteryPublisher for KernelReceiverTransport<I> {
    /// Forwards one lane's battery state through this writer file. It needs
    /// no authorization session: the kernel accepts it from the exclusive
    /// writer of the bound generation and publishes it without a device
    /// query.
    fn publish_child_battery(
        &self,
        lane: ChildBatteryLane,
        state: &ChildBatteryState,
    ) -> Result<(), KernelTransportError> {
        let request = state.encode(lane, self.material.generation_id)?;
        self.io
            .report_child_battery(request)
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::KernelRejected))
    }
}

impl<I: KernelIo> Drop for KernelReceiverTransport<I> {
    fn drop(&mut self) {
        let _ = self.end_active_session(HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
    }
}

fn decode_digest(value: &str) -> Result<[u8; 32], KernelTransportError> {
    if value.len() != 64
        || value
            .as_bytes()
            .iter()
            .any(|byte| !matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        return Err(profile_mismatch());
    }
    let mut decoded = [0_u8; 32];
    for (index, destination) in decoded.iter_mut().enumerate() {
        let offset = index * 2;
        *destination =
            (hex_nibble(value.as_bytes()[offset]) << 4) | hex_nibble(value.as_bytes()[offset + 1]);
    }
    Ok(decoded)
}

const fn hex_nibble(value: u8) -> u8 {
    match value {
        b'0'..=b'9' => value - b'0',
        b'a'..=b'f' => value - b'a' + 10,
        _ => 0,
    }
}

fn uapi_size<T>() -> Result<u32, KernelTransportError> {
    u32::try_from(size_of::<T>())
        .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::AbiMismatch))
}

fn io_error(_: crate::KernelIoError) -> KernelTransportError {
    KernelTransportError::uncertain(KernelTransportErrorKind::Io)
}

fn profile_mismatch() -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::ProfileMismatch)
}
