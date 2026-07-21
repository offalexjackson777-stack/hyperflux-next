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
import hfxdev.openrazer as openrazer
from hfxdev.openrazer import (
    _ClassCatalog,
    load_import_selection,
    load_imported_metadata,
    transformed_metadata,
)
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
            "6820f9da169d354bc7e6e93a0aa8683a6bb75792",
        )
        self.assertEqual(
            upstreams["polychromatic"]["commit"],
            "716737bf444ab7e962f4d3b7a5188b2f07c0c4a1",
        )
        self.assertTrue(
            all(value["build_network_access"] is False for value in upstreams.values())
        )

    def test_every_adapter_is_sdk_only_and_preserves_unrelated_devices(self) -> None:
        for adapter in self.catalog["adapters"]:
            self.assertEqual(adapter["transport_access"], "sdk-only")
            self.assertEqual(adapter["unrelated_device_policy"], "preserve")
            self.assertEqual(adapter["sdk_protocol_versions"], [5])
            self.assertTrue(adapter["owns"])
            self.assertTrue(adapter["must_not_own"])

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

    def test_openrazer_import_matches_exact_pinned_checkout_when_available(self) -> None:
        source_value = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        if source_value is None:
            self.skipTest("HFX_OPENRAZER_SOURCE_DIR is not configured")
        source = Path(source_value)
        self.assertEqual(transformed_metadata(ROOT, source), load_imported_metadata(ROOT))

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


if __name__ == "__main__":
    unittest.main()
