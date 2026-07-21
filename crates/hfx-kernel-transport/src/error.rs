// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{TransportFailure, TransportFailureFacts};
use hfx_domain::{DeliveredFrameCount, DeviceApplicationState, SideEffectCertainty};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KernelTransportErrorKind {
    Io,
    AbiMismatch,
    ReceiverMismatch,
    GenerationMismatch,
    ProfileMismatch,
    UnsupportedBackend,
    InvalidSessionMaterial,
    SessionUnavailable,
    InvalidDispatch,
    Encoding,
    OutcomeRetainedFailure,
    OutcomeEvicted,
    OutcomeUnavailable,
    OutcomeConflict,
    KernelRejected,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KernelTransportError {
    kind: KernelTransportErrorKind,
    facts: TransportFailureFacts,
}

impl KernelTransportError {
    #[must_use]
    pub const fn kind(&self) -> KernelTransportErrorKind {
        self.kind
    }

    #[must_use]
    pub const fn failure_facts(&self) -> TransportFailureFacts {
        self.facts
    }

    pub(crate) fn safe(kind: KernelTransportErrorKind) -> Self {
        Self {
            kind,
            facts: safe_facts(),
        }
    }

    pub(crate) fn uncertain(kind: KernelTransportErrorKind) -> Self {
        Self {
            kind,
            facts: uncertain_facts(),
        }
    }

    pub(crate) const fn retained(
        kind: KernelTransportErrorKind,
        facts: TransportFailureFacts,
    ) -> Self {
        Self { kind, facts }
    }
}

impl fmt::Display for KernelTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self.kind {
            KernelTransportErrorKind::Io => "kernel transport I/O failed",
            KernelTransportErrorKind::AbiMismatch => "kernel transport ABI is incompatible",
            KernelTransportErrorKind::ReceiverMismatch => {
                "kernel transport belongs to another receiver"
            }
            KernelTransportErrorKind::GenerationMismatch => "kernel transport generation is stale",
            KernelTransportErrorKind::ProfileMismatch => {
                "kernel transport profile binding is invalid"
            }
            KernelTransportErrorKind::UnsupportedBackend => {
                "kernel transport backend is unsupported"
            }
            KernelTransportErrorKind::InvalidSessionMaterial => {
                "kernel transport session material is invalid"
            }
            KernelTransportErrorKind::SessionUnavailable => {
                "kernel transport session is unavailable"
            }
            KernelTransportErrorKind::InvalidDispatch => "kernel transport dispatch is invalid",
            KernelTransportErrorKind::Encoding => "kernel transport dispatch cannot be encoded",
            KernelTransportErrorKind::OutcomeRetainedFailure => {
                "kernel transport retained a failed outcome"
            }
            KernelTransportErrorKind::OutcomeEvicted => "kernel transport outcome was evicted",
            KernelTransportErrorKind::OutcomeUnavailable => {
                "kernel transport outcome is unavailable"
            }
            KernelTransportErrorKind::OutcomeConflict => {
                "kernel transport outcome identity conflicts"
            }
            KernelTransportErrorKind::KernelRejected => "kernel transport rejected the dispatch",
        })
    }
}

impl std::error::Error for KernelTransportError {}

impl TransportFailure for KernelTransportError {
    fn facts(&self) -> TransportFailureFacts {
        self.facts
    }
}

pub(crate) fn safe_facts() -> TransportFailureFacts {
    TransportFailureFacts {
        delivered_frames: zero_delivered(),
        side_effect_certainty: SideEffectCertainty::None,
        live_write_executed: false,
        automatic_retry_safe: true,
        device_application: DeviceApplicationState::Unverified,
    }
}

pub(crate) fn uncertain_facts() -> TransportFailureFacts {
    TransportFailureFacts {
        delivered_frames: zero_delivered(),
        side_effect_certainty: SideEffectCertainty::Possible,
        live_write_executed: true,
        automatic_retry_safe: false,
        device_application: DeviceApplicationState::Unverified,
    }
}

pub(crate) fn zero_delivered() -> DeliveredFrameCount {
    DeliveredFrameCount::try_from(0_u16).expect("zero is a canonical delivered-frame count")
}
