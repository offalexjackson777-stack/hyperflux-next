# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.integrations import (
    _validate_adapters,
    _validate_upstreams,
    compiled_catalog,
    load_integration_catalog,
)
from hfxdev.model import ModelError
from hfxdev.model import load_json as model_load_json
import hfxdev.native_control as native_control
import hfxdev.native_command as native_command
import hfxdev.native_probe as native_probe
import hfxdev.openrazer as openrazer
import hfxdev.openrazer_native_route as openrazer_native_route
from hfxdev.native_control import (
    load_native_control_policy,
    load_native_method_inventory,
    transformed_native_inventory,
)
from hfxdev.native_command import (
    load_native_command_catalog,
    load_native_command_policy,
    transformed_native_command_catalog,
)
from hfxdev.native_probe import (
    load_native_probe_catalog,
    load_native_probe_policy,
    transformed_native_probe_catalog,
)
from hfxdev.openrazer import (
    _ClassCatalog,
    effective_event_regex,
    load_import_selection,
    load_imported_metadata,
    transformed_metadata,
    verify_global_event_file_matcher,
)
from hfxdev.openrazer_native_route import (
    load_native_transform,
    load_native_route_catalog,
    transformed_native_route_catalog,
)
from hfxdev.generators.integrations import python_openrazer_metadata
from hfxdev.generators.linux_runtime import systemd_service
from hfxdev.generators.openrazer_native import (
    driver_preflight_service,
    key_event_udev_rules,
    modules_load,
    relay_service,
)
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.openrazer_native_route import load_native_deployment
from hfxdev.profiles import compiled_catalog as compiled_profile_catalog
from hfxdev.profiles import load_profile_inputs


def _build_directory() -> Path:
    """Return the ignored build folder, created on a fresh checkout."""
    directory = ROOT / "build"
    directory.mkdir(exist_ok=True)
    return directory


class IntegrationCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_integration_catalog(ROOT)

    def test_catalog_is_deterministic_pinned_and_network_independent(self) -> None:
        self.assertEqual(compiled_catalog(ROOT), compiled_catalog(ROOT))
        upstreams = {value["id"]: value for value in self.catalog["upstreams"]}
        self.assertEqual(
            upstreams["openrgb"]["commit"],
            "8e16de27858cf2be6af54a33ce003df74cb9fd3e",
        )
        self.assertEqual(
            upstreams["openrazer"]["commit"],
            "1ef8a91712906a89fd332a2ed62bbd105315d6d1",
        )
        self.assertEqual(
            upstreams["polychromatic"]["commit"],
            "716737bf444ab7e962f4d3b7a5188b2f07c0c4a1",
        )
        self.assertTrue(
            all(value["build_network_access"] is False for value in upstreams.values())
        )

    def test_every_adapter_is_sdk_only_and_preserves_unrelated_devices(self) -> None:
        expected_protocols = {
            "openrazer-native-route": [7],
            "openrgb-native": [7],
        }
        for adapter in self.catalog["adapters"]:
            self.assertEqual(adapter["transport_access"], "sdk-only")
            self.assertEqual(adapter["unrelated_device_policy"], "preserve")
            self.assertEqual(
                adapter["sdk_protocol_versions"], expected_protocols[adapter["id"]]
            )
            self.assertTrue(adapter["owns"])
            self.assertTrue(adapter["must_not_own"])

    def test_native_routes_own_transport_while_upstreams_keep_device_semantics(self) -> None:
        adapters = {value["id"]: value for value in self.catalog["adapters"]}
        openrazer = adapters["openrazer-native-route"]
        self.assertEqual(openrazer["kind"], "transport-adapter")
        self.assertEqual(openrazer["coexistence_policy"], "upstream-transport-extension")
        self.assertIn("native-control", openrazer["feature_families"])
        self.assertTrue(
            any("device commands" in value for value in openrazer["must_not_own"])
        )

        openrgb = adapters["openrgb-native"]
        self.assertEqual(openrgb["status"], "software-verified")
        self.assertIn("native-control", openrgb["feature_families"])
        self.assertTrue(
            any("device models" in value for value in openrgb["must_not_own"])
        )

    def test_child_presentation_matches_one_canonical_upstream_pin(self) -> None:
        upstreams = {value["id"]: value for value in self.catalog["upstreams"]}
        for profile in load_profile_inputs(ROOT).profiles:
            presentation = profile.get("presentation")
            if presentation is None:
                continue
            upstream = upstreams[presentation["upstream_id"]]
            self.assertEqual(presentation["owner"], upstream["name"])
            self.assertEqual(presentation["project_version"], upstream["version"])
            self.assertEqual(presentation["source_commit"], upstream["commit"])

    def test_unknown_keys_mutable_fetches_and_device_suppression_fail_closed(self) -> None:
        upstream_values = deepcopy(self.catalog["upstreams"])
        upstream_values[0]["surprise"] = True
        with self.assertRaisesRegex(ModelError, "unsupported keys: surprise"):
            _validate_upstreams(upstream_values)

        upstream_values = deepcopy(self.catalog["upstreams"])
        upstream_values[0]["build_network_access"] = True
        with self.assertRaisesRegex(ModelError, "must not fetch mutable upstream"):
            _validate_upstreams(upstream_values)

        adapter_values = deepcopy(self.catalog["adapters"])
        adapter_values[0]["unrelated_device_policy"] = "hide"
        upstreams = _validate_upstreams(deepcopy(self.catalog["upstreams"]))
        with self.assertRaisesRegex(ModelError, "must be preserved"):
            _validate_adapters(adapter_values, upstreams)

    def test_openrazer_import_is_pinned_licensed_and_profile_bound(self) -> None:
        selection = load_import_selection(ROOT)
        metadata = load_imported_metadata(ROOT)
        self.assertEqual(selection["source_commit"], metadata["upstream"]["commit"])
        self.assertIn("GPL-2.0-or-later", metadata["upstream"]["license_expression"])
        profiles = {device["profile_id"] for device in metadata["devices"]}
        self.assertEqual(
            profiles,
            {
                "child.razer.basilisk-v3-pro-35k.00cd",
                "child.razer.deathstalker-v2-pro-tkl.0296",
            },
        )
        for device in metadata["devices"]:
            self.assertTrue(device["presentation"]["image_url"].startswith("https://"))
            self.assertTrue(device["presentation"]["has_matrix"])
            self.assertEqual(device["advertised_methods"], sorted(device["advertised_methods"]))

    def test_openrazer_native_method_inventory_is_exact_and_fail_closed(self) -> None:
        policy = load_native_control_policy(ROOT)
        inventory = load_native_method_inventory(ROOT)
        self.assertEqual(policy["report_size"], 90)
        self.assertEqual(inventory["report_size"], 90)
        self.assertEqual(inventory["upstream"]["commit"], policy["source_commit"])

        devices = {value["profile_id"]: value for value in inventory["devices"]}
        mouse = devices["child.razer.basilisk-v3-pro-35k.00cd"]
        keyboard = devices["child.razer.deathstalker-v2-pro-tkl.0296"]
        self.assertEqual(mouse["wireless_identity"]["product_id"], 0x00CD)
        self.assertEqual(mouse["direct_usb_reference"]["product_id"], 0x00CC)
        self.assertEqual(keyboard["wireless_identity"]["product_id"], 0x0296)
        self.assertEqual(keyboard["direct_usb_reference"]["product_id"], 0x0298)
        keyboard_methods = {value["id"]: value for value in keyboard["methods"]}
        self.assertEqual(
            keyboard_methods["set_brightness"]["origin"], "device-kind-mixin"
        )
        self.assertEqual(
            keyboard_methods["set_brightness"]["readback"], "get_brightness"
        )

        for device in devices.values():
            methods = {value["id"]: value for value in device["methods"]}
            self.assertEqual(methods["get_serial"]["classification"], "read-only")
            self.assertEqual(methods["get_serial"]["privacy"], "redacted-equality-only")
            for probe in device["qualification_probes"]:
                self.assertEqual(methods[probe]["classification"], "read-only")
            for method in methods.values():
                if method["classification"] == "reversible-with-readback":
                    self.assertIn(method["readback"], methods)

    def test_openrazer_native_policy_rejects_an_unclassified_upstream_method(self) -> None:
        metadata = deepcopy(load_imported_metadata(ROOT))
        metadata["devices"][0]["advertised_methods"].append("new_unreviewed_method")
        metadata["devices"][0]["advertised_methods"].sort()

        with patch.object(native_control, "load_imported_metadata", return_value=metadata):
            with self.assertRaisesRegex(ModelError, "unclassified: new_unreviewed_method"):
                load_native_control_policy(ROOT)

    def test_openrazer_inherited_daemon_methods_are_source_derived(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory)
            hardware = source / "daemon" / "openrazer_daemon" / "hardware"
            hardware.mkdir(parents=True)
            (hardware / "device_base.py").write_text(
                "\n".join(
                    (
                        "class RazerDevice:",
                        "    pass",
                        "class Brightness(RazerDevice):",
                        "    def __init__(self, *args, **kwargs):",
                        "        if 'additional_methods' in kwargs:",
                        "            kwargs['additional_methods'].extend(['get_brightness', 'set_brightness'])",
                        "        else:",
                        "            kwargs['additional_methods'] = ['get_brightness', 'set_brightness']",
                    )
                ),
                encoding="utf-8",
            )
            (hardware / "keyboards.py").write_text(
                "\n".join(
                    (
                        "from openrazer_daemon.hardware.device_base import Brightness as _Brightness",
                        "class Keyboard(_Brightness):",
                        "    def __init__(self, *args, **kwargs):",
                        "        if 'additional_methods' in kwargs:",
                        "            kwargs['additional_methods'].extend(['get_keyboard_layout'])",
                        "        else:",
                        "            kwargs['additional_methods'] = ['get_keyboard_layout']",
                        "class Selected(Keyboard):",
                        "    pass",
                    )
                ),
                encoding="utf-8",
            )
            (hardware / "mouse.py").write_text("", encoding="utf-8")
            self.assertEqual(
                native_control._source_bound_additional_methods(source, "Selected"),
                {"get_brightness", "get_keyboard_layout", "set_brightness"},
            )

    def test_openrazer_keyboard_command_policy_covers_inherited_brightness(self) -> None:
        inventory = load_native_method_inventory(ROOT)
        keyboard = next(
            device
            for device in inventory["devices"]
            if device["profile_id"] == "child.razer.deathstalker-v2-pro-tkl.0296"
        )
        self.assertIn("set_brightness", {method["id"] for method in keyboard["methods"]})
        policy = load_native_command_policy(ROOT)
        keyboard_policy = next(
            family
            for family in policy["families"]
            if family["device_kind"] == "keyboard"
        )
        brightness = next(
            rule for rule in keyboard_policy["rules"] if rule["id"] == "brightness"
        )
        self.assertEqual(brightness["methods"], ["set_brightness"])
        self.assertEqual(brightness["command_class"], 0x0F)
        self.assertEqual(brightness["command_id"], 0x04)
        self.assertEqual(brightness["matcher"]["fields"][0]["bytes"], [1, 5])

    def test_openrazer_keyboard_startup_brightness_cannot_lose_command_coverage(
        self,
    ) -> None:
        policy_path = (
            ROOT / "integrations" / "openrazer" / "native-command-policy.json"
        )
        policy = deepcopy(model_load_json(policy_path))
        keyboard = next(
            family
            for family in policy["families"]
            if family["device_kind"] == "keyboard"
        )
        keyboard["rules"] = [
            rule for rule in keyboard["rules"] if rule["id"] != "brightness"
        ]

        def substituted_load(path: Path):
            if path == policy_path:
                return deepcopy(policy)
            return model_load_json(path)

        with patch.object(native_command, "load_json", side_effect=substituted_load):
            source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
            if source_value is None:
                self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
            with self.assertRaisesRegex(
                ModelError,
                "family policy misses set_brightness",
            ):
                transformed_native_command_catalog(ROOT, Path(source_value))

    def test_openrazer_native_probe_catalog_is_source_bound_and_privacy_safe(self) -> None:
        policy = load_native_probe_policy(ROOT)
        catalog = load_native_probe_catalog(ROOT)
        self.assertEqual(catalog["report_size"], 90)
        self.assertEqual(catalog["upstream"]["commit"], policy["source_commit"])
        self.assertEqual(catalog["busy_retry"], {"status": 1, "maximum_attempts": 5})
        comparisons = {value["method"]: value["comparison"] for value in policy["bindings"]}
        self.assertEqual(comparisons["get_battery"], "domain-valid")
        self.assertEqual(comparisons["is_charging"], "domain-valid")
        self.assertEqual(comparisons["get_serial"], "redacted-equality")
        self.assertEqual(comparisons["get_firmware"], "exact-semantic")

        devices = {value["profile_id"]: value for value in catalog["devices"]}
        mouse = devices["child.razer.basilisk-v3-pro-35k.00cd"]
        keyboard = devices["child.razer.deathstalker-v2-pro-tkl.0296"]
        self.assertEqual(mouse["wireless_identity"]["product_id"], 0x00CD)
        self.assertEqual(mouse["direct_usb_reference"]["product_id"], 0x00CC)
        self.assertEqual(keyboard["wireless_identity"]["product_id"], 0x0296)
        self.assertEqual(keyboard["direct_usb_reference"]["product_id"], 0x0298)
        mouse_brightness = next(
            probe for probe in mouse["probes"] if probe["method"] == "get_brightness"
        )
        keyboard_brightness = next(
            probe
            for probe in keyboard["probes"]
            if probe["method"] == "get_brightness"
        )
        for probe, arguments in (
            (mouse_brightness, [1, 0, 0]),
            (keyboard_brightness, [1, 5, 0]),
        ):
            request = probe["request"]
            self.assertEqual(request["command_class"], 0x0F)
            self.assertEqual(request["command_id"], 0x84)
            self.assertEqual(request["data_size"], 3)
            self.assertEqual(request["arguments"], arguments)
            self.assertTrue(request["transaction_id_from_upstream"])
            self.assertTrue(request["crc_recomputed_by_upstream"])

        for device in devices.values():
            signatures = set()
            for probe in device["probes"]:
                request = probe["request"]
                signature = (
                    request["command_class"],
                    request["command_id"],
                    request["data_size"],
                    tuple(request["arguments"]),
                )
                self.assertNotIn(signature, signatures)
                signatures.add(signature)
                self.assertTrue(request["transaction_id_from_upstream"])
                self.assertTrue(request["crc_recomputed_by_upstream"])
                self.assertEqual(probe["comparison"], comparisons[probe["method"]])
                if probe["method"] == "get_serial":
                    self.assertEqual(probe["privacy"], "redacted-equality-only")
                    self.assertEqual(
                        probe["response_summary"], "redacted-serial-equality"
                    )

        serialized = str(catalog)
        for forbidden in (
            "serial_value",
            "raw_report",
            "request_bytes",
            "response_bytes",
            "/home/",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_openrazer_native_probe_catalog_matches_exact_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        self.assertEqual(
            transformed_native_probe_catalog(ROOT, Path(source_value)),
            load_native_probe_catalog(ROOT),
        )

    def test_openrazer_native_command_catalog_matches_exact_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        self.assertEqual(
            transformed_native_command_catalog(ROOT, Path(source_value)),
            load_native_command_catalog(ROOT),
        )

    def test_openrazer_native_command_policy_rejects_source_fixed_byte_drift(self) -> None:
        policy = deepcopy(load_native_command_policy(ROOT))
        mouse = next(
            family
            for family in policy["families"]
            if family["device_kind"] == "mouse"
        )
        dpi = next(rule for rule in mouse["rules"] if rule["id"] == "dpi")
        dpi["matcher"]["fields"][0]["bytes"] = [0]
        with TemporaryDirectory(
            prefix="test-openrazer-command-policy-",
            dir=_build_directory(),
        ) as directory:
            source = Path(directory)
            driver = source / "driver"
            driver.mkdir()
            (driver / "razercommon.h").write_text(
                "#define VARSTORE 0x01\n",
                encoding="utf-8",
            )
            (driver / "razerchromacommon.c").write_text(
                "\n".join(
                    (
                        "struct razer_report razer_chroma_misc_set_dpi_xy(void)",
                        "{",
                        "    struct razer_report report = get_razer_report(0x04, 0x05, 0x07);",
                        "    report.arguments[0] = VARSTORE;",
                        "    return report;",
                        "}",
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ModelError,
                "source-fixed argument 0=0x01 is rejected",
            ):
                native_command._verify_rule_source_semantics(
                    source,
                    dpi,
                    "test DPI rule",
                )

    def test_openrazer_probe_harness_uses_the_executable_build_root(self) -> None:
        with TemporaryDirectory(
            prefix="test-openrazer-probe-",
            dir=_build_directory(),
        ) as directory:
            temporary_root = Path(directory)
            root = temporary_root / "repository"
            source_root = temporary_root / "source"
            (source_root / "driver").mkdir(parents=True)
            (source_root / "driver" / "razercommon.h").write_text(
                "", encoding="utf-8"
            )
            (source_root / "driver" / "razerchromacommon.c").write_text(
                "", encoding="utf-8"
            )
            calls: list[dict[str, object]] = []

            def recording_temporary_directory(*args, **kwargs):
                calls.append(dict(kwargs))
                return TemporaryDirectory(*args, **kwargs)

            with patch.object(
                native_probe,
                "TemporaryDirectory",
                side_effect=recording_temporary_directory,
            ):
                reports = native_probe._compile_report_expressions(
                    root,
                    source_root,
                    ["get_razer_report(0x00, 0x80, 0x00)"],
                )

            self.assertEqual(len(reports), 1)
            self.assertEqual(
                calls,
                [
                    {
                        "prefix": "hyperflux-openrazer-probe-",
                        "dir": root / "build",
                    }
                ],
            )

    def test_openrazer_native_route_catalog_is_fully_authority_bound(self) -> None:
        routes = load_native_route_catalog(ROOT)
        metadata = load_imported_metadata(ROOT)
        probes = load_native_probe_catalog(ROOT)
        self.assertEqual(
            [route["profile_id"] for route in routes["routes"]],
            [device["profile_id"] for device in metadata["devices"]],
        )
        probes_by_profile = {
            device["profile_id"]: device for device in probes["devices"]
        }
        for route in routes["routes"]:
            direct = probes_by_profile[route["profile_id"]]["direct_usb_reference"]
            self.assertEqual(route["direct_data_product_ids"], [direct["product_id"]])

    def test_openrazer_native_transform_auxiliary_source_set_is_exact(self) -> None:
        transform = load_native_transform(ROOT)
        auxiliary = {
            record["path"]
            for record in transform["files"]
            if not record["path"].startswith("driver/")
        }
        self.assertEqual(
            auxiliary,
            {
                "install_files/udev/99-razer.rules",
                "install_files/udev/razer_mount",
            },
        )

        drifted = deepcopy(transform)
        drifted["files"][-1]["path"] = "install_files/udev/unreviewed-helper"
        path = ROOT / "integrations/openrazer/native-route/transform.json"

        def substituted_load(candidate: Path):
            if candidate == path:
                return deepcopy(drifted)
            return model_load_json(candidate)

        with patch.object(
            openrazer_native_route,
            "load_json",
            side_effect=substituted_load,
        ), self.assertRaisesRegex(ModelError, "outside the approved source set"):
            load_native_transform(ROOT)

    def test_openrazer_driver_preflight_is_unprivileged_and_outside_relay_namespace(
        self,
    ) -> None:
        runtime = load_linux_runtime(ROOT)
        deployment = load_native_deployment(ROOT)
        self.assertIs(deployment.relay.automatic_restart, False)
        transform = load_native_transform(ROOT)
        self.assertEqual(
            deployment.upstream_repository, transform["upstream"]["repository"]
        )
        self.assertEqual(deployment.upstream_commit, transform["upstream"]["commit"])
        service = relay_service(deployment, runtime)
        preflight = driver_preflight_service(deployment, runtime)
        self.assertIn(
            f"Requires={runtime.bridge.service_unit} "
            f"{deployment.relay.preflight_service_unit}",
            service,
        )
        self.assertIn(
            f"After={runtime.bridge.service_unit} "
            f"{deployment.relay.preflight_service_unit} systemd-udevd.service",
            service,
        )
        self.assertIn("Before=systemd-user-sessions.service", service)
        # REL-003: the native drivers load at boot, so the first virtual Razer
        # device is never bound by hid-generic and re-probed under the relay.
        boot_modules = [
            line for line in modules_load(deployment).splitlines() if line and not line.startswith("#")
        ]
        self.assertEqual(boot_modules, list(deployment.driver.modules))
        self.assertIn("razerkbd", boot_modules)
        self.assertIn("razermouse", boot_modules)
        self.assertIn(
            f"SupplementaryGroups={runtime.bridge.client_group}", service
        )
        self.assertNotIn(
            f"SupplementaryGroups={runtime.bridge.kernel_access_group}", service
        )
        self.assertIn("ProtectKernelModules=yes", service)
        self.assertIn("ProtectKernelTunables=yes", service)
        # REL-020: only each OpenRazer driver's unbind file is writable in /sys.
        self.assertIn(
            "ReadWritePaths="
            + " ".join(
                f"-/sys/bus/hid/drivers/{module}/unbind"
                for module in deployment.driver.modules
            ),
            service,
        )
        sys_paths = [
            path
            for line in service.splitlines()
            if line.startswith(("ReadWritePaths=", "ReadOnlyPaths="))
            for path in line.partition("=")[2].split()
            if "/sys/" in path
        ]
        self.assertTrue(sys_paths)
        self.assertTrue(all(path.endswith("/unbind") for path in sys_paths))
        self.assertNotIn("ExecStartPre=", service)
        self.assertNotIn("ExecCondition=", service)
        self.assertNotIn("check-openrazer-driver", service)
        self.assertNotIn("/usr/lib/modules", service)
        self.assertNotIn("/sys/module", service)
        self.assertIn(
            f"ConditionFileIsExecutable={deployment.compatibility.daemon_executable_path}",
            service,
        )
        self.assertIn("Restart=no", service)
        self.assertNotIn("RestartSec=", service)
        self.assertNotIn("StartLimitIntervalSec=infinity", service)

        self.assertIn("Type=oneshot", preflight)
        self.assertIn("DynamicUser=yes", preflight)
        preflight_directives = {
            line.partition("=")[0]
            for line in preflight.splitlines()
            if "=" in line and not line.startswith("#")
        }
        self.assertNotIn("User", preflight_directives)
        self.assertNotIn("Group", preflight_directives)
        self.assertNotIn("SupplementaryGroups", preflight_directives)
        self.assertIn(
            f"ExecStart={runtime.operations.activation_path} check-openrazer-driver",
            preflight,
        )
        self.assertIn("ProtectKernelModules=no", preflight)
        self.assertIn("RestrictAddressFamilies=none", preflight)
        self.assertIn(
            f"InaccessiblePaths={runtime.bridge.runtime_directory}", preflight
        )
        self.assertIn(
            "ReadOnlyPaths=/usr/lib/hyperflux-next /usr/lib/modules", preflight
        )
        self.assertIn(
            "SystemCallFilter=~@mount @module @raw-io @reboot @swap", preflight
        )
        self.assertIn("CapabilityBoundingSet=\n", preflight)
        self.assertIn("AmbientCapabilities=\n", preflight)
        self.assertNotIn("CAP_SYS_MODULE", preflight)
        self.assertNotIn("CAP_DAC_OVERRIDE", preflight)
        self.assertIn("PrivateDevices=yes", preflight)
        self.assertIn("DevicePolicy=closed", preflight)
        self.assertNotIn("DeviceAllow=", preflight)
        self.assertIn("Restart=no", preflight)
        self.assertNotIn("RestartSec=", preflight)
        self.assertNotIn("Upholds=", preflight)
        self.assertIn("TimeoutStartSec=10s", preflight)
        metadata_commands = len(deployment.driver.modules) + len(
            deployment.compatibility.runtime_capability.modules
        )
        self.assertLess(
            metadata_commands * runtime.operations.status_timeout_ms,
            10_000,
        )
        drifted_runtime = replace(
            runtime,
            operations=replace(runtime.operations, status_timeout_ms=2_000),
        )
        with self.assertRaisesRegex(ModelError, "command budget exceeds"):
            driver_preflight_service(deployment, drifted_runtime)
        self.assertNotIn("RemainAfterExit=", preflight)
        self.assertIn(f"Before={deployment.relay.service_unit}", preflight)
        self.assertIn(
            f"After={runtime.bridge.service_unit} systemd-udevd.service", preflight
        )

        bridge_service = systemd_service(
            runtime, wanted_units=(deployment.relay.service_unit,)
        )
        wants = next(
            line for line in bridge_service.splitlines() if line.startswith("Wants=")
        ).removeprefix("Wants=").split()
        self.assertIn(deployment.relay.service_unit, wants)
        self.assertNotIn(deployment.relay.preflight_service_unit, wants)
        self.assertNotIn(f"Requires={deployment.relay.service_unit}", bridge_service)
        self.assertNotIn(
            f"Requires={deployment.relay.preflight_service_unit}", bridge_service
        )

        invalid = deepcopy(
            model_load_json(
                ROOT / "integrations/openrazer/native-route/deployment.json"
            )
        )
        invalid["relay"]["automatic_restart"] = True

        def substituted_load(path: Path):
            if path == ROOT / "integrations/openrazer/native-route/deployment.json":
                return deepcopy(invalid)
            return model_load_json(path)

        with patch.object(
            openrazer_native_route,
            "load_json",
            side_effect=substituted_load,
        ), self.assertRaisesRegex(ModelError, "automatic restart must remain disabled"):
            load_native_deployment(ROOT)

    def test_openrazer_key_event_links_match_only_their_keyboard(self) -> None:
        deployment = load_native_deployment(ROOT)
        catalog = load_native_route_catalog(ROOT)
        matchers = {
            route["profile_id"]: route["event_file_regex"] for route in catalog["routes"]
        }
        self.assertEqual(
            deployment.key_events.udev_rules_path,
            "/usr/lib/udev/rules.d/70-hyperflux-next-openrazer-keys.rules",
        )
        for link in deployment.key_events.links:
            self.assertRegex(link.link_name, matchers[link.profile_id])
            self.assertTrue(link.link_name.startswith("hyperflux-next-"))

        rules = key_event_udev_rules(deployment, compiled_profile_catalog(ROOT))
        self.assertIn(
            'ENV{ID_BUS}=="usb", ENV{ID_VENDOR_ID}=="1532", ENV{ID_MODEL_ID}=="00cf", '
            'ENV{ID_USB_INTERFACE_NUM}=="01", SYMLINK+="input/by-id/'
            'hyperflux-next-DSV2Pro_TKL_000000000000-if01-event-mouse", TAG+="uaccess"',
            rules,
        )
        self.assertIn('ACTION=="remove", GOTO=', rules)
        self.assertNotIn("GROUP=", rules)
        self.assertNotIn("MODE=", rules)

        source = model_load_json(ROOT / "integrations/openrazer/native-route/deployment.json")
        basilisk = "child.razer.basilisk-v3-pro-35k.00cd"
        cases = {
            "does not match the OpenRazer event pattern": lambda value: value["key_events"][
                "links"
            ][0].update(link_name="hyperflux-next-Other_000000000000-if01-event-mouse"),
            "also matches another OpenRazer device": lambda value: value["key_events"][
                "links"
            ][0].update(
                link_name=(
                    "hyperflux-next-DSV2Pro_TKL_000000000000-if01-event-mouse-"
                    "Razer_Basilisk_V3_Pro_35K_000000000000-if01-event-kbd"
                )
            ),
            "profile has no native route": lambda value: value["key_events"]["links"][
                0
            ].update(profile_id="child.razer.unknown.0000"),
            "link name is invalid": lambda value: value["key_events"]["links"][0].update(
                link_name="DSV2Pro_TKL_000000000000-if01-event-mouse"
            ),
            "receiver USB interface is invalid": lambda value: value["key_events"]["links"][
                0
            ].update(receiver_usb_interface=True),
            "access must stay with the seat user": lambda value: value["key_events"].update(
                access="group"
            ),
            "udev rules path differs": lambda value: value["key_events"].update(
                udev_rules_path="/usr/lib/udev/rules.d/99-hyperflux-next-openrazer-keys.rules"
            ),
        }
        self.assertIn(basilisk, matchers)
        for message, mutate in cases.items():
            invalid = deepcopy(source)
            mutate(invalid)

            def substituted_load(path: Path, invalid=invalid):
                if path == ROOT / "integrations/openrazer/native-route/deployment.json":
                    return deepcopy(invalid)
                return model_load_json(path)

            with self.subTest(message), patch.object(
                openrazer_native_route,
                "load_json",
                side_effect=substituted_load,
            ), self.assertRaisesRegex(ModelError, message):
                load_native_deployment(ROOT)

    def test_openrazer_native_route_catalog_matches_exact_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        self.assertEqual(
            transformed_native_route_catalog(ROOT, Path(source_value)),
            load_native_route_catalog(ROOT),
        )

    def test_openrazer_native_probe_policy_rejects_unbound_qualification_probe(self) -> None:
        inventory = deepcopy(load_native_method_inventory(ROOT))
        inventory["devices"][0]["qualification_probes"].append("unbound_probe")
        with patch.object(
            native_probe,
            "load_native_method_inventory",
            return_value=inventory,
        ):
            with self.assertRaisesRegex(ModelError, "coverage drift"):
                load_native_probe_policy(ROOT)

    def test_openrazer_python_metadata_is_generated_from_the_pinned_import(self) -> None:
        metadata = load_imported_metadata(ROOT)
        rendered = python_openrazer_metadata(metadata)
        self.assertIn("OPENRAZER_DEVICES_BY_PROFILE", rendered)
        self.assertIn(metadata["upstream"]["commit"], rendered)
        self.assertIn("SPDX-License-Identifier: GPL-2.0-or-later", rendered)

    def test_openrazer_import_matches_exact_pinned_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        source = Path(source_value)
        self.assertEqual(transformed_metadata(ROOT, source), load_imported_metadata(ROOT))

    def test_openrazer_native_inventory_matches_exact_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        source = Path(source_value)
        self.assertEqual(
            transformed_native_inventory(ROOT, source),
            load_native_method_inventory(ROOT),
        )

    def test_openrazer_import_rejects_nested_metadata_drift(self) -> None:
        metadata = deepcopy(load_imported_metadata(ROOT))
        metadata["devices"][0]["source"]["class_name"] = "DifferentDevice"

        def substituted_load(path: Path):
            if path == ROOT / "integrations" / "openrazer" / "metadata.json":
                return deepcopy(metadata)
            return model_load_json(path)

        with patch.object(openrazer, "load_json", side_effect=substituted_load):
            with self.assertRaisesRegex(ModelError, "source class drifts"):
                load_imported_metadata(ROOT)

    def test_openrazer_ast_import_handles_diamond_inheritance_without_execution(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.py"
            path.write_text(
                "\n".join(
                    (
                        "class Root:",
                        "    METHODS = ['root']",
                        "class Left(Root):",
                        "    USB_VID = 1",
                        "class Right(Root):",
                        "    USB_PID = 2",
                        "class Child(Left, Right):",
                        "    METHODS = ['child']",
                    )
                ),
                encoding="utf-8",
            )
            _, values = _ClassCatalog(path).resolve("Child")
        self.assertEqual(
            values,
            {"METHODS": ["child"], "USB_PID": 2, "USB_VID": 1},
        )

    def test_openrazer_event_matcher_resolves_inheritance_without_execution(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.py"
            path.write_text(
                "\n".join(
                    (
                        "import re",
                        "class Root:",
                        "    EVENT_FILE_REGEX = re.compile(r\"root-event\")",
                        "class Child(Root):",
                        "    pass",
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(effective_event_regex(path, "Child"), "root-event")

    def test_openrazer_event_matcher_accepts_source_defined_nullable_presentation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.py"
            path.write_text(
                "class Root:\n    EVENT_FILE_REGEX = None\nclass Child(Root):\n    pass\n",
                encoding="utf-8",
            )
            self.assertIsNone(effective_event_regex(path, "Child"))
            with self.assertRaisesRegex(ModelError, "absent from event matcher source"):
                effective_event_regex(path, "Missing")

    def test_openrazer_global_event_binding_is_structural_and_fail_closed(self) -> None:
        valid = "\n".join(
            (
                "import os",
                "class RazerDevice:",
                "    def __init__(self):",
                "        self.event_files = []",
                "        search_dir = '/dev/input/by-id/'",
                "        for event_file in os.listdir(search_dir):",
                "            if self.EVENT_FILE_REGEX.match(event_file):",
                "                self.event_files.append(os.path.join(search_dir, event_file))",
            )
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "device_base.py"
            path.write_text(valid, encoding="utf-8")
            verify_global_event_file_matcher(path)

            path.write_text(valid.replace("/dev/input/by-id/", "input"), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "global event-file matcher"):
                verify_global_event_file_matcher(path)


if __name__ == "__main__":
    unittest.main()
