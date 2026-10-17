# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.coverage import (
    METRIC_NAMES,
    CoverageMetric,
    CoverageRatchet,
    load_rust_coverage_policy,
    parse_llvm_coverage_summary,
    rust_coverage_policy_from_json,
    tighten_rust_coverage_policy,
    tightened_rust_coverage_policy,
    verify_rust_coverage_report,
)
from hfxdev.model import ModelError


REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "ab" * 32

# The 2.0.0 limits. Tightening only ever lowers not-covered limits, so the
# committed policy may never be weaker than this.
RELEASE_2_0_MAXIMUM_NOTCOVERED = {"lines": 10208, "functions": 1111, "regions": 13836}

# The 2.0.0 baseline shares. They are a frozen floor that tighten never moves.
RELEASE_2_0_RATIO_FLOOR = {
    "lines": {"covered": 27391, "count": 37599},
    "functions": {"covered": 2691, "count": 3802},
    "regions": {"covered": 35767, "count": 49603},
}

# Totals measured on 1b2cf77 with the pinned command, used as a realistic case.
REAL_REPORT = {
    "lines": CoverageMetric(39979, 29909, 10070),
    "functions": CoverageMetric(4074, 2965, 1109),
    "regions": CoverageMetric(53108, 39417, 13691),
}

# Coverage before the 2.0.0 baseline; every later policy must beat its ratio.
SUPERSEDED = {
    "lines": CoverageMetric(38069, 27728, 10341),
    "functions": CoverageMetric(3854, 2723, 1131),
    "regions": CoverageMetric(50329, 36245, 14084),
}


class RustCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = json.loads(
            (ROOT / "assurance" / "rust-coverage.json").read_text(encoding="utf-8")
        )
        cls.policy = load_rust_coverage_policy(ROOT)

    def measured(self, **notcovered_below_limit: int) -> dict[str, CoverageMetric]:
        """A measurement that sits the given distance below each limit.

        A positive distance is newly tested code: the total stays and covered
        rises. A negative distance is newly added untested code: covered stays
        and the total rises, so only the not-covered limit is exceeded.
        """

        result = {}
        for name in METRIC_NAMES:
            ratchet = self.policy.metrics[name]
            distance = notcovered_below_limit.get(name, 0)
            notcovered = ratchet.maximum_notcovered - distance
            covered = ratchet.baseline.count - ratchet.maximum_notcovered + max(distance, 0)
            result[name] = CoverageMetric(
                count=covered + notcovered,
                covered=covered,
                notcovered=notcovered,
            )
        return result

    def tightened(self, measured: dict[str, CoverageMetric]) -> dict[str, object]:
        return tightened_rust_coverage_policy(
            self.value,
            self.policy,
            measured,
            source_revision=REVISION,
            report_sha256=DIGEST,
        )

    def report(self, measured: dict[str, CoverageMetric]) -> dict[str, object]:
        return {
            "version": "3.1.0",
            "type": "llvm.coverage.json.export",
            "data": [
                {
                    "totals": {
                        name: {
                            "count": metric.count,
                            "covered": metric.covered,
                            "notcovered": metric.notcovered,
                            "percent": 50.0,
                        }
                        for name, metric in measured.items()
                    }
                }
            ],
        }

    def parse(self, value: object) -> dict[str, CoverageMetric]:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "coverage.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return parse_llvm_coverage_summary(path)

    def test_committed_policy_never_weakens_the_release_floor(self) -> None:
        self.assertEqual(
            {
                name: (tolerance.tighten_margin, tolerance.stale_tolerance)
                for name, tolerance in self.policy.tolerance.items()
            },
            {"lines": (5, 10), "functions": (0, 1), "regions": (5, 10)},
        )
        for name, previous in SUPERSEDED.items():
            with self.subTest(metric=name):
                ratchet = self.policy.metrics[name]
                self.assertLessEqual(
                    ratchet.maximum_notcovered, RELEASE_2_0_MAXIMUM_NOTCOVERED[name]
                )
                self.assertLess(ratchet.maximum_notcovered, previous.notcovered)
                baseline = ratchet.baseline
                self.assertGreater(
                    baseline.covered * previous.count,
                    previous.covered * baseline.count,
                )

    def test_measurement_at_each_limit_passes_through_the_report_parser(self) -> None:
        measured = self.parse(self.report(self.measured()))
        verify_rust_coverage_report(self.policy, measured)

    def test_covered_and_uncovered_regressions_fail_closed(self) -> None:
        uncovered = self.measured(functions=-1)
        with self.assertRaisesRegex(ModelError, "functions uncovered count regressed"):
            verify_rust_coverage_report(self.policy, uncovered)

        covered = self.measured()
        lines = covered["lines"]
        minimum = self.policy.metrics["lines"].minimum_covered
        covered["lines"] = replace(
            lines, count=minimum - 1 + lines.notcovered, covered=minimum - 1
        )
        with self.assertRaisesRegex(ModelError, "lines covered count regressed"):
            verify_rust_coverage_report(self.policy, covered)

    def test_improvement_within_the_stale_tolerance_passes(self) -> None:
        verify_rust_coverage_report(
            self.policy, self.measured(lines=10, functions=1, regions=10)
        )

    def test_improvement_beyond_the_stale_tolerance_asks_for_tighten(self) -> None:
        for name, distance in (("lines", 11), ("functions", 2), ("regions", 11)):
            with self.subTest(metric=name):
                measured = self.measured(**{name: distance})
                limit = self.policy.metrics[name].maximum_notcovered
                with self.assertRaises(ModelError) as raised:
                    verify_rust_coverage_report(self.policy, measured)
                message = str(raised.exception)
                self.assertIn(
                    f"{name}: {limit - distance} not covered, limit {limit}", message
                )
                self.assertIn("Run `./hfx coverage tighten`", message)
                self.assertIn("commit assurance/rust-coverage.json", message)
                self.assertIn("--report PATH", message)
                verify_rust_coverage_report(self.policy, measured, require_tight=False)

    def test_regression_is_reported_before_staleness(self) -> None:
        measured = self.measured(lines=100, functions=-1)
        with self.assertRaisesRegex(ModelError, "functions uncovered count regressed"):
            verify_rust_coverage_report(self.policy, measured)

    def test_tighten_lowers_limits_to_the_measurement_plus_margin(self) -> None:
        measured = self.measured(lines=150, functions=4, regions=200)
        value = self.tightened(measured)
        policy = rust_coverage_policy_from_json(ROOT, value)
        self.assertEqual(policy.source_revision, REVISION)
        self.assertEqual(policy.report_sha256, DIGEST)
        for name, margin in (("lines", 5), ("functions", 0), ("regions", 5)):
            with self.subTest(metric=name):
                ratchet = policy.metrics[name]
                actual = measured[name]
                self.assertEqual(ratchet.maximum_notcovered, actual.notcovered + margin)
                self.assertEqual(ratchet.baseline.count, actual.count)
                self.assertEqual(ratchet.baseline.notcovered, ratchet.maximum_notcovered)
                self.assertEqual(
                    ratchet.baseline.covered, actual.count - ratchet.maximum_notcovered
                )
                self.assertEqual(
                    ratchet.minimum_covered, self.policy.metrics[name].minimum_covered
                )
                self.assertLess(
                    ratchet.maximum_notcovered,
                    self.policy.metrics[name].maximum_notcovered,
                )
        # The measured run passes its own limits, ratio and staleness check.
        verify_rust_coverage_report(policy, measured)
        # Everything outside the tolerance and metrics stays as committed.
        self.assertEqual(
            {key: item for key, item in value.items() if key not in {"baseline", "metrics"}},
            {
                key: item
                for key, item in self.value.items()
                if key not in {"baseline", "metrics"}
            },
        )
        self.assertEqual(value["baseline"]["report_format"], "llvm.coverage.json.export-3.1.0")

    def test_limit_written_from_either_machine_passes_on_the_other(self) -> None:
        # CI measures 3 more lines and regions not covered than a local run of
        # the same sources, and the same functions.
        local = self.measured(lines=150, functions=4, regions=200)
        ci = {
            name: (
                metric
                if name == "functions"
                else replace(
                    metric, covered=metric.covered - 3, notcovered=metric.notcovered + 3
                )
            )
            for name, metric in local.items()
        }
        from_local = rust_coverage_policy_from_json(ROOT, self.tightened(local))
        verify_rust_coverage_report(from_local, ci)
        from_ci = rust_coverage_policy_from_json(ROOT, self.tightened(ci))
        verify_rust_coverage_report(from_ci, local)

    def test_tighten_never_raises_a_limit_or_moves_the_covered_minimum(self) -> None:
        measured = self.measured(lines=2, functions=0, regions=4)
        value = self.tightened(measured)
        policy = rust_coverage_policy_from_json(ROOT, value)
        for name in METRIC_NAMES:
            with self.subTest(metric=name):
                before = self.policy.metrics[name]
                after = policy.metrics[name]
                self.assertEqual(after.maximum_notcovered, before.maximum_notcovered)
                self.assertEqual(after.minimum_covered, before.minimum_covered)
        verify_rust_coverage_report(policy, measured)

        # Tightening twice from the same report changes nothing further.
        lowered = self.measured(lines=150, functions=4, regions=200)
        once = self.tightened(lowered)
        twice = tightened_rust_coverage_policy(
            once,
            rust_coverage_policy_from_json(ROOT, once),
            lowered,
            source_revision=REVISION,
            report_sha256=DIGEST,
        )
        self.assertEqual(once, twice)

    def test_tighten_refuses_a_regressed_measurement(self) -> None:
        for name in METRIC_NAMES:
            with self.subTest(metric=name):
                with self.assertRaisesRegex(ModelError, f"{name} uncovered count regressed"):
                    self.tightened(self.measured(**{name: -1}))

    def test_tighten_rewrites_the_committed_file_only_when_a_limit_moves(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "assurance").mkdir()
            shutil.copyfile(
                ROOT / "assurance" / "rust-coverage.json",
                root / "assurance" / "rust-coverage.json",
            )
            shutil.copytree(ROOT / "toolchains", root / "toolchains")
            policy_path = root / "assurance" / "rust-coverage.json"
            report = root / "coverage.json"

            report.write_text(json.dumps(self.report(self.measured())), encoding="utf-8")
            unchanged = tighten_rust_coverage_policy(root, report, source_revision=REVISION)
            self.assertFalse(unchanged.written)
            self.assertEqual(
                policy_path.read_bytes(), (ROOT / "assurance" / "rust-coverage.json").read_bytes()
            )

            lowered = self.measured(lines=150, functions=4, regions=200)
            report.write_text(json.dumps(self.report(lowered)), encoding="utf-8")
            result = tighten_rust_coverage_policy(root, report, source_revision=REVISION)
            self.assertTrue(result.written)
            text = policy_path.read_text(encoding="utf-8")
            self.assertEqual(text, json.dumps(json.loads(text), indent=2) + "\n")
            written = load_rust_coverage_policy(root)
            verify_rust_coverage_report(written, lowered)
            self.assertEqual(
                [row[0] for row in result.rows], list(METRIC_NAMES)
            )
            self.assertIn("| lines |", result.table())

    def test_tolerance_policy_is_bounded(self) -> None:
        cases = (
            ({"tighten_margin": 6, "stale_tolerance": 5}, "below its tighten margin"),
            ({"tighten_margin": 5, "stale_tolerance": 101}, "exceeds 100"),
            ({"tighten_margin": 5}, "missing or unknown"),
            ({"tighten_margin": -1, "stale_tolerance": 5}, "integer"),
        )
        for tolerance, message in cases:
            with self.subTest(message=message):
                value = copy.deepcopy(self.value)
                value["tolerance"]["lines"] = tolerance
                with self.assertRaisesRegex(ModelError, message):
                    rust_coverage_policy_from_json(ROOT, value)
        missing = copy.deepcopy(self.value)
        del missing["tolerance"]
        with self.assertRaisesRegex(ModelError, "top-level"):
            rust_coverage_policy_from_json(ROOT, missing)

    def test_raising_a_limit_without_its_baseline_is_rejected(self) -> None:
        # The loader checks the file against itself only; loosening is a
        # reviewed manual edit, never something tighten produces.
        value = copy.deepcopy(self.value)
        value["metrics"]["functions"]["maximum_notcovered"] += 1
        with self.assertRaisesRegex(ModelError, "weakens its baseline"):
            rust_coverage_policy_from_json(ROOT, value)

    def test_covered_minimum_may_not_exceed_its_baseline(self) -> None:
        value = copy.deepcopy(self.value)
        lines = value["metrics"]["lines"]
        lines["minimum_covered"] = lines["baseline_covered"] + 1
        with self.assertRaisesRegex(ModelError, "minimum covered exceeds its baseline"):
            rust_coverage_policy_from_json(ROOT, value)

    def limits_at(self, measured: dict[str, CoverageMetric]):
        """The committed policy with every count limit placed at a measurement.

        Only the frozen ratio floor can then fail the measurement.
        """

        return replace(
            self.policy,
            metrics={
                name: CoverageRatchet(
                    baseline=metric,
                    minimum_covered=min(
                        metric.covered, self.policy.metrics[name].minimum_covered
                    ),
                    maximum_notcovered=metric.notcovered,
                )
                for name, metric in measured.items()
            },
        )

    def test_ratio_floor_is_the_frozen_2_0_record(self) -> None:
        self.assertEqual(self.value["ratio_floor"], RELEASE_2_0_RATIO_FLOOR)
        for name, floor in RELEASE_2_0_RATIO_FLOOR.items():
            with self.subTest(metric=name):
                self.assertEqual(self.policy.ratio_floor[name].covered, floor["covered"])
                self.assertEqual(self.policy.ratio_floor[name].count, floor["count"])

    def test_tighten_leaves_the_ratio_floor_alone(self) -> None:
        value = self.tightened(self.measured(lines=150, functions=4, regions=200))
        self.assertEqual(value["ratio_floor"], self.value["ratio_floor"])
        self.assertEqual(
            rust_coverage_policy_from_json(ROOT, value).ratio_floor,
            self.policy.ratio_floor,
        )

    def test_deleting_tested_code_from_the_real_report_passes_the_ratio_floor(self) -> None:
        policy = self.limits_at(REAL_REPORT)
        verify_rust_coverage_report(policy, REAL_REPORT)
        cleaned = dict(REAL_REPORT)
        lines = cleaned["lines"]
        cleaned["lines"] = replace(
            lines, count=lines.count - 1000, covered=lines.covered - 1000
        )
        verify_rust_coverage_report(policy, cleaned)

    def test_share_below_the_ratio_floor_still_fails(self) -> None:
        for name, floor in RELEASE_2_0_RATIO_FLOOR.items():
            with self.subTest(metric=name):
                at_floor = dict(REAL_REPORT)
                at_floor[name] = CoverageMetric(
                    floor["count"], floor["covered"], floor["count"] - floor["covered"]
                )
                below = dict(REAL_REPORT)
                below[name] = CoverageMetric(
                    floor["count"],
                    floor["covered"] - 1,
                    floor["count"] - floor["covered"] + 1,
                )
                verify_rust_coverage_report(self.limits_at(at_floor), at_floor)
                with self.assertRaisesRegex(ModelError, f"{name} ratio regressed below its floor"):
                    verify_rust_coverage_report(self.limits_at(below), below)

    def test_ratio_floor_record_is_fail_closed(self) -> None:
        cases = (
            ({"covered": 2, "count": 1}, "exceeds its count"),
            ({"covered": 1}, "missing or unknown"),
            ({"covered": 39000, "count": 39979}, "baseline is below its ratio floor"),
        )
        for floor, message in cases:
            with self.subTest(message=message):
                value = copy.deepcopy(self.value)
                value["ratio_floor"]["lines"] = floor
                with self.assertRaisesRegex(ModelError, message):
                    rust_coverage_policy_from_json(ROOT, value)

    def test_external_report_shape_and_totals_are_fail_closed(self) -> None:
        cases = (
            ({}, "format"),
            ({"version": "3.1.0", "type": "llvm.coverage.json.export", "data": []}, "one result"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ModelError, message):
                self.parse(value)

        inconsistent = self.report(self.measured())
        inconsistent["data"][0]["totals"]["regions"]["notcovered"] += 1  # type: ignore[index]
        with self.assertRaisesRegex(ModelError, "inconsistent"):
            self.parse(inconsistent)

        invalid = self.report(self.measured())
        invalid["data"][0]["totals"]["lines"]["covered"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ModelError, "integer"):
            self.parse(invalid)


if __name__ == "__main__":
    unittest.main()
