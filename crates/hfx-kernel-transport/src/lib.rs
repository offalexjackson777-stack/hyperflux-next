// SPDX-License-Identifier: GPL-2.0-only

#![deny(unsafe_code)]

mod encoder;
mod error;
mod generated;
mod io;
#[allow(unsafe_code)]
mod ioctl_sys;
mod observation;
mod reader;
mod router;
mod transport;

pub use encoder::{EncodedTransaction, ReceiverFrameEncoderRegistry};
pub use error::{KernelTransportError, KernelTransportErrorKind};
pub use generated::*;
pub use io::{KernelIo, KernelIoError, LinuxKernelIo};
pub use observation::{KernelObservationBatch, RawKernelObservation};
pub use reader::{KernelEndpointFlags, KernelEndpointInfo, KernelObservationReader};
pub use router::{KernelRouteError, KernelTransportRouter};
pub use transport::{KernelReceiverTransport, KernelSessionMaterial};

#[cfg(test)]
mod tests {
    use core::mem::{align_of, size_of};

    use super::*;

    #[test]
    fn generated_layout_matches_the_canonical_uapi() {
        assert_eq!(size_of::<AlignedU64>(), 8);
        assert_eq!(align_of::<AlignedU64>(), 8);
        assert_eq!(size_of::<HfxUapiInfo>(), 40);
        assert_eq!(size_of::<HfxUapiBeginSession>(), 128);
        assert_eq!(size_of::<HfxUapiEndSession>(), 32);
        assert_eq!(size_of::<HfxUapiFrame>(), 112);
        assert_eq!(size_of::<HfxUapiSubmit>(), 1_872);
        assert_eq!(size_of::<HfxUapiTransactionResult>(), 104);
        assert_eq!(size_of::<HfxUapiObservation>(), 40);
        assert_eq!(size_of::<HfxUapiReadObservations>(), 1_328);
    }

    #[test]
    fn ioctl_descriptors_use_bounded_generated_structures() {
        let descriptors = [
            HFX_UAPI_IOCTL_GET_INFO,
            HFX_UAPI_IOCTL_BEGIN_SESSION,
            HFX_UAPI_IOCTL_END_SESSION,
            HFX_UAPI_IOCTL_SUBMIT,
            HFX_UAPI_IOCTL_GET_TRANSACTION_RESULT,
            HFX_UAPI_IOCTL_READ_OBSERVATIONS,
        ];
        assert!(
            descriptors
                .iter()
                .all(|descriptor| descriptor.size < (1 << 14))
        );
        assert_eq!(HFX_UAPI_IOCTL_MAGIC, 0xb7);
        assert_eq!(HFX_UAPI_ABI_VERSION, 1);
    }
}
