# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
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
import hfxdev.native_probe as native_probe
import hfxdev.openrazer as openrazer
import hfxdev.openrazer_native_route as openrazer_native_route
from hfxdev.native_control import (
    load_native_control_policy,
    load_native_method_inventory,
    transformed_native_inventory,
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
from hfxdev.generators.openrazer_native import relay_service
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.openrazer_native_route import load_native_deployment
from hfxdev.profiles import load_profile_inputs


class IntegrationCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_integration_catalog(ROOT)

    def test_catalog_is_deterministic_pinned_and_network_independent(self) -> None:
        self.assertEqual(compiled_catalog(ROOT), compiled_catalog(ROOT))
        upstreams = {value["id"]: value for value in self.catalog["upstreams"]}
        self.assertEqual(
            upstreams["openrgb"]["commit"],
            "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0",
        )
        self.assertEqual(
            upstreams["openrazer"]["commit"],
            "ddea89607b86023e88563b8feabc8fa2d5773cdd",
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
            "openrazer-compatibility": [5],
            "openrazer-native-route": [7],
            "openrgb-native": [7],
            "polychromatic-native": [5],
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

    def test_openrazer_relay_keeps_module_preflight_outside_its_sealed_namespace(self) -> None:
        runtime = load_linux_runtime(ROOT)
        service = relay_service(load_native_deployment(ROOT), runtime)
        self.assertIn(f"Requires={runtime.bridge.service_unit}", service)
        self.assertIn("ProtectKernelModules=yes", service)
        self.assertNotIn("ExecStartPre=", service)

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
