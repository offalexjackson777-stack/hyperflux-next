# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.journey_coverage import EXPECTED_ERAS, load_journey_source_coverage
from hfxdev.model import ModelError


class JourneySourceMapTests(unittest.TestCase):
    def test_public_source_map_uses_only_public_authorities_and_tests(self) -> None:
        coverage = load_journey_source_coverage(ROOT)
        self.assertFalse(coverage["publication"]["direct_quotes"])
        self.assertTrue(coverage["publication"]["public_sources_only"])
        self.assertEqual(coverage["prior_art_review"]["absolute_novelty_claims"], 0)
        serialized = json.dumps(coverage)
        for forbidden in ("/home/", "session_id", "source-thread", "private_overlay"):
            self.assertNotIn(forbidden, serialized)
        for era in coverage["eras"]:
            kinds = {source["kind"] for source in era["technical_sources"]}
            self.assertIn("authority", kinds)
            self.assertIn("test", kinds)
            for source in era["technical_sources"]:
                self.assertTrue((ROOT / source["reference"]).is_file())

    def test_public_source_map_rejects_private_paths_and_claim_drift(self) -> None:
        canonical = json.loads(
            (ROOT / "assurance" / "journey-source-coverage.json").read_text(
                encoding="utf-8"
            )
        )
        mutations = (
            lambda value: value["publication"].update(direct_quotes=True),
            lambda value: value["publication"].update(public_sources_only=False),
            lambda value: value["prior_art_review"].update(absolute_novelty_claims=1),
            lambda value: value["eras"][0].update(order=2),
            lambda value: value["prior_art_review"]["sources"].__setitem__(
                0, "/home/private-source.json"
            ),
            lambda value: value["eras"][0]["technical_sources"][0].update(
                kind="conversation"
            ),
        )
        for mutate in mutations:
            value = copy.deepcopy(canonical)
            mutate(value)

            def fake_load(path: Path) -> object:
                if path.name == "journey-source-coverage.json":
                    return value
                return json.loads(path.read_text(encoding="utf-8"))

            with self.subTest(mutation=mutate), patch(
                "hfxdev.journey_coverage.load_json", side_effect=fake_load
            ), self.assertRaises(ModelError):
                load_journey_source_coverage(ROOT)

    def test_public_journey_has_six_eras_and_a_neutral_source_map(self) -> None:
        coverage = load_journey_source_coverage(ROOT)
        text = (ROOT / coverage["public_document"]).read_text(encoding="utf-8")
        era_headings = [
            line for line in text.splitlines() if re.fullmatch(r"## Era [1-6]: .+", line)
        ]
        self.assertEqual(
            era_headings,
            [
                f"## Era {index}: {title}"
                for index, (_, title) in enumerate(EXPECTED_ERAS, start=1)
            ],
        )
        self.assertEqual(text.count("# Project Journey"), 1)
        self.assertGreaterEqual(len(re.findall(r"\bI\b", text)), 6)
        self.assertIn("## Technical Appendix", text)
        self.assertIn("### Technical source map", text)
        self.assertIn("### Product and package boundary", text)
        self.assertIn("### Accepted limitations", text)
        self.assertIn("### Prior-art and novelty boundary", text)
        forbidden = (
            r"/(?:home|tmp)/[^\s]+",
            r"\b019[0-9a-f]{5}-[0-9a-f-]{20,}\b",
            r"(?:first[- ]ever|world.?s first|only project|unprecedented)",
            r"(?:private filesystem path|internal authorization transcript)",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
