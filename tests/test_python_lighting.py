# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from hyperflux_sdk.client import TransactionSubmission
from hyperflux_sdk.codec import from_wire
from hyperflux_sdk.errors import InvalidLightingFrame, OwnershipConflict, SessionInactive
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    FindingId,
    LeaseDurationMs,
    LeaseId,
    LeaseState,
    MonotonicMs,
    ProtocolErrorKind,
    TransactionId,
)
from hyperflux_sdk.generated import protocol_v5_types as v5
from hyperflux_sdk.lighting import (
    LightingIntent,
    LightingSession,
    LightingUpdate,
    lighting_target,
    rgb,
)


def _controller() -> v5.ControllerView:
    return from_wire(
        v5.ControllerView,
        {
            "receiver_id": "receiver-1",
            "generation_id": "7",
            "device_id": "mouse-1",
            "endpoint_id": "mouse-wireless",
            "device_kind": "mouse",
            "product_id": 205,
            "receiver_profile": {
                "profile_id": "receiver.razer.hyperflux-v2.00cf",
                "profile_digest": "a" * 64,
            },
            "device_profile": {
                "profile_id": "child.razer.basilisk-v3-pro-35k.00cd",
                "profile_digest": "b" * 64,
            },
            "model_name": "Razer Basilisk V3 Pro 35K",
            "presentation": {
                "upstream_id": "openrgb",
                "owner": "OpenRGB",
                "project_version": "1.0rc3",
                "source_revision": "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0",
                "model_key": "basilisk_v3_pro_35k_wireless_device",
                "layout_key": None,
                "transport_variant": "wireless",
            },
            "availability": "ready",
            "battery": {
                "availability": "reported",
                "percentage": 79,
                "freshness": "fresh",
                "confidence": "observed",
                "observed_at_ms": "10",
            },
            "capabilities": ["lighting.direct-frame", "lighting.static", "lighting.off"],
            "lighting": {
                "physical_led_count": 13,
                "application_slot_count": 13,
                "rows": 1,
                "columns": 13,
            },
            "resource": {
                "receiver_id": "receiver-1",
                "generation_id": "7",
                "device_id": "mouse-1",
                "kind": "lighting",
            },
            "ownership": {"state": "unowned", "detail": {}},
            "actions": {"can_acquire": True, "can_release": False, "can_submit_now": False},
        },
    )


class FakeBridge:
    def __init__(self) -> None:
        self.resources: tuple[v5.ResourceKey, ...] = ()
        self.submissions: list[TransactionSubmission] = []
        self.counter = 0
        self.connection_epoch = 1

    def acquire_lease(self, resources, duration_ms):
        self.resources = resources
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                LeaseId("lease-1"),
                ClientId("polychromatic"),
                resources,
                MonotonicMs(10_000),
                LeaseState.GRANTED,
            )
        )

    def renew_lease(self, lease_id, duration_ms):
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("polychromatic"),
                self.resources,
                MonotonicMs(20_000),
                LeaseState.RENEWED,
            )
        )

    def release_lease(self, lease_id):
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("polychromatic"),
                self.resources,
                MonotonicMs(20_000),
                LeaseState.RELEASED,
            )
        )

    def next_transaction_id(self):
        self.counter += 1
        return TransactionId(f"transaction-{self.counter}")

    def submit_transaction(self, submission):
        self.submissions.append(submission)
        return v5.TransactionResultUnavailable(
            v5.TransactionUnavailable(
                submission.transaction_id,
                ProtocolErrorKind.OUTCOME_UNKNOWN,
                FindingId("HFX-TRANSACTION-001"),
            )
        )


class PythonLightingTests(unittest.TestCase):
    def test_session_binds_profile_generation_and_static_intent(self) -> None:
        target = lighting_target(_controller())
        bridge = FakeBridge()
        session = LightingSession.acquire(bridge, (target,), LeaseDurationMs(5_000))
        colors = tuple(rgb(1, 2, 3) for _ in range(13))
        session.submit(
            LightingIntent.STATIC,
            (LightingUpdate(target, colors),),
            MonotonicMs(9_000),
        )
        submission = bridge.submissions[0]
        self.assertEqual(submission.generation_id.value, 7)
        self.assertEqual(submission.device_profiles[0].application_slot_count.value, 13)
        self.assertEqual(submission.stable_intents[0].mode.value, "static")
        self.assertEqual(len(submission.frames[0].colors), 13)
        session.release()
        self.assertFalse(session.active)
        with self.assertRaises(SessionInactive):
            session.submit(
                LightingIntent.STATIC,
                (LightingUpdate(target, colors),),
                MonotonicMs(9_000),
            )

    def test_off_rejects_non_black_and_lease_conflict_is_atomic(self) -> None:
        target = lighting_target(_controller())
        session = LightingSession.acquire(FakeBridge(), (target,), LeaseDurationMs(5_000))
        with self.assertRaises(InvalidLightingFrame):
            session.submit(
                LightingIntent.OFF,
                (LightingUpdate(target, tuple(rgb(1, 0, 0) for _ in range(13))),),
                MonotonicMs(9_000),
            )

        class ConflictBridge(FakeBridge):
            def acquire_lease(self, resources, duration_ms):
                return v5.LeaseResultConflict(
                    v5.LeaseConflict(
                        ClientId("openrgb"),
                        resources[0],
                    )
                )

        with self.assertRaisesRegex(OwnershipConflict, "openrgb"):
            LightingSession.acquire(ConflictBridge(), (target,), LeaseDurationMs(5_000))

    def test_connection_epoch_change_invalidates_an_existing_lease(self) -> None:
        target = lighting_target(_controller())
        bridge = FakeBridge()
        session = LightingSession.acquire(bridge, (target,), LeaseDurationMs(5_000))
        bridge.connection_epoch += 1
        with self.assertRaisesRegex(SessionInactive, "connection changed"):
            session.submit(
                LightingIntent.STATIC,
                (LightingUpdate(target, tuple(rgb(1, 2, 3) for _ in range(13))),),
                MonotonicMs(9_000),
            )
        self.assertFalse(session.active)


if __name__ == "__main__":
    unittest.main()
