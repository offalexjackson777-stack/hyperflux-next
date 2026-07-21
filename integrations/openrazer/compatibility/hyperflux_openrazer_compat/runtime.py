# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from threading import RLock
import time
from typing import Callable, Protocol

from hyperflux_sdk.channel import UnixChannelConfig
from hyperflux_sdk.client import ClientConfig, TransactionSubmission
from hyperflux_sdk.errors import HyperFluxSdkError, OwnershipConflict
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    ClientName,
    ControllerAvailability,
    DeviceApplicationState,
    LeaseDurationMs,
    LeaseId,
    MonotonicMs,
    ProtocolFeatureId,
    SideEffectCertainty,
    TransactionId,
    TransactionState,
)
from hyperflux_sdk.generated import protocol_v5_types as v5
from hyperflux_sdk.lighting import (
    LightingIntent,
    LightingSession,
    LightingTarget,
    LightingUpdate,
    lighting_target,
    rgb,
)
from hyperflux_sdk.recovery import RecoveringClient, UnixClientFactory

from .matrix import MatrixBuffer, MatrixError, Rgb
from .model import ControllerRecord, records_from_view


CLIENT_FEATURES = (
    "atomic-transactions",
    "integration-view-projection",
    "ownership-leases",
    "profile-bound-transactions",
    "semantic-stable-lighting",
)
OPTIONAL_FEATURES = ("structured-diagnostics",)
LEASE_DURATION_MS = 10_000
LEASE_RENEW_WINDOW_MS = 3_000
TRANSACTION_TIMEOUT_SECONDS = 5.0
TRANSACTION_POLL_SECONDS = 0.01
MAX_MONOTONIC_MS = (1 << 64) - 1


class CompatibilityRuntimeError(RuntimeError):
    """One explicit compatibility failure suitable for a D-Bus error."""


class ControllerUnavailable(CompatibilityRuntimeError):
    """The receiver route cannot currently accept a controller write."""


class TransactionIncomplete(CompatibilityRuntimeError):
    """A transaction did not establish one complete committed outcome."""


class RuntimeClient(Protocol):
    @property
    def connection_epoch(self) -> int: ...

    def integration_view(self) -> v5.IntegrationView: ...

    def next_transaction_id(self) -> TransactionId: ...

    def acquire_lease(
        self, resources: tuple[v5.ResourceKey, ...], duration_ms: LeaseDurationMs
    ) -> v5.LeaseResult: ...

    def renew_lease(
        self, lease_id: LeaseId, duration_ms: LeaseDurationMs
    ) -> v5.LeaseResult: ...

    def release_lease(self, lease_id: LeaseId) -> v5.LeaseResult: ...

    def submit_transaction(self, submission: TransactionSubmission) -> v5.TransactionResult: ...

    def transaction_outcome(self, transaction_id: TransactionId) -> v5.TransactionResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LightingState:
    mode: str
    brightness: int
    colors: tuple[Rgb, ...]


@dataclass(slots=True)
class _EffectContext:
    target: LightingTarget
    session: LightingSession
    pending_transaction: TransactionId | None = None


def default_client(socket_path: Path | None = None) -> RecoveringClient:
    suffix = f"{os.getpid():x}-{secrets.token_hex(6)}"
    config = ClientConfig(
        client_id=ClientId(f"openrazer-compat-{suffix}"),
        client_name=ClientName("HyperFlux Next private OpenRazer compatibility"),
        required_features=tuple(ProtocolFeatureId(value) for value in CLIENT_FEATURES),
        optional_features=tuple(ProtocolFeatureId(value) for value in OPTIONAL_FEATURES),
    )
    channel = (
        UnixChannelConfig()
        if socket_path is None
        else UnixChannelConfig(socket_path=socket_path)
    )
    return RecoveringClient(UnixClientFactory(channel, config))


class OpenRazerRuntime:
    """SDK-only OpenRazer projection with no raw receiver access or write replay."""

    def __init__(
        self,
        client: RuntimeClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleeper = sleeper
        self._records: dict[str, ControllerRecord] = {}
        self._states: dict[str, LightingState] = {}
        self._matrices: dict[str, MatrixBuffer] = {}
        self._effects: dict[str, _EffectContext] = {}
        self._effect_failures: dict[str, int] = {}
        self._lock = RLock()

    @classmethod
    def production(cls, socket_path: Path | None = None) -> OpenRazerRuntime:
        return cls(default_client(socket_path))

    def close(self) -> None:
        with self._lock:
            for serial in tuple(self._effects):
                self._abandon_effect(serial, release=True)
            self._client.close()

    def refresh(self) -> tuple[ControllerRecord, ...]:
        with self._lock:
            records = {record.serial: record for record in records_from_view(self._client.integration_view())}
            for serial, previous in tuple(self._records.items()):
                current = records.get(serial)
                if current is None or not _same_authority(previous, current):
                    self._abandon_effect(serial, release=True)
                    self._matrices.pop(serial, None)
                if current is None:
                    self._states.pop(serial, None)
                    self._effect_failures.pop(serial, None)
                    continue
                state = self._states.get(serial)
                if state is not None and len(state.colors) != current.led_count:
                    self._states.pop(serial, None)
            self._records = records
            return self.records(refresh=False)

    def records(self, *, refresh: bool = True) -> tuple[ControllerRecord, ...]:
        with self._lock:
            if refresh:
                self.refresh()
            return tuple(self._records[key] for key in sorted(self._records))

    def record(self, serial: str, *, refresh: bool = True) -> ControllerRecord:
        with self._lock:
            if refresh:
                self.refresh()
            try:
                return self._records[serial]
            except KeyError as error:
                raise ControllerUnavailable(
                    "the HyperFlux controller is no longer exported by the private provider"
                ) from error

    def brightness(self, serial: str) -> float:
        with self._lock:
            record = self.record(serial, refresh=False)
            return float(self._state(serial, record.led_count).brightness)

    def apply_static(self, serial: str, color: Rgb) -> None:
        _validate_rgb(color)
        with self._lock:
            record = self.record(serial)
            current = self._state(serial, record.led_count)
            candidate = LightingState("static", current.brightness, (color,) * record.led_count)
            self._submit_stable(record, candidate, LightingIntent.STATIC)
            self._states[serial] = candidate
            self._matrices.pop(serial, None)

    def apply_off(self, serial: str) -> None:
        with self._lock:
            record = self.record(serial)
            current = self._state(serial, record.led_count)
            candidate = LightingState(
                "off",
                current.brightness,
                ((0, 0, 0),) * record.led_count,
            )
            self._submit_stable(record, candidate, LightingIntent.OFF)
            self._states[serial] = candidate
            self._matrices.pop(serial, None)

    def apply_brightness(self, serial: str, brightness: float) -> None:
        if isinstance(brightness, bool) or not isinstance(brightness, (int, float)):
            raise CompatibilityRuntimeError("OpenRazer brightness must be numeric")
        rounded = int(round(float(brightness)))
        if not 0 <= rounded <= 100 or abs(float(brightness) - rounded) > 0.000_001:
            raise CompatibilityRuntimeError(
                "OpenRazer brightness must be a whole percentage from 0 through 100"
            )
        with self._lock:
            record = self.record(serial)
            current = self._state(serial, record.led_count)
            if current.mode == "unknown":
                raise CompatibilityRuntimeError(
                    "set Static, Off, or one complete matrix frame before changing brightness"
                )
            candidate = LightingState(current.mode, rounded, current.colors)
            if current.mode == "effect":
                self._submit_effect(record, candidate.colors, candidate.brightness)
            else:
                intent = LightingIntent.OFF if current.mode == "off" else LightingIntent.STATIC
                self._submit_stable(record, candidate, intent)
            self._states[serial] = candidate

    def stage_matrix(self, serial: str, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise MatrixError("OpenRazer matrix data must be a byte array")
        with self._lock:
            record = self.record(serial, refresh=False)
            self._matrices.setdefault(serial, MatrixBuffer()).stage(record, payload)

    def commit_matrix(self, serial: str) -> None:
        with self._lock:
            record = self.record(serial)
            matrix = self._matrices.get(serial)
            if matrix is None:
                raise MatrixError("no OpenRazer matrix frame has been staged")
            colors = matrix.complete(record)
            current = self._state(serial, record.led_count)
            self._submit_effect(record, colors, current.brightness)
            self._states[serial] = LightingState("effect", current.brightness, colors)
            matrix.clear()

    def _state(self, serial: str, led_count: int) -> LightingState:
        state = self._states.get(serial)
        if state is None:
            return LightingState("unknown", 100, ((0, 0, 0),) * led_count)
        if len(state.colors) != led_count:
            raise CompatibilityRuntimeError("controller lighting dimensions changed")
        return state

    def _submit_stable(
        self,
        record: ControllerRecord,
        state: LightingState,
        intent: LightingIntent,
    ) -> None:
        self._require_ready(record)
        self._abandon_effect(record.serial, release=True)
        target = lighting_target(record.controller)
        session = LightingSession.acquire(
            self._client,
            (target,),
            LeaseDurationMs(LEASE_DURATION_MS),
        )
        try:
            result = session.submit(
                intent,
                (LightingUpdate(target, self._wire_colors(self._scaled(state.colors, state.brightness))),),
                self._deadline_ms(),
            )
            self._await_complete(result, 1)
        finally:
            try:
                session.release()
            except BaseException:
                session.abandon()

    def _submit_effect(
        self,
        record: ControllerRecord,
        colors: tuple[Rgb, ...],
        brightness: int,
    ) -> None:
        self._require_ready(record)
        if len(colors) != record.led_count:
            raise CompatibilityRuntimeError("OpenRazer frame dimensions changed unexpectedly")
        try:
            context = self._effect_context(record)
            self._poll_effect(context)
            self._renew_effect(context)
            result = context.session.submit(
                LightingIntent.EFFECT_FRAME,
                (LightingUpdate(context.target, self._wire_colors(self._scaled(colors, brightness))),),
                self._deadline_ms(),
            )
            context.pending_transaction = self._consume_effect_result(result)
            self._effect_failures.pop(record.serial, None)
        except OwnershipConflict:
            self._abandon_effect(record.serial)
            self._effect_failures.pop(record.serial, None)
            raise
        except (HyperFluxSdkError, CompatibilityRuntimeError):
            failures = self._effect_failures.get(record.serial, 0) + 1
            self._effect_failures[record.serial] = failures
            self._abandon_effect(record.serial)
            if failures < 3:
                raise CompatibilityRuntimeError(
                    "the OpenRazer frame was not accepted and was not replayed"
                ) from None
            raise CompatibilityRuntimeError(
                "three consecutive OpenRazer frames failed without replay"
            ) from None

    def _effect_context(self, record: ControllerRecord) -> _EffectContext:
        target = lighting_target(record.controller)
        current = self._effects.get(record.serial)
        if current is not None and current.session.matches(target):
            return current
        self._abandon_effect(record.serial, release=True)
        session = LightingSession.acquire(
            self._client,
            (target,),
            LeaseDurationMs(LEASE_DURATION_MS),
        )
        context = _EffectContext(target, session)
        self._effects[record.serial] = context
        return context

    def _renew_effect(self, context: _EffectContext) -> None:
        expires = context.session.expires_at_ms
        if expires is None or expires.value - self._now_ms() <= LEASE_RENEW_WINDOW_MS:
            context.session.renew(LeaseDurationMs(LEASE_DURATION_MS))

    def _poll_effect(self, context: _EffectContext) -> None:
        if context.pending_transaction is None:
            return
        result = self._client.transaction_outcome(context.pending_transaction)
        context.pending_transaction = self._consume_effect_result(result)

    def _consume_effect_result(self, result: v5.TransactionResult) -> TransactionId | None:
        if isinstance(result, v5.TransactionResultProgress):
            return result.detail.transaction_id
        if isinstance(result, v5.TransactionResultUnavailable):
            raise TransactionIncomplete(
                "an OpenRazer frame has no retained outcome and was not replayed"
            )
        terminal = result.detail
        if terminal.state is TransactionState.SUPERSEDED:
            return None
        self._require_complete_terminal(terminal, 1)
        return None

    def _await_complete(self, result: v5.TransactionResult, expected_frames: int) -> None:
        end = self._clock() + TRANSACTION_TIMEOUT_SECONDS
        while isinstance(result, v5.TransactionResultProgress):
            if self._clock() >= end:
                raise TransactionIncomplete(
                    "the OpenRazer transaction remained pending and was not replayed"
                )
            self._sleeper(TRANSACTION_POLL_SECONDS)
            result = self._client.transaction_outcome(result.detail.transaction_id)
        if isinstance(result, v5.TransactionResultUnavailable):
            raise TransactionIncomplete(
                "the OpenRazer transaction outcome is unavailable and was not replayed"
            )
        self._require_complete_terminal(result.detail, expected_frames)

    @staticmethod
    def _require_complete_terminal(
        terminal: v5.TransactionTerminal,
        expected_frames: int,
    ) -> None:
        if not (
            terminal.state is TransactionState.SUCCEEDED
            and terminal.declared_frames.value == expected_frames
            and terminal.delivered_frames.value == expected_frames
            and terminal.side_effect_certainty is SideEffectCertainty.COMMITTED
            and terminal.live_write_executed
            and terminal.device_application
            in (DeviceApplicationState.CONFIRMED, DeviceApplicationState.UNVERIFIED)
        ):
            raise TransactionIncomplete(
                f"the OpenRazer transaction ended as {terminal.state.value} without complete delivery"
            )

    def _abandon_effect(self, serial: str, *, release: bool = False) -> None:
        context = self._effects.pop(serial, None)
        if context is None:
            return
        if release:
            try:
                context.session.release()
                return
            except BaseException:
                pass
        context.session.abandon()

    @staticmethod
    def _require_ready(record: ControllerRecord) -> None:
        if record.controller.availability is not ControllerAvailability.READY:
            raise ControllerUnavailable("the paired controller is sleeping or unavailable")

    def _deadline_ms(self) -> MonotonicMs:
        return MonotonicMs(min(MAX_MONOTONIC_MS, self._now_ms() + 5_000))

    def _now_ms(self) -> int:
        return min(MAX_MONOTONIC_MS, int(self._clock() * 1_000))

    @staticmethod
    def _scaled(colors: tuple[Rgb, ...], brightness: int) -> tuple[Rgb, ...]:
        return tuple(
            (
                (color[0] * brightness + 50) // 100,
                (color[1] * brightness + 50) // 100,
                (color[2] * brightness + 50) // 100,
            )
            for color in colors
        )

    @staticmethod
    def _wire_colors(colors: tuple[Rgb, ...]) -> tuple[v5.RgbColor, ...]:
        return tuple(rgb(*color) for color in colors)


def _same_authority(left: ControllerRecord, right: ControllerRecord) -> bool:
    first = left.controller
    second = right.controller
    return (
        first.receiver_id == second.receiver_id
        and first.generation_id == second.generation_id
        and first.device_id == second.device_id
        and first.endpoint_id == second.endpoint_id
        and first.receiver_profile == second.receiver_profile
        and first.device_profile == second.device_profile
        and left.rows == right.rows
        and left.columns == right.columns
    )


def _validate_rgb(color: Rgb) -> None:
    if (
        len(color) != 3
        or any(isinstance(value, bool) or not isinstance(value, int) for value in color)
        or any(not 0 <= value <= 255 for value in color)
    ):
        raise CompatibilityRuntimeError("OpenRazer supplied an invalid RGB color")


__all__ = [
    "CompatibilityRuntimeError",
    "ControllerUnavailable",
    "LightingState",
    "OpenRazerRuntime",
    "RuntimeClient",
    "TransactionIncomplete",
    "default_client",
]
