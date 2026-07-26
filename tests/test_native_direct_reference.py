# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
import hfxdev.native_direct_reference as direct
from hfxdev.native_parity import DirectSemanticReference


ROUTE = {
    "driver_family": "razermouse",
    "vendor_id": 0x1532,
    "direct_data_product_ids": [0x00CC],
}
PROFILE = {
    "probes": [
        {"method": "get_serial", "driver": {"attribute": "device_serial"}},
        {"method": "get_dpi_stages", "driver": {"attribute": "dpi_stages"}},
    ]
}


class DirectTree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.devices = root / "devices"
        self.drivers = root / "drivers"
        self.devices.mkdir()
        self.drivers.mkdir()

    def add(
        self,
        *,
        identity: str,
        bus: int,
        device: int,
        port: int = 2,
        attributes: tuple[str, ...] = ("device_serial", "dpi_stages"),
    ) -> None:
        usb = self.devices / f"usb{bus}" / f"{bus}-{port}"
        hid = usb / f"{bus}-{port}:1.0" / identity
        hid.mkdir(parents=True)
        (usb / "busnum").write_text(f"{bus}\n", encoding="ascii")
        (usb / "devnum").write_text(f"{device}\n", encoding="ascii")
        for attribute in attributes:
            (hid / attribute).write_bytes(b"fixture")
        family = self.drivers / "razermouse"
        family.mkdir(exist_ok=True)
        (family / identity).symlink_to(hid)


class NativeDirectReferenceTests(unittest.TestCase):
    def test_exact_direct_identity_and_complete_attribute_carrier_are_required(self) -> None:
        with TemporaryDirectory() as directory:
            tree = DirectTree(Path(directory))
            tree.add(identity="0003:1532:00CD.0001", bus=3, device=4)
            tree.add(
                identity="0003:1532:00CC.0002",
                bus=3,
                device=5,
                attributes=("device_serial",),
            )
            tree.add(identity="0003:1532:00CC.0003", bus=3, device=6)
            endpoint = direct.find_exact_direct_endpoint(
                ROUTE,
                PROFILE,
                driver_root=tree.drivers,
                devices_root=tree.devices,
            )
        self.assertEqual((endpoint.bus, endpoint.device), (3, 6))
        self.assertEqual(endpoint.driver_entry.name, "0003:1532:00CC.0003")

    def test_absent_or_duplicate_direct_data_routes_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            tree = DirectTree(Path(directory))
            with self.assertRaisesRegex(ModelError, "data route.*absent"):
                direct.find_exact_direct_endpoint(
                    ROUTE,
                    PROFILE,
                    driver_root=tree.drivers,
                    devices_root=tree.devices,
                )
            tree.add(identity="0003:1532:00CC.0001", bus=3, device=5, port=2)
            tree.add(identity="0003:1532:00CC.0002", bus=3, device=6, port=3)
            with self.assertRaisesRegex(ModelError, "more than one exact"):
                direct.find_exact_direct_endpoint(
                    ROUTE,
                    PROFILE,
                    driver_root=tree.drivers,
                    devices_root=tree.devices,
                )

    def test_only_source_bound_attributes_are_read(self) -> None:
        seen: list[str] = []
        with patch.object(
            direct,
            "_read_attribute",
            side_effect=lambda path: seen.append(path.name),
        ):
            result = direct.trigger_direct_attributes(Path("/sys/fake"), PROFILE)
        self.assertEqual(result, ("device_serial", "dpi_stages"))
        self.assertEqual(seen, ["device_serial", "dpi_stages"])

    def test_private_capture_directory_is_removed_on_failure(self) -> None:
        path: Path | None = None
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with direct._private_capture_directory(os.getuid(), os.getgid()) as directory:
                path = directory
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                raise RuntimeError("stop")
        self.assertIsNotNone(path)
        self.assertFalse(path.exists())

    def test_dumpcap_uses_private_files_and_hard_capture_bounds(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        with TemporaryDirectory() as directory:
            capture = Path(directory) / "direct.pcapng"
            with (
                patch.object(direct.shutil, "which", return_value="/usr/bin/dumpcap"),
                patch.object(direct.subprocess, "Popen", return_value=process) as popen,
                patch.object(direct.time, "sleep"),
            ):
                started = direct._start_dumpcap("usbmon3", capture)
            command = popen.call_args.args[0]
            diagnostic = popen.call_args.kwargs["stderr"]
            self.assertIs(started.process, process)
            self.assertNotEqual(diagnostic, subprocess.PIPE)
            self.assertTrue(diagnostic.closed)
            self.assertIn("duration:5", command)
            self.assertIn(
                f"filesize:{direct.MAX_CAPTURE_BYTES // 1024}",
                command,
            )
            self.assertEqual(started.diagnostic_path.stat().st_mode & 0o777, 0o600)

    def test_capture_export_filters_to_the_exact_physical_device(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with (
            patch.object(direct.shutil, "which", return_value="/usr/bin/tshark"),
            patch.object(direct.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(
                direct._export_capture(Path("direct.pcapng"), bus=3, device=12),
                "",
            )
        command = run.call_args.args[0]
        self.assertIn("-n", command)
        filter_index = command.index("-Y")
        self.assertEqual(
            command[filter_index + 1],
            "usb.bus_id == 3 && usb.device_address == 12",
        )

    def test_staged_session_keeps_serial_material_in_process_only(self) -> None:
        preflight = SimpleNamespace(
            route=ROUTE,
            profile=PROFILE,
            revision="a" * 40,
        )
        reference = DirectSemanticReference(
            evidence={
                "observations": [
                    {"method": "get_serial"},
                    {"method": "get_dpi_stages"},
                ]
            },
            serial_values={"get_serial": b"PRIVATE-SERIAL"},
        )
        captured = direct.CapturedDirectReference(
            reference=reference,
            attributes_read=("device_serial", "dpi_stages"),
            bus=3,
            device=12,
        )
        expected = SimpleNamespace(
            conclusion="passed",
            passed=("get_serial", "get_dpi_stages"),
            missing=(),
            failed=(),
        )
        with (
            patch.object(direct, "prepare_live_parity", return_value=preflight),
            patch.object(direct, "_operator_identity", return_value=(os.getuid(), os.getgid())),
            patch.object(direct, "capture_direct_reference", return_value=captured),
            patch.object(direct, "_wait_for_operator"),
            patch.object(direct, "_require_direct_route_absent"),
            patch.object(direct, "run_live_parity", return_value=expected) as run,
            patch("builtins.print"),
        ):
            result = direct.run_staged_live_parity(
                ROOT,
                profile_id="child.razer.example.0001",
                package=Path("candidate.pkg.tar.zst"),
                package_sha256="b" * 64,
                expected_generation=7,
                approval_text=direct.STAGED_PARITY_APPROVAL,
                user="operator",
            )
        self.assertIs(result, expected)
        keywords = run.call_args.kwargs
        self.assertEqual(
            keywords["direct_serial_values"], {"get_serial": b"PRIVATE-SERIAL"}
        )
        self.assertFalse(keywords["direct_evidence_path"].exists())

    def test_staged_session_rejects_incomplete_direct_capture_before_transition(self) -> None:
        preflight = SimpleNamespace(
            route=ROUTE,
            profile=PROFILE,
            revision="a" * 40,
        )
        captured = direct.CapturedDirectReference(
            reference=DirectSemanticReference(
                evidence={"observations": [{"method": "get_serial"}]},
                serial_values={"get_serial": b"PRIVATE-SERIAL"},
            ),
            attributes_read=("device_serial",),
            bus=3,
            device=12,
        )
        with (
            patch.object(direct, "prepare_live_parity", return_value=preflight),
            patch.object(direct, "_operator_identity", return_value=(os.getuid(), os.getgid())),
            patch.object(direct, "capture_direct_reference", return_value=captured),
            patch.object(direct, "_wait_for_operator") as wait,
        ):
            with self.assertRaisesRegex(ModelError, "missed source-bound methods"):
                direct.run_staged_live_parity(
                    ROOT,
                    profile_id="child.razer.example.0001",
                    package=Path("candidate.pkg.tar.zst"),
                    package_sha256="b" * 64,
                    expected_generation=7,
                    approval_text=direct.STAGED_PARITY_APPROVAL,
                    user="operator",
                )
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
