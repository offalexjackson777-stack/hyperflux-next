# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
from threading import RLock
import time
from typing import Callable

from hyperflux_sdk.channel import UnixChannelConfig
from hyperflux_sdk.client import ClientConfig
from hyperflux_sdk.errors import (
    HyperFluxSdkError,
    OwnershipConflict,
)
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    ClientName,
    ControllerAvailability,
    DeviceApplicationState,
    DeviceKind,
    LeaseDurationMs,
    MonotonicMs,
    ProtocolFeatureId,
    SideEffectCertainty,
    TelemetryAvailability,
    TransactionId,
    TransactionState,
)
from hyperflux_sdk.generated.openrazer_metadata import OPENRAZER_DEVICES_BY_PROFILE
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

from .state import Rgb, StableState, StateStore


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


class IntegrationError(RuntimeError):
    """One user-facing native Polychromatic integration failure."""


class ControllerUnavailable(IntegrationError):
    """A paired controller currently has no writable receiver route."""


class TransactionIncomplete(IntegrationError):
    """A transaction did not reach one complete committed terminal result."""


@dataclass(frozen=True, slots=True)
class ControllerRecord:
    serial: str
    controller: v5.ControllerView
    image_url: str
    model_name: str
    vendor_id: int
    product_id: int
    rows: int
    columns: int

    @property
    def led_count(self) -> int:
        return self.controller.lighting.application_slot_count.value

    @property
    def device_kind(self) -> DeviceKind:
        return self.controller.device_kind


@dataclass(slots=True)
class _EffectContext:
    target: LightingTarget
    session: LightingSession
    pending_transaction: TransactionId | None = None


def pseudonymous_serial(receiver_id: str, device_id: str) -> str:
    """Derive a stable application key without exposing hardware serials or routes."""

    material = b"hyperflux-next-polychromatic-v1\0" + receiver_id.encode("utf-8")
    material += b"\0" + device_id.encode("utf-8")
    return "hfx-" + hashlib.sha256(material).hexdigest()[:24]


def default_client() -> RecoveringClient:
    suffix = f"{os.getpid():x}-{secrets.token_hex(6)}"
    config = ClientConfig(
        client_id=ClientId(f"polychromatic-{suffix}"),
        client_name=ClientName("HyperFlux Next Polychromatic backend"),
        required_features=tuple(ProtocolFeatureId(value) for value in CLIENT_FEATURES),
        optional_features=tuple(ProtocolFeatureId(value) for value in OPTIONAL_FEATURES),
    )
    return RecoveringClient(UnixClientFactory(UnixChannelConfig(), config))


class HyperFluxRuntime:
    """SDK-only controller projection and lighting lifecycle for Polychromatic."""

    def __init__(
        self,
        client: RecoveringClient,
        state_store: StateStore,
        *,
        metadata: Mapping[str, Mapping[str, object]] = OPENRAZER_DEVICES_BY_PROFILE,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._state_store = state_store
        self._metadata = metadata
        self._clock = clock
        self._sleeper = sleeper
        self._records: dict[str, ControllerRecord] = {}
        self._effects: dict[str, _EffectContext] = {}
        self._effect_failures: dict[str, int] = {}
        self._lock = RLock()

    @classmethod
    def production(cls, storage_directory: Path) -> HyperFluxRuntime:
        return cls(default_client(), StateStore(storage_directory / "stable-lighting.json"))

    def close(self) -> None:
        with self._lock:
            for context in self._effects.values():
                try:
                    context.session.release()
                except BaseException:
                    context.session.abandon()
            self._effects.clear()
            self._effect_failures.clear()
            self._client.close()

    def refresh(self) -> tuple[ControllerRecord, ...]:
        with self._lock:
            view = self._client.integration_view()
            records: dict[str, ControllerRecord] = {}
            for receiver in view.receivers:
                for controller in receiver.controllers:
                    record = self._record(controller)
                    if record.serial in records:
                        raise IntegrationError(
                            "the bridge projected duplicate Polychromatic controller identity"
                        )
                    records[record.serial] = record
            self._records = records
            return self.records(refresh=False)

    def records(self, *, refresh: bool = True) -> tuple[ControllerRecord, ...]:
        with self._lock:
            if refresh:
                self.refresh()
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda value: (
                        value.device_kind.value,
                        value.model_name.casefold(),
                        value.serial,
                    ),
                )
            )

    def record(self, serial: str, *, refresh: bool = True) -> ControllerRecord:
        with self._lock:
            if refresh:
                self.refresh()
            try:
                return self._records[serial]
            except KeyError as error:
                raise ControllerUnavailable(
                    "the HyperFlux controller is no longer available through the receiver"
                ) from error

    def stable_state(self, serial: str) -> StableState:
        with self._lock:
            record = self.record(serial, refresh=False)
            return self._state_store.load(serial, record.led_count)

    def apply_static(self, serial: str, indexes: tuple[int, ...], color: Rgb) -> None:
        with self._lock:
            record = self.record(serial)
            state = self._state_store.load(serial, record.led_count)
            self._validate_indexes(indexes, record.led_count)
            if state.mode == "unknown" and len(indexes) != record.led_count:
                raise IntegrationError(
                    "set the complete device to Static or Off before changing one zone"
                )
            colors = list(state.colors)
            for index in indexes:
                colors[index] = color
            candidate = StableState("static", state.brightness, tuple(colors))
            self._submit_stable(record, candidate, LightingIntent.STATIC)
            self._state_store.save(serial, candidate)

    def apply_off(self, serial: str, indexes: tuple[int, ...]) -> None:
        with self._lock:
            record = self.record(serial)
            state = self._state_store.load(serial, record.led_count)
            self._validate_indexes(indexes, record.led_count)
            if state.mode == "unknown" and len(indexes) != record.led_count:
                raise IntegrationError(
                    "set the complete device to Static or Off before changing one zone"
                )
            colors = list(state.colors)
            for index in indexes:
                colors[index] = (0, 0, 0)
            complete_off = not any(any(color) for color in colors)
            mode = "off" if complete_off else "static"
            intent = LightingIntent.OFF if complete_off else LightingIntent.STATIC
            candidate = StableState(mode, state.brightness, tuple(colors))
            self._submit_stable(record, candidate, intent)
            self._state_store.save(serial, candidate)

    def apply_brightness(self, serial: str, brightness: int) -> None:
        with self._lock:
            record = self.record(serial)
            state = self._state_store.load(serial, record.led_count)
            if state.mode == "unknown":
                raise IntegrationError(
                    "choose Static or Off once before changing HyperFlux brightness"
                )
            candidate = StableState(state.mode, brightness, state.colors)
            intent = LightingIntent.OFF if state.mode == "off" else LightingIntent.STATIC
            self._submit_stable(record, candidate, intent)
            self._state_store.save(serial, candidate)

    def effect_frame(self, serial: str, colors: tuple[Rgb, ...], brightness: int) -> bool:
        """Submit one software-effect frame; temporary absence skips rather than kills playback."""

        with self._lock:
            try:
                record = self.record(serial, refresh=False)
            except ControllerUnavailable:
                try:
                    record = self.record(serial)
                except (ControllerUnavailable, HyperFluxSdkError):
                    self._effect_failures.pop(serial, None)
                    return False
            if len(colors) != record.led_count:
                raise IntegrationError("Polychromatic effect frame dimensions changed unexpectedly")
            if record.controller.availability is not ControllerAvailability.READY:
                try:
                    record = self.record(serial)
                except (ControllerUnavailable, HyperFluxSdkError):
                    return False
                if record.controller.availability is not ControllerAvailability.READY:
                    self._effect_failures.pop(serial, None)
                    return False
            scaled = self._scaled(colors, brightness)
            try:
                context = self._effect_context(record)
                self._poll_effect(context)
                self._renew_effect(context)
                result = context.session.submit(
                    LightingIntent.EFFECT_FRAME,
                    (LightingUpdate(context.target, self._wire_colors(scaled)),),
                    self._deadline_ms(),
                )
                context.pending_transaction = self._consume_effect_result(result)
                self._effect_failures.pop(serial, None)
                return True
            except OwnershipConflict:
                self._abandon_effect(serial)
                self._effect_failures.pop(serial, None)
                raise
            except (HyperFluxSdkError, IntegrationError):
                failures = self._effect_failures.get(serial, 0) + 1
                self._effect_failures[serial] = failures
                self._abandon_effect(serial)
                try:
                    current = self.record(serial)
                except (ControllerUnavailable, HyperFluxSdkError):
                    self._effect_failures.pop(serial, None)
                    return False
                if current.controller.availability is not ControllerAvailability.READY:
                    self._effect_failures.pop(serial, None)
                    return False
                if failures < 3:
                    return False
                raise IntegrationError(
                    "HyperFlux could not deliver three consecutive Polychromatic effect frames"
                ) from None

    def release_effect(self, serial: str) -> None:
        with self._lock:
            self._abandon_effect(serial, release=True)
            self._effect_failures.pop(serial, None)

    def _record(self, controller: v5.ControllerView) -> ControllerRecord:
        profile_id = controller.device_profile.profile_id.value
        metadata = self._metadata.get(profile_id)
        model_name = controller.model_name.value
        image_url = ""
        vendor_id = 0x1532
        product_id = controller.product_id.value
        rows = controller.lighting.rows.value
        columns = controller.lighting.columns.value
        if metadata is not None:
            identity = metadata.get("identity")
            presentation = metadata.get("presentation")
            if not isinstance(identity, Mapping) or not isinstance(presentation, Mapping):
                raise IntegrationError("pinned OpenRazer metadata is structurally invalid")
            expected = (
                identity.get("product_id"),
                identity.get("device_kind"),
                presentation.get("matrix_rows"),
                presentation.get("matrix_columns"),
            )
            actual = (product_id, controller.device_kind.value, rows, columns)
            if expected != actual:
                raise IntegrationError(
                    "pinned OpenRazer metadata does not match qualified bridge authority"
                )
            model_name = str(identity["model_name"])
            image_url = str(presentation["image_url"])
            vendor_id = int(identity["vendor_id"])
        return ControllerRecord(
            serial=pseudonymous_serial(
                controller.receiver_id.value,
                controller.device_id.value,
            ),
            controller=controller,
            image_url=image_url,
            model_name=model_name,
            vendor_id=vendor_id,
            product_id=product_id,
            rows=rows,
            columns=columns,
        )

    def _submit_stable(
        self,
        record: ControllerRecord,
        state: StableState,
        intent: LightingIntent,
    ) -> None:
        if record.controller.availability is not ControllerAvailability.READY:
            raise ControllerUnavailable("the paired controller is sleeping or unavailable")
        target = lighting_target(record.controller)
        colors = self._scaled(state.colors, state.brightness)
        session = LightingSession.acquire(
            self._client,
            (target,),
            LeaseDurationMs(LEASE_DURATION_MS),
        )
        try:
            result = session.submit(
                intent,
                (LightingUpdate(target, self._wire_colors(colors)),),
                self._deadline_ms(),
            )
            self._await_complete(result, 1)
        finally:
            try:
                session.release()
            except BaseException:
                session.abandon()

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
                "a prior effect frame has no retained outcome and was not replayed"
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
                    "transaction outcome is still pending and was not replayed"
                )
            self._sleeper(TRANSACTION_POLL_SECONDS)
            result = self._client.transaction_outcome(result.detail.transaction_id)
        if isinstance(result, v5.TransactionResultUnavailable):
            raise TransactionIncomplete(
                "transaction outcome is unavailable and was not replayed"
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
                f"transaction ended as {terminal.state.value} without complete delivery"
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

    def _deadline_ms(self) -> MonotonicMs:
        return MonotonicMs(min(MAX_MONOTONIC_MS, self._now_ms() + 5_000))

    def _now_ms(self) -> int:
        return min(MAX_MONOTONIC_MS, int(self._clock() * 1_000))

    @staticmethod
    def _validate_indexes(indexes: tuple[int, ...], led_count: int) -> None:
        if (
            not indexes
            or len(indexes) != len(set(indexes))
            or any(isinstance(index, bool) or not 0 <= index < led_count for index in indexes)
        ):
            raise IntegrationError("Polychromatic zone contains an invalid LED index")

    @staticmethod
    def _scaled(colors: tuple[Rgb, ...], brightness: int) -> tuple[Rgb, ...]:
        if isinstance(brightness, bool) or not 0 <= brightness <= 100:
            raise IntegrationError("Polychromatic brightness must be from 0 through 100")
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

    def battery(self, serial: str) -> int | None:
        with self._lock:
            try:
                record = self.record(serial)
            except ControllerUnavailable:
                return None
            return self.battery_from_record(record)

    @staticmethod
    def battery_from_record(record: ControllerRecord) -> int | None:
        observation = record.controller.battery
        if (
            observation.availability is not TelemetryAvailability.REPORTED
            or observation.percentage is None
        ):
            return None
        return observation.percentage.value
