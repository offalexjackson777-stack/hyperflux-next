// SPDX-License-Identifier: GPL-2.0-only

use crate::observation::decode_observations;
use crate::{
    AlignedU64, EncodedTransaction, HFX_UAPI_ABI_VERSION, HFX_UAPI_INFO_FLAG_DISCONNECTING,
    HFX_UAPI_INFO_FLAG_SESSION_ACTIVE, HFX_UAPI_INFO_FLAG_SUSPENDED, HFX_UAPI_MAX_SESSION_NS,
    HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE, HFX_UAPI_RESULT_FLAG_WRITE_STARTED,
    HFX_UAPI_REVOKE_REASON_EXPLICIT, HFX_UAPI_REVOKE_REASON_SERVICE_LOSS,
    HFX_UAPI_TRANSPORT_STATUS_CONFLICT, HFX_UAPI_TRANSPORT_STATUS_EVICTED,
    HFX_UAPI_TRANSPORT_STATUS_FAILED, HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED,
    HFX_UAPI_TRANSPORT_STATUS_RESERVED, HFX_UAPI_TRANSPORT_STATUS_REVOKED,
    HFX_UAPI_TRANSPORT_STATUS_STARTED, HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED,
    HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE, HfxUapiBeginSession, HfxUapiEndSession, HfxUapiInfo,
    HfxUapiReadObservations, HfxUapiSubmit, HfxUapiTransactionResult, KernelIo,
    KernelObservationBatch, KernelTransportError, KernelTransportErrorKind, LinuxKernelIo,
    ReceiverFrameEncoderRegistry,
};
use hfx_core::{
    ReceiverTransport, TransportDispatch, TransportFailureFacts, TransportReceipt,
    TransportReconciliation, TransportTerminal,
};
use hfx_domain::{
    DeliveredFrameCount, DeviceApplicationState, GenerationId, ProfileDigest, ProfileId,
    ProfileKind, ReceiverId, SideEffectCertainty,
};
use hfx_profiles::RuntimeProfileCatalog;
use serde_json::to_vec;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::path::Path;
use std::sync::Mutex;
use std::time::Duration;

const SESSION_RENEWAL_MARGIN_NS: u64 = 5_000_000_000;
const DISPATCH_BINDING_CAPACITY: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KernelSessionMaterial {
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub receiver_profile_id: ProfileId,
    pub receiver_profile_digest: ProfileDigest,
    pub capability_digest: [u8; 32],
    pub daemon_nonce: [u8; 32],
    pub session_duration: Duration,
}

impl KernelSessionMaterial {
    fn validate(&self) -> Result<u64, KernelTransportError> {
        let duration = u64::try_from(self.session_duration.as_nanos()).map_err(|_| {
            KernelTransportError::safe(KernelTransportErrorKind::InvalidSessionMaterial)
        })?;
        if duration <= SESSION_RENEWAL_MARGIN_NS
            || duration > HFX_UAPI_MAX_SESSION_NS
            || self.capability_digest.iter().all(|byte| *byte == 0)
            || self.daemon_nonce.iter().all(|byte| *byte == 0)
        {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::InvalidSessionMaterial,
            ));
        }
        Ok(duration)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct KernelSessionState {
    authorization_epoch: u64,
    expires_boottime_ns: u64,
    active: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DispatchBinding {
    fingerprint: [u8; 32],
    authorization_epoch: u64,
}

#[derive(Debug, Default)]
struct DispatchHistory {
    maximum_nonce: u64,
    bindings: BTreeMap<u64, DispatchBinding>,
    order: VecDeque<u64>,
}

enum DispatchLookup {
    Known(u64),
    New(u64),
    Forgotten,
    Conflict,
}

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

    fn validate_binding_info(&self, info: &HfxUapiInfo) -> Result<(), KernelTransportError> {
        if info.version != HFX_UAPI_ABI_VERSION
            || usize::try_from(info.size).ok() != Some(size_of::<HfxUapiInfo>())
        {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::AbiMismatch,
            ));
        }
        if info.vendor_id != self.receiver_vendor_id || info.product_id != self.receiver_product_id
        {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::ReceiverMismatch,
            ));
        }
        if info.receiver_generation.0 != self.material.generation_id.get() {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::GenerationMismatch,
            ));
        }
        Ok(())
    }

    fn validate_writable_info(&self, info: &HfxUapiInfo) -> Result<(), KernelTransportError> {
        self.validate_binding_info(info)?;
        if info.flags & (HFX_UAPI_INFO_FLAG_DISCONNECTING | HFX_UAPI_INFO_FLAG_SUSPENDED) != 0
            || info.bound_interfaces == 0
        {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::SessionUnavailable,
            ));
        }
        Ok(())
    }

    fn ensure_session(&self) -> Result<u64, KernelTransportError> {
        let now = self.io.boottime_ns().map_err(io_error)?;
        let info = self.io.get_info().map_err(io_error)?;
        self.validate_writable_info(&info)?;
        let mut state = self.session.lock().map_err(|_| {
            KernelTransportError::uncertain(KernelTransportErrorKind::SessionUnavailable)
        })?;
        let kernel_active = info.flags & HFX_UAPI_INFO_FLAG_SESSION_ACTIVE != 0;
        if state.active
            && kernel_active
            && info.authorization_epoch.0 == state.authorization_epoch
            && now.saturating_add(SESSION_RENEWAL_MARGIN_NS) < state.expires_boottime_ns
        {
            return Ok(state.authorization_epoch);
        }
        if kernel_active {
            if !state.active || info.authorization_epoch.0 != state.authorization_epoch {
                return Err(KernelTransportError::uncertain(
                    KernelTransportErrorKind::SessionUnavailable,
                ));
            }
            self.end_session_record(*state, HFX_UAPI_REVOKE_REASON_EXPLICIT)?;
            state.active = false;
        }
        let expires = now.checked_add(self.session_duration_ns).ok_or_else(|| {
            KernelTransportError::safe(KernelTransportErrorKind::InvalidSessionMaterial)
        })?;
        let mut begin = HfxUapiBeginSession {
            version: HFX_UAPI_ABI_VERSION,
            size: uapi_size::<HfxUapiBeginSession>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            expires_boottime_ns: AlignedU64(expires),
            profile_digest: self.profile_digest,
            capability_digest: self.material.capability_digest,
            daemon_nonce: self.material.daemon_nonce,
            authorization_epoch: AlignedU64(0),
        };
        self.io.begin_session(&mut begin).map_err(io_error)?;
        if begin.authorization_epoch.0 == 0 {
            return Err(KernelTransportError::uncertain(
                KernelTransportErrorKind::SessionUnavailable,
            ));
        }
        *state = KernelSessionState {
            authorization_epoch: begin.authorization_epoch.0,
            expires_boottime_ns: expires,
            active: true,
        };
        Ok(state.authorization_epoch)
    }

    fn end_active_session(&self, reason: u32) -> Result<(), KernelTransportError> {
        let mut state = self.session.lock().map_err(|_| {
            KernelTransportError::uncertain(KernelTransportErrorKind::SessionUnavailable)
        })?;
        if !state.active {
            return Ok(());
        }
        self.end_session_record(*state, reason)?;
        state.active = false;
        Ok(())
    }

    fn end_session_record(
        &self,
        state: KernelSessionState,
        reason: u32,
    ) -> Result<(), KernelTransportError> {
        let request = HfxUapiEndSession {
            version: HFX_UAPI_ABI_VERSION,
            size: uapi_size::<HfxUapiEndSession>()?,
            receiver_generation: AlignedU64(self.material.generation_id.get()),
            authorization_epoch: AlignedU64(state.authorization_epoch),
            reason,
            reserved: 0,
        };
        self.io.end_session(request).map_err(io_error)
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

    fn lookup_dispatch(
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

    fn remember_dispatch(
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

    fn reconcile_exact(
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

    fn submit_new(
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
}

impl<I: KernelIo> Drop for KernelReceiverTransport<I> {
    fn drop(&mut self) {
        let _ = self.end_active_session(HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
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

fn dispatch_fingerprint(
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

fn semantic_count(value: usize) -> Result<DeliveredFrameCount, KernelTransportError> {
    let value = u16::try_from(value)
        .map_err(|_| KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeConflict))?;
    DeliveredFrameCount::try_from(value)
        .map_err(|_| KernelTransportError::uncertain(KernelTransportErrorKind::OutcomeConflict))
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
