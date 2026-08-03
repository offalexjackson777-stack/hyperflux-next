# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


METRIC_KEYS = {
    "id",
    "title",
    "measurement_kind",
    "source",
    "selector",
    "unit",
    "maximum",
    "status",
    "sample_count",
    "warmup_count",
    "release_blocking",
    "rationale",
}
KINDS = {"json-pointer", "artifact-size", "staged-payload-size", "physical"}
UNITS = {"bytes", "count", "milliseconds", "percent"}
STATUSES = {"enforced-software", "blocked-by-physical-evidence"}


@dataclass(frozen=True)
class PerformanceMetric:
    id: str
    title: str
    measurement_kind: str
    source: str
    selector: str
    unit: str
    maximum: float
    status: str
    sample_count: int
    warmup_count: int
    release_blocking: bool
    rationale: str


def _source_path(root: Path, value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{label}: source path must be a string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ModelError(f"{label}: source path escapes the repository")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ModelError(f"{label}: source path does not exist")
    return value


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 10000:
        raise ModelError(f"{label}: must be an integer from {minimum} through 10000")
    return value


def load_performance_budgets(root: Path) -> tuple[PerformanceMetric, ...]:
    value = load_json(root / "assurance" / "performance-budgets.json")
    if set(value) != {"$schema", "schema", "metrics"}:
        raise ModelError("performance budgets have missing or unknown top-level fields")
    if value["schema"] != "hyperflux-performance-budgets-v1":
        raise ModelError("unsupported performance-budget schema")
    raw_metrics = value["metrics"]
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise ModelError("performance budgets must contain metrics")
    metrics: list[PerformanceMetric] = []
    for index, raw in enumerate(raw_metrics):
        if not isinstance(raw, dict) or set(raw) != METRIC_KEYS:
            raise ModelError(f"performance metric {index}: missing or unknown fields")
        metric_id = raw["id"]
        title = raw["title"]
        kind = raw["measurement_kind"]
        selector = raw["selector"]
        unit = raw["unit"]
        maximum = raw["maximum"]
        status = raw["status"]
        release_blocking = raw["release_blocking"]
        rationale = raw["rationale"]
        if not isinstance(metric_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", metric_id):
            raise ModelError(f"performance metric {index}: invalid id")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ModelError(f"performance metric {metric_id}: invalid title")
        if kind not in KINDS or unit not in UNITS or status not in STATUSES:
            raise ModelError(f"performance metric {metric_id}: invalid kind, unit, or status")
        if not isinstance(selector, str) or not selector or len(selector) > 256:
            raise ModelError(f"performance metric {metric_id}: invalid selector")
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
            raise ModelError(f"performance metric {metric_id}: maximum must be positive")
        if not isinstance(release_blocking, bool):
            raise ModelError(f"performance metric {metric_id}: release flag must be boolean")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 300:
            raise ModelError(f"performance metric {metric_id}: invalid rationale")
        if kind == "physical" and status != "blocked-by-physical-evidence":
            raise ModelError(f"performance metric {metric_id}: physical metric cannot be software-enforced")
        if kind != "physical" and status != "enforced-software":
            raise ModelError(f"performance metric {metric_id}: software metric cannot claim a physical block")
        metrics.append(
            PerformanceMetric(
                id=metric_id,
                title=title.strip(),
                measurement_kind=kind,
                source=_source_path(root, raw["source"], f"performance metric {metric_id}"),
                selector=selector,
                unit=unit,
                maximum=float(maximum),
                status=status,
                sample_count=_positive_integer(raw["sample_count"], f"performance metric {metric_id} samples"),
                warmup_count=_positive_integer(
                    raw["warmup_count"], f"performance metric {metric_id} warmups", allow_zero=True
                ),
                release_blocking=release_blocking,
                rationale=rationale.strip(),
            )
        )
    require_unique([metric.id for metric in metrics], "performance metric id")
    return tuple(metrics)


def _json_pointer(value: Any, pointer: str, label: str) -> Any:
    if not pointer.startswith("/") or pointer.endswith("/"):
        raise ModelError(f"{label}: JSON pointer is not canonical")
    current = value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise ModelError(f"{label}: JSON pointer does not resolve")
        current = current[token]
    return current


def verify_static_performance_budgets(
    root: Path, metrics: tuple[PerformanceMetric, ...]
) -> dict[str, float]:
    results: dict[str, float] = {}
    for metric in metrics:
        if metric.measurement_kind != "json-pointer":
            continue
        value = _json_pointer(
            load_json(root / metric.source), metric.selector, f"performance metric {metric.id}"
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelError(f"performance metric {metric.id}: selected value is not numeric")
        measured = float(value)
        if measured > metric.maximum:
            raise ModelError(
                f"performance metric {metric.id}: {measured:g} {metric.unit} exceeds "
                f"{metric.maximum:g} {metric.unit}"
            )
        results[metric.id] = measured
    return results


def verify_package_performance_budgets(
    metrics: tuple[PerformanceMetric, ...],
    artifact_sizes: dict[str, int],
    staged_payload_size: int,
) -> dict[str, float]:
    results: dict[str, float] = {}
    for metric in metrics:
        if metric.measurement_kind == "artifact-size":
            if metric.selector not in artifact_sizes:
                raise ModelError(
                    f"performance metric {metric.id}: artifact {metric.selector} is unavailable"
                )
            measured = float(artifact_sizes[metric.selector])
        elif metric.measurement_kind == "staged-payload-size":
            measured = float(staged_payload_size)
        else:
            continue
        if measured > metric.maximum:
            raise ModelError(
                f"performance metric {metric.id}: {measured:g} {metric.unit} exceeds "
                f"{metric.maximum:g} {metric.unit}"
            )
        results[metric.id] = measured
    return results
