# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .channel import RpcChannel, UnixChannelConfig, UnixRpcChannel
from .errors import BridgeError, NegotiationError, ResponseMismatch, UnexpectedResponse
from .generated.domain_types import (
    ClientId,
    ClientName,
    EventBatchLimit,
    GenerationId,
    LeaseDurationMs,
    LeaseId,
    MonotonicMs,
    ProfileDigest,
    ProfileId,
    ProtocolFeatureId,
    ProtocolVersion,
    ReceiverId,
    RequestId,
    SubscriptionId,
    TransactionClass,
    TransactionId,
)
from .generated import protocol_v5_types as v5
from .identity import IdentitySource, ProcessIdentitySource


@dataclass(frozen=True, slots=True)
class ClientConfig:
    client_id: ClientId
    client_name: ClientName
    required_features: tuple[ProtocolFeatureId, ...]
    optional_features: tuple[ProtocolFeatureId, ...] = ()


@dataclass(frozen=True, slots=True)
class EventSubscription:
    subscription_id: SubscriptionId | None
    expected_cursor: v5.EventCursor | None
    max_events: EventBatchLimit


@dataclass(frozen=True, slots=True)
class TransactionSubmission:
    transaction_id: TransactionId
    lease_id: LeaseId
    receiver_id: ReceiverId
    generation_id: GenerationId
    receiver_profile_id: ProfileId
    receiver_profile_digest: ProfileDigest
    device_profiles: tuple[v5.DeviceProfileBinding, ...]
    transaction_class: TransactionClass
    stable_intents: tuple[v5.StableLightingIntent, ...]
    deadline_ms: MonotonicMs
    resources: tuple[v5.ResourceKey, ...]
    frames: tuple[v5.LightingFrame, ...]


class Client:
    """Protocol-v5 HyperFlux client with request and bridge-instance binding."""

    def __init__(
        self,
        channel: RpcChannel,
        identities: IdentitySource,
        client_id: ClientId,
        hello: v5.ServerHello,
    ) -> None:
        self._channel = channel
        self._identities = identities
        self._client_id = client_id
        self._hello = hello

    @classmethod
    def connect(
        cls,
        channel: RpcChannel,
        config: ClientConfig,
        identities: IdentitySource | None = None,
    ) -> Client:
        identities = identities or ProcessIdentitySource()
        request_id = identities.next_request_id()
        version = ProtocolVersion(5)
        request = v5.RpcRequestNegotiate(
            v5.NegotiationRequestEnvelope(
                request_id=request_id,
                params=v5.ClientHello(
                    client_id=config.client_id,
                    client_name=config.client_name,
                    minimum_version=version,
                    maximum_version=version,
                    required_features=config.required_features,
                    optional_features=config.optional_features,
                ),
            )
        )
        response = channel.exchange(request)
        cls._validate_request_id(response, request_id)
        if isinstance(response, v5.ErrorEnvelope):
            raise cls._bridge_error(response)
        if not isinstance(response, v5.RpcResponseNegotiateSuccess):
            raise NegotiationError("bridge returned an unexpected negotiation response")
        envelope = response.response
        hello = envelope.result
        if (
            envelope.server_instance_id != hello.server_instance_id
            or hello.selected_version != version
            or any(feature not in hello.enabled_features for feature in config.required_features)
        ):
            raise NegotiationError("bridge returned an invalid protocol-v5 negotiation")
        return cls(channel, identities, config.client_id, hello)

    @classmethod
    def connect_unix(
        cls,
        channel_config: UnixChannelConfig,
        client_config: ClientConfig,
    ) -> Client:
        channel = UnixRpcChannel.connect(channel_config)
        try:
            return cls.connect(channel, client_config)
        except BaseException:
            channel.close()
            raise

    @property
    def client_id(self) -> ClientId:
        return self._client_id

    @property
    def server_hello(self) -> v5.ServerHello:
        return self._hello

    def next_transaction_id(self) -> TransactionId:
        return self._identities.next_transaction_id()

    def close(self) -> None:
        self._channel.close()

    def integration_view(self) -> v5.IntegrationView:
        return self._call(
            v5.RpcRequestIntegrationView,
            v5.EmptyRequest(),
            v5.RpcResponseIntegrationViewSuccess,
        )

    def snapshot(self) -> v5.BridgeSnapshot:
        return self._call(
            v5.RpcRequestSnapshot,
            v5.EmptyRequest(),
            v5.RpcResponseSnapshotSuccess,
        )

    def diagnostics(self) -> v5.DiagnosticSnapshot:
        return self._call(
            v5.RpcRequestDiagnostics,
            v5.EmptyRequest(),
            v5.RpcResponseDiagnosticsSuccess,
        )

    def acquire_lease(
        self,
        resources: tuple[v5.ResourceKey, ...],
        duration_ms: LeaseDurationMs,
    ) -> v5.LeaseResult:
        request_id = self._identities.next_request_id()
        params = v5.LeaseRequest(request_id, self._client_id, resources, duration_ms)
        return self._exchange_session(
            v5.RpcRequestAcquireLease,
            params,
            v5.RpcResponseAcquireLeaseSuccess,
            request_id,
        )

    def renew_lease(self, lease_id: LeaseId, duration_ms: LeaseDurationMs) -> v5.LeaseResult:
        request_id = self._identities.next_request_id()
        params = v5.RenewLeaseRequest(request_id, self._client_id, lease_id, duration_ms)
        return self._exchange_session(
            v5.RpcRequestRenewLease,
            params,
            v5.RpcResponseRenewLeaseSuccess,
            request_id,
        )

    def release_lease(self, lease_id: LeaseId) -> v5.LeaseResult:
        request_id = self._identities.next_request_id()
        params = v5.ReleaseLeaseRequest(request_id, self._client_id, lease_id)
        return self._exchange_session(
            v5.RpcRequestReleaseLease,
            params,
            v5.RpcResponseReleaseLeaseSuccess,
            request_id,
        )

    def submit_transaction(self, submission: TransactionSubmission) -> v5.TransactionResult:
        request_id = self._identities.next_request_id()
        params = v5.TransactionRequest(
            request_id=request_id,
            transaction_id=submission.transaction_id,
            client_id=self._client_id,
            lease_id=submission.lease_id,
            receiver_id=submission.receiver_id,
            generation_id=submission.generation_id,
            receiver_profile_id=submission.receiver_profile_id,
            receiver_profile_digest=submission.receiver_profile_digest,
            device_profiles=submission.device_profiles,
            transaction_class=submission.transaction_class,
            stable_intents=submission.stable_intents,
            deadline_ms=submission.deadline_ms,
            resources=submission.resources,
            frames=submission.frames,
        )
        return self._exchange_session(
            v5.RpcRequestSubmitTransaction,
            params,
            v5.RpcResponseSubmitTransactionSuccess,
            request_id,
        )

    def transaction_outcome(self, transaction_id: TransactionId) -> v5.TransactionResult:
        request_id = self._identities.next_request_id()
        params = v5.TransactionLookup(request_id, self._client_id, transaction_id)
        return self._exchange_session(
            v5.RpcRequestTransactionOutcome,
            params,
            v5.RpcResponseTransactionOutcomeSuccess,
            request_id,
        )

    def subscribe(self, subscription: EventSubscription) -> v5.EventBatch:
        request_id = self._identities.next_request_id()
        params = v5.SubscriptionRequest(
            client_id=self._client_id,
            subscription_id=subscription.subscription_id,
            expected_cursor=subscription.expected_cursor,
            max_events=subscription.max_events,
        )
        return self._exchange_session(
            v5.RpcRequestSubscribe,
            params,
            v5.RpcResponseSubscribeSuccess,
            request_id,
        )

    def _call(
        self,
        request_type: type,
        params: object,
        response_type: type,
    ) -> Any:
        request_id = self._identities.next_request_id()
        return self._exchange_session(request_type, params, response_type, request_id)

    def _exchange_session(
        self,
        request_type: type,
        params: object,
        response_type: type,
        request_id: RequestId,
    ) -> Any:
        envelope = v5.SessionRequestEnvelope(
            request_id=request_id,
            protocol_session_id=self._hello.protocol_session_id,
            negotiation_token=self._hello.negotiation_token,
            params=params,
        )
        response = self._channel.exchange(request_type(envelope))
        self._validate_request_id(response, request_id)
        server_instance = self._server_instance(response)
        if server_instance != self._hello.server_instance_id:
            raise ResponseMismatch("bridge process identity changed; reconnect the SDK client")
        if isinstance(response, v5.ErrorEnvelope):
            raise self._bridge_error(response)
        if not isinstance(response, response_type):
            raise UnexpectedResponse(f"unexpected response to {request_type.METHOD}")
        return response.response.result

    @staticmethod
    def _validate_request_id(response: v5.RpcResponse, expected: RequestId) -> None:
        actual = response.request_id if isinstance(response, v5.ErrorEnvelope) else response.response.request_id
        if actual != expected:
            raise ResponseMismatch("bridge response does not match the SDK request identity")

    @staticmethod
    def _server_instance(response: v5.RpcResponse) -> object:
        return (
            response.server_instance_id
            if isinstance(response, v5.ErrorEnvelope)
            else response.response.server_instance_id
        )

    @staticmethod
    def _bridge_error(response: v5.ErrorEnvelope) -> BridgeError:
        return BridgeError(
            response.error.message.value,
            response.error.finding_id.value,
            response.error.kind.value,
        )

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
