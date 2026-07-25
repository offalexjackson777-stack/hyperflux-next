# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.install import load_install_manifest
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, load_json


class InstallManifestTests(unittest.TestCase):
    def test_projected_generated_outputs_are_planned_before_they_exist(self) -> None:
        projected = ROOT / "docs" / "generated" / "future-contract.md"
        self.assertFalse(projected.exists())
        manifest = load_install_manifest(ROOT, projected_files=(projected,))
        matches = [
            file
            for file in manifest.files
            if file.source == projected
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            str(matches[0].destination),
            "/usr/share/doc/hyperflux-next/generated/future-contract.md",
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.value = load_json(ROOT / "packaging" / "install.json")
        cls.manifest = load_install_manifest(ROOT)

    def test_policy_is_non_activating_and_configuration_preserving(self) -> None:
        policy = self.manifest.policy
        self.assertFalse(policy.hardware_writes_on_install)
        self.assertFalse(policy.start_service_on_install)
        self.assertFalse(policy.enable_service_on_install)
        self.assertTrue(policy.preserve_configuration)
        preserved = [file for file in self.manifest.files if file.preserve]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(
            str(preserved[0].destination), "/etc/hyperflux-next/bridge.json"
        )

    def test_runtime_destinations_and_dkms_source_are_derived(self) -> None:
        self.assertEqual(
            self.manifest.build("bridge-daemon").destination,
            "/usr/lib/hyperflux-next/hyperflux-next-bridge",
        )
        self.assertEqual(
            self.manifest.build("operations-cli").destination,
            "/usr/bin/hyperfluxctl",
        )
        destinations = {str(file.destination) for file in self.manifest.files}
        source_directory = load_linux_runtime(ROOT).kernel.source_directory
        self.assertIn(f"{source_directory}/dkms.conf", destinations)
        self.assertIn(f"{source_directory}/hyperflux-next-core.c", destinations)

    def test_rust_installables_use_role_specific_release_profiles(self) -> None:
        cargo_profiles = {
            build.id: build.cargo_profile
            for build in self.manifest.builds
            if build.kind == "cargo-binary"
        }
        self.assertEqual(
            cargo_profiles,
            {
                "bridge-daemon": "release",
                "operations-cli": "operations",
                "activation-utility": "operations",
                "openrazer-native-relay": "relay",
            },
        )
        cargo = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
        optimized = {
            "inherits": "release",
            "codegen-units": 1,
            "lto": "thin",
            "panic": "abort",
            "strip": "symbols",
        }
        self.assertEqual(cargo["profile"]["operations"], optimized)
        self.assertEqual(cargo["profile"]["relay"], optimized)

    def test_python_sdk_license_is_generated_from_repository_authority(self) -> None:
        self.assertEqual(
            (ROOT / "sdk/python/LICENSE").read_bytes(),
            (ROOT / "LICENSE").read_bytes(),
        )
        self.assertIn(
            'license-files = ["LICENSE"]',
            (ROOT / "sdk/python/pyproject.toml").read_text(encoding="utf-8"),
        )

    def test_destinations_are_unique_and_machine_independent(self) -> None:
        destinations = [str(file.destination) for file in self.manifest.files]
        destinations.extend(
            build.destination
            for build in self.manifest.builds
            if build.destination is not None
        )
        self.assertEqual(len(destinations), len(set(destinations)))
        self.assertTrue(all(path.startswith(("/etc/", "/usr/")) for path in destinations))
        self.assertFalse(any("/home/" in path for path in destinations))

    def test_loader_rejects_activation_and_destination_drift(self) -> None:
        cases = []
        activating = copy.deepcopy(self.value)
        activating["policy"]["start_service_on_install"] = True
        cases.append(activating)

        stale_binary = copy.deepcopy(self.value)
        stale_binary["builds"][0]["destination"] = "/usr/bin/hyperflux-next-bridge"
        cases.append(stale_binary)

        unknown_profile = copy.deepcopy(self.value)
        unknown_profile["builds"][0]["profile"] = "unreviewed"
        cases.append(unknown_profile)

        traversal = copy.deepcopy(self.value)
        traversal["payloads"][0]["destination"] = "/usr/share/../escape"
        cases.append(traversal)

        duplicate = copy.deepcopy(self.value)
        duplicate["payloads"][1]["destination"] = duplicate["payloads"][2]["destination"]
        cases.append(duplicate)

        wrong_schema = copy.deepcopy(self.value)
        wrong_schema["$schema"] = "../schemas/other.schema.json"
        cases.append(wrong_schema)

        duplicate_distribution = copy.deepcopy(self.value)
        python_builds = [
            build
            for build in duplicate_distribution["builds"]
            if build["kind"] == "python-project"
        ]
        python_builds[1]["distribution"] = python_builds[0]["distribution"]
        cases.append(duplicate_distribution)

        for index, value in enumerate(cases):
            with self.subTest(index=index), patch(
                "hfxdev.install.load_json", return_value=value
            ):
                with self.assertRaises(ModelError):
                    load_install_manifest(ROOT)


if __name__ == "__main__":
    unittest.main()
