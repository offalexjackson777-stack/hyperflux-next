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

from hfxdev.install import _first_run_activation, load_install_manifest
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, load_json


class InstallManifestTests(unittest.TestCase):
    def test_development_projections_are_not_runtime_payloads(self) -> None:
        projected = ROOT / "docs" / "generated" / "future-contract.md"
        self.assertFalse(projected.exists())
        manifest = load_install_manifest(ROOT, projected_files=(projected,))
        matches = [
            file
            for file in manifest.files
            if file.source == projected
        ]
        self.assertEqual(matches, [])

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

    def test_first_run_activation_enables_without_start_and_reboots(self) -> None:
        runtime = load_linux_runtime(ROOT)
        activation = self.manifest.first_run_activation
        self.assertEqual(
            activation.enable_command(runtime),
            'sudo systemctl enable hyperflux-next-bridge.service '
            '"hyperflux-next-client@$(id -u).socket"',
        )
        self.assertEqual(activation.activation_command(), "sudo reboot")
        self.assertEqual(
            activation.disconnected_command(runtime),
            "sudo /usr/lib/hyperflux-next/hyperflux-next-activate "
            "activate-disconnected-driver",
        )
        canonical = self.value["first_run_activation"]
        for field, value in (
            ("unit_enable", "enable-and-start"),
            ("activation", "live-start"),
            ("disconnected_activation_precondition", "none"),
        ):
            with self.subTest(field=field):
                changed = dict(canonical, **{field: value})
                with self.assertRaises(ModelError):
                    _first_run_activation(changed)
        with self.assertRaises(ModelError):
            _first_run_activation(dict(canonical, live_start=True))

    def test_no_document_starts_hyperflux_units_during_enable(self) -> None:
        runtime = load_linux_runtime(ROOT)
        units = (
            runtime.bridge.service_unit.removesuffix(".service"),
            runtime.desktop_access.socket_unit.split("@", 1)[0],
        )
        candidates = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        candidates.extend(sorted((ROOT / "packaging").rglob("*.install")))
        for path in candidates:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "enable --now" in line or "--now enable" in line:
                    with self.subTest(path=str(path.relative_to(ROOT))):
                        self.assertFalse(any(unit in line for unit in units), line)

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
        self.assertIn(
            "/usr/lib/systemd/system/hyperflux-next-client@.socket", destinations
        )
        self.assertIn(
            "/usr/lib/systemd/system/hyperflux-next-client@.service", destinations
        )
        self.assertIn(
            "/usr/lib/systemd/system/hyperflux-next-openrazer-driver-preflight.service",
            destinations,
        )
        self.assertFalse(
            any("user-runtime-dir@.service.d" in destination for destination in destinations)
        )

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

    def test_development_python_sdk_license_is_generated_from_repository_authority(self) -> None:
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

    def test_split_components_exclude_development_and_application_payloads(self) -> None:
        self.assertEqual(
            {build.id for build in self.manifest.builds_for("full")},
            {
                "openrazer-native-relay",
                "openrazer-native-driver",
                "openrazer-native-udev-rules",
                "openrazer-native-mount-helper",
                "openrgb-native",
            },
        )
        self.assertEqual(
            {file.component for file in self.manifest.files_for("full")},
            {
                "openrazer-native-relay-service",
                "openrazer-native-driver-preflight-service",
                "openrazer-system-group",
                "openrazer-native-modules-load",
                "full-bridge-integration-drop-in",
                "full-component-descriptor",
                "openrgb-license",
            },
        )
        self.assertEqual(
            {str(file.destination) for file in self.manifest.files_for("full")},
            {
                "/usr/lib/systemd/system/hyperflux-next-openrazer-relay.service",
                "/usr/lib/systemd/system/hyperflux-next-openrazer-driver-preflight.service",
                "/usr/lib/sysusers.d/openrazer.conf",
                "/usr/lib/modules-load.d/hyperflux-next-openrazer.conf",
                "/usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf",
                "/usr/share/hyperflux-next/components/full.json",
                "/usr/share/licenses/hyperflux-next-full/LICENSE",
            },
        )
        self.assertNotIn(
            "python-sdk", {build.id for build in self.manifest.builds}
        )
        forbidden_prefixes = (
            "/usr/include/",
            "/usr/share/hyperflux-next/schemas/",
            "/usr/share/hyperflux-next/catalogs/",
            "/usr/share/hyperflux-next/qualification-console/",
        )
        self.assertFalse(
            any(
                str(file.destination).startswith(forbidden_prefixes)
                for file in self.manifest.files
            )
        )
        core_destinations = {
            str(file.destination) for file in self.manifest.files_for("linux")
        } | {
            build.destination
            for build in self.manifest.builds_for("linux")
            if build.destination is not None
        }
        self.assertFalse(
            any(
                "openrazer" in destination or "/openrgb/" in destination
                for destination in core_destinations
            )
        )

    def test_full_descriptor_and_bridge_layering_are_generated_exactly(self) -> None:
        descriptor = load_json(ROOT / "packaging/generated/full-component.json")
        self.assertEqual(
            descriptor,
            {
                "$schema": "https://hyperflux.dev/schemas/distribution-component-descriptor-v1.json",
                "schema": "hyperflux-distribution-component-v1",
                "component_id": "full",
                "product_version": "2.1.0",
                "capabilities": ["openrazer-native-route", "openrgb-plugin"],
            },
        )
        bridge = (ROOT / "packaging/generated/hyperflux-next-bridge.service").read_text(
            encoding="utf-8"
        )
        drop_in = (
            ROOT
            / "packaging/generated/hyperflux-next-bridge.service.d/50-full.conf"
        ).read_text(encoding="utf-8")
        self.assertNotIn("openrazer", bridge)
        self.assertIn("Wants=hyperflux-next-openrazer-relay.service", drop_in)
        self.assertIn("Before=hyperflux-next-openrazer-relay.service", drop_in)

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

        wrong_component = copy.deepcopy(self.value)
        next(
            build
            for build in wrong_component["builds"]
            if build["id"] == "openrgb-native"
        )["distribution_component"] = "linux"
        cases.append(wrong_component)

        full_payload = copy.deepcopy(self.value)
        payload = copy.deepcopy(full_payload["payloads"][-1])
        payload["id"] = "forbidden-full-payload"
        payload["distribution_component"] = "full"
        payload["destination"] = "/usr/share/hyperflux-next-full/LICENSE"
        full_payload["payloads"].append(payload)
        cases.append(full_payload)

        wrong_relay_source = copy.deepcopy(self.value)
        next(
            payload
            for payload in wrong_relay_source["payloads"]
            if payload["id"] == "openrazer-native-relay-service"
        )["source"] = "packaging/generated/hyperflux-next-openrazer-driver-preflight.service"
        cases.append(wrong_relay_source)

        executable_relay = copy.deepcopy(self.value)
        next(
            payload
            for payload in executable_relay["payloads"]
            if payload["id"] == "openrazer-native-relay-service"
        )["mode"] = "0755"
        cases.append(executable_relay)

        preserved_relay = copy.deepcopy(self.value)
        next(
            payload
            for payload in preserved_relay["payloads"]
            if payload["id"] == "openrazer-native-relay-service"
        )["preserve"] = True
        cases.append(preserved_relay)

        wrong_preflight_source = copy.deepcopy(self.value)
        next(
            payload
            for payload in wrong_preflight_source["payloads"]
            if payload["id"] == "openrazer-native-driver-preflight-service"
        )["source"] = "packaging/generated/hyperflux-next-openrazer-relay.service"
        cases.append(wrong_preflight_source)

        executable_preflight = copy.deepcopy(self.value)
        next(
            payload
            for payload in executable_preflight["payloads"]
            if payload["id"] == "openrazer-native-driver-preflight-service"
        )["mode"] = "0755"
        cases.append(executable_preflight)

        preserved_preflight = copy.deepcopy(self.value)
        next(
            payload
            for payload in preserved_preflight["payloads"]
            if payload["id"] == "openrazer-native-driver-preflight-service"
        )["preserve"] = True
        cases.append(preserved_preflight)

        for index, value in enumerate(cases):
            with self.subTest(index=index), patch(
                "hfxdev.install.load_json", return_value=value
            ):
                with self.assertRaises(ModelError):
                    load_install_manifest(ROOT)


if __name__ == "__main__":
    unittest.main()
