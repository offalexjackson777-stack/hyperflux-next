// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use crate::{AuthorizedSession, BackendRequestContext, SessionIdentityError};
use hfx_domain::{
    ClientId, ClientName, ComponentVersion, FindingId, LeaseId, NativeReportHex, ProtocolFeatureId,
    RequestId, ServerInstanceId,
};
use hfx_protocol::{
    BridgeSnapshot, ClientHello, DiagnosticSnapshot, EventBatch, IntegrationView, LeaseRequest,
    LeaseResult, NativeExchangeMatchedResponse, NativeExchangeNotSent, NativeExchangeOutcome,
    NativeExchangeRequest, NativeExchangeResult, NegotiationRequestEnvelope, ReleaseLeaseRequest,
    RenewLeaseRequest, SessionRequestEnvelope, SubscriptionRequest, TransactionLookup,
    TransactionRequest, TransactionResult,
};
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

#[derive(Debug, Default)]
struct NativeBackend {
    dispatched: Vec<NativeRouteToken>,
    rejected: Vec<NativeExchangeRejection>,
    invalidations: Vec<(NativeRouteToken, NativeExchangeRejection)>,
}

impl BridgeRpcBackend for NativeBackend {
    fn snapshot(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn integration_view(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn acquire_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn renew_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn release_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn submit_transaction(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn transaction_outcome(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn subscribe(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn diagnostics(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn native_transact(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        self.dispatched.push(request.route_token.clone());
        Ok(NativeExchangeOutcome {
            request_id: request.request_id,
            route_token: request.route_token,
            result: NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: request.report,
            }),
        })
    }

    fn reject_native_exchange(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
        rejection: NativeExchangeRejection,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        self.rejected.push(rejection);
        Ok(NativeExchangeOutcome {
            request_id: request.request_id,
            route_token: request.route_token,
            result: NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: rejection.error_kind(),
                finding_id: FindingId::try_from(rejection.error_code().as_str())
                    .expect("catalog code is a finding identity"),
            }),
        })
    }

    fn take_native_route_invalidations(
        &mut self,
    ) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
        std::mem::take(&mut self.invalidations)
    }

    fn disconnect(&mut self, _session: &AuthorizedSession) -> Result<(), RpcFailure> {
        Ok(())
    }
}

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test value is canonical")
}

fn actor_pair(
    native_capacity: u16,
) -> (
    BridgeActor<DeterministicIdentities, NativeBackend>,
    BridgeActorHandle,
) {
    let capacity = QueueCapacity::try_from(8_u16).expect("capacity is canonical");
    BridgeActor::new(
        BridgeSessionConfig {
            server_instance_id: text::<ServerInstanceId>("server-native-scheduler"),
            bridge_version: text::<ComponentVersion>("0.0.0-test"),
            event_buffer_capacity: capacity,
        },
        DeterministicIdentities(1),
        SessionRegistry::new(capacity),
        NativeBackend::default(),
        BridgeActorLimits {
            command_capacity: capacity,
            connection_capacity: capacity,
            native_transaction_capacity: QueueCapacity::try_from(native_capacity)
                .expect("native capacity is canonical"),
            response_timeout: Duration::from_secs(2),
        },
    )
    .expect("actor starts")
}

fn actor(native_capacity: u16) -> BridgeActor<DeterministicIdentities, NativeBackend> {
    actor_pair(native_capacity).0
}

fn open_and_negotiate(
    actor: &mut BridgeActor<DeterministicIdentities, NativeBackend>,
) -> (BridgeConnectionId, hfx_protocol::ServerHello) {
    let (open_reply, open_response) = mpsc::sync_channel(1);
    assert!(
        actor
            .process(BridgeActorCommand::Open { reply: open_reply })
            .is_none()
    );
    let connection_id = open_response
        .recv()
        .expect("open response is sent")
        .expect("connection opens");

    let (reply, response) = mpsc::sync_channel(1);
    let negotiation = RpcRequest::Negotiate(NegotiationRequestEnvelope {
        request_id: text::<RequestId>("request-negotiate-native-scheduler"),
        params: ClientHello {
            client_id: text::<ClientId>("client-native-scheduler"),
            client_name: text::<ClientName>("Native scheduler test"),
            minimum_version: ProtocolVersion::try_from(7_u16).expect("version is canonical"),
            maximum_version: ProtocolVersion::try_from(7_u16).expect("version is canonical"),
            required_features: vec![text::<ProtocolFeatureId>("native-receiver-tunnel")],
            optional_features: Vec::new(),
        },
    });
    assert!(
        actor
            .process(BridgeActorCommand::Request {
                connection_id,
                request: Box::new(negotiation),
                reply,
            })
            .is_none()
    );
    let response = response
        .recv()
        .expect("negotiation response is sent")
        .expect("actor remains available");
    let RpcResponse::NegotiateSuccess(envelope) = response else {
        panic!("native negotiation succeeds: {response:?}");
    };
    (connection_id, envelope.result)
}

fn native_request(
    hello: &hfx_protocol::ServerHello,
    request_id: &str,
    route_fill: char,
    timeout_ms: u16,
) -> RpcRequest {
    RpcRequest::NativeTransact(SessionRequestEnvelope {
        request_id: text::<RequestId>(request_id),
        protocol_session_id: hello.protocol_session_id.clone(),
        negotiation_token: hello.negotiation_token.clone(),
        params: NativeExchangeRequest {
            request_id: text::<RequestId>(request_id),
            client_id: text::<ClientId>("client-native-scheduler"),
            lease_id: text::<LeaseId>("lease-native-scheduler"),
            route_token: text::<NativeRouteToken>(&route_fill.to_string().repeat(64)),
            report: text::<NativeReportHex>(&"00".repeat(90)),
            timeout_ms: NativeExchangeTimeoutMs::try_from(timeout_ms)
                .expect("timeout is canonical"),
        },
    })
}

fn enqueue(
    actor: &mut BridgeActor<DeterministicIdentities, NativeBackend>,
    connection_id: BridgeConnectionId,
    request: RpcRequest,
) -> Receiver<Result<RpcResponse, BridgeActorError>> {
    let (reply, response) = mpsc::sync_channel(1);
    assert!(
        actor
            .process(BridgeActorCommand::Request {
                connection_id,
                request: Box::new(request),
                reply,
            })
            .is_none()
    );
    response
}

#[test]
fn actor_dispatches_route_local_fifo_with_fair_sibling_turns() {
    let mut actor = actor(8);
    let (connection_id, hello) = open_and_negotiate(&mut actor);
    let responses = [
        native_request(&hello, "request-mouse-1", 'a', 1_000),
        native_request(&hello, "request-mouse-2", 'a', 1_000),
        native_request(&hello, "request-keyboard-1", 'b', 1_000),
        native_request(&hello, "request-mouse-3", 'a', 1_000),
        native_request(&hello, "request-keyboard-2", 'b', 1_000),
    ]
    .into_iter()
    .map(|request| enqueue(&mut actor, connection_id, request))
    .collect::<Vec<_>>();

    for _ in 0..responses.len() {
        actor.dispatch_next_native();
    }
    assert_eq!(
        actor
            .backend
            .dispatched
            .iter()
            .map(|token| token.as_str().chars().next().expect("token is nonempty"))
            .collect::<Vec<_>>(),
        vec!['a', 'b', 'a', 'b', 'a']
    );
    for response in responses {
        assert!(matches!(
            response.recv().expect("response is sent"),
            Ok(RpcResponse::NativeTransactSuccess(_))
        ));
    }
}

#[test]
fn run_loop_deterministically_drains_an_already_queued_batch() {
    let (actor, handle) = actor_pair(8);
    let (first_reply, first_response) = mpsc::sync_channel(1);
    let (second_reply, second_response) = mpsc::sync_channel(1);
    let (shutdown_reply, shutdown_response) = mpsc::sync_channel(1);
    handle
        .sender
        .try_send(BridgeActorCommand::Open { reply: first_reply })
        .expect("first open command queues");
    handle
        .sender
        .try_send(BridgeActorCommand::Open {
            reply: second_reply,
        })
        .expect("second open command queues");
    handle
        .sender
        .try_send(BridgeActorCommand::Shutdown {
            reply: shutdown_reply,
        })
        .expect("shutdown command queues");

    let mut ticks = 0_u8;
    let exit = actor
        .run_with_runtime_tick(Duration::from_mins(1), |_backend, _sessions| {
            ticks += 1;
            Ok::<bool, Infallible>(true)
        })
        .expect("runtime tick cannot fail");

    assert_eq!(
        first_response
            .recv()
            .expect("first open response is sent")
            .expect("first connection opens"),
        BridgeConnectionId(1)
    );
    assert_eq!(
        second_response
            .recv()
            .expect("second open response is sent")
            .expect("second connection opens"),
        BridgeConnectionId(2)
    );
    assert_eq!(
        shutdown_response.recv().expect("shutdown response is sent"),
        exit
    );
    assert_eq!(exit.connections_cleaned, 2);
    assert_eq!(exit.cleanup_failures, 0);
    assert_eq!(ticks, 2);
}

#[test]
fn saturation_and_expiry_are_retained_not_sent_without_dispatch() {
    let mut actor = actor(1);
    let (connection_id, hello) = open_and_negotiate(&mut actor);
    let first = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-first", 'a', 1_000),
    );
    let saturated = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-saturated", 'b', 1_000),
    );
    let saturated = saturated.recv().expect("saturation response is sent");
    assert!(matches!(
        saturated,
        Ok(RpcResponse::NativeTransactSuccess(envelope))
            if matches!(
                envelope.result.result,
                NativeExchangeResult::NotSent(NativeExchangeNotSent {
                    error_kind: hfx_domain::ProtocolErrorKind::QueueFull,
                    ..
                })
            )
    ));
    assert!(actor.backend.dispatched.is_empty());

    actor.dispatch_next_native();
    assert!(matches!(
        first.recv().expect("first response is sent"),
        Ok(RpcResponse::NativeTransactSuccess(_))
    ));
    let expired = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-expired", 'c', 1),
    );
    std::thread::sleep(Duration::from_millis(3));
    actor.dispatch_next_native();
    let expired = expired.recv().expect("expiry response is sent");
    assert!(matches!(
        expired,
        Ok(RpcResponse::NativeTransactSuccess(envelope))
            if matches!(
                envelope.result.result,
                NativeExchangeResult::NotSent(NativeExchangeNotSent {
                    error_kind: hfx_domain::ProtocolErrorKind::DeadlineExceeded,
                    ..
                })
            )
    ));
    assert_eq!(actor.backend.dispatched.len(), 1);
    assert_eq!(
        actor.backend.rejected,
        vec![
            NativeExchangeRejection::QueueFull,
            NativeExchangeRejection::DeadlineExceeded,
        ]
    );
}

#[test]
fn route_invalidation_cancels_exact_lane_and_preserves_sibling_service() {
    let mut actor = actor(8);
    let (connection_id, hello) = open_and_negotiate(&mut actor);
    let mouse_one = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-invalidated-1", 'a', 1_000),
    );
    let mouse_two = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-invalidated-2", 'a', 1_000),
    );
    let keyboard = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-sibling", 'b', 1_000),
    );
    actor.backend.invalidations.push((
        text::<NativeRouteToken>(&"a".repeat(64)),
        NativeExchangeRejection::StaleGeneration,
    ));

    actor.cancel_invalidated_native();
    for response in [mouse_one, mouse_two] {
        assert!(matches!(
            response.recv().expect("cancellation response is sent"),
            Ok(RpcResponse::NativeTransactSuccess(envelope))
                if matches!(
                    envelope.result.result,
                    NativeExchangeResult::NotSent(NativeExchangeNotSent {
                        error_kind: hfx_domain::ProtocolErrorKind::StaleGeneration,
                        ..
                    })
                )
        ));
    }
    assert!(actor.backend.dispatched.is_empty());

    actor.dispatch_next_native();
    assert!(matches!(
        keyboard.recv().expect("sibling response is sent"),
        Ok(RpcResponse::NativeTransactSuccess(_))
    ));
    assert_eq!(
        actor.backend.dispatched,
        vec![text::<NativeRouteToken>(&"b".repeat(64))]
    );
    assert_eq!(
        actor.backend.rejected,
        vec![
            NativeExchangeRejection::StaleGeneration,
            NativeExchangeRejection::StaleGeneration,
        ]
    );
}

#[test]
fn disconnect_cancels_queued_native_work_before_session_revocation() {
    let mut actor = actor(8);
    let (connection_id, hello) = open_and_negotiate(&mut actor);
    let pending = enqueue(
        &mut actor,
        connection_id,
        native_request(&hello, "request-disconnect", 'a', 1_000),
    );

    actor.close(connection_id).expect("connection closes");
    assert!(matches!(
        pending.recv().expect("cancellation response is sent"),
        Ok(RpcResponse::NativeTransactSuccess(envelope))
            if matches!(
                envelope.result.result,
                NativeExchangeResult::NotSent(NativeExchangeNotSent {
                    error_kind: hfx_domain::ProtocolErrorKind::OwnershipConflict,
                    ..
                })
            )
    ));
    assert!(actor.backend.dispatched.is_empty());
    assert_eq!(
        actor.backend.rejected,
        vec![NativeExchangeRejection::SessionEnded]
    );
}
