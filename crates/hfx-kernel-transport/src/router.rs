// SPDX-License-Identifier: GPL-2.0-only

use crate::{KernelTransportError, KernelTransportErrorKind};
use hfx_core::{ReceiverTransport, TransportDispatch, TransportReceipt, TransportReconciliation};
use hfx_domain::{GenerationId, QueueCapacity, ReceiverId};
use std::collections::BTreeMap;
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KernelRouteError {
    CapacityExhausted,
    TransportBindingMismatch,
}

impl fmt::Display for KernelRouteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::CapacityExhausted => "kernel receiver route capacity is exhausted",
            Self::TransportBindingMismatch => {
                "kernel writer transport does not match the receiver generation"
            }
        })
    }
}

impl std::error::Error for KernelRouteError {}

enum KernelRoute<T> {
    Observed(GenerationId),
    Writable {
        generation_id: GenerationId,
        transport: T,
    },
}

impl<T> KernelRoute<T> {
    const fn generation_id(&self) -> GenerationId {
        match self {
            Self::Observed(generation_id) | Self::Writable { generation_id, .. } => *generation_id,
        }
    }

    const fn is_writable(&self) -> bool {
        matches!(self, Self::Writable { .. })
    }
}

/// Bounded receiver routing for one bridge process.
///
/// A read-only route is enough for generation-bound lifecycle state. Only an
/// explicitly installed writer transport can reconcile or dispatch lighting.
/// Replacing a generation drops the old writer and therefore revokes its
/// kernel session before the new route becomes authoritative.
pub struct KernelTransportRouter<T> {
    capacity: usize,
    routes: BTreeMap<ReceiverId, KernelRoute<T>>,
}

impl<T> KernelTransportRouter<T> {
    #[must_use]
    pub fn new(capacity: QueueCapacity) -> Self {
        Self {
            capacity: usize::from(capacity.get()),
            routes: BTreeMap::new(),
        }
    }

    /// Adds or replaces one passively observed receiver generation.
    ///
    /// # Errors
    ///
    /// Returns a bounded capacity error without changing existing routes.
    pub fn observe(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
    ) -> Result<Option<GenerationId>, KernelRouteError> {
        if !self.routes.contains_key(&receiver_id) && self.routes.len() >= self.capacity {
            return Err(KernelRouteError::CapacityExhausted);
        }
        Ok(self
            .routes
            .insert(receiver_id, KernelRoute::Observed(generation_id))
            .map(|route| route.generation_id()))
    }

    /// Installs one already-admitted writer transport for an exact route.
    ///
    /// # Errors
    ///
    /// Rejects a transport that does not report the supplied receiver and
    /// generation, or a new route when the bounded registry is full.
    pub fn install_writable(
        &mut self,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        transport: T,
    ) -> Result<Option<GenerationId>, KernelRouteError>
    where
        T: ReceiverTransport<Error = KernelTransportError>,
    {
        if transport.current_generation(&receiver_id) != Some(generation_id) {
            return Err(KernelRouteError::TransportBindingMismatch);
        }
        if !self.routes.contains_key(&receiver_id) && self.routes.len() >= self.capacity {
            return Err(KernelRouteError::CapacityExhausted);
        }
        Ok(self
            .routes
            .insert(
                receiver_id,
                KernelRoute::Writable {
                    generation_id,
                    transport,
                },
            )
            .map(|route| route.generation_id()))
    }

    /// Removes only the exact generation named by the discovery boundary.
    pub fn remove(&mut self, receiver_id: &ReceiverId, generation_id: GenerationId) -> bool {
        if self
            .routes
            .get(receiver_id)
            .is_some_and(|route| route.generation_id() == generation_id)
        {
            self.routes.remove(receiver_id);
            true
        } else {
            false
        }
    }

    #[must_use]
    pub fn is_writable(&self, receiver_id: &ReceiverId) -> bool {
        self.routes
            .get(receiver_id)
            .is_some_and(KernelRoute::is_writable)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.routes.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.routes.is_empty()
    }
}

impl<T> ReceiverTransport for KernelTransportRouter<T>
where
    T: ReceiverTransport<Error = KernelTransportError>,
{
    type Error = KernelTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        let route = self.routes.get(receiver_id)?;
        match route {
            KernelRoute::Observed(generation_id) => Some(*generation_id),
            KernelRoute::Writable {
                generation_id,
                transport,
            } => (transport.current_generation(receiver_id) == Some(*generation_id))
                .then_some(*generation_id),
        }
    }

    fn write_available(&self, receiver_id: &ReceiverId, generation_id: GenerationId) -> bool {
        matches!(
            self.routes.get(receiver_id),
            Some(KernelRoute::Writable {
                generation_id: active,
                transport,
            }) if *active == generation_id
                && transport.current_generation(receiver_id) == Some(generation_id)
        )
    }

    fn reconcile(&self, dispatch: &TransportDispatch) -> TransportReconciliation {
        match self.routes.get(&dispatch.receiver_id) {
            Some(KernelRoute::Writable {
                generation_id,
                transport,
            }) if *generation_id == dispatch.generation_id => transport.reconcile(dispatch),
            _ => TransportReconciliation::Unavailable,
        }
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        match self.routes.get_mut(&dispatch.receiver_id) {
            Some(KernelRoute::Writable {
                generation_id,
                transport,
            }) if *generation_id == dispatch.generation_id => transport.dispatch(dispatch),
            _ => Err(KernelTransportError::safe(
                KernelTransportErrorKind::SessionUnavailable,
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_core::{TransportFailureFacts, TransportTerminal};
    use hfx_domain::{
        AuthorizationEpoch, ColorChannel, DeliveredFrameCount, DeviceApplicationState,
        DispatchNonce, FrameIndex, LedCount, LogicalDeviceId, ProfileDigest, ProfileId,
        SideEffectCertainty,
    };
    use hfx_protocol::{DeviceProfileBinding, LightingFrame, RgbColor};
    use std::cell::Cell;

    struct FakeTransport {
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        dispatches: Cell<usize>,
    }

    impl FakeTransport {
        fn new(receiver_id: &str, generation_id: u64) -> Self {
            Self {
                receiver_id: text(receiver_id),
                generation_id: generation(generation_id),
                dispatches: Cell::new(0),
            }
        }
    }

    impl ReceiverTransport for FakeTransport {
        type Error = KernelTransportError;

        fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
            (receiver_id == &self.receiver_id).then_some(self.generation_id)
        }

        fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
            TransportReconciliation::NotObserved
        }

        fn dispatch(
            &mut self,
            _dispatch: &TransportDispatch,
        ) -> Result<TransportReceipt, Self::Error> {
            self.dispatches.set(self.dispatches.get() + 1);
            Ok(TransportReceipt {
                terminal: TransportTerminal::Delivered,
                delivered_frames: DeliveredFrameCount::try_from(1_u16).expect("one frame is valid"),
                side_effect_certainty: SideEffectCertainty::Committed,
                live_write_executed: true,
                automatic_retry_safe: false,
                device_application: DeviceApplicationState::Unverified,
            })
        }
    }

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test identifier is valid")
    }

    fn generation(value: u64) -> GenerationId {
        GenerationId::try_from(value).expect("test generation is valid")
    }

    fn capacity(value: u16) -> QueueCapacity {
        QueueCapacity::try_from(value).expect("test capacity is valid")
    }

    fn dispatch(receiver_id: &str, generation_id: u64) -> TransportDispatch {
        let device_id: LogicalDeviceId = text("mouse-1");
        let channel = ColorChannel::try_from(1_u8).expect("test channel is valid");
        TransportDispatch {
            session_id: text("session-1"),
            authorization_epoch: AuthorizationEpoch::try_from(1_u64).expect("test epoch is valid"),
            dispatch_nonce: DispatchNonce::try_from(1_u64).expect("test nonce is valid"),
            receiver_id: text(receiver_id),
            generation_id: generation(generation_id),
            transaction_id: text("transaction-1"),
            request_digest: text(&"a".repeat(64)),
            receiver_profile_id: text::<ProfileId>("profile.receiver"),
            receiver_profile_digest: text::<ProfileDigest>(&"b".repeat(64)),
            device_profiles: vec![DeviceProfileBinding {
                device_id: device_id.clone(),
                profile_id: text("profile.mouse"),
                profile_digest: text(&"c".repeat(64)),
                application_slot_count: LedCount::try_from(1_u16).expect("test LED count is valid"),
            }],
            frames: vec![LightingFrame {
                device_id,
                frame_index: FrameIndex::try_from(0_u32).expect("zero frame is valid"),
                colors: vec![RgbColor {
                    red: channel,
                    green: channel,
                    blue: channel,
                }],
            }],
        }
    }

    #[test]
    fn observed_routes_expose_generation_but_never_write() {
        let receiver_id: ReceiverId = text("receiver-1");
        let mut router = KernelTransportRouter::<FakeTransport>::new(capacity(2));
        router
            .observe(receiver_id.clone(), generation(1))
            .expect("observed route fits");
        let request = dispatch("receiver-1", 1);

        assert_eq!(router.current_generation(&receiver_id), Some(generation(1)));
        assert!(!router.write_available(&receiver_id, generation(1)));
        assert!(!router.is_writable(&receiver_id));
        assert_eq!(
            router.reconcile(&request),
            TransportReconciliation::Unavailable
        );
        let error = router
            .dispatch(&request)
            .expect_err("read-only route rejects writes");
        assert_eq!(error.kind(), KernelTransportErrorKind::SessionUnavailable);
        assert_eq!(
            error.failure_facts(),
            TransportFailureFacts {
                delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("zero is valid"),
                side_effect_certainty: SideEffectCertainty::None,
                live_write_executed: false,
                automatic_retry_safe: true,
                device_application: DeviceApplicationState::Unverified,
            }
        );
    }

    #[test]
    fn writable_routes_dispatch_only_the_exact_generation() {
        let receiver_id: ReceiverId = text("receiver-1");
        let mut router = KernelTransportRouter::new(capacity(2));
        router
            .install_writable(
                receiver_id.clone(),
                generation(2),
                FakeTransport::new("receiver-1", 2),
            )
            .expect("writer route matches");

        assert!(router.is_writable(&receiver_id));
        assert!(router.write_available(&receiver_id, generation(2)));
        assert!(router.dispatch(&dispatch("receiver-1", 2)).is_ok());
        assert_eq!(
            router.reconcile(&dispatch("receiver-1", 1)),
            TransportReconciliation::Unavailable
        );
    }

    #[test]
    fn replacement_capacity_and_exact_removal_are_fail_closed() {
        let receiver_one: ReceiverId = text("receiver-1");
        let receiver_two: ReceiverId = text("receiver-2");
        let mut router = KernelTransportRouter::<FakeTransport>::new(capacity(1));

        assert_eq!(
            router
                .observe(receiver_one.clone(), generation(1))
                .expect("first route fits"),
            None
        );
        assert_eq!(
            router
                .observe(receiver_one.clone(), generation(2))
                .expect("same receiver is replaced"),
            Some(generation(1))
        );
        assert_eq!(
            router.observe(receiver_two, generation(1)),
            Err(KernelRouteError::CapacityExhausted)
        );
        assert!(!router.remove(&receiver_one, generation(1)));
        assert!(router.remove(&receiver_one, generation(2)));
        assert!(router.is_empty());
    }

    #[test]
    fn writer_binding_must_match_receiver_and_generation() {
        let mut router = KernelTransportRouter::new(capacity(1));
        assert_eq!(
            router.install_writable(
                text("receiver-1"),
                generation(2),
                FakeTransport::new("receiver-1", 1),
            ),
            Err(KernelRouteError::TransportBindingMismatch)
        );
        assert!(router.is_empty());
    }
}
