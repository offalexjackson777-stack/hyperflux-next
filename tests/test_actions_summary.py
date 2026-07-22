# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.actions_summary import render_actions_summary, write_actions_summary
from hfxdev.model import ModelError


REVISION = "a" * 40


def _result() -> dict:
    return {
        "$schema": "https://hyperflux.dev/schemas/verification-run-v1.json",
        "schema": "hyperflux-verification-run-v1",
        "run_id": "hfxv-" + "b" * 20,
        "lane": "fast",
        "selection": {
            "mode": "changed-paths",
            "base_revision": "c" * 40,
            "changed_paths": ["README.md"],
            "unmatched_paths": [],
        },
        "source": {
            "revision": REVISION,
            "commit_epoch": 1,
            "worktree": "clean",
            "worktree_sha256": "d" * 64,
        },
        "started_at": "2026-07-22T00:00:00Z",
        "finished_at": "2026-07-22T00:00:02Z",
        "status": "failed",
        "duration_ms": 2000,
        "nodes": [
            {
                "id": "generated-freshness",
                "title": "Generated repository artifacts are current",
                "domain": "generation",
                "runner": "generated-freshness",
                "dependencies": [],
                "input_sha256": "e" * 64,
                "status": "passed",
                "started_at": "2026-07-22T00:00:00Z",
                "finished_at": "2026-07-22T00:00:01Z",
                "duration_ms": 900,
                "produced_evidence": ["generation-result"],
                "error": None,
            },
            {
                "id": "documentation-portal-contracts",
                "title": "Portal contracts",
                "domain": "documentation",
                "runner": "documentation-portal-contracts",
                "dependencies": ["generated-freshness"],
                "input_sha256": "f" * 64,
                "status": "failed",
                "started_at": "2026-07-22T00:00:01Z",
                "finished_at": "2026-07-22T00:00:02Z",
                "duration_ms": 1100,
                "produced_evidence": ["documentation-portal-manifest"],
                "error": "bounded failure",
            },
        ],
    }


class ActionsSummaryTests(unittest.TestCase):
    def test_summary_reports_required_evidence_without_changing_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "result.json"
            result.write_text(json.dumps(_result()), encoding="utf-8")
            summary = render_actions_summary(ROOT, result, expected_revision=REVISION)
        self.assertIn("Source revision", summary)
        self.assertIn("changed-paths", summary)
        self.assertIn("Generated freshness | passed", summary)
        self.assertIn("documentation-portal-contracts", summary)
        self.assertIn("## Failures", summary)
        self.assertIn("## Release-gate impact", summary)
        self.assertIn("cannot mutate canonical release-gate state", summary)
        self.assertNotIn("bounded failure", summary)

    def test_missing_result_still_produces_a_truthful_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "summary.md"
            write_actions_summary(
                ROOT,
                Path(temporary) / "missing.json",
                output,
                expected_revision=REVISION,
            )
            summary = output.read_text(encoding="utf-8")
        self.assertIn("Result unavailable", summary)
        self.assertIn(REVISION, summary)
        self.assertIn("No structured node timings", summary)

    def test_revision_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "result.json"
            result.write_text(json.dumps(_result()), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "does not match"):
                render_actions_summary(ROOT, result, expected_revision="1" * 40)


if __name__ == "__main__":
    unittest.main()
