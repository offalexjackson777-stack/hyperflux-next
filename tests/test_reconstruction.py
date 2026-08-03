# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.reconstruction import (
    _is_generated_source,
    load_interface_baseline,
    load_reconstruction_ledger,
    render_public_api_consumer,
    verify_historical_generated_output_projection,
)


class ReconstructionLedgerTests(unittest.TestCase):
    def test_ledger_covers_every_phase_one_inventory_class(self) -> None:
        ledger = load_reconstruction_ledger(ROOT)

        self.assertEqual(ledger["baseline_commit"], "c348305bb9c6790141932ddf79f707e5223bb51c")
        self.assertEqual(ledger["target"], {"version": "2.0.0", "package_release": 1})
        self.assertEqual(len(ledger["binaries"]), 7)
        self.assertEqual(len(ledger["package_artifacts"]), 25)
        self.assertGreaterEqual(len(ledger["dependencies"]), 40)
        self.assertEqual(len(ledger["handwritten_modules"]), 34)

    def test_component_moves_are_explicit_and_stock_applications_stay_optional(self) -> None:
        ledger = load_reconstruction_ledger(ROOT)
        decisions = {
            entry["authority_key"]: (entry["action"], entry["target_owner"])
            for section in ("package_artifacts", "dependencies", "compatibility_subsystems")
            for entry in ledger[section]
        }

        self.assertEqual(
            decisions["install-build:openrazer-native-relay"], ("MOVE", "full")
        )
        self.assertEqual(
            decisions["install-build:openrgb-native"], ("KEEP", "full")
        )
        for package in ("openrgb", "openrazer-daemon", "python-openrazer", "polychromatic"):
            self.assertEqual(
                decisions[f"arch:full:optional:{package}"], ("KEEP", "full")
            )

    def test_unreviewed_binary_or_large_module_fails_closed(self) -> None:
        with mock.patch(
            "hfxdev.reconstruction._cargo_binary_paths",
            return_value={"crates/unreviewed/src/main.rs"},
        ):
            with self.assertRaisesRegex(ModelError, "binary coverage"):
                load_reconstruction_ledger(ROOT)

        with mock.patch(
            "hfxdev.reconstruction._large_handwritten_modules",
            return_value={"crates/unreviewed/src/lib.rs": 801},
        ):
            with self.assertRaisesRegex(ModelError, "lack reconstruction decisions"):
                load_reconstruction_ledger(ROOT)

    def test_phase_four_moves_must_finish_below_the_review_signal(self) -> None:
        with mock.patch(
            "hfxdev.reconstruction._large_handwritten_modules",
            return_value={"tools/hfxdev/openrazer.py": 800},
        ):
            with self.assertRaisesRegex(ModelError, "Phase 4 extraction remains"):
                load_reconstruction_ledger(ROOT)

    def test_sibling_test_modules_are_not_production_behemoths(self) -> None:
        self.assertTrue(
            _is_generated_source(Path("crates/hfx-daemon/src/restoration/tests.rs"))
        )
        self.assertFalse(
            _is_generated_source(Path("crates/hfx-daemon/src/restoration.rs"))
        )


class ReconstructionInterfaceBaselineTests(unittest.TestCase):
    def test_release_8_package_and_generated_interfaces_are_exact(self) -> None:
        baseline = load_interface_baseline(ROOT)

        self.assertEqual(
            [package["component"] for package in baseline["packages"]],
            ["linux", "full"],
        )
        self.assertEqual(
            [len(package["payload"]) for package in baseline["packages"]],
            [76, 9],
        )
        self.assertEqual(len(baseline["generated_outputs"]), 169)
        self.assertEqual(len(baseline["rust_root_exports"]), 14)
        self.assertEqual(
            sum(len(crate["symbols"]) for crate in baseline["rust_root_exports"]),
            954,
        )
        verify_historical_generated_output_projection(baseline)

    def test_generated_output_projection_is_self_contained_without_history(self) -> None:
        baseline = load_interface_baseline(ROOT)
        verify_historical_generated_output_projection(baseline)

        altered = copy.deepcopy(baseline)
        altered["generated_outputs"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ModelError, "projection differs"):
            verify_historical_generated_output_projection(altered)

    def test_compile_consumer_is_reproducible_and_current_exports_compile(self) -> None:
        baseline = load_interface_baseline(ROOT)
        consumer = ROOT / "tests/fixtures/rust-public-api-consumer/src/lib.rs"
        self.assertEqual(
            consumer.read_text(encoding="utf-8"),
            render_public_api_consumer(baseline),
        )

        def registry_pins(path: Path) -> set[tuple[str, str, str, str]]:
            lock = tomllib.loads(path.read_text(encoding="utf-8"))
            return {
                (
                    package["name"],
                    package["version"],
                    package["source"],
                    package["checksum"],
                )
                for package in lock["package"]
                if "source" in package
            }

        self.assertEqual(
            registry_pins(ROOT / "tests/fixtures/rust-public-api-consumer/Cargo.lock"),
            registry_pins(ROOT / "Cargo.lock"),
        )

        result = subprocess.run(
            [
                "cargo",
                "check",
                "--offline",
                "--locked",
                "--manifest-path",
                "tests/fixtures/rust-public-api-consumer/Cargo.toml",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stock_application_payload_or_config_drift_fails_closed(self) -> None:
        baseline = load_interface_baseline(ROOT)
        stock_collision = copy.deepcopy(baseline)
        stock_collision["packages"][0]["payload"].append(
            {
                "path": "usr/share/polychromatic/private.py",
                "type": "file",
                "mode": "644",
                "uid": 0,
                "gid": 0,
                "size": 1,
                "sha256": "0" * 64,
            }
        )
        stock_collision["packages"][0]["payload"].sort(key=lambda entry: entry["path"])
        with mock.patch("hfxdev.reconstruction.load_json", return_value=stock_collision):
            with self.assertRaisesRegex(ModelError, "stock application payloads"):
                load_interface_baseline(ROOT)

        config_drift = copy.deepcopy(baseline)
        config_drift["packages"][0]["pkginfo"].pop("backup")
        with mock.patch("hfxdev.reconstruction.load_json", return_value=config_drift):
            with self.assertRaisesRegex(ModelError, "configuration preservation"):
                load_interface_baseline(ROOT)

if __name__ == "__main__":
    unittest.main()
