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
from hfxdev.profiles import load_profile_inputs


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
