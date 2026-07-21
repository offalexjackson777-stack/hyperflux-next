# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .client import TransactionSubmission
from .errors import (
    InvalidController,
    InvalidLightingFrame,
    OwnershipConflict,
    SessionInactive,
)
from .generated.domain_types import (
    ColorChannel,
    EndpointId,
    GenerationId,
    LeaseDurationMs,
    LeaseId,
    LeaseState,
    LedCount,
    LogicalDeviceId,
    MonotonicMs,
    ProfileDigest,
    ProfileId,
    ReceiverId,
    ResourceKind,
    StableLightingMode,
    TransactionClass,
    TransactionId,
)
from .generated import protocol_v5_types as v5


class LightingIntent(Enum):
    EFFECT_FRAME = "effect-frame"
    STATIC = "static"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class LightingTarget:
    receiver_id: ReceiverId
    generation_id: GenerationId
    device_id: LogicalDeviceId
    endpoint_id: EndpointId
    receiver_profile: v5.ProfileBindingView
    device_profile: v5.ProfileBindingView
    application_slot_count: LedCount
    resource: v5.ResourceKey


@dataclass(frozen=True, slots=True)
class LightingUpdate:
    target: LightingTarget
    colors: tuple[v5.RgbColor, ...]


class LightingBridge(Protocol):
    @property
    def connection_epoch(self) -> int: ...

    def acquire_lease(
        self, resources: tuple[v5.ResourceKey, ...], duration_ms: LeaseDurationMs
    ) -> v5.LeaseResult: ...

    def renew_lease(self, lease_id: LeaseId, duration_ms: LeaseDurationMs) -> v5.LeaseResult: ...

    def release_lease(self, lease_id: LeaseId) -> v5.LeaseResult: ...

    def submit_transaction(self, submission: TransactionSubmission) -> v5.TransactionResult: ...

    def next_transaction_id(self) -> TransactionId: ...


def rgb(red: int, green: int, blue: int) -> v5.RgbColor:
    return v5.RgbColor(ColorChannel(red), ColorChannel(green), ColorChannel(blue))


def lighting_target(controller: v5.ControllerView) -> LightingTarget:
    capabilities = {capability.value for capability in controller.capabilities}
    resource = controller.resource
    if (
        resource.receiver_id != controller.receiver_id
        or resource.generation_id != controller.generation_id
        or resource.device_id != controller.device_id
        or resource.kind is not ResourceKind.LIGHTING
        or controller.lighting.application_slot_count.value == 0
        or "lighting.direct-frame" not in capabilities
    ):
        raise InvalidController(
            "integration controller lacks an exact writable lighting binding"
        )
    return LightingTarget(
        receiver_id=controller.receiver_id,
        generation_id=controller.generation_id,
        device_id=controller.device_id,
        endpoint_id=controller.endpoint_id,
        receiver_profile=controller.receiver_profile,
        device_profile=controller.device_profile,
        application_slot_count=controller.lighting.application_slot_count,
        resource=resource,
    )


def _resource_key(resource: v5.ResourceKey) -> tuple[str, int, str, str]:
    return (
        resource.receiver_id.value,
        resource.generation_id.value,
        resource.device_id.value,
        resource.kind.value,
    )


def _resources(targets: tuple[LightingTarget, ...]) -> tuple[v5.ResourceKey, ...]:
    return tuple(sorted((target.resource for target in targets), key=_resource_key))


def _grant(
    result: v5.LeaseResult,
    expected_resources: tuple[v5.ResourceKey, ...],
    expected_state: LeaseState,
    expected_lease: LeaseId | None = None,
) -> v5.LeaseGrant:
    if isinstance(result, v5.LeaseResultConflict):
        owner = result.detail.conflicting_client.value
        raise OwnershipConflict(f"HyperFlux lighting is controlled by {owner}")
    if isinstance(result, v5.LeaseResultRejected):
        raise OwnershipConflict(
            f"the bridge rejected the lighting lease [{result.detail.finding_id.value}]"
        )
    grant = result.detail
    if (
        grant.state is not expected_state
        or tuple(sorted(grant.resources, key=_resource_key)) != expected_resources
        or (expected_lease is not None and grant.lease_id != expected_lease)
    ):
        raise OwnershipConflict("bridge returned a lease grant with mismatched authority")
    return grant


class LightingSession:
    """One renewable, atomic set of generation-bound lighting resources."""

    def __init__(
        self,
        bridge: LightingBridge,
        targets: tuple[LightingTarget, ...],
        grant: v5.LeaseGrant,
    ) -> None:
        self._bridge = bridge
        self._targets = targets
        self._grant: v5.LeaseGrant | None = grant
        self._connection_epoch = bridge.connection_epoch

    @classmethod
    def acquire(
        cls,
        bridge: LightingBridge,
        targets: tuple[LightingTarget, ...],
        duration_ms: LeaseDurationMs,
    ) -> LightingSession:
        if not 1 <= len(targets) <= 32:
            raise InvalidController("a lighting session requires between one and 32 controllers")
        seen: set[tuple[str, int, str, str]] = set()
        receiver_authority: dict[str, tuple[GenerationId, v5.ProfileBindingView]] = {}
        for target in targets:
            resource = target.resource
            key = _resource_key(resource)
            if (
                target.application_slot_count.value == 0
                or resource.kind is not ResourceKind.LIGHTING
                or resource.receiver_id != target.receiver_id
                or resource.generation_id != target.generation_id
                or resource.device_id != target.device_id
                or key in seen
            ):
                raise InvalidController("a lighting target has duplicate or mismatched authority")
            seen.add(key)
            current = (target.generation_id, target.receiver_profile)
            previous = receiver_authority.setdefault(target.receiver_id.value, current)
            if previous != current:
                raise InvalidController("one receiver has conflicting generation authority")
        resources = _resources(targets)
        result = bridge.acquire_lease(resources, duration_ms)
        grant = _grant(result, resources, LeaseState.GRANTED)
        return cls(bridge, targets, grant)

    @property
    def active(self) -> bool:
        return self._grant is not None

    @property
    def lease_id(self) -> LeaseId | None:
        return None if self._grant is None else self._grant.lease_id

    @property
    def expires_at_ms(self) -> MonotonicMs | None:
        return None if self._grant is None else self._grant.expires_at_ms

    @property
    def targets(self) -> tuple[LightingTarget, ...]:
        return self._targets

    def matches(self, target: LightingTarget) -> bool:
        return self.active and target in self._targets

    def renew(self, duration_ms: LeaseDurationMs) -> None:
        grant = self._require_grant()
        try:
            self._grant = _grant(
                self._bridge.renew_lease(grant.lease_id, duration_ms),
                _resources(self._targets),
                LeaseState.RENEWED,
                grant.lease_id,
            )
        except BaseException:
            self.abandon()
            raise

    def release(self) -> None:
        if self._grant is None:
            return
        grant = self._require_grant()
        try:
            _grant(
                self._bridge.release_lease(grant.lease_id),
                _resources(self._targets),
                LeaseState.RELEASED,
                grant.lease_id,
            )
        except BaseException:
            self.abandon()
            raise
        self._grant = None

    def abandon(self) -> None:
        self._grant = None

    def submit(
        self,
        intent: LightingIntent,
        updates: tuple[LightingUpdate, ...],
        deadline_ms: MonotonicMs,
    ) -> v5.TransactionResult:
        grant = self._require_grant()
        if not updates:
            raise InvalidLightingFrame("a lighting transaction requires at least one frame")
        authority = updates[0].target
        seen: set[tuple[str, int, str, str]] = set()
        for update in updates:
            target = update.target
            key = _resource_key(target.resource)
            if (
                target.receiver_id != authority.receiver_id
                or target.generation_id != authority.generation_id
                or target.receiver_profile != authority.receiver_profile
            ):
                raise InvalidLightingFrame(
                    "one lighting transaction must stay inside one receiver generation"
                )
            if target not in self._targets or key in seen:
                raise InvalidLightingFrame(
                    "lighting transaction names a duplicate or unowned controller"
                )
            seen.add(key)
            if len(update.colors) != target.application_slot_count.value:
                raise InvalidLightingFrame(
                    "lighting frame does not match the qualified application slot count"
                )
            if intent is LightingIntent.OFF and any(
                color.red.value or color.green.value or color.blue.value
                for color in update.colors
            ):
                raise InvalidLightingFrame("an Off intent may contain only black values")
        transaction_class = (
            TransactionClass.EFFECT_FRAME
            if intent is LightingIntent.EFFECT_FRAME
            else TransactionClass.STATIC_LIGHTING
        )
        stable_intents: tuple[v5.StableLightingIntent, ...] = ()
        if intent is not LightingIntent.EFFECT_FRAME:
            mode = StableLightingMode.OFF if intent is LightingIntent.OFF else StableLightingMode.STATIC
            stable_intents = tuple(
                v5.StableLightingIntent(update.target.device_id, mode) for update in updates
            )
        device_profiles = tuple(
            v5.DeviceProfileBinding(
                device_id=update.target.device_id,
                profile_id=update.target.device_profile.profile_id,
                profile_digest=update.target.device_profile.profile_digest,
                application_slot_count=update.target.application_slot_count,
            )
            for update in updates
        )
        frames = tuple(
            v5.LightingFrame(update.target.device_id, index, update.colors)
            for index, update in enumerate(updates)
        )
        resources = tuple(sorted((update.target.resource for update in updates), key=_resource_key))
        return self._bridge.submit_transaction(
            TransactionSubmission(
                transaction_id=self._bridge.next_transaction_id(),
                lease_id=grant.lease_id,
                receiver_id=authority.receiver_id,
                generation_id=authority.generation_id,
                receiver_profile_id=authority.receiver_profile.profile_id,
                receiver_profile_digest=authority.receiver_profile.profile_digest,
                device_profiles=device_profiles,
                transaction_class=transaction_class,
                stable_intents=stable_intents,
                deadline_ms=deadline_ms,
                resources=resources,
                frames=frames,
            )
        )

    def _require_grant(self) -> v5.LeaseGrant:
        if self._grant is None:
            raise SessionInactive("the HyperFlux lighting session is no longer active")
        if self._bridge.connection_epoch != self._connection_epoch:
            self.abandon()
            raise SessionInactive(
                "the bridge connection changed and invalidated the lighting lease"
            )
        return self._grant

    def __enter__(self) -> LightingSession:
        return self

    def __exit__(self, exception_type: object, *_: object) -> None:
        if exception_type is None:
            self.release()
            return
        try:
            self.release()
        except BaseException:
            self.abandon()
