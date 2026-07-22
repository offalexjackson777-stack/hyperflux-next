# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import hashlib
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.migration import summary
from hfxdev.assurance import load_design_coverage
from hfxdev.model import load_foundation
from hfxdev.render import rendered_binary_files, rendered_files
from hfxdev.model import load_json
from hfxdev.profiles import load_profile_inputs


class FoundationTests(unittest.TestCase):
    def test_design_book_has_one_truthful_coverage_entry_per_section(self) -> None:
        entries = load_design_coverage(ROOT)
        self.assertEqual([entry.section for entry in entries], list(range(1, 68)))
        self.assertEqual(entries[-1].status, "publication-locked")
        self.assertTrue(entries[-1].release_blocking)

    def test_incomplete_design_sections_name_remaining_work(self) -> None:
        entries = load_design_coverage(ROOT)
        for entry in entries:
            with self.subTest(section=entry.section):
                if entry.status in {
                    "partially-implemented",
                    "blocked-by-physical-evidence",
                    "publication-locked",
                }:
                    self.assertTrue(entry.remaining)

    def test_direction_reaches_receiver_without_cycles(self) -> None:
        constitution, _, _ = load_foundation(ROOT)
        direction = constitution["direction"]
        self.assertEqual(direction[0], "applications")
        self.assertEqual(direction[-1], "receiver")
        self.assertEqual(len(direction), len(set(direction)))

    def test_publication_is_explicitly_locked(self) -> None:
        constitution, _, _ = load_foundation(ROOT)
        interlock = constitution["publication_interlock"]
        self.assertTrue(interlock["remote_repository_created"])
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
        first_binary = rendered_binary_files(ROOT)
        second_binary = rendered_binary_files(ROOT)
        self.assertEqual(first_binary, second_binary)
        preview = first_binary[ROOT / "docs" / "assets" / "social-preview.png"]
        self.assertEqual(preview[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(preview[16:20], "big"), 1280)
        self.assertEqual(int.from_bytes(preview[20:24], "big"), 640)

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
                self.assertLessEqual(int(item["minimum"]), int(item["maximum"]))
                expected_type = str if item["json_encoding"] == "decimal-string" else int
                self.assertIsInstance(item["minimum"], expected_type)
                self.assertIsInstance(item["maximum"], expected_type)

    def test_profile_sources_are_bound_to_frozen_evidence_inventories(self) -> None:
        profiles = load_profile_inputs(ROOT)
        self.assertEqual(len(profiles.profiles), 4)
        self.assertEqual(len(profiles.candidates), 23)
        self.assertEqual(len(profiles.source_sha256), 64)

    def test_vendored_cpp_dependencies_match_their_manifest(self) -> None:
        manifest = load_json(ROOT / "sdk" / "cpp" / "vendor" / "manifest.json")
        self.assertEqual(manifest["schema"], "hyperflux-vendored-cpp-dependencies-v1")
        self.assertTrue(manifest["dependencies"])
        for dependency in manifest["dependencies"]:
            with self.subTest(dependency=dependency["id"]):
                path = ROOT / dependency["path"]
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(digest, dependency["sha256"])
                self.assertTrue(dependency["license_expression"])


if __name__ == "__main__":
    unittest.main()
