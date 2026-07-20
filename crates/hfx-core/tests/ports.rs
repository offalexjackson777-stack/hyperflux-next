// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::{generation, receiver_profile_digest, receiver_profile_id, text, time};
use hfx_core::{
    Clock, ReceiverTransport, TransportDispatch, TransportFailure, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal,
};
use hfx_domain::{DeliveredFrameCount, DeviceApplicationState, SideEffectCertainty};

struct FakeClock(hfx_domain::MonotonicMs);

impl Clock for FakeClock {
    fn now(&self) -> hfx_domain::MonotonicMs {
        self.0
    }
}

struct FakeTransport {
    receiver: hfx_domain::ReceiverId,
    generation: hfx_domain::GenerationId,
    captured: Option<TransportDispatch>,
}

#[derive(Clone, Copy, Debug)]
struct FakeTransportError;

impl TransportFailure for FakeTransportError {
    fn facts(&self) -> TransportFailureFacts {
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("frame count is valid"),
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: DeviceApplicationState::Unverified,
        }
    }
}

impl ReceiverTransport for FakeTransport {
    type Error = FakeTransportError;

    fn current_generation(
        &self,
        receiver_id: &hfx_domain::ReceiverId,
    ) -> Option<hfx_domain::GenerationId> {
        (receiver_id == &self.receiver).then_some(self.generation)
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        TransportReconciliation::NotObserved
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.captured = Some(dispatch.clone());
        Ok(TransportReceipt {
            terminal: TransportTerminal::Delivered,
            delivered_frames: DeliveredFrameCount::try_from(0_u16).expect("frame count is valid"),
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry_safe: false,
            device_application: DeviceApplicationState::Unverified,
        })
    }
}

#[test]
fn core_ports_are_deterministic_and_transport_bindings_remain_distinct() {
    let clock = FakeClock(time(42));
    assert_eq!(clock.now().get(), 42);
    let mut transport = FakeTransport {
        receiver: text("receiver-1"),
        generation: generation(7),
        captured: None,
    };
    let dispatch = TransportDispatch {
        session_id: text("session-1"),
        authorization_epoch: hfx_domain::AuthorizationEpoch::try_from(3_u64)
            .expect("epoch is valid"),
        dispatch_nonce: hfx_domain::DispatchNonce::try_from(9_u64).expect("nonce is valid"),
        receiver_id: text("receiver-1"),
        generation_id: generation(7),
        transaction_id: text("transaction-1"),
        request_digest: text(&"a".repeat(64)),
        receiver_profile_id: receiver_profile_id(),
        receiver_profile_digest: receiver_profile_digest(),
        device_profiles: Vec::new(),
        frames: Vec::new(),
    };
    let receipt = transport
        .dispatch(&dispatch)
        .expect("fake transport delivers");
    assert_eq!(receipt.terminal, TransportTerminal::Delivered);
    let captured = transport.captured.expect("dispatch is captured");
    assert_eq!(captured.session_id.as_str(), "session-1");
    assert_eq!(captured.authorization_epoch.get(), 3);
    assert_eq!(captured.dispatch_nonce.get(), 9);
    assert_eq!(captured.generation_id.get(), 7);
}
