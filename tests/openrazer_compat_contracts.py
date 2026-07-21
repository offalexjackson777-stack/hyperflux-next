# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]

from hyperflux_sdk.client import TransactionSubmission
from hyperflux_sdk.codec import from_wire
from hyperflux_sdk.errors import HyperFluxSdkError
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    LeaseId,
    LeaseState,
    MonotonicMs,
    TransactionId,
)
from hyperflux_sdk.generated import protocol_v5_types as v5

from hyperflux_openrazer_compat.contract import (
    CONTRACT,
    ISOLATED_SESSION_MARKER,
    IdentityMode,
    identity_for_mode,
)
from hyperflux_openrazer_compat.generated_contract import OPENRAZER_COMPATIBILITY_CONTRACT
from hyperflux_openrazer_compat.matrix import MatrixBuffer, MatrixError
from hyperflux_openrazer_compat.model import (
    CompatibilityModelError,
    ControllerRecord,
    records_from_view,
)
from hyperflux_openrazer_compat.runtime import (
    CompatibilityRuntimeError,
    OpenRazerRuntime,
)
from hyperflux_openrazer_compat.service import (
    ServiceController,
    initialize_dbus_mainloop,
    run_loop,
)


MOUSE_PROFILE = "child.razer.basilisk-v3-pro-35k.00cd"
KEYBOARD_PROFILE = "child.razer.deathstalker-v2-pro-tkl.0296"


def _controller_payload(kind: str, *, generation: int = 7, availability: str = "ready") -> dict[str, object]:
    values = {
        "mouse": {
            "columns": 13,
            "count": 13,
            "device_id": "mouse-1",
            "endpoint_id": "mouse-wireless",
            "product_id": 0x00CD,
            "profile_id": MOUSE_PROFILE,
            "rows": 1,
        },
        "keyboard": {
            "columns": 17,
            "count": 102,
            "device_id": "keyboard-1",
            "endpoint_id": "keyboard-wireless",
            "product_id": 0x0296,
            "profile_id": KEYBOARD_PROFILE,
            "rows": 6,
        },
    }[kind]
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
        "model_name": f"untrusted {kind}",
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
        "battery": {
            "availability": "reported",
            "percentage": 79,
            "freshness": "fresh",
            "confidence": "observed",
            "observed_at_ms": "10",
        },
        "capabilities": [
            "lighting.brightness",
            "lighting.complete-black",
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


def _view(*kinds: str, generation: int = 7, availability: str = "ready") -> v5.IntegrationView:
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
                            availability=availability,
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
        self.acquisitions: list[tuple[v5.ResourceKey, ...]] = []
        self.releases: list[LeaseId] = []
        self.renewals: list[LeaseId] = []
        self.submissions: list[TransactionSubmission] = []
        self._leases: dict[LeaseId, tuple[v5.ResourceKey, ...]] = {}
        self._counter = 0
        self.submit_error = False
        self.closed = False

    def integration_view(self) -> v5.IntegrationView:
        return self.view

    def acquire_lease(self, resources, _duration_ms):
        lease_id = LeaseId(f"lease-{len(self.acquisitions) + 1}")
        self.acquisitions.append(resources)
        self._leases[lease_id] = resources
        return v5.LeaseResultGranted(
            v5.LeaseGrant(
                lease_id,
                ClientId("openrazer-compat"),
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
                ClientId("openrazer-compat"),
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
                ClientId("openrazer-compat"),
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
        if self.submit_error:
            raise HyperFluxSdkError("synthetic uncertain transport failure")
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


def _frame(record: ControllerRecord) -> bytes:
    payload = bytearray()
    for row in range(record.rows):
        payload.extend((row, 0, record.columns - 1))
        for column in range(record.columns):
            payload.extend((row + 1, column + 2, row + column + 3))
    return bytes(payload)


class OpenRazerContractTests(unittest.TestCase):
    def test_built_wheel_contains_private_entry_points_and_exact_license(self) -> None:
        wheel_directory = Path(os.environ["HFX_OPENRAZER_WHEEL_DIR"])
        wheels = tuple(wheel_directory.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
            distribution = "hyperflux_next_openrazer_compat-0.0.0.dev1.dist-info"
            entry_points = archive.read(f"{distribution}/entry_points.txt").decode("utf-8")
            metadata = archive.read(f"{distribution}/METADATA").decode("utf-8")
            packaged_license = archive.read(f"{distribution}/licenses/LICENSE")
        self.assertEqual(
            entry_points,
            "[console_scripts]\n"
            "hyperflux-openrazer-compat = hyperflux_openrazer_compat.cli:main\n"
            "hyperflux-openrazer-session = hyperflux_openrazer_compat.session:main\n",
        )
        self.assertIn("License-Expression: GPL-2.0-or-later\n", metadata)
        self.assertEqual(
            packaged_license,
            (ROOT / "integrations" / "openrazer" / "compatibility" / "LICENSE").read_bytes(),
        )
        self.assertTrue(
            {
                "hyperflux_openrazer_compat/__init__.py",
                "hyperflux_openrazer_compat/cli.py",
                "hyperflux_openrazer_compat/contract.py",
                "hyperflux_openrazer_compat/generated_contract.py",
                "hyperflux_openrazer_compat/matrix.py",
                "hyperflux_openrazer_compat/model.py",
                "hyperflux_openrazer_compat/runtime.py",
                "hyperflux_openrazer_compat/service.py",
                "hyperflux_openrazer_compat/session.py",
            }
            <= names
        )

    def test_generated_contract_matches_canonical_private_policy(self) -> None:
        canonical = json.loads(
            (ROOT / "integrations" / "openrazer" / "compatibility.json").read_text()
        )
        self.assertEqual(OPENRAZER_COMPATIBILITY_CONTRACT, canonical)
        self.assertEqual(CONTRACT["schema"], "hyperflux-openrazer-compatibility-v1")
        identity = identity_for_mode(IdentityMode.PRIVATE, environment={})
        self.assertEqual(identity.bus_name, "dev.hyperflux.OpenRazer1")
        self.assertFalse(identity.claims_official_name)
        with self.assertRaisesRegex(ValueError, "isolated"):
            identity_for_mode(IdentityMode.ORG_RAZER_PRIVATE_SESSION, environment={})
        legacy = identity_for_mode(
            IdentityMode.ORG_RAZER_PRIVATE_SESSION,
            environment={
                ISOLATED_SESSION_MARKER: "1",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private",
            },
        )
        self.assertEqual(legacy.bus_name, "org.razer")
        self.assertTrue(legacy.claims_official_name)

    def test_exact_metadata_and_independent_controller_projection(self) -> None:
        records = records_from_view(_view("mouse", "keyboard"))
        self.assertEqual(len(records), 2)
        by_kind = {record.device_kind.value: record for record in records}
        self.assertEqual(
            by_kind["mouse"].model_name,
            "Razer Basilisk V3 Pro 35K (Wireless)",
        )
        self.assertEqual(by_kind["mouse"].led_count, 13)
        self.assertEqual(
            by_kind["keyboard"].model_name,
            "Razer DeathStalker V2 Pro TKL (Wireless)",
        )
        self.assertEqual(by_kind["keyboard"].led_count, 102)
        self.assertNotEqual(by_kind["mouse"].serial, by_kind["keyboard"].serial)
        self.assertNotIn("receiver-1", by_kind["mouse"].serial)
        self.assertEqual(len(records_from_view(_view("mouse"))), 1)

    def test_projection_rejects_metadata_or_capability_drift(self) -> None:
        controller = _controller_payload("mouse")
        controller["capabilities"] = ["lighting.direct-frame"]
        view = from_wire(
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
                        "generation_id": "7",
                        "profile": None,
                        "model_name": None,
                        "lifecycle": "active",
                        "stable_restore_enabled": False,
                        "restore_state": "idle",
                        "inventory": [],
                        "controllers": [controller],
                    }
                ],
            },
        )
        with self.assertRaisesRegex(CompatibilityModelError, "capabilities"):
            records_from_view(view)

    def test_matrix_requires_one_complete_exact_generation_frame(self) -> None:
        record = records_from_view(_view("mouse"))[0]
        matrix = MatrixBuffer()
        with self.assertRaisesRegex(MatrixError, "empty"):
            matrix.stage(record, b"")
        matrix.stage(record, bytes((0, 0, 0, 1, 2, 3)))
        with self.assertRaisesRegex(MatrixError, "incomplete"):
            matrix.complete(record)
        matrix.clear()
        matrix.stage(record, _frame(record))
        colors = matrix.complete(record)
        self.assertEqual(len(colors), 13)
        changed = records_from_view(_view("mouse", generation=8))[0]
        with self.assertRaisesRegex(MatrixError, "stale"):
            matrix.complete(changed)

    def test_static_brightness_off_and_matrix_use_typed_sdk_authority(self) -> None:
        client = FakeClient(_view("mouse"))
        runtime = OpenRazerRuntime(client)
        record = runtime.refresh()[0]
        runtime.apply_static(record.serial, (200, 100, 50))
        first = client.submissions[-1]
        self.assertEqual(first.stable_intents[0].mode.value, "static")
        self.assertEqual(first.frames[0].colors[0].red.value, 200)
        runtime.apply_brightness(record.serial, 50.0)
        second = client.submissions[-1]
        self.assertEqual(second.frames[0].colors[0].red.value, 100)
        runtime.apply_off(record.serial)
        third = client.submissions[-1]
        self.assertEqual(third.stable_intents[0].mode.value, "off")
        self.assertTrue(all(color.red.value == 0 for color in third.frames[0].colors))
        runtime.stage_matrix(record.serial, _frame(record))
        runtime.commit_matrix(record.serial)
        fourth = client.submissions[-1]
        self.assertEqual(fourth.transaction_class.value, "effect-frame")
        self.assertEqual(len(fourth.frames[0].colors), 13)
        self.assertEqual(len(client.acquisitions), 4)
        self.assertEqual(len(client.releases), 3)

    def test_sleep_and_uncertain_transport_fail_without_replay(self) -> None:
        sleeping = FakeClient(_view("mouse", availability="sleeping"))
        runtime = OpenRazerRuntime(sleeping)
        record = runtime.refresh()[0]
        with self.assertRaisesRegex(CompatibilityRuntimeError, "sleeping"):
            runtime.apply_static(record.serial, (1, 2, 3))
        self.assertFalse(sleeping.submissions)

        client = FakeClient(_view("mouse"))
        runtime = OpenRazerRuntime(client)
        record = runtime.refresh()[0]
        client.submit_error = True
        runtime.stage_matrix(record.serial, _frame(record))
        with self.assertRaisesRegex(CompatibilityRuntimeError, "not replayed"):
            runtime.commit_matrix(record.serial)
        self.assertEqual(len(client.submissions), 1)

    def test_generation_change_retires_effect_lease_and_staged_frame(self) -> None:
        client = FakeClient(_view("mouse"))
        runtime = OpenRazerRuntime(client)
        record = runtime.refresh()[0]
        runtime.stage_matrix(record.serial, _frame(record))
        runtime.commit_matrix(record.serial)
        runtime.stage_matrix(record.serial, _frame(record))
        client.view = _view("mouse", generation=8)
        changed = runtime.refresh()[0]
        self.assertEqual(changed.serial, record.serial)
        self.assertEqual(changed.controller.generation_id.value, 8)
        self.assertEqual(len(client.releases), 1)
        with self.assertRaisesRegex(MatrixError, "no OpenRazer matrix"):
            runtime.commit_matrix(changed.serial)


class _FixtureRuntime:
    def __init__(self, log_path: Path) -> None:
        self._records = {record.serial: record for record in records_from_view(_view("mouse", "keyboard"))}
        self._brightness = {serial: 100.0 for serial in self._records}
        self._matrices = {serial: MatrixBuffer() for serial in self._records}
        self._log_path = log_path

    def refresh(self) -> tuple[ControllerRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def record(self, serial: str, *, refresh: bool = True) -> ControllerRecord:
        del refresh
        return self._records[serial]

    def brightness(self, serial: str) -> float:
        return self._brightness[serial]

    def apply_static(self, serial: str, color: tuple[int, int, int]) -> None:
        self._write("static", serial, color=list(color))

    def apply_off(self, serial: str) -> None:
        self._write("off", serial)

    def apply_brightness(self, serial: str, brightness: float) -> None:
        self._brightness[serial] = brightness
        self._write("brightness", serial, brightness=brightness)

    def stage_matrix(self, serial: str, payload: bytes) -> None:
        self._matrices[serial].stage(self._records[serial], payload)
        self._write("stage", serial, bytes=len(payload))

    def commit_matrix(self, serial: str) -> None:
        colors = self._matrices[serial].complete(self._records[serial])
        self._matrices[serial].clear()
        self._write("custom", serial, colors=len(colors))

    def close(self) -> None:
        self._write("closed", "service")

    def _write(self, operation: str, serial: str, **values: object) -> None:
        record = {"operation": operation, "serial": serial, **values}
        with self._log_path.open("a", encoding="ascii") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())


def _fixture_service(mode: str, log_path: Path) -> int:
    initialize_dbus_mainloop()
    identity = identity_for_mode(IdentityMode(mode))
    runtime = _FixtureRuntime(log_path)
    controller = ServiceController(runtime, identity, 300_000, runtime.refresh())
    return run_loop(controller)


class OpenRazerDbusContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise unittest.SkipTest("D-Bus contracts require dbus-run-session")
        source = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source is None:
            raise unittest.SkipTest("pinned OpenRazer source is unavailable")
        sys.path.insert(0, str(Path(source) / "pylib"))

    def test_private_identity_does_not_claim_org_razer(self) -> None:
        with TemporaryDirectory() as directory:
            process, ready = self._start("private", Path(directory) / "events.jsonl")
            try:
                self.assertEqual(ready["bus_name"], "dev.hyperflux.OpenRazer1")
                self.assertFalse(ready["official_name_claimed"])
                import dbus

                bus = dbus.SessionBus()
                daemon = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
                names = dbus.Interface(daemon, "org.freedesktop.DBus").ListNames()
                self.assertIn("dev.hyperflux.OpenRazer1", names)
                self.assertNotIn("org.razer", names)
                root = bus.get_object(
                    "dev.hyperflux.OpenRazer1",
                    "/dev/hyperflux/OpenRazer1",
                )
                devices = dbus.Interface(root, "razer.devices").getDevices()
                self.assertEqual(len(devices), 2)
            finally:
                self._stop(process)

    def test_pinned_openrazer_client_sees_only_qualified_methods(self) -> None:
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "events.jsonl"
            process, ready = self._start("org-razer-private-session", log_path)
            try:
                self.assertTrue(ready["official_name_claimed"])
                from openrazer.client import DeviceManager

                manager = DeviceManager()
                self.assertEqual(len(manager.devices), 2)
                by_type = {device.type: device for device in manager.devices}
                mouse = by_type["mouse"]
                keyboard = by_type["keyboard"]
                self.assertEqual(mouse.name, "Razer Basilisk V3 Pro 35K (Wireless)")
                self.assertEqual(
                    keyboard.name,
                    "Razer DeathStalker V2 Pro TKL (Wireless)",
                )
                for device, dimensions in ((mouse, (1, 13)), (keyboard, (6, 17))):
                    self.assertTrue(device.has("lighting_static"))
                    self.assertTrue(device.has("lighting_none"))
                    self.assertTrue(device.has("lighting_led_matrix"))
                    self.assertTrue(device.has("brightness"))
                    self.assertFalse(device.has("lighting_spectrum"))
                    self.assertFalse(device.has("lighting_wave"))
                    self.assertFalse(device.has("battery"))
                    self.assertFalse(device.has("dpi"))
                    self.assertEqual((device.fx.advanced.rows, device.fx.advanced.cols), dimensions)
                    self.assertTrue(device.device_image.startswith("https://"))

                mouse.fx.static(12, 34, 56)
                mouse.brightness = 75.0
                mouse.fx.none()
                for device in (mouse, keyboard):
                    advanced = device.fx.advanced
                    assert advanced is not None
                    for row in range(advanced.rows):
                        for column in range(advanced.cols):
                            advanced.matrix[row, column] = (row + 1, column + 2, 3)
                    advanced.draw()

                import dbus

                bus = dbus.SessionBus()
                root = bus.get_object("org.razer", "/org/razer")
                serial = str(dbus.Interface(root, "razer.devices").getDevices()[0])
                path = f"/org/razer/device/{serial}"
                xml = str(
                    dbus.Interface(
                        bus.get_object("org.razer", path),
                        "org.freedesktop.DBus.Introspectable",
                    ).Introspect()
                )
                actual = {
                    interface.attrib["name"]: sorted(
                        method.attrib["name"]
                        for method in interface.findall("method")
                    )
                    for interface in ET.fromstring(xml).findall("interface")
                    if interface.attrib["name"].startswith("razer.")
                }
                expected = {
                    interface["name"]: sorted(method["name"] for method in interface["methods"])
                    for interface in CONTRACT["interfaces"]
                    if interface["name"].startswith("razer.device.")
                }
                self.assertEqual(actual, expected)
            finally:
                self._stop(process)
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            operations = [event["operation"] for event in events]
            self.assertEqual(operations.count("static"), 1)
            self.assertEqual(operations.count("brightness"), 1)
            self.assertEqual(operations.count("off"), 1)
            self.assertEqual(operations.count("custom"), 2)
            self.assertEqual(operations.count("stage"), 2)
            self.assertEqual(operations[-1], "closed")

    @staticmethod
    def _start(mode: str, log_path: Path) -> tuple[subprocess.Popen[str], dict[str, object]]:
        environment = os.environ.copy()
        if mode == "org-razer-private-session":
            environment[ISOLATED_SESSION_MARKER] = "1"
        process = subprocess.Popen(
            [sys.executable, __file__, "--service-fixture", mode, str(log_path)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            raise AssertionError("fixture readiness pipe is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"fixture exited before readiness: {process.returncode}")
                if selector.select(max(0.0, deadline - time.monotonic())):
                    line = process.stdout.readline()
                    if line:
                        return process, json.loads(line)
        finally:
            selector.close()
        process.kill()
        raise AssertionError("fixture did not become ready")

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--service-fixture":
    raise SystemExit(_fixture_service(sys.argv[2], Path(sys.argv[3])))

if __name__ == "__main__":
    unittest.main()
