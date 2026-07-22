# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.knowledge_review import validate_reviewed_facts
from hfxdev.model import ModelError, load_json
from hfxdev.openrazer import extract_openrazer_catalog
from hfxdev.openrgb import extract_openrgb_catalog
from hfxdev.profiles import load_profile_inputs


class UpstreamImporterTests(unittest.TestCase):
    def test_openrazer_ast_import_preserves_inheritance_without_execution(self) -> None:
        source = ROOT / "tests" / "fixtures" / "upstreams" / "openrazer"
        catalog = extract_openrazer_catalog(
            source,
            repository="https://github.com/openrazer/openrazer.git",
            commit="0" * 40,
            version="fixture",
            license_expression="GPL-2.0-or-later",
        )
        records = {record["record_id"]: record for record in catalog["records"]}
        mouse = records["openrazer:ExampleMouseWireless"]
        self.assertEqual(mouse["usb_identity"], {"vendor_id": 0x1532, "product_id": 0x00A8})
        self.assertEqual(mouse["source_route"], "vendor-wireless-receiver")
        self.assertEqual(mouse["facts"]["dpi_max"], 30000)
        self.assertEqual(mouse["lighting_topology"], {"matrix_dimensions": [1, 3]})
        self.assertEqual(
            mouse["settings_methods"],
            ["get_battery", "get_dpi_xy", "is_charging", "set_dpi_xy"],
        )

    def test_openrgb_initializer_import_resolves_pids_zones_and_layouts(self) -> None:
        source = ROOT / "tests" / "fixtures" / "upstreams" / "openrgb"
        catalog = extract_openrgb_catalog(
            source,
            repository="https://github.com/CalcProgrammer1/OpenRGB.git",
            commit="0" * 40,
            version="fixture",
            license_expression="GPL-2.0-only",
        )
        records = {record["record_id"]: record for record in catalog["records"]}
        mouse = records["openrgb:example_mouse_wireless_device"]
        self.assertEqual(mouse["usb_identity"], {"vendor_id": 0x1532, "product_id": 0x00A8})
        self.assertTrue(mouse["facts"]["detector_registered"])
        self.assertEqual(mouse["lighting_topology"]["application_slot_count"], 3)
        self.assertEqual(
            [zone["name"] for zone in mouse["lighting_topology"]["zones"]],
            ["Logo", "LED Strip"],
        )
        keyboard = records["openrgb:example_keyboard_wired_device"]
        self.assertEqual(keyboard["lighting_topology"]["layout_key"], "example_keyboard_layout")


class ReviewedDeviceKnowledgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.links = load_json(ROOT / "knowledge" / "candidate-links.json")
        cls.reviewed = load_json(ROOT / "knowledge" / "reviewed-facts.json")
        selected_snapshot = cls.links["candidate_snapshot_id"]
        cls.candidates = {
            candidate["id"]: candidate
            for candidate in load_profile_inputs(ROOT).candidates
            if candidate["snapshot_id"] == selected_snapshot
        }

    def test_reviewed_scope_is_complete_and_measured(self) -> None:
        sources, candidates, reviewed_on = validate_reviewed_facts(
            self.reviewed, self.candidates
        )
        self.assertEqual(reviewed_on, "2026-07-21")
        self.assertEqual(len(sources), 35)
        self.assertEqual(len(candidates), 12)
        self.assertEqual(sum(len(value["facts"]) for value in candidates.values()), 191)
        self.assertEqual(sum(len(value["gaps"]) for value in candidates.values()), 23)

    def test_reviewed_facts_cover_only_the_selected_snapshot(self) -> None:
        self.assertEqual(set(self.candidates), {link["candidate_id"] for link in self.links["links"]})
        self.assertEqual(len(self.links["links"]), 12)

    def test_unknown_source_reference_fails_closed(self) -> None:
        reviewed = deepcopy(self.reviewed)
        reviewed["candidates"][0]["facts"][0]["source_ids"] = ["unknown-source"]
        with self.assertRaisesRegex(ModelError, "source is not selected"):
            validate_reviewed_facts(reviewed, self.candidates)


if __name__ == "__main__":
    unittest.main()
