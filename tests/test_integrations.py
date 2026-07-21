# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.integrations import (
    _validate_adapters,
    _validate_upstreams,
    compiled_catalog,
    load_integration_catalog,
)
from hfxdev.model import ModelError
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
            self.assertIn(3, adapter["sdk_protocol_versions"])
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


if __name__ == "__main__":
    unittest.main()
