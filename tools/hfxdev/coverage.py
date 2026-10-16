# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from .development import load_development_environment
from .model import ModelError, load_json


MAX_REPORT_BYTES = 1_048_576
METRIC_NAMES = ("lines", "functions", "regions")
POLICY_SCHEMA = "hyperflux-rust-coverage-v2"
POLICY_PATH = PurePosixPath("assurance/rust-coverage.json")
MAX_STALE_TOLERANCE = 100
TIGHTEN_COMMAND = "./hfx coverage tighten"
EXPECTED_COMMAND = (
    "cargo",
    "llvm-cov",
    "--workspace",
    "--all-targets",
    "--locked",
    "--offline",
    "--json",
    "--summary-only",
    "--output-path",
    "build/rust-coverage-current.json",
    "--",
    "--quiet",
)


@dataclass(frozen=True)
class CoverageMetric:
    count: int
    covered: int
    notcovered: int


@dataclass(frozen=True)
class CoverageRatchet:
    baseline: CoverageMetric
    minimum_covered: int
    maximum_notcovered: int


@dataclass(frozen=True)
class CoverageTolerance:
    """How the not-covered limit of one metric follows measured coverage.

    ``tighten_margin`` is added to the measured not-covered count when the
    limit is rewritten, so run-to-run noise cannot fail a fresh limit.
    ``stale_tolerance`` is how far below the limit a measurement may fall
    before the committed limit counts as stale and must be tightened.
    """

    tighten_margin: int
    stale_tolerance: int


@dataclass(frozen=True)
class CoverageRatioFloor:
    """A frozen covered share, stored as an exact fraction.

    The floor is the 2.0 baseline share. It is never rewritten by tighten, so
    deleting code that has tests stays possible while the share stays above it.
    """

    covered: int
    count: int


@dataclass(frozen=True)
class RustCoveragePolicy:
    command: tuple[str, ...]
    output_path: str
    source_revision: str
    report_sha256: str
    tolerance: dict[str, CoverageTolerance]
    metrics: dict[str, CoverageRatchet]
    ratio_floor: dict[str, CoverageRatioFloor]


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"Rust coverage {label} must be an integer at least {minimum}")
    return value


def _metric(value: Any, label: str) -> CoverageRatchet:
    expected = {
        "baseline_count",
        "baseline_covered",
        "baseline_notcovered",
        "minimum_covered",
        "maximum_notcovered",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ModelError(f"Rust coverage {label} metric has missing or unknown fields")
    count = _integer(value["baseline_count"], f"{label} baseline count", positive=True)
    covered = _integer(
        value["baseline_covered"], f"{label} baseline covered", positive=True
    )
    notcovered = _integer(
        value["baseline_notcovered"], f"{label} baseline not-covered"
    )
    minimum_covered = _integer(
        value["minimum_covered"], f"{label} minimum covered", positive=True
    )
    maximum_notcovered = _integer(
        value["maximum_notcovered"], f"{label} maximum not-covered"
    )
    if count != covered + notcovered:
        raise ModelError(f"Rust coverage {label} baseline totals are inconsistent")
    if maximum_notcovered > notcovered:
        raise ModelError(f"Rust coverage {label} ratchet weakens its baseline")
    # The covered minimum is a floor, not a ratchet: tighten never raises it,
    # so code with tests can be deleted. It may not exceed the measurement.
    if minimum_covered > covered:
        raise ModelError(f"Rust coverage {label} minimum covered exceeds its baseline")
    return CoverageRatchet(
        baseline=CoverageMetric(count=count, covered=covered, notcovered=notcovered),
        minimum_covered=minimum_covered,
        maximum_notcovered=maximum_notcovered,
    )


def _tolerance(value: Any, label: str) -> CoverageTolerance:
    if not isinstance(value, dict) or set(value) != {"tighten_margin", "stale_tolerance"}:
        raise ModelError(f"Rust coverage {label} tolerance has missing or unknown fields")
    margin = _integer(value["tighten_margin"], f"{label} tighten margin")
    tolerance = _integer(value["stale_tolerance"], f"{label} stale tolerance")
    if tolerance < margin:
        raise ModelError(
            f"Rust coverage {label} stale tolerance is below its tighten margin"
        )
    if tolerance > MAX_STALE_TOLERANCE:
        raise ModelError(
            f"Rust coverage {label} stale tolerance exceeds {MAX_STALE_TOLERANCE}"
        )
    return CoverageTolerance(tighten_margin=margin, stale_tolerance=tolerance)


def _ratio_floor(value: Any, label: str) -> CoverageRatioFloor:
    if not isinstance(value, dict) or set(value) != {"covered", "count"}:
        raise ModelError(f"Rust coverage {label} ratio floor has missing or unknown fields")
    covered = _integer(value["covered"], f"{label} ratio floor covered", positive=True)
    count = _integer(value["count"], f"{label} ratio floor count", positive=True)
    if covered > count:
        raise ModelError(f"Rust coverage {label} ratio floor covered exceeds its count")
    return CoverageRatioFloor(covered=covered, count=count)


def _below_floor(covered: int, count: int, floor: CoverageRatioFloor) -> bool:
    return covered * floor.count < floor.covered * count


def load_rust_coverage_policy(root: Path) -> RustCoveragePolicy:
    return rust_coverage_policy_from_json(
        root, load_json(root / Path(POLICY_PATH.as_posix()))
    )


def rust_coverage_policy_from_json(root: Path, value: dict[str, Any]) -> RustCoveragePolicy:
    if set(value) != {
        "$schema",
        "schema",
        "tool",
        "command",
        "baseline",
        "tolerance",
        "metrics",
        "ratio_floor",
    }:
        raise ModelError("Rust coverage policy has missing or unknown top-level fields")
    if value["schema"] != POLICY_SCHEMA:
        raise ModelError("unsupported Rust coverage policy schema")
    tool = value["tool"]
    if not isinstance(tool, dict) or set(tool) != {
        "name",
        "version",
        "rust_toolchain",
        "network_access",
    }:
        raise ModelError("Rust coverage tool binding is malformed")
    if tool != {
        "name": "cargo-llvm-cov",
        "version": "0.8.7",
        "rust_toolchain": "1.95.0-x86_64-unknown-linux-gnu",
        "network_access": False,
    }:
        raise ModelError("Rust coverage tool binding drifted")
    environment = load_development_environment(root)
    package_versions = {package.name: package.version for package in environment.packages}
    if package_versions.get("cargo-llvm-cov") != "0.8.7-1":
        raise ModelError("Rust coverage tool differs from the pinned environment")
    if environment.rust_toolchain != tool["rust_toolchain"]:
        raise ModelError("Rust coverage toolchain differs from the pinned environment")

    command = value["command"]
    if not isinstance(command, list) or tuple(command) != EXPECTED_COMMAND:
        raise ModelError("Rust coverage command is not the bounded offline command")
    output = PurePosixPath(command[9])
    if output.is_absolute() or ".." in output.parts or output.parts[0] != "build":
        raise ModelError("Rust coverage output path escapes build/")

    baseline = value["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {
        "source_revision",
        "report_sha256",
        "report_format",
    }:
        raise ModelError("Rust coverage baseline binding is malformed")
    source_revision = baseline["source_revision"]
    report_sha256 = baseline["report_sha256"]
    if not isinstance(source_revision, str) or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ModelError("Rust coverage baseline revision is invalid")
    if not isinstance(report_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", report_sha256) is None:
        raise ModelError("Rust coverage baseline report digest is invalid")
    if baseline["report_format"] != "llvm.coverage.json.export-3.1.0":
        raise ModelError("Rust coverage baseline report format drifted")

    raw_tolerance = value["tolerance"]
    if not isinstance(raw_tolerance, dict) or tuple(raw_tolerance) != METRIC_NAMES:
        raise ModelError("Rust coverage tolerance must be ordered lines, functions, regions")
    tolerance = {name: _tolerance(raw_tolerance[name], name) for name in METRIC_NAMES}

    raw_metrics = value["metrics"]
    if not isinstance(raw_metrics, dict) or tuple(raw_metrics) != METRIC_NAMES:
        raise ModelError("Rust coverage metrics must be ordered lines, functions, regions")
    metrics = {name: _metric(raw_metrics[name], name) for name in METRIC_NAMES}

    raw_floor = value["ratio_floor"]
    if not isinstance(raw_floor, dict) or tuple(raw_floor) != METRIC_NAMES:
        raise ModelError("Rust coverage ratio floor must be ordered lines, functions, regions")
    ratio_floor = {name: _ratio_floor(raw_floor[name], name) for name in METRIC_NAMES}
    for name in METRIC_NAMES:
        baseline = metrics[name].baseline
        if _below_floor(baseline.covered, baseline.count, ratio_floor[name]):
            raise ModelError(f"Rust coverage {name} baseline is below its ratio floor")
    return RustCoveragePolicy(
        command=tuple(command),
        output_path=output.as_posix(),
        source_revision=source_revision,
        report_sha256=report_sha256,
        tolerance=tolerance,
        metrics=metrics,
        ratio_floor=ratio_floor,
    )


def parse_llvm_coverage_summary(path: Path) -> dict[str, CoverageMetric]:
    if path.is_symlink() or not path.is_file():
        raise ModelError("Rust coverage report is not a regular file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_REPORT_BYTES:
        raise ModelError("Rust coverage report size is outside its bound")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelError("Rust coverage report is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ModelError("Rust coverage report must be an object")
    if value.get("version") != "3.1.0" or value.get("type") != "llvm.coverage.json.export":
        raise ModelError("Rust coverage report format is unsupported")
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ModelError("Rust coverage report must contain one result set")
    totals = data[0].get("totals")
    if not isinstance(totals, dict):
        raise ModelError("Rust coverage report totals are missing")
    metrics: dict[str, CoverageMetric] = {}
    for name in METRIC_NAMES:
        raw = totals.get(name)
        if not isinstance(raw, dict):
            raise ModelError(f"Rust coverage report {name} totals are missing")
        count = _integer(raw.get("count"), f"report {name} count", positive=True)
        covered = _integer(raw.get("covered"), f"report {name} covered")
        if covered > count:
            raise ModelError(f"Rust coverage report {name} covered count exceeds total")
        notcovered = count - covered
        if "notcovered" in raw and _integer(
            raw["notcovered"], f"report {name} not-covered"
        ) != notcovered:
            raise ModelError(f"Rust coverage report {name} not-covered total is inconsistent")
        metrics[name] = CoverageMetric(
            count=count,
            covered=covered,
            notcovered=notcovered,
        )
    return metrics


def _verify_no_regression(
    policy: RustCoveragePolicy,
    measured: dict[str, CoverageMetric],
) -> None:
    if tuple(measured) != METRIC_NAMES:
        raise ModelError("Rust coverage measured metrics are incomplete or reordered")
    for name in METRIC_NAMES:
        actual = measured[name]
        ratchet = policy.metrics[name]
        if actual.covered < ratchet.minimum_covered:
            raise ModelError(
                f"Rust coverage {name} covered count regressed: "
                f"{actual.covered} < {ratchet.minimum_covered}"
            )
        if actual.notcovered > ratchet.maximum_notcovered:
            raise ModelError(
                f"Rust coverage {name} uncovered count regressed: "
                f"{actual.notcovered} > {ratchet.maximum_notcovered}"
            )
        if _below_floor(actual.covered, actual.count, policy.ratio_floor[name]):
            raise ModelError(f"Rust coverage {name} ratio regressed below its floor")


def verify_rust_coverage_report(
    policy: RustCoveragePolicy,
    measured: dict[str, CoverageMetric],
) -> None:
    _verify_no_regression(policy, measured)


def tightened_rust_coverage_policy(
    value: dict[str, Any],
    policy: RustCoveragePolicy,
    measured: dict[str, CoverageMetric],
    *,
    source_revision: str,
    report_sha256: str,
) -> dict[str, Any]:
    """Return policy JSON whose not-covered limits follow the measurement.

    Each not-covered limit becomes the measured count plus its tighten margin,
    unless the committed limit is already lower, so a limit never rises. The
    baseline is rewritten to describe the measurement, conservatively
    (``baseline_notcovered`` equals the new limit). ``minimum_covered`` and
    ``ratio_floor`` are left as committed. A measurement that regresses any
    limit is refused.
    """

    _verify_no_regression(policy, measured)
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ModelError("Rust coverage source revision is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", report_sha256) is None:
        raise ModelError("Rust coverage report digest is invalid")
    metrics: dict[str, Any] = {}
    for name in METRIC_NAMES:
        actual = measured[name]
        ratchet = policy.metrics[name]
        maximum = min(
            ratchet.maximum_notcovered,
            actual.notcovered + policy.tolerance[name].tighten_margin,
        )
        baseline_covered = actual.count - maximum
        if baseline_covered < max(ratchet.minimum_covered, 1):
            raise ModelError(
                f"Rust coverage {name} measurement leaves no baseline above "
                "the minimum covered count"
            )
        metrics[name] = {
            "baseline_count": actual.count,
            "baseline_covered": baseline_covered,
            "baseline_notcovered": maximum,
            "minimum_covered": ratchet.minimum_covered,
            "maximum_notcovered": maximum,
        }
    result = dict(value)
    result["baseline"] = {
        **value["baseline"],
        "source_revision": source_revision,
        "report_sha256": report_sha256,
    }
    result["metrics"] = metrics
    return result


@dataclass(frozen=True)
class CoverageTightening:
    written: bool
    rows: tuple[tuple[str, int, int, int], ...]

    def table(self) -> str:
        lines = [
            "| Metric | Measured not covered | Limit before | Limit after |",
            "| --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {name} | {measured} | {before} | {after} |"
            for name, measured, before, after in self.rows
        )
        return "\n".join(lines)


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError("cannot resolve the measured source revision") from error
    return result.stdout.strip()


def tighten_rust_coverage_policy(
    root: Path,
    report: Path | None = None,
    *,
    source_revision: str | None = None,
) -> CoverageTightening:
    """Rewrite the committed policy from a llvm-cov JSON summary."""

    path = root / Path(POLICY_PATH.as_posix())
    value = load_json(path)
    policy = rust_coverage_policy_from_json(root, value)
    report = root / policy.output_path if report is None else report
    measured = parse_llvm_coverage_summary(report)
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    revision = _git_head(root) if source_revision is None else source_revision
    updated = tightened_rust_coverage_policy(
        value,
        policy,
        measured,
        source_revision=revision,
        report_sha256=digest,
    )
    rust_coverage_policy_from_json(root, updated)
    rows = tuple(
        (
            name,
            measured[name].notcovered,
            policy.metrics[name].maximum_notcovered,
            updated["metrics"][name]["maximum_notcovered"],
        )
        for name in METRIC_NAMES
    )
    changed = any(
        updated["metrics"][name]["maximum_notcovered"]
        != value["metrics"][name]["maximum_notcovered"]
        for name in METRIC_NAMES
    )
    if changed:
        path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    return CoverageTightening(written=changed, rows=rows)
