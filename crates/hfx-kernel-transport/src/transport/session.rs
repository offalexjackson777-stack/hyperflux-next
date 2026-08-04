// SPDX-License-Identifier: GPL-2.0-only

//! Generation-scoped kernel authorization session lifecycle.

use super::{KernelReceiverTransport, io_error, uapi_size};
use crate::{
    AlignedU64, HFX_UAPI_ABI_VERSION, HFX_UAPI_INFO_FLAG_DISCONNECTING,
    HFX_UAPI_INFO_FLAG_SESSION_ACTIVE, HFX_UAPI_INFO_FLAG_SUSPENDED, HFX_UAPI_MAX_SESSION_NS,
    HFX_UAPI_REVOKE_REASON_EXPLICIT, HfxUapiBeginSession, HfxUapiEndSession, HfxUapiInfo, KernelIo,
    KernelTransportError, KernelTransportErrorKind,
};
use hfx_domain::{GenerationId, ProfileDigest, ProfileId, ReceiverId};
use std::mem::size_of;
use std::time::Duration;

const SESSION_RENEWAL_MARGIN_NS: u64 = 5_000_000_000;

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
    pub(super) fn validate(&self) -> Result<u64, KernelTransportError> {
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
pub(super) struct KernelSessionState {
    authorization_epoch: u64,
    expires_boottime_ns: u64,
    active: bool,
}

impl<I: KernelIo> KernelReceiverTransport<I> {
    pub(super) fn validate_binding_info(
        &self,
        info: &HfxUapiInfo,
    ) -> Result<(), KernelTransportError> {
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

    pub(super) fn validate_writable_info(
        &self,
        info: &HfxUapiInfo,
    ) -> Result<(), KernelTransportError> {
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

    pub(super) fn ensure_session(&self) -> Result<u64, KernelTransportError> {
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

    pub(super) fn end_active_session(&self, reason: u32) -> Result<(), KernelTransportError> {
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
}
