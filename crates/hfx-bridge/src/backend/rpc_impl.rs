// SPDX-License-Identifier: GPL-2.0-only

//! Complete public RPC implementation over the bridge composition state.

use super::CoreBridgeBackend;
use super::failures::{
    desired_effect_failure, event_failure, finding, identity_failure, lease_failure,
    native_exchange_failure, native_route_failure, ownership_failure, snapshot_failure,
    subscription_failure, transaction_failure,
};
use super::native::{
    decode_native_report, native_command_allowed, native_profile_for_route,
    native_route_token_digest, validated_native_transport_result,
};
use crate::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, NativeExchangeRejection,
    RestorationRuntime, RpcFailure, SnapshotProjector,
};
use hfx_core::{
    Clock, EventSink, NATIVE_REPORT_BYTES, NativeEndpointLane, NativeTransportDispatch,
    OutcomeLookup, ReceiverTransport, SubmissionResult, WallClock,
};
use hfx_domain::{
    DeviceKind, NativeRouteAccess, NativeRouteState, NativeRouteToken, ProtocolErrorKind,
};
use hfx_errors::ErrorCode;
use hfx_integration_model::project_integration_view;
use hfx_profiles::validate_native_request;
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EventBatch, IntegrationView, LeaseRequest, LeaseResult,
    NativeExchangeOutcome, NativeExchangeRequest, NativeRouteEventBatch, NativeRouteInventory,
    ReleaseLeaseRequest, RenewLeaseRequest, SubscriptionRequest, TransactionLookup,
    TransactionRequest, TransactionResult, TransactionUnavailable,
};

impl<C, W, T, R, S> BridgeRpcBackend for CoreBridgeBackend<C, W, T, R, S>
where
    C: Clock,
    W: WallClock,
    T: ReceiverTransport,
    R: RestorationRuntime,
    S: EventSink,
{
    fn snapshot(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        SnapshotProjector::new(&self.profiles)
            .project(
                &self.receivers,
                &mut self.leases,
                &self.events,
                &self.restoration,
                self.clock.now(),
            )
            .map_err(|error| snapshot_failure(&error))
    }

    fn integration_view(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        let snapshot = self.snapshot(context)?;
        project_integration_view(
            &snapshot,
            self.profiles.catalog(),
            Some(&context.session().client_id),
        )
        .map_err(|_| RpcFailure::internal())
    }

    fn acquire_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.qualify_resources(&request)?;
        let lease_id = self.identities.lease_id().map_err(identity_failure)?;
        let decision = self
            .leases
            .acquire_decision(request, lease_id, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        Ok(decision.result)
    }

    fn renew_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let decision = self
            .leases
            .renew_request_decision(request, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        Ok(decision.result)
    }

    fn release_lease(
        &mut self,
        context: BackendRequestContext<'_>,
        request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let released_lease_id = request.lease_id.clone();
        let decision = self
            .leases
            .release_request_decision(request, self.clock.now())
            .map_err(|error| lease_failure(&error))?;
        if decision.changed
            && let LeaseResult::Granted(grant) = &decision.result
        {
            self.record_ownership_change(grant.lease_id.clone())?;
        }
        if decision.changed {
            self.desired_effects.remove_lease(&released_lease_id);
        }
        Ok(decision.result)
    }

    fn submit_transaction(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.desired_effects
            .validate_admission(&request)
            .map_err(desired_effect_failure)?;
        let nonce = self.identities.dispatch_nonce().map_err(identity_failure)?;
        let submission = context.session().submission_binding(nonce);
        let desired_request = request.clone();
        let profiles = self.profiles.view(&self.receivers);
        let result = self
            .transactions
            .submit(
                request,
                submission.clone(),
                self.clock.now(),
                context.sessions(),
                &self.leases,
                &profiles,
                &profiles,
                &self.transport,
                &mut self.events,
                &mut self.event_sink,
            )
            .map_err(transaction_failure)?;
        if matches!(result, SubmissionResult::Queued(_)) {
            self.desired_effects
                .capture_admitted(&desired_request, &submission)
                .map_err(desired_effect_failure)?;
        }
        Ok(match result {
            SubmissionResult::Queued(progress) => TransactionResult::Progress(progress),
            SubmissionResult::Replay(result) => result,
        })
    }

    fn transaction_outcome(
        &mut self,
        context: BackendRequestContext<'_>,
        request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        Ok(
            match self
                .transactions
                .outcome(&request.client_id, &request.transaction_id)
            {
                OutcomeLookup::Retained(result) => result.clone(),
                OutcomeLookup::Evicted => TransactionResult::Unavailable(TransactionUnavailable {
                    transaction_id: request.transaction_id,
                    error_kind: ProtocolErrorKind::OutcomeEvicted,
                    finding_id: finding(ErrorCode::HfxOutcome002),
                }),
                OutcomeLookup::Unknown => TransactionResult::Unavailable(TransactionUnavailable {
                    transaction_id: request.transaction_id,
                    error_kind: ProtocolErrorKind::OutcomeUnknown,
                    finding_id: finding(ErrorCode::HfxOutcome001),
                }),
                OutcomeLookup::Forbidden => return Err(ownership_failure()),
            },
        )
    }

    fn subscribe(
        &mut self,
        context: BackendRequestContext<'_>,
        request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        let subscription_id = self
            .subscriptions
            .resolve(context.session(), &request, &mut self.identities)
            .map_err(subscription_failure)?;
        self.events
            .read(subscription_id, &request)
            .map_err(|error| event_failure(&error))
    }

    fn diagnostics(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        Ok(self.diagnostics.snapshot(
            self.events.latest_sequence(),
            self.config.event_capacity,
            self.config.transaction_capacity,
        ))
    }

    fn native_enumerate(
        &mut self,
        context: BackendRequestContext<'_>,
    ) -> Result<NativeRouteInventory, RpcFailure> {
        Self::authorize(context)?;
        self.tick()?;
        self.synchronize_native_routes()?;
        Ok(self.native_routes.inventory())
    }

    fn native_transact(
        &mut self,
        context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        if let Some(rejection) = self.native_invalidations.rejection(&request.route_token) {
            return self.reject_native_before_set(request, rejection.error_kind());
        }
        if let Some(outcome) = self
            .native_exchanges
            .retained(&request)
            .map_err(native_exchange_failure)?
        {
            return Ok(outcome);
        }

        self.synchronize_native_routes()?;
        let Some(route) = self
            .native_routes
            .route_by_token(&request.route_token)
            .cloned()
        else {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        };
        if route.access != NativeRouteAccess::Qualified {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        }
        if route.route_state != NativeRouteState::Available {
            return self.reject_native_before_set(request, ProtocolErrorKind::TransportFailure);
        }
        let Some(resource) = route.resource.clone() else {
            return self.reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
        };
        if !self.leases.owns(
            &request.client_id,
            &request.lease_id,
            std::slice::from_ref(&resource),
            self.clock.now(),
        ) {
            return self.reject_native_before_set(request, ProtocolErrorKind::OwnershipConflict);
        }
        if self.transport.current_generation(&route.receiver_id) != Some(route.generation_id) {
            return self.reject_native_before_set(request, ProtocolErrorKind::StaleGeneration);
        }
        if !self
            .transport
            .native_exchange_available(&route.receiver_id, route.generation_id)
        {
            return self.reject_native_before_set(request, ProtocolErrorKind::TransportFailure);
        }
        if usize::from(route.report_bytes.get()) != NATIVE_REPORT_BYTES {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        }
        let native_profile = match native_profile_for_route(&self.profiles, &self.receivers, &route)
        {
            Ok(profile) => profile,
            Err(error_kind) => return self.reject_native_before_set(request, error_kind),
        };

        let endpoint = match route.device_kind {
            DeviceKind::Mouse => NativeEndpointLane::Pointer,
            DeviceKind::Keyboard => NativeEndpointLane::Keyboard,
            _ => {
                return self
                    .reject_native_before_set(request, ProtocolErrorKind::UnsupportedFeature);
            }
        };
        let Ok(report) = decode_native_report(&request.report) else {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        };
        if !native_command_allowed(&self.source_validated_devices, &route, &report) {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        }
        if validate_native_request(&native_profile, &report).is_err() {
            return self.reject_native_before_set(request, ProtocolErrorKind::InvalidRequest);
        }
        let route_token_digest = native_route_token_digest(&request.route_token);
        let dispatch = NativeTransportDispatch {
            session_id: context.session().session_id.clone(),
            authorization_epoch: context.session().authorization_epoch,
            dispatch_nonce: self.identities.dispatch_nonce().map_err(identity_failure)?,
            receiver_id: route.receiver_id,
            generation_id: route.generation_id,
            endpoint,
            route_token_digest,
            report,
            timeout_ms: request.timeout_ms,
        };
        let transport_outcome = self.transport.native_exchange(&dispatch);
        let result = validated_native_transport_result(
            &native_profile,
            &report,
            &dispatch,
            &transport_outcome,
        )?;
        self.complete_native_exchange(request, result)
    }

    fn reject_native_exchange(
        &mut self,
        context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
        rejection: NativeExchangeRejection,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.reject_native_before_set(request, rejection.error_kind())
    }

    fn take_native_route_invalidations(
        &mut self,
    ) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
        self.native_invalidations.take_pending()
    }

    fn native_watch(
        &mut self,
        context: BackendRequestContext<'_>,
        request: SubscriptionRequest,
    ) -> Result<NativeRouteEventBatch, RpcFailure> {
        Self::authorize_client(context, &request.client_id)?;
        self.tick()?;
        self.synchronize_native_routes()?;
        let subscription_id = self
            .native_subscriptions
            .resolve(context.session(), &request, &mut self.identities)
            .map_err(subscription_failure)?;
        self.native_routes
            .read(subscription_id, &request)
            .map_err(native_route_failure)
    }

    fn disconnect(&mut self, session: &AuthorizedSession) -> Result<(), RpcFailure> {
        self.tick()?;
        self.desired_effects.remove_session(&session.session_id);
        let _ = self.subscriptions.revoke_session(session);
        let _ = self.native_subscriptions.revoke_session(session);
        let released = self.leases.release_client(&session.client_id);
        let mut first_failure = None;
        for grant in released {
            if let Err(failure) = self.record_ownership_change(grant.lease_id) {
                first_failure.get_or_insert(failure);
            }
        }
        if let Err(error) = self.transactions.invalidate_session(
            &session.session_id,
            &mut self.events,
            &mut self.event_sink,
        ) {
            first_failure.get_or_insert_with(|| transaction_failure(error));
        }
        first_failure.map_or(Ok(()), Err)
    }
}
