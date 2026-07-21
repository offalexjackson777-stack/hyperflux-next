// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    BridgeSessionConfig, ConnectionDispatcher, CoreBridgeBackend, CoreBridgeConfig,
    DisabledRestorationSource, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationRuntime, RestorationSnapshotSource, RuntimeProfileAuthority, SessionIdentityError,
    SessionIdentitySource, SessionRegistry, StableCaptureStatus,
};
use hfx_core::{
    ChildIdentity, Clock, CompletedTransaction, EndpointIdentity, EventDelivery, EventSink,
    LeaseManager, LifecycleLimits, ObservationStamp, PersistenceOperation, ProfileRegistry,
    ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverTransport, RestorationError,
    RestoreGenerationRetirement, StableCommitOutcome, TransactionCoordinator, TransportDispatch,
    TransportFailure, TransportFailureFacts, TransportReceipt, TransportReconciliation,
    TransportTerminal, WallClock,
};
use hfx_domain::{
    ClientId, ClientName, ColorChannel, ComponentVersion, ConnectionMode, DeliveredFrameCount,
    DeviceApplicationState, DeviceKind, EventBatchLimit, EventKind, EvidenceClaimId,
    EvidenceConfidence, FrameIndex, GenerationId, LeaseDurationMs, LogicalDeviceId, MonotonicMs,
    ProductId, ProjectionRevision, ProtocolErrorKind, ProtocolFeatureId, ProtocolVersion,
    QueueCapacity, ReceiverId, RequestId, ResourceKind, RouteKind, RouteState, SequenceNumber,
    ServerInstanceId, SideEffectCertainty, StableLightingMode, StreamEpoch, StreamId,
    TransactionClass, TransactionId, TransactionState, VendorId, WallClockUnixMs,
};
use hfx_protocol::{
    DeviceProfileBinding, EmptyRequest, EventCursor, LeaseRequest, LeaseResult, LightingFrame,
    NegotiationRequestEnvelope, ResourceKey, RgbColor, RpcRequest, RpcResponse,
    SessionRequestEnvelope, StableLightingIntent, SubscriptionRequest, TransactionLookup,
    TransactionRequest, TransactionResult,
};

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity is canonical")
}

fn generation(value: u64) -> GenerationId {
    GenerationId::try_from(value).expect("test generation is canonical")
}

fn time(value: u64) -> MonotonicMs {
    MonotonicMs::try_from(value).expect("test time is canonical")
}

fn stamp(sequence: u64) -> ObservationStamp {
    ObservationStamp::new(
        generation(1),
        SequenceNumber::try_from(sequence).expect("sequence is canonical"),
        time(sequence),
        EvidenceConfidence::Observed,
        text::<EvidenceClaimId>(&format!("claim-{sequence}")),
    )
    .expect("stamp is canonical")
}

#[derive(Clone, Copy, Debug)]
struct TestClock(MonotonicMs);

impl Clock for TestClock {
    fn now(&self) -> MonotonicMs {
        self.0
    }
}

#[derive(Clone, Copy, Debug)]
struct TestWallClock(WallClockUnixMs);

impl WallClock for TestWallClock {
    fn now_unix_ms(&self) -> WallClockUnixMs {
        self.0
    }
}

#[derive(Clone, Debug)]
struct TestTransport {
    receiver_id: ReceiverId,
    generation_id: GenerationId,
    dispatches: Vec<TransportDispatch>,
}

#[derive(Clone, Copy, Debug)]
struct TestTransportError;

impl TransportFailure for TestTransportError {
    fn facts(&self) -> TransportFailureFacts {
        TransportFailureFacts {
            delivered_frames: DeliveredFrameCount::try_from(0_u16)
                .expect("zero frames is canonical"),
            side_effect_certainty: SideEffectCertainty::None,
            live_write_executed: false,
            automatic_retry_safe: true,
            device_application: DeviceApplicationState::Unverified,
        }
    }
}

impl ReceiverTransport for TestTransport {
    type Error = TestTransportError;

    fn current_generation(&self, receiver_id: &ReceiverId) -> Option<GenerationId> {
        (receiver_id == &self.receiver_id).then_some(self.generation_id)
    }

    fn reconcile(&self, _dispatch: &TransportDispatch) -> TransportReconciliation {
        TransportReconciliation::NotObserved
    }

    fn dispatch(&mut self, dispatch: &TransportDispatch) -> Result<TransportReceipt, Self::Error> {
        self.dispatches.push(dispatch.clone());
        Ok(TransportReceipt {
            terminal: TransportTerminal::Delivered,
            delivered_frames: DeliveredFrameCount::try_from(
                u16::try_from(dispatch.frames.len()).expect("frame count fits"),
            )
            .expect("frame count is canonical"),
            side_effect_certainty: SideEffectCertainty::Committed,
            live_write_executed: true,
            automatic_retry_safe: false,
            device_application: DeviceApplicationState::Unverified,
        })
    }
}

#[derive(Debug, Default)]
struct TestEventSink(Vec<hfx_protocol::BridgeEvent>);

impl EventSink for TestEventSink {
    fn try_emit(&mut self, event: &hfx_protocol::BridgeEvent) -> EventDelivery {
        self.0.push(event.clone());
        EventDelivery::Accepted
    }
}

#[derive(Debug)]
struct DeterministicIdentities(u8);

impl SessionIdentitySource for DeterministicIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.0;
            self.0 = self.0.wrapping_add(1);
        }
        Ok(())
    }
}

type TestBackend<R = DisabledRestorationSource> =
    CoreBridgeBackend<TestClock, TestWallClock, TestTransport, R, TestEventSink>;

fn runtime_state() -> (ReceiverLifecycleRegistry, RuntimeProfileAuthority) {
    let mut machine = ReceiverLifecycleMachine::new(text("receiver-1"), LifecycleLimits::default())
        .expect("lifecycle initializes");
    machine.discover(stamp(1));
    let mouse_id: LogicalDeviceId = text("mouse");
    machine
        .register_device(
            ChildIdentity::new(
                mouse_id.clone(),
                DeviceKind::Mouse,
                ProductId::try_from(0x00cd_u16).expect("product id is canonical"),
            )
            .expect("mouse identity is canonical"),
            stamp(2),
        )
        .expect("mouse registers");
    machine
        .register_endpoint(
            &mouse_id,
            EndpointIdentity::new(
                text("mouse-hyperflux"),
                RouteKind::HyperfluxWireless,
                ConnectionMode::Hyperflux24ghz,
            )
            .expect("endpoint is canonical"),
            stamp(3),
        )
        .expect("endpoint registers");
    machine
        .observe_route(
            &mouse_id,
            &text("mouse-hyperflux"),
            RouteState::Available,
            stamp(4),
        )
        .expect("mouse route becomes available");
    let mut receivers = ReceiverLifecycleRegistry::default();
    receivers.register(machine).expect("receiver registers");
    let mut profiles = RuntimeProfileAuthority::load(4).expect("profiles load");
    profiles
        .bind_receiver(
            text("receiver-1"),
            generation(1),
            VendorId::try_from(0x1532_u16).expect("vendor id is canonical"),
            ProductId::try_from(0x00cf_u16).expect("product id is canonical"),
        )
        .expect("receiver profile binds");
    (receivers, profiles)
}

fn backend_with_restoration<R: RestorationRuntime>(restoration: R) -> TestBackend<R> {
    let (receivers, profiles) = runtime_state();
    let capacity = QueueCapacity::try_from(16_u16).expect("capacity is canonical");
    CoreBridgeBackend::new(
        CoreBridgeConfig {
            lifecycle_limits: LifecycleLimits::default(),
            lease_capacity: capacity,
            lease_history_capacity: capacity,
            transaction_capacity: capacity,
            event_capacity: capacity,
            diagnostic_capacity: capacity,
            subscription_capacity: capacity,
            stream_id: text::<StreamId>("stream-1"),
            stream_epoch: StreamEpoch::try_from(1_u64).expect("stream epoch is canonical"),
            projection_revision: ProjectionRevision::try_from(1_u32)
                .expect("projection revision is canonical"),
        },
        TestClock(time(100)),
        TestWallClock(WallClockUnixMs::try_from(1_000_u64).expect("wall-clock time is canonical")),
        TestTransport {
            receiver_id: text("receiver-1"),
            generation_id: generation(1),
            dispatches: Vec::new(),
        },
        restoration,
        &mut DeterministicIdentities(0),
        receivers,
        profiles,
        TestEventSink::default(),
    )
    .expect("backend composes")
}

fn backend() -> TestBackend {
    backend_with_restoration(DisabledRestorationSource)
}

#[derive(Clone, Copy, Debug, Default)]
struct RejectingRestoration;

impl RestorationSnapshotSource for RejectingRestoration {
    fn restoration(
        &self,
        _receiver_id: &ReceiverId,
        _generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        Ok(ReceiverRestorationSnapshot {
            stable_restore_enabled: false,
            restore_state: hfx_domain::RestoreState::Idle,
        })
    }
}

impl RestorationRuntime for RejectingRestoration {
    fn capture_completed(
        &mut self,
        _completed: &CompletedTransaction,
        _captured_at: WallClockUnixMs,
    ) -> Result<StableCommitOutcome, RestorationError> {
        Err(RestorationError::Persistence(
            PersistenceOperation::SaveIntent,
        ))
    }

    fn retire_generation<T, E>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        _now: MonotonicMs,
        _transport: &T,
        _leases: &mut LeaseManager,
        _transactions: &TransactionCoordinator,
        _events: &mut hfx_core::BoundedEventLog,
        _sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink,
    {
        Ok(RestoreGenerationRetirement {
            receiver_id: receiver_id.clone(),
            generation_id,
            updated: Vec::new(),
            already_terminal: 0,
        })
    }
}

fn new_dispatcher() -> ConnectionDispatcher {
    ConnectionDispatcher::new(BridgeSessionConfig {
        server_instance_id: text::<ServerInstanceId>("server-1"),
        bridge_version: text::<ComponentVersion>("0.0.0-test"),
        event_buffer_capacity: QueueCapacity::try_from(16_u16).expect("capacity is canonical"),
    })
}

fn negotiate<R: RestorationRuntime>(
    dispatcher: &mut ConnectionDispatcher,
    identities: &mut DeterministicIdentities,
    sessions: &mut SessionRegistry,
    backend: &mut TestBackend<R>,
) -> hfx_protocol::ServerHello {
    let features = [
        "ownership-leases",
        "atomic-transactions",
        "event-subscriptions",
        "profile-bound-transactions",
        "semantic-stable-lighting",
        "structured-diagnostics",
    ];
    let response = dispatcher.dispatch(
        RpcRequest::Negotiate(NegotiationRequestEnvelope {
            request_id: text::<RequestId>("request-negotiate"),
            params: hfx_protocol::ClientHello {
                client_id: text::<ClientId>("client-1"),
                client_name: text::<ClientName>("Core backend test"),
                minimum_version: ProtocolVersion::try_from(1_u16).expect("version is canonical"),
                maximum_version: ProtocolVersion::try_from(3_u16).expect("version is canonical"),
                required_features: Vec::new(),
                optional_features: features
                    .into_iter()
                    .map(text::<ProtocolFeatureId>)
                    .collect(),
            },
        }),
        identities,
        sessions,
        backend,
    );
    let RpcResponse::NegotiateSuccess(envelope) = response else {
        panic!("negotiation must succeed: {response:?}");
    };
    envelope.result
}

fn resource(device_id: &str, generation_id: u64) -> ResourceKey {
    ResourceKey {
        receiver_id: text("receiver-1"),
        generation_id: generation(generation_id),
        device_id: text(device_id),
        kind: ResourceKind::Lighting,
    }
}

fn lease_request(request_id: &str, resource: ResourceKey) -> LeaseRequest {
    LeaseRequest {
        request_id: text(request_id),
        client_id: text("client-1"),
        resources: vec![resource],
        duration_ms: LeaseDurationMs::try_from(10_000_u32).expect("duration is canonical"),
    }
}

fn static_transaction<R: RestorationRuntime>(
    backend: &TestBackend<R>,
    lease_id: hfx_domain::LeaseId,
) -> TransactionRequest {
    let view = backend.profiles().view(backend.receivers());
    let receiver_profile = view
        .receiver_profile(&text("receiver-1"), generation(1))
        .expect("receiver profile is qualified");
    let mouse_profile = view
        .device_profile(&resource("mouse", 1))
        .expect("mouse profile is qualified");
    TransactionRequest {
        request_id: text("request-transaction"),
        transaction_id: text::<TransactionId>("transaction-1"),
        client_id: text("client-1"),
        lease_id,
        receiver_id: text("receiver-1"),
        generation_id: generation(1),
        receiver_profile_id: receiver_profile.profile_id,
        receiver_profile_digest: receiver_profile.profile_digest,
        device_profiles: vec![DeviceProfileBinding {
            device_id: text("mouse"),
            profile_id: mouse_profile.profile_id,
            profile_digest: mouse_profile.profile_digest,
            application_slot_count: mouse_profile.application_slot_count,
        }],
        transaction_class: TransactionClass::StaticLighting,
        stable_intents: vec![StableLightingIntent {
            device_id: text("mouse"),
            mode: StableLightingMode::Static,
        }],
        deadline_ms: time(10_000),
        resources: vec![resource("mouse", 1)],
        frames: vec![LightingFrame {
            device_id: text("mouse"),
            frame_index: FrameIndex::try_from(0_u32).expect("index is canonical"),
            colors: (0..13)
                .map(|index| RgbColor {
                    red: ColorChannel::try_from(index).expect("color is canonical"),
                    green: ColorChannel::try_from(0_u8).expect("color is canonical"),
                    blue: ColorChannel::try_from(255_u8).expect("color is canonical"),
                })
                .collect(),
        }],
    }
}

fn assert_error(response: RpcResponse, finding: &str, kind: ProtocolErrorKind) {
    let RpcResponse::Error(envelope) = response else {
        panic!("response must be an error: {response:?}");
    };
    assert_eq!(envelope.error.finding_id.as_str(), finding);
    assert_eq!(envelope.error.kind, kind);
}

#[test]
#[allow(clippy::too_many_lines)]
fn production_backend_composes_authority_replay_dispatch_events_and_cleanup() {
    let mut backend = backend();
    let mut sessions = SessionRegistry::new(
        QueueCapacity::try_from(4_u16).expect("session capacity is canonical"),
    );
    let mut dispatcher = new_dispatcher();
    let mut session_identities = DeterministicIdentities(80);
    let hello = negotiate(
        &mut dispatcher,
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );

    let snapshot = dispatcher.dispatch(
        RpcRequest::Snapshot(SessionRequestEnvelope {
            request_id: text("request-snapshot"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: EmptyRequest {},
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::SnapshotSuccess(snapshot) = snapshot else {
        panic!("snapshot must succeed: {snapshot:?}");
    };
    assert_eq!(snapshot.result.receivers.len(), 1);
    assert_eq!(snapshot.result.receivers[0].devices.len(), 1);
    assert_eq!(
        snapshot.result.receivers[0].devices[0]
            .profile_id
            .as_ref()
            .map(hfx_domain::ProfileId::as_str),
        Some("child.razer.basilisk-v3-pro-35k.00cd")
    );

    for (request_id, target, expected_finding, expected_kind) in [
        (
            "request-stale",
            resource("mouse", 2),
            "HFX-GENERATION-001",
            ProtocolErrorKind::StaleGeneration,
        ),
        (
            "request-unknown",
            resource("unknown", 1),
            "HFX-PROFILE-001",
            ProtocolErrorKind::UnsupportedFeature,
        ),
    ] {
        let response = dispatcher.dispatch(
            RpcRequest::AcquireLease(SessionRequestEnvelope {
                request_id: text(request_id),
                protocol_session_id: hello.protocol_session_id.clone(),
                negotiation_token: hello.negotiation_token.clone(),
                params: lease_request(request_id, target),
            }),
            &mut session_identities,
            &mut sessions,
            &mut backend,
        );
        assert_error(response, expected_finding, expected_kind);
    }

    let acquire = RpcRequest::AcquireLease(SessionRequestEnvelope {
        request_id: text("request-acquire"),
        protocol_session_id: hello.protocol_session_id.clone(),
        negotiation_token: hello.negotiation_token.clone(),
        params: lease_request("request-acquire", resource("mouse", 1)),
    });
    let first = dispatcher.dispatch(
        acquire.clone(),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let replay = dispatcher.dispatch(
        acquire,
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert_eq!(first, replay);
    let RpcResponse::AcquireLeaseSuccess(acquired) = first else {
        panic!("lease must be granted");
    };
    let LeaseResult::Granted(grant) = acquired.result else {
        panic!("lease result must be granted");
    };

    let subscribed = dispatcher.dispatch(
        RpcRequest::Subscribe(SessionRequestEnvelope {
            request_id: text("request-subscribe"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: None,
                expected_cursor: None,
                max_events: EventBatchLimit::try_from(16_u16).expect("limit is canonical"),
            },
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::SubscribeSuccess(subscribed) = subscribed else {
        panic!("subscription must succeed: {subscribed:?}");
    };
    assert_eq!(subscribed.result.events.len(), 1);
    assert_eq!(
        subscribed.result.events[0].kind,
        EventKind::OwnershipChanged
    );
    let subscription_id = subscribed.result.subscription_id;
    let cursor: EventCursor = subscribed.result.next_cursor;

    let transaction = static_transaction(&backend, grant.lease_id);
    let submit = RpcRequest::SubmitTransaction(SessionRequestEnvelope {
        request_id: transaction.request_id.clone(),
        protocol_session_id: hello.protocol_session_id.clone(),
        negotiation_token: hello.negotiation_token.clone(),
        params: transaction.clone(),
    });
    let queued = dispatcher.dispatch(
        submit.clone(),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let queued_replay = dispatcher.dispatch(
        submit.clone(),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert_eq!(queued, queued_replay);
    assert_eq!(backend.queued_transactions(), 1);

    let dispatch_result = backend
        .dispatch_next(&sessions)
        .expect("one queued transaction dispatches");
    assert!(matches!(
        dispatch_result.completed,
        Some(ref terminal) if terminal.state == TransactionState::Succeeded
    ));
    assert_eq!(backend.transport().dispatches.len(), 1);

    let terminal_replay =
        dispatcher.dispatch(submit, &mut session_identities, &mut sessions, &mut backend);
    assert!(matches!(
        terminal_replay,
        RpcResponse::SubmitTransactionSuccess(ref envelope)
            if matches!(envelope.result, TransactionResult::Terminal(_))
    ));
    assert_eq!(backend.transport().dispatches.len(), 1);

    let outcome = dispatcher.dispatch(
        RpcRequest::TransactionOutcome(SessionRequestEnvelope {
            request_id: text("request-outcome"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: TransactionLookup {
                request_id: text("request-outcome"),
                client_id: text("client-1"),
                transaction_id: text("transaction-1"),
            },
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(
        outcome,
        RpcResponse::TransactionOutcomeSuccess(ref envelope)
            if matches!(envelope.result, TransactionResult::Terminal(_))
    ));

    let continued = dispatcher.dispatch(
        RpcRequest::Subscribe(SessionRequestEnvelope {
            request_id: text("request-subscribe-next"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: Some(subscription_id),
                expected_cursor: Some(cursor),
                max_events: EventBatchLimit::try_from(16_u16).expect("limit is canonical"),
            },
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::SubscribeSuccess(continued) = continued else {
        panic!("continuation must succeed: {continued:?}");
    };
    assert_eq!(continued.result.events.len(), 1);
    assert_eq!(
        continued.result.events[0].kind,
        EventKind::TransactionCompleted
    );

    let diagnostics = dispatcher.dispatch(
        RpcRequest::Diagnostics(SessionRequestEnvelope {
            request_id: text("request-diagnostics"),
            protocol_session_id: hello.protocol_session_id,
            negotiation_token: hello.negotiation_token,
            params: EmptyRequest {},
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(
        diagnostics,
        RpcResponse::DiagnosticsSuccess(ref envelope) if envelope.result.findings.is_empty()
    ));

    dispatcher
        .disconnect(&mut sessions, &mut backend)
        .expect("disconnect cleanup succeeds");
    assert!(sessions.is_empty());
    assert_eq!(backend.active_subscriptions(), 0);

    let mut replacement = new_dispatcher();
    let replacement_hello = negotiate(
        &mut replacement,
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let reacquired = replacement.dispatch(
        RpcRequest::AcquireLease(SessionRequestEnvelope {
            request_id: text("request-reacquire"),
            protocol_session_id: replacement_hello.protocol_session_id,
            negotiation_token: replacement_hello.negotiation_token,
            params: lease_request("request-reacquire", resource("mouse", 1)),
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(
        reacquired,
        RpcResponse::AcquireLeaseSuccess(ref envelope)
            if matches!(envelope.result, LeaseResult::Granted(_))
    ));
}

#[test]
fn lease_expiry_is_one_observable_ownership_transition() {
    let mut backend = backend();
    let mut sessions = SessionRegistry::new(
        QueueCapacity::try_from(4_u16).expect("session capacity is canonical"),
    );
    let mut dispatcher = new_dispatcher();
    let mut session_identities = DeterministicIdentities(120);
    let hello = negotiate(
        &mut dispatcher,
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );

    let acquired = dispatcher.dispatch(
        RpcRequest::AcquireLease(SessionRequestEnvelope {
            request_id: text("request-expiring-acquire"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: lease_request("request-expiring-acquire", resource("mouse", 1)),
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::AcquireLeaseSuccess(acquired) = acquired else {
        panic!("lease must be granted: {acquired:?}");
    };
    let LeaseResult::Granted(grant) = acquired.result else {
        panic!("lease result must be granted");
    };

    let initial = dispatcher.dispatch(
        RpcRequest::Subscribe(SessionRequestEnvelope {
            request_id: text("request-expiry-subscribe"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: None,
                expected_cursor: None,
                max_events: EventBatchLimit::try_from(16_u16).expect("limit is canonical"),
            },
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::SubscribeSuccess(initial) = initial else {
        panic!("initial subscription must succeed: {initial:?}");
    };
    assert_eq!(initial.result.events.len(), 1);

    *backend.clock_mut() = TestClock(time(10_100));
    backend.tick().expect("expiry tick succeeds");
    backend.tick().expect("repeated expiry tick is idempotent");

    let expired = dispatcher.dispatch(
        RpcRequest::Subscribe(SessionRequestEnvelope {
            request_id: text("request-expiry-continuation"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: SubscriptionRequest {
                client_id: text("client-1"),
                subscription_id: Some(initial.result.subscription_id),
                expected_cursor: Some(initial.result.next_cursor),
                max_events: EventBatchLimit::try_from(16_u16).expect("limit is canonical"),
            },
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::SubscribeSuccess(expired) = expired else {
        panic!("expiry continuation must succeed: {expired:?}");
    };
    assert_eq!(expired.result.events.len(), 1);
    assert_eq!(expired.result.events[0].kind, EventKind::OwnershipChanged);
    assert_eq!(
        expired.result.events[0].lease_id.as_ref(),
        Some(&grant.lease_id)
    );

    let reacquired = dispatcher.dispatch(
        RpcRequest::AcquireLease(SessionRequestEnvelope {
            request_id: text("request-after-expiry"),
            protocol_session_id: hello.protocol_session_id,
            negotiation_token: hello.negotiation_token,
            params: lease_request("request-after-expiry", resource("mouse", 1)),
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(
        reacquired,
        RpcResponse::AcquireLeaseSuccess(ref envelope)
            if matches!(envelope.result, LeaseResult::Granted(_))
    ));
}

#[test]
fn persistence_failure_never_rewrites_terminal_truth_or_retries_hardware() {
    let mut backend = backend_with_restoration(RejectingRestoration);
    let mut sessions = SessionRegistry::new(
        QueueCapacity::try_from(4_u16).expect("session capacity is canonical"),
    );
    let mut dispatcher = new_dispatcher();
    let mut session_identities = DeterministicIdentities(160);
    let hello = negotiate(
        &mut dispatcher,
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );

    let acquired = dispatcher.dispatch(
        RpcRequest::AcquireLease(SessionRequestEnvelope {
            request_id: text("request-persistence-acquire"),
            protocol_session_id: hello.protocol_session_id.clone(),
            negotiation_token: hello.negotiation_token.clone(),
            params: lease_request("request-persistence-acquire", resource("mouse", 1)),
        }),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    let RpcResponse::AcquireLeaseSuccess(acquired) = acquired else {
        panic!("lease must be granted: {acquired:?}");
    };
    let LeaseResult::Granted(grant) = acquired.result else {
        panic!("lease result must be granted");
    };

    let transaction = static_transaction(&backend, grant.lease_id);
    let submit = RpcRequest::SubmitTransaction(SessionRequestEnvelope {
        request_id: transaction.request_id.clone(),
        protocol_session_id: hello.protocol_session_id,
        negotiation_token: hello.negotiation_token,
        params: transaction,
    });
    let queued = dispatcher.dispatch(
        submit.clone(),
        &mut session_identities,
        &mut sessions,
        &mut backend,
    );
    assert!(matches!(
        queued,
        RpcResponse::SubmitTransactionSuccess(ref envelope)
            if matches!(envelope.result, TransactionResult::Progress(_))
    ));

    let completed = backend
        .dispatch_next(&sessions)
        .expect("hardware completion remains observable");
    assert!(matches!(
        completed.completed,
        Some(ref completion)
            if completion.state == TransactionState::Succeeded
                && !completion.automatic_retry
    ));
    assert!(matches!(
        completed.stable_capture,
        StableCaptureStatus::Failed(RestorationError::Persistence(
            PersistenceOperation::SaveIntent
        ))
    ));
    assert_eq!(backend.transport().dispatches.len(), 1);

    let replay = dispatcher.dispatch(submit, &mut session_identities, &mut sessions, &mut backend);
    assert!(matches!(
        replay,
        RpcResponse::SubmitTransactionSuccess(ref envelope)
            if matches!(
                envelope.result,
                TransactionResult::Terminal(ref terminal)
                    if terminal.state == TransactionState::Succeeded
                        && !terminal.automatic_retry
            )
    ));
    assert_eq!(backend.transport().dispatches.len(), 1);

    let empty = backend
        .dispatch_next(&sessions)
        .expect("an empty queue remains healthy");
    assert!(empty.completed.is_none());
    assert_eq!(empty.stable_capture, StableCaptureStatus::NotApplicable);
    assert_eq!(backend.transport().dispatches.len(), 1);
}
