// SPDX-License-Identifier: GPL-2.0-only

use crate::observation::decode_observations;
use crate::{
    AlignedU64, HFX_UAPI_ABI_VERSION, HFX_UAPI_INFO_FLAG_DISCONNECTING,
    HFX_UAPI_INFO_FLAG_SESSION_ACTIVE, HFX_UAPI_INFO_FLAG_SUSPENDED,
    HFX_UAPI_INFO_FLAG_WRITER_OPEN, HfxUapiInfo, HfxUapiReadObservations, KernelIo,
    KernelObservationBatch, KernelTransportError, KernelTransportErrorKind, LinuxKernelIo,
};
use hfx_domain::{GenerationId, ProductId, VendorId};
use std::path::Path;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KernelEndpointInfo {
    pub generation_id: GenerationId,
    pub vendor_id: VendorId,
    pub product_id: ProductId,
    pub bound_interfaces: u16,
    pub flags: KernelEndpointFlags,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KernelEndpointFlags(u32);

impl KernelEndpointFlags {
    #[must_use]
    pub const fn disconnecting(self) -> bool {
        self.0 & HFX_UAPI_INFO_FLAG_DISCONNECTING != 0
    }

    #[must_use]
    pub const fn suspended(self) -> bool {
        self.0 & HFX_UAPI_INFO_FLAG_SUSPENDED != 0
    }

    #[must_use]
    pub const fn writer_open(self) -> bool {
        self.0 & HFX_UAPI_INFO_FLAG_WRITER_OPEN != 0
    }

    #[must_use]
    pub const fn session_active(self) -> bool {
        self.0 & HFX_UAPI_INFO_FLAG_SESSION_ACTIVE != 0
    }
}

/// Passive, generation-bound view of one kernel receiver endpoint.
pub struct KernelObservationReader<I: KernelIo> {
    io: I,
    binding: KernelEndpointInfo,
}

impl KernelObservationReader<LinuxKernelIo> {
    /// Opens one endpoint without requesting writer access.
    ///
    /// # Errors
    ///
    /// Returns a bounded error without exposing the private endpoint path.
    pub fn open(path: &Path) -> Result<Self, KernelTransportError> {
        let io = LinuxKernelIo::open_read_only(path)
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::Io))?;
        Self::new(io)
    }
}

impl<I: KernelIo> KernelObservationReader<I> {
    /// Validates and binds one injected passive kernel endpoint.
    ///
    /// # Errors
    ///
    /// Rejects incompatible ABI values and malformed receiver identity.
    pub fn new(io: I) -> Result<Self, KernelTransportError> {
        let binding = decode_info(&io.get_info().map_err(io_error)?)?;
        Ok(Self { io, binding })
    }

    #[must_use]
    pub const fn binding(&self) -> KernelEndpointInfo {
        self.binding
    }

    /// Returns the Linux boottime clock used by kernel observation stamps.
    ///
    /// # Errors
    ///
    /// Returns a bounded clock boundary failure.
    pub fn boottime_ns(&self) -> Result<u64, KernelTransportError> {
        self.io.boottime_ns().map_err(io_error)
    }

    /// Refreshes endpoint flags while requiring the original identity and
    /// generation to remain exact.
    ///
    /// # Errors
    ///
    /// Returns a typed stale or mismatched endpoint error.
    pub fn info(&self) -> Result<KernelEndpointInfo, KernelTransportError> {
        let info = decode_info(&self.io.get_info().map_err(io_error)?)?;
        if info.generation_id != self.binding.generation_id {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::GenerationMismatch,
            ));
        }
        if info.vendor_id != self.binding.vendor_id || info.product_id != self.binding.product_id {
            return Err(KernelTransportError::safe(
                KernelTransportErrorKind::ReceiverMismatch,
            ));
        }
        Ok(info)
    }

    /// Reads one bounded passive-observation batch after an exact cursor.
    ///
    /// # Errors
    ///
    /// Returns a typed I/O, ABI, or generation failure.
    pub fn read_observations(
        &self,
        after_sequence: u64,
    ) -> Result<KernelObservationBatch, KernelTransportError> {
        let mut request = HfxUapiReadObservations {
            version: HFX_UAPI_ABI_VERSION,
            size: u32::try_from(size_of::<HfxUapiReadObservations>())
                .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::AbiMismatch))?,
            receiver_generation: AlignedU64(self.binding.generation_id.get()),
            after_sequence: AlignedU64(after_sequence),
            ..HfxUapiReadObservations::default()
        };
        self.io.read_observations(&mut request).map_err(io_error)?;
        decode_observations(self.binding.generation_id, after_sequence, &request)
    }
}

fn decode_info(info: &HfxUapiInfo) -> Result<KernelEndpointInfo, KernelTransportError> {
    if info.version != HFX_UAPI_ABI_VERSION
        || usize::try_from(info.size).ok() != Some(size_of::<HfxUapiInfo>())
        || info.flags
            & !(HFX_UAPI_INFO_FLAG_DISCONNECTING
                | HFX_UAPI_INFO_FLAG_SUSPENDED
                | HFX_UAPI_INFO_FLAG_WRITER_OPEN
                | HFX_UAPI_INFO_FLAG_SESSION_ACTIVE)
            != 0
    {
        return Err(KernelTransportError::safe(
            KernelTransportErrorKind::AbiMismatch,
        ));
    }
    Ok(KernelEndpointInfo {
        generation_id: GenerationId::try_from(info.receiver_generation.0).map_err(|_| {
            KernelTransportError::safe(KernelTransportErrorKind::GenerationMismatch)
        })?,
        vendor_id: VendorId::try_from(info.vendor_id)
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::ReceiverMismatch))?,
        product_id: ProductId::try_from(info.product_id)
            .map_err(|_| KernelTransportError::safe(KernelTransportErrorKind::ReceiverMismatch))?,
        bound_interfaces: info.bound_interfaces,
        flags: KernelEndpointFlags(info.flags),
    })
}

fn io_error(_: crate::KernelIoError) -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::Io)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        HfxUapiBeginSession, HfxUapiEndSession, HfxUapiSubmit, HfxUapiTransactionResult,
        KernelIoError,
    };
    use std::cell::Cell;

    struct FakeIo {
        info: Cell<HfxUapiInfo>,
    }

    impl FakeIo {
        fn valid() -> Self {
            Self {
                info: Cell::new(HfxUapiInfo {
                    version: HFX_UAPI_ABI_VERSION,
                    size: u32::try_from(size_of::<HfxUapiInfo>()).expect("info size fits"),
                    receiver_generation: AlignedU64(7),
                    vendor_id: 0x1532,
                    product_id: 0x00cf,
                    bound_interfaces: 6,
                    ..HfxUapiInfo::default()
                }),
            }
        }
    }

    impl KernelIo for FakeIo {
        fn boottime_ns(&self) -> Result<u64, KernelIoError> {
            Ok(1)
        }

        fn get_info(&self) -> Result<HfxUapiInfo, KernelIoError> {
            Ok(self.info.get())
        }

        fn begin_session(&self, _: &mut HfxUapiBeginSession) -> Result<(), KernelIoError> {
            Err(KernelIoError::from_raw_os_error(1))
        }

        fn end_session(&self, _: HfxUapiEndSession) -> Result<(), KernelIoError> {
            Err(KernelIoError::from_raw_os_error(1))
        }

        fn submit(&self, _: &mut HfxUapiSubmit) -> Result<(), KernelIoError> {
            Err(KernelIoError::from_raw_os_error(1))
        }

        fn get_transaction_result(
            &self,
            _: &mut HfxUapiTransactionResult,
        ) -> Result<(), KernelIoError> {
            Err(KernelIoError::from_raw_os_error(1))
        }

        fn read_observations(&self, _: &mut HfxUapiReadObservations) -> Result<(), KernelIoError> {
            Err(KernelIoError::from_raw_os_error(1))
        }
    }

    #[test]
    fn passive_reader_binds_exact_identity_without_opening_a_session() {
        let reader = KernelObservationReader::new(FakeIo::valid()).expect("reader binds");
        let info = reader.binding();
        assert_eq!(info.generation_id.get(), 7);
        assert_eq!(info.vendor_id.get(), 0x1532);
        assert_eq!(info.product_id.get(), 0x00cf);
        assert_eq!(info.bound_interfaces, 6);
        assert!(!info.flags.disconnecting());
        assert!(!info.flags.suspended());
        assert!(!info.flags.writer_open());
        assert!(!info.flags.session_active());
    }

    #[test]
    fn changed_generation_and_unknown_flags_fail_closed() {
        let io = FakeIo::valid();
        let reader = KernelObservationReader::new(io).expect("reader binds");
        let mut changed = reader.io.info.get();
        changed.receiver_generation = AlignedU64(8);
        reader.io.info.set(changed);
        assert_eq!(
            reader.info().expect_err("generation changes"),
            KernelTransportError::safe(KernelTransportErrorKind::GenerationMismatch)
        );

        let mut invalid = FakeIo::valid().info.get();
        invalid.flags = 1 << 31;
        assert!(matches!(
            KernelObservationReader::new(FakeIo {
                info: Cell::new(invalid)
            }),
            Err(error) if error.kind() == KernelTransportErrorKind::AbiMismatch
        ));
    }
}
