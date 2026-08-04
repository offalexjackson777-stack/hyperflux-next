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
        self.assertEqual(
            self.policy.source_revision,
            "446c2c064444e98b0f89b5ef2ee7db5e8ce546a0",
        )
        self.assertEqual(
            self.policy.report_sha256,
            "4054b352a6cc8d856de2fcb3d17da64b4237eab38ebec5808164fc342bea2c48",
        )
        measured = self.parse(self.report())
        verify_rust_coverage_report(self.policy, measured)
        self.assertEqual(measured["lines"], CoverageMetric(37599, 27391, 10208))
        self.assertEqual(measured["functions"], CoverageMetric(3802, 2691, 1111))
        self.assertEqual(measured["regions"], CoverageMetric(49603, 35767, 13836))

        superseded = {
            "lines": CoverageMetric(38069, 27728, 10341),
            "functions": CoverageMetric(3854, 2723, 1131),
            "regions": CoverageMetric(50329, 36245, 14084),
        }
        for name, previous in superseded.items():
            with self.subTest(metric=name):
                current = measured[name]
                self.assertLess(current.notcovered, previous.notcovered)
                self.assertGreater(
                    current.covered * previous.count,
                    previous.covered * current.count,
                )

    def test_covered_uncovered_and_ratio_regressions_fail_closed(self) -> None:
        baseline = self.parse(self.report())
        covered = dict(baseline)
        covered["lines"] = replace(covered["lines"], covered=27390, notcovered=10209)
        with self.assertRaisesRegex(ModelError, "covered count regressed"):
            verify_rust_coverage_report(self.policy, covered)

        uncovered = dict(baseline)
        uncovered["functions"] = replace(
            uncovered["functions"], count=3803, notcovered=1112
        )
        with self.assertRaisesRegex(ModelError, "uncovered count regressed"):
            verify_rust_coverage_report(self.policy, uncovered)

        ratio = dict(baseline)
        ratio["regions"] = replace(
            ratio["regions"], count=49604, covered=35767, notcovered=13837
        )
        weakened = replace(
            self.policy,
            metrics={
                **self.policy.metrics,
                "regions": replace(
                    self.policy.metrics["regions"], maximum_notcovered=13837
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
