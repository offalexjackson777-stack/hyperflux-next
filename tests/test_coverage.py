# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.coverage import (
    CoverageMetric,
    load_rust_coverage_policy,
    parse_llvm_coverage_summary,
    verify_rust_coverage_report,
)
from hfxdev.model import ModelError


class RustCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_rust_coverage_policy(ROOT)

    def report(self) -> dict[str, object]:
        totals = {}
        for name, ratchet in self.policy.metrics.items():
            totals[name] = {
                "count": ratchet.baseline.count,
                "covered": ratchet.baseline.covered,
                "notcovered": ratchet.baseline.notcovered,
                "percent": 50.0,
            }
        return {
            "version": "3.1.0",
            "type": "llvm.coverage.json.export",
            "data": [{"totals": totals}],
        }

    def parse(self, value: object) -> dict[str, CoverageMetric]:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "coverage.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return parse_llvm_coverage_summary(path)

    def test_exact_baseline_passes_the_count_and_ratio_ratchet(self) -> None:
        measured = self.parse(self.report())
        verify_rust_coverage_report(self.policy, measured)
        self.assertEqual(measured["lines"].covered, 27728)
        self.assertEqual(measured["functions"].notcovered, 1131)

    def test_covered_uncovered_and_ratio_regressions_fail_closed(self) -> None:
        baseline = self.parse(self.report())
        covered = dict(baseline)
        covered["lines"] = replace(covered["lines"], covered=27727, notcovered=10342)
        with self.assertRaisesRegex(ModelError, "covered count regressed"):
            verify_rust_coverage_report(self.policy, covered)

        uncovered = dict(baseline)
        uncovered["functions"] = replace(
            uncovered["functions"], count=3855, notcovered=1132
        )
        with self.assertRaisesRegex(ModelError, "uncovered count regressed"):
            verify_rust_coverage_report(self.policy, uncovered)

        ratio = dict(baseline)
        ratio["regions"] = replace(
            ratio["regions"], count=50330, covered=36245, notcovered=14085
        )
        weakened = replace(
            self.policy,
            metrics={
                **self.policy.metrics,
                "regions": replace(
                    self.policy.metrics["regions"], maximum_notcovered=14085
                ),
            },
        )
        with self.assertRaisesRegex(ModelError, "ratio regressed"):
            verify_rust_coverage_report(weakened, ratio)

    def test_external_report_shape_and_totals_are_fail_closed(self) -> None:
        cases = (
            ({}, "format"),
            ({"version": "3.1.0", "type": "llvm.coverage.json.export", "data": []}, "one result"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ModelError, message):
                self.parse(value)

        inconsistent = self.report()
        inconsistent["data"][0]["totals"]["regions"]["notcovered"] += 1  # type: ignore[index]
        with self.assertRaisesRegex(ModelError, "inconsistent"):
            self.parse(inconsistent)

        invalid = self.report()
        invalid["data"][0]["totals"]["lines"]["covered"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ModelError, "integer"):
            self.parse(invalid)


if __name__ == "__main__":
    unittest.main()
