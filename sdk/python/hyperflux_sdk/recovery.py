# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Callable, Protocol, TypeVar

from .channel import UnixChannelConfig
from .client import (
    Client,
    ClientConfig,
    EventSubscription,
    TransactionSubmission,
)
from .errors import CodecError, FramingError, HyperFluxSdkError, ResponseMismatch, SessionInactive
from .generated.domain_types import (
    FindingId,
    LeaseDurationMs,
    LeaseId,
    ProtocolErrorKind,
    TransactionId,
)
from .generated import protocol_v5_types as v5
from .identity import IdentitySource, ProcessIdentitySource


class ClientFactory(Protocol):
    def connect(self) -> Client: ...


@dataclass(slots=True)
class UnixClientFactory:
    channel_config: UnixChannelConfig
    client_config: ClientConfig
    identities: IdentitySource = field(default_factory=ProcessIdentitySource)

    def connect(self) -> Client:
        return Client.connect_unix(
            self.channel_config,
            self.client_config,
            self.identities,
        )


_Result = TypeVar("_Result")
_CONNECTION_ERRORS = (CodecError, FramingError, ResponseMismatch)
_MAX_CONNECTION_EPOCH = (1 << 64) - 1


class RecoveringClient:
    """Reconnect SDK reads while never replaying an uncertain hardware write."""

    def __init__(self, factory: ClientFactory, *, connect_immediately: bool = False) -> None:
        self._factory = factory
        self._connection: Client | None = None
        self._connection_epoch = 0
        self._lock = RLock()
        if connect_immediately:
            with self._lock:
                self._reconnect()

    @property
    def connection_epoch(self) -> int:
        with self._lock:
            return self._connection_epoch

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                if self._connection_epoch < _MAX_CONNECTION_EPOCH:
                    self._connection_epoch += 1

    def integration_view(self) -> v5.IntegrationView:
        return self._retry_read(lambda client: client.integration_view())

    def snapshot(self) -> v5.BridgeSnapshot:
        return self._retry_read(lambda client: client.snapshot())

    def diagnostics(self) -> v5.DiagnosticSnapshot:
        return self._retry_read(lambda client: client.diagnostics())

    def next_transaction_id(self) -> TransactionId:
        return self._retry_read(lambda client: client.next_transaction_id())

    def acquire_lease(
        self,
        resources: tuple[v5.ResourceKey, ...],
        duration_ms: LeaseDurationMs,
    ) -> v5.LeaseResult:
        return self._retry_read(lambda client: client.acquire_lease(resources, duration_ms))

    def renew_lease(self, lease_id: LeaseId, duration_ms: LeaseDurationMs) -> v5.LeaseResult:
        with self._lock:
            client = self._ensure_connected()
            try:
                return client.renew_lease(lease_id, duration_ms)
            except _CONNECTION_ERRORS:
                self._reconnect()
                raise SessionInactive(
                    "the bridge connection changed and invalidated the lighting lease"
                ) from None

    def release_lease(self, lease_id: LeaseId) -> v5.LeaseResult:
        with self._lock:
            client = self._ensure_connected()
            try:
                return client.release_lease(lease_id)
            except _CONNECTION_ERRORS:
                self._reconnect()
                raise SessionInactive(
                    "the bridge connection changed and invalidated the lighting lease"
                ) from None

    def submit_transaction(self, submission: TransactionSubmission) -> v5.TransactionResult:
        with self._lock:
            client = self._ensure_connected()
            try:
                return client.submit_transaction(submission)
            except _CONNECTION_ERRORS:
                transaction_id = submission.transaction_id
                try:
                    client = self._reconnect()
                    return client.transaction_outcome(transaction_id)
                except HyperFluxSdkError:
                    return self._unknown_outcome(transaction_id)

    def transaction_outcome(self, transaction_id: TransactionId) -> v5.TransactionResult:
        return self._retry_read(lambda client: client.transaction_outcome(transaction_id))

    def subscribe(self, subscription: EventSubscription) -> v5.EventBatch:
        with self._lock:
            client = self._ensure_connected()
            try:
                return client.subscribe(subscription)
            except _CONNECTION_ERRORS:
                client = self._reconnect()
                fresh = client.subscribe(
                    EventSubscription(None, None, subscription.max_events)
                )
                return replace(fresh, cursor_gap=True, has_more=False)

    def _retry_read(self, operation: Callable[[Client], _Result]) -> _Result:
        with self._lock:
            client = self._ensure_connected()
            try:
                return operation(client)
            except _CONNECTION_ERRORS:
                return operation(self._reconnect())

    def _ensure_connected(self) -> Client:
        if self._connection is None:
            return self._reconnect()
        return self._connection

    def _reconnect(self) -> Client:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._advance_epoch()
        candidate = self._factory.connect()
        self._connection = candidate
        return candidate

    def _advance_epoch(self) -> None:
        if self._connection_epoch == _MAX_CONNECTION_EPOCH:
            raise OverflowError("SDK connection epoch is exhausted")
        self._connection_epoch += 1

    @staticmethod
    def _unknown_outcome(transaction_id: TransactionId) -> v5.TransactionResult:
        return v5.TransactionResultUnavailable(
            v5.TransactionUnavailable(
                transaction_id,
                ProtocolErrorKind.OUTCOME_UNKNOWN,
                FindingId("HFX-OUTCOME-001"),
            )
        )

    def __enter__(self) -> RecoveringClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
