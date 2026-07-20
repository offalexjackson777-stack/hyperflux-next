# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.migration import summary
from hfxdev.model import load_foundation
from hfxdev.render import rendered_files
from hfxdev.model import load_json


class FoundationTests(unittest.TestCase):
    def test_direction_reaches_receiver_without_cycles(self) -> None:
        constitution, _, _ = load_foundation(ROOT)
        direction = constitution["direction"]
        self.assertEqual(direction[0], "applications")
        self.assertEqual(direction[-1], "receiver")
        self.assertEqual(len(direction), len(set(direction)))

    def test_publication_is_explicitly_locked(self) -> None:
        constitution, _, _ = load_foundation(ROOT)
        interlock = constitution["publication_interlock"]
        self.assertFalse(interlock["remote_repository_created"])
        self.assertFalse(interlock["publication_authorized"])
        self.assertIn("HFX-GATE-PUBLICATION-DECISION", interlock["required_gate_ids"])

    def test_every_component_has_positive_and_negative_ownership(self) -> None:
        constitution, _, _ = load_foundation(ROOT)
        for component in constitution["components"]:
            with self.subTest(component=component["id"]):
                self.assertTrue(component["owns"])
                self.assertTrue(component["must_not_own"])

    def test_ledger_defaults_to_exclusion(self) -> None:
        _, _, ledger = load_foundation(ROOT)
        self.assertEqual(ledger["default_disposition"], "REJECT_UNTIL_REVIEWED")

    def test_generated_views_are_deterministic(self) -> None:
        first = rendered_files(ROOT)
        second = rendered_files(ROOT)
        self.assertEqual(first, second)

    def test_summary_reports_progress_without_mutation(self) -> None:
        text = summary(ROOT)
        self.assertIn("Default: REJECT_UNTIL_REVIEWED", text)
        self.assertIn("Subsystem decisions:", text)

    def test_domain_catalog_names_are_unique_across_kinds(self) -> None:
        catalog = load_json(ROOT / "schemas" / "domain-catalog.json")
        names = [item["name"] for key in ("numeric_types", "string_types", "enums") for item in catalog[key]]
        self.assertEqual(len(names), len(set(names)))

    def test_domain_catalog_ranges_are_ordered(self) -> None:
        catalog = load_json(ROOT / "schemas" / "domain-catalog.json")
        for item in catalog["numeric_types"]:
            with self.subTest(type=item["name"]):
                self.assertLessEqual(item["minimum"], item["maximum"])


if __name__ == "__main__":
    unittest.main()
