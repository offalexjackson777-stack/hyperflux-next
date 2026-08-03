# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .development import load_development_environment
from .model import ModelError, load_json


MAX_REPORT_BYTES = 1_048_576
METRIC_NAMES = ("lines", "functions", "regions")
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
class RustCoveragePolicy:
    command: tuple[str, ...]
    output_path: str
    source_revision: str
    report_sha256: str
    metrics: dict[str, CoverageRatchet]


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
    if minimum_covered < covered or maximum_notcovered > notcovered:
        raise ModelError(f"Rust coverage {label} ratchet weakens its baseline")
    return CoverageRatchet(
        baseline=CoverageMetric(count=count, covered=covered, notcovered=notcovered),
        minimum_covered=minimum_covered,
        maximum_notcovered=maximum_notcovered,
    )


def load_rust_coverage_policy(root: Path) -> RustCoveragePolicy:
    value = load_json(root / "assurance" / "rust-coverage.json")
    if set(value) != {"$schema", "schema", "tool", "command", "baseline", "metrics"}:
        raise ModelError("Rust coverage policy has missing or unknown top-level fields")
    if value["schema"] != "hyperflux-rust-coverage-v1":
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

    raw_metrics = value["metrics"]
    if not isinstance(raw_metrics, dict) or tuple(raw_metrics) != METRIC_NAMES:
        raise ModelError("Rust coverage metrics must be ordered lines, functions, regions")
    metrics = {name: _metric(raw_metrics[name], name) for name in METRIC_NAMES}
    return RustCoveragePolicy(
        command=tuple(command),
        output_path=output.as_posix(),
        source_revision=source_revision,
        report_sha256=report_sha256,
        metrics=metrics,
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


def verify_rust_coverage_report(
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
        baseline = ratchet.baseline
        if actual.covered * baseline.count < baseline.covered * actual.count:
            raise ModelError(f"Rust coverage {name} ratio regressed below the baseline")
