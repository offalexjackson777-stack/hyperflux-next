# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]

from polychromatic.backends._backend import Backend
from polychromatic.middleman import Middleman

from hyperflux_sdk.client import TransactionSubmission
from hyperflux_sdk.codec import from_wire
from hyperflux_sdk.errors import HyperFluxSdkError, OwnershipConflict
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    FindingId,
    LeaseId,
    LeaseState,
    MonotonicMs,
    ProtocolErrorKind,
    TransactionId,
)
from hyperflux_sdk.generated.openrazer_metadata import OPENRAZER_DEVICES_BY_PROFILE
from hyperflux_sdk.generated import protocol_v5_types as v5

from hyperflux_polychromatic.backend import HyperFluxBackend
from hyperflux_polychromatic.runtime import (
    HyperFluxRuntime,
    IntegrationError,
    pseudonymous_serial,
)
from hyperflux_polychromatic.state import StableState, StateError, StateStore


MOUSE_PROFILE = "child.razer.basilisk-v3-pro-35k.00cd"
KEYBOARD_PROFILE = "child.razer.deathstalker-v2-pro-tkl.0296"


def _controller_payload(
    kind: str,
    *,
    generation: int = 7,
    availability: str = "ready",
    battery: int | None = 79,
) -> dict[str, object]:
    values = {
        "mouse": {
            "columns": 13,
            "count": 13,
            "device_id": "mouse-1",
            "endpoint_id": "mouse-wireless",
            "model_name": "untrusted mouse presentation",
            "product_id": 0x00CD,
            "profile_id": MOUSE_PROFILE,
            "rows": 1,
        },
        "keyboard": {
            "columns": 17,
            "count": 102,
            "device_id": "keyboard-1",
            "endpoint_id": "keyboard-wireless",
            "model_name": "untrusted keyboard presentation",
            "product_id": 0x0296,
            "profile_id": KEYBOARD_PROFILE,
            "rows": 6,
        },
    }[kind]
    battery_payload: dict[str, object]
    if battery is None:
        battery_payload = {
            "availability": "unavailable",
            "percentage": None,
            "freshness": "unknown",
            "confidence": "unknown",
            "observed_at_ms": None,
        }
    else:
        battery_payload = {
            "availability": "reported",
            "percentage": battery,
            "freshness": "fresh",
            "confidence": "observed",
            "observed_at_ms": "10",
        }
    return {
        "receiver_id": "receiver-1",
        "generation_id": str(generation),
        "device_id": values["device_id"],
        "endpoint_id": values["endpoint_id"],
        "device_kind": kind,
        "product_id": values["product_id"],
        "receiver_profile": {
            "profile_id": "receiver.razer.hyperflux-v2.00cf",
            "profile_digest": "a" * 64,
        },
        "device_profile": {
            "profile_id": values["profile_id"],
            "profile_digest": "b" * 64,
        },
        "model_name": values["model_name"],
        "presentation": {
            "upstream_id": "openrgb",
            "owner": "OpenRGB",
            "project_version": "1.0rc3",
            "source_revision": "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0",
            "model_key": f"{kind}-model",
            "layout_key": None,
            "transport_variant": "wireless",
        },
        "availability": availability,
        "battery": battery_payload,
        "capabilities": [
            "lighting.direct-frame",
            "lighting.off",
            "lighting.software-effect-frames",
            "lighting.static",
            "telemetry.battery-percent",
        ],
        "lighting": {
            "physical_led_count": values["count"],
            "application_slot_count": values["count"],
            "rows": values["rows"],
            "columns": values["columns"],
        },
        "resource": {
            "receiver_id": "receiver-1",
            "generation_id": str(generation),
            "device_id": values["device_id"],
            "kind": "lighting",
        },
        "ownership": {"state": "unowned", "detail": {}},
        "actions": {
            "can_acquire": availability == "ready",
            "can_release": False,
            "can_submit_now": False,
        },
    }


def _view(
    *kinds: str,
    generation: int = 7,
    availability: dict[str, str] | None = None,
    battery: dict[str, int | None] | None = None,
) -> v5.IntegrationView:
    availability = availability or {}
    battery = battery or {}
    return from_wire(
        v5.IntegrationView,
        {
            "cursor": {
                "stream_id": "stream-1",
                "stream_epoch": "1",
                "projection_revision": 1,
                "sequence": "0",
            },
            "receivers": [
                {
                    "receiver_id": "receiver-1",
                    "generation_id": str(generation),
                    "profile": {
                        "profile_id": "receiver.razer.hyperflux-v2.00cf",
                        "profile_digest": "a" * 64,
                    },
                    "model_name": "Razer HyperFlux V2",
                    "lifecycle": "active",
                    "stable_restore_enabled": False,
                    "restore_state": "idle",
                    "inventory": [],
                    "controllers": [
                        _controller_payload(
                            kind,
                            generation=generation,
                            availability=availability.get(kind, "ready"),
                            battery=battery.get(kind, 79),
                        )
                        for kind in kinds
                    ],
                }
            ],
        },
    )


def _terminal(submission: TransactionSubmission) -> v5.TransactionResult:
    return from_wire(
        v5.TransactionResult,
        {
            "outcome": "terminal",
            "detail": {
                "request_id": f"request-{submission.transaction_id.value}",
                "request_digest": "c" * 64,
                "transaction_id": submission.transaction_id.value,
                "receiver_id": submission.receiver_id.value,
                "generation_id": str(submission.generation_id.value),
                "state": "succeeded",
                "declared_frames": len(submission.frames),
                "delivered_frames": len(submission.frames),
                "side_effect_certainty": "committed",
                "live_write_executed": True,
                "automatic_retry": False,
                "device_application": "confirmed",
                "terminal_sequence": "1",
                "error_kind": None,
                "superseded_by": None,
            },
        },
    )


class FakeClient:
    def __init__(self, view: v5.IntegrationView) -> None:
        self.view = view
        self.connection_epoch = 1
        self.integration_view_calls = 0
        self.acquisitions: list[tuple[v5.ResourceKey, ...]] = []
        self.releases: list[LeaseId] = []
        self.renewals: list[LeaseId] = []
        self.submissions: list[TransactionSubmission] = []
        self._leases: dict[LeaseId, tuple[v5.ResourceKey, ...]] = {}
        self._counter = 0
        self.acquire_conflict: str | None = None
        self.submit_result = "success"
        self.closed = False

    def integration_view(self) -> v5.IntegrationView:
        self.integration_view_calls += 1
        return self.view

    def acquire_lease(self, resources, _duration_ms):
        if self.acquire_conflict is not None:
            return v5.LeaseResultConflict(
                v5.LeaseConflict(ClientId(self.acquire_conflict), resources[0])
            )
        lease_id = LeaseId(f"lease-{len(self.acquisitions) + 1}")
        self.acquisitions.append(resources)
        self._leases[lease_id] = resources
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("polychromatic"),
                resources,
                MonotonicMs(10_000),
                LeaseState.GRANTED,
            )
        )

    def renew_lease(self, lease_id, _duration_ms):
        self.renewals.append(lease_id)
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("polychromatic"),
                self._leases[lease_id],
                MonotonicMs(20_000),
                LeaseState.RENEWED,
            )
        )

    def release_lease(self, lease_id):
        self.releases.append(lease_id)
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("polychromatic"),
                self._leases[lease_id],
                MonotonicMs(20_000),
                LeaseState.RELEASED,
            )
        )

    def next_transaction_id(self) -> TransactionId:
        self._counter += 1
        return TransactionId(f"transaction-{self._counter}")

    def submit_transaction(self, submission: TransactionSubmission) -> v5.TransactionResult:
        self.submissions.append(submission)
        if self.submit_result == "sdk-error":
            raise HyperFluxSdkError("synthetic transport failure")
        if self.submit_result == "unavailable":
            return v5.TransactionResultUnavailable(
                v5.TransactionUnavailable(
                    submission.transaction_id,
                    ProtocolErrorKind.OUTCOME_UNKNOWN,
                    FindingId("HFX-OUTCOME-001"),
                )
            )
        return _terminal(submission)

    def transaction_outcome(self, transaction_id: TransactionId) -> v5.TransactionResult:
        matching = next(
            submission
            for submission in reversed(self.submissions)
            if submission.transaction_id == transaction_id
        )
        return _terminal(matching)

    def close(self) -> None:
        self.closed = True
        self.connection_epoch += 1


class _Debug:
    debug = ""

    def stdout(self, *_args, **_kwargs) -> None:
        return None


class _Paths:
    def __init__(self, config: str) -> None:
        self.config = config


class _Common:
    @staticmethod
    def get_form_factor(_translate, form_factor: str) -> dict[str, str]:
        return {
            "id": form_factor,
            "icon": f"/icons/devices/{form_factor}.svg",
            "label": form_factor.title(),
        }

    @staticmethod
    def get_icon(folder: str, name: str) -> str:
        return f"/icons/{folder}/{name}.svg"

    @staticmethod
    def get_exception_as_string(error: BaseException) -> str:
        return f"{type(error).__name__}: {error}"


class _Base:
    def __init__(self, config: str) -> None:
        self._ = lambda value: value
        self.paths = _Paths(config)
        self.common = _Common()
        self.dbg = _Debug()


class PolychromaticStateContracts(unittest.TestCase):
    def test_private_state_round_trips_atomically_without_hardware_identity(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "stable-lighting.json"
            store = StateStore(path)
            serial = pseudonymous_serial("receiver-private", "mouse-private")
            expected = StableState("static", 75, ((1, 2, 3), (4, 5, 6)))
            store.save(serial, expected)

            self.assertEqual(store.load(serial, 2), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            document = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(set(document), {"devices", "schema"})
            self.assertEqual(set(document["devices"]), {serial})
            payload = path.read_text(encoding="ascii")
            self.assertNotIn("receiver-private", payload)
            self.assertNotIn("mouse-private", payload)
            self.assertFalse(tuple(path.parent.glob("*.tmp")))

    def test_state_rejects_duplicates_symlinks_and_broad_permissions(self) -> None:
        serial = pseudonymous_serial("receiver-1", "mouse-1")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stable-lighting.json"
            path.write_text(
                '{"schema":"hyperflux-polychromatic-state-v1",'
                '"schema":"hyperflux-polychromatic-state-v1","devices":{}}',
                encoding="ascii",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(StateError, "duplicate"):
                StateStore(path).load(serial, 13)

            target = root / "target.json"
            target.write_text('{"schema":"hyperflux-polychromatic-state-v1","devices":{}}')
            target.chmod(0o600)
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(StateError, "cannot open"):
                StateStore(path).load(serial, 13)

            path.unlink()
            path.write_text('{"schema":"hyperflux-polychromatic-state-v1","devices":{}}')
            path.chmod(0o644)
            with self.assertRaisesRegex(StateError, "permissions"):
                StateStore(path).load(serial, 13)

    def test_state_lock_prevents_lost_concurrent_device_updates(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state" / "stable-lighting.json")
            serials = [pseudonymous_serial("receiver-1", f"device-{index}") for index in range(16)]

            def save(item: tuple[int, str]) -> None:
                index, serial = item
                store.save(serial, StableState("static", index, ((index, 0, 0),)))

            with ThreadPoolExecutor(max_workers=8) as executor:
                tuple(executor.map(save, enumerate(serials)))
            for index, serial in enumerate(serials):
                self.assertEqual(
                    store.load(serial, 1),
                    StableState("static", index, ((index, 0, 0),)),
                )

    def test_dimension_change_invalidates_only_the_stale_device_state(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            serial = pseudonymous_serial("receiver-1", "mouse-1")
            store.save(serial, StableState("static", 80, ((1, 2, 3),) * 13))
            self.assertEqual(store.load(serial, 12), StableState.unknown(12))


class PolychromaticRuntimeContracts(unittest.TestCase):
    def _runtime(self, client: FakeClient, directory: str) -> HyperFluxRuntime:
        return HyperFluxRuntime(
            client,
            StateStore(Path(directory) / "state.json"),
            clock=lambda: 1.0,
            sleeper=lambda _seconds: None,
        )

    def test_exact_pinned_presentation_is_independent_and_generation_stable(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse", "keyboard"))
            runtime = self._runtime(client, directory)
            records = {record.device_kind.value: record for record in runtime.refresh()}
            self.assertEqual(records["mouse"].model_name, OPENRAZER_DEVICES_BY_PROFILE[MOUSE_PROFILE]["identity"]["model_name"])
            self.assertEqual(records["mouse"].image_url, OPENRAZER_DEVICES_BY_PROFILE[MOUSE_PROFILE]["presentation"]["image_url"])
            self.assertEqual((records["mouse"].rows, records["mouse"].columns), (1, 13))
            self.assertEqual((records["keyboard"].rows, records["keyboard"].columns), (6, 17))
            mouse_serial = records["mouse"].serial
            keyboard_serial = records["keyboard"].serial
            self.assertNotEqual(mouse_serial, keyboard_serial)

            client.view = _view("mouse", "keyboard", generation=8)
            refreshed = {record.device_kind.value: record for record in runtime.refresh()}
            self.assertEqual(refreshed["mouse"].serial, mouse_serial)
            self.assertEqual(refreshed["keyboard"].serial, keyboard_serial)
            self.assertEqual(refreshed["mouse"].controller.generation_id.value, 8)

    def test_mouse_only_and_keyboard_only_views_do_not_require_a_sibling(self) -> None:
        with TemporaryDirectory() as directory:
            for kind in ("mouse", "keyboard"):
                client = FakeClient(_view(kind))
                runtime = self._runtime(client, directory)
                records = runtime.refresh()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].device_kind.value, kind)

    def test_stable_zone_brightness_and_complete_off_are_full_frames(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse"))
            runtime = self._runtime(client, directory)
            mouse = runtime.refresh()[0]
            all_leds = tuple(range(13))
            with self.assertRaisesRegex(IntegrationError, "complete device"):
                runtime.apply_static(mouse.serial, (0,), (255, 0, 0))

            runtime.apply_static(mouse.serial, all_leds, (10, 20, 30))
            runtime.apply_static(mouse.serial, (0,), (100, 0, 0))
            runtime.apply_brightness(mouse.serial, 50)
            runtime.apply_off(mouse.serial, (0,))
            runtime.apply_off(mouse.serial, all_leds)

            self.assertEqual(len(client.submissions), 5)
            self.assertTrue(all(len(value.frames[0].colors) == 13 for value in client.submissions))
            partial = client.submissions[1].frames[0].colors
            self.assertEqual((partial[0].red.value, partial[0].green.value, partial[0].blue.value), (100, 0, 0))
            self.assertEqual((partial[1].red.value, partial[1].green.value, partial[1].blue.value), (10, 20, 30))
            scaled = client.submissions[2].frames[0].colors
            self.assertEqual((scaled[0].red.value, scaled[0].green.value, scaled[0].blue.value), (50, 0, 0))
            self.assertEqual((scaled[1].red.value, scaled[1].green.value, scaled[1].blue.value), (5, 10, 15))
            self.assertEqual(client.submissions[3].stable_intents[0].mode.value, "static")
            self.assertEqual(client.submissions[4].stable_intents[0].mode.value, "off")
            self.assertTrue(
                all(
                    color.red.value == color.green.value == color.blue.value == 0
                    for color in client.submissions[4].frames[0].colors
                )
            )
            self.assertEqual(len(client.acquisitions), 5)
            self.assertEqual(len(client.releases), 5)

    def test_effect_session_reuses_lease_and_rebinds_new_generation(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse"))
            runtime = self._runtime(client, directory)
            mouse = runtime.refresh()[0]
            colors = ((1, 2, 3),) * 13
            self.assertTrue(runtime.effect_frame(mouse.serial, colors, 100))
            self.assertTrue(runtime.effect_frame(mouse.serial, colors, 50))
            self.assertEqual(len(client.acquisitions), 1)
            self.assertEqual(client.submissions[0].transaction_class.value, "effect-frame")
            self.assertFalse(client.submissions[0].stable_intents)

            client.view = _view("mouse", generation=8)
            runtime.refresh()
            self.assertTrue(runtime.effect_frame(mouse.serial, colors, 100))
            self.assertEqual(len(client.acquisitions), 2)
            self.assertEqual(len(client.releases), 1)
            self.assertEqual(client.submissions[-1].generation_id.value, 8)
            runtime.release_effect(mouse.serial)
            self.assertEqual(len(client.releases), 2)

    def test_effect_failures_escalate_and_ownership_conflict_is_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse"))
            runtime = self._runtime(client, directory)
            mouse = runtime.refresh()[0]
            colors = ((1, 2, 3),) * 13
            client.submit_result = "unavailable"
            self.assertFalse(runtime.effect_frame(mouse.serial, colors, 100))
            self.assertFalse(runtime.effect_frame(mouse.serial, colors, 100))
            with self.assertRaisesRegex(IntegrationError, "three consecutive"):
                runtime.effect_frame(mouse.serial, colors, 100)

            client.submit_result = "success"
            client.acquire_conflict = "openrgb"
            with self.assertRaisesRegex(OwnershipConflict, "openrgb"):
                runtime.effect_frame(mouse.serial, colors, 100)

    def test_metadata_drift_fails_closed_before_presentation_or_writes(self) -> None:
        metadata = {
            profile: {
                "identity": dict(value["identity"]),
                "presentation": dict(value["presentation"]),
            }
            for profile, value in OPENRAZER_DEVICES_BY_PROFILE.items()
        }
        metadata[MOUSE_PROFILE]["identity"]["product_id"] = 0xFFFF
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse"))
            runtime = HyperFluxRuntime(
                client,
                StateStore(Path(directory) / "state.json"),
                metadata=metadata,
            )
            with self.assertRaisesRegex(IntegrationError, "does not match"):
                runtime.refresh()
            self.assertFalse(client.submissions)

    def test_unavailable_battery_is_unknown_without_charging_claims(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = self._runtime(
                FakeClient(_view("keyboard", battery={"keyboard": None})),
                directory,
            )
            keyboard = runtime.refresh()[0]
            self.assertIsNone(runtime.battery_from_record(keyboard))


class PolychromaticBackendContracts(unittest.TestCase):
    def test_backend_projects_exact_native_devices_without_suppression(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(_view("mouse", "keyboard"))
            runtime = HyperFluxRuntime(
                client,
                StateStore(Path(directory) / "state.json"),
                clock=lambda: 1.0,
                sleeper=lambda _seconds: None,
            )
            backend = HyperFluxBackend(_Base(directory), runtime=runtime)
            self.assertTrue(backend.init())
            calls_after_init = client.integration_view_calls
            devices = backend.get_devices()
            self.assertEqual(client.integration_view_calls, calls_after_init + 1)
            self.assertEqual(
                {device.name for device in devices},
                {
                    "Razer Basilisk V3 Pro 35K (Wireless)",
                    "Razer DeathStalker V2 Pro TKL (Wireless)",
                },
            )
            mouse = next(device for device in devices if device.form_factor["id"] == "mouse")
            keyboard = next(device for device in devices if device.form_factor["id"] == "keyboard")
            self.assertEqual((mouse.matrix.rows, mouse.matrix.cols), (1, 13))
            self.assertEqual((keyboard.matrix.rows, keyboard.matrix.cols), (6, 17))
            self.assertEqual([zone.zone_id for zone in mouse.zones], ["main", "scroll", "logo", "led-strip"])
            self.assertEqual([zone.zone_id for zone in keyboard.zones], ["main"])
            self.assertEqual(mouse.battery.percentage, 79)
            self.assertFalse(mouse.battery.is_charging)
            self.assertEqual(backend.get_unsupported_devices(), [])
            self.assertEqual((backend.project_url, backend.bug_url, backend.releases_url), ("", "", ""))

            mouse.matrix.set(0, 0, 9, 8, 7)
            mouse.matrix.draw()
            frame = client.submissions[-1].frames[0]
            self.assertEqual(
                (frame.colors[0].red.value, frame.colors[0].green.value, frame.colors[0].blue.value),
                (9, 8, 7),
            )
            self.assertEqual(len(frame.colors), 13)

    def test_package_declares_one_native_backend_entry_point(self) -> None:
        import tomllib

        document = tomllib.loads(
            (ROOT / "integrations" / "polychromatic" / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(document["project"]["license"], "GPL-3.0-only")
        self.assertEqual(
            document["project"]["entry-points"]["polychromatic.backends"],
            {"hyperflux": "hyperflux_polychromatic:HyperFluxBackend"},
        )

    def test_built_wheel_contains_native_entry_point_and_exact_license(self) -> None:
        wheel_directory = Path(os.environ["HFX_POLYCHROMATIC_WHEEL_DIR"])
        wheels = tuple(wheel_directory.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
            distribution = "hyperflux_next_polychromatic-0.0.0.dev1.dist-info"
            entry_points = archive.read(f"{distribution}/entry_points.txt").decode("utf-8")
            metadata = archive.read(f"{distribution}/METADATA").decode("utf-8")
            packaged_license = archive.read(f"{distribution}/licenses/LICENSE")
        self.assertEqual(
            entry_points,
            "[polychromatic.backends]\nhyperflux = hyperflux_polychromatic:HyperFluxBackend\n",
        )
        self.assertIn("License-Expression: GPL-3.0-only\n", metadata)
        self.assertEqual(
            packaged_license,
            (ROOT / "integrations" / "polychromatic" / "LICENSE").read_bytes(),
        )
        self.assertTrue(
            {
                "hyperflux_polychromatic/__init__.py",
                "hyperflux_polychromatic/backend.py",
                "hyperflux_polychromatic/runtime.py",
                "hyperflux_polychromatic/state.py",
            }
            <= names
        )


class _EntryPoints(list):
    def select(self, *, group: str):
        return self if group == "polychromatic.backends" else _EntryPoints()


class _EntryPoint:
    def __init__(self, name: str, backend: type[Backend]) -> None:
        self.name = name
        self._backend = backend

    def load(self) -> type[Backend]:
        return self._backend


class PolychromaticLoaderContracts(unittest.TestCase):
    def test_external_backend_is_loaded_beside_builtin_openrazer(self) -> None:
        class OpenRazerBackend(Backend):
            def __init__(self, base) -> None:
                super().__init__(base)
                self.backend_id = "openrazer"

            def init(self):
                return True

        class ExternalBackend(Backend):
            def __init__(self, base) -> None:
                super().__init__(base)
                self.backend_id = "hyperflux"

            def init(self):
                return True

        module = types.ModuleType("polychromatic.backends.openrazer")
        module.OpenRazerBackend = OpenRazerBackend
        import polychromatic.middleman as middleman

        with TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {"polychromatic.backends.openrazer": module}):
                with patch.object(
                    middleman.metadata,
                    "entry_points",
                    return_value=_EntryPoints([_EntryPoint("hyperflux", ExternalBackend)]),
                ):
                    instance = Middleman()
                    instance._base = _Base(directory)
                    instance.init()
        self.assertEqual([backend.backend_id for backend in instance.backends], ["openrazer", "hyperflux"])
        self.assertFalse(instance.import_errors)

    def test_external_collision_is_rejected_without_hiding_openrazer(self) -> None:
        class OpenRazerBackend(Backend):
            def __init__(self, base) -> None:
                super().__init__(base)
                self.backend_id = "openrazer"

            def init(self):
                return True

        class CollisionBackend(OpenRazerBackend):
            pass

        module = types.ModuleType("polychromatic.backends.openrazer")
        module.OpenRazerBackend = OpenRazerBackend
        import polychromatic.middleman as middleman

        with TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {"polychromatic.backends.openrazer": module}):
                with patch.object(
                    middleman.metadata,
                    "entry_points",
                    return_value=_EntryPoints([_EntryPoint("openrazer", CollisionBackend)]),
                ):
                    instance = Middleman()
                    instance._base = _Base(directory)
                    instance.init()
        self.assertEqual([backend.backend_id for backend in instance.backends], ["openrazer"])
        self.assertIn("openrazer", instance.import_errors)


if __name__ == "__main__":
    unittest.main()
