# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any

from .ci import _bounded_command
from .model import ModelError, load_json
from .performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
)
from .testgraph import load_test_catalog


REVIEW_STATUSES = {
    "code-removal": "reviewed-no-unproven-removal",
    "rust-unsafe": "enforced",
    "rust-sanitizer": "unsupported-by-pinned-toolchain",
    "openrgb-thread-sanitizer": "enforced",
    "diagnostics-and-logs": "enforced",
    "parsing-and-copies": "reviewed-no-change",
    "idle-wakeups-and-polling": "reviewed-bounded",
    "package-and-binary-size": "measured",
    "startup-latency": "unmeasured-physical",
    "reconnect-latency": "unmeasured-physical",
    "publication-latency": "unmeasured-physical",
    "command-latency": "unmeasured-physical",
    "capability-discovery": "enforced",
}
ARTIFACT_IDS = (
    "activation-utility",
    "bridge-daemon",
    "openrazer-native-driver",
    "openrazer-native-mount-helper",
    "openrazer-native-relay",
    "openrazer-native-udev-rules",
    "openrgb-native",
    "operations-cli",
)
POLLING_PATTERNS = {
    "watch_ms": r"const WATCH_INTERVAL: Duration = Duration::from_millis\((\d+)\);",
    "startup_retry_ms": r"const STARTUP_BRIDGE_RETRY_INTERVAL: Duration = Duration::from_millis\((\d+)\);",
    "direct_scan_ms": r"const DIRECT_SCAN_INTERVAL: Duration = Duration::from_millis\((\d+)\);",
    "identity_stability_ms": r"const ROUTE_IDENTITY_STABILITY_INTERVAL: Duration = Duration::from_millis\((\d+)\);",
    "identity_fast_retry_ms": r"const ROUTE_IDENTITY_FAST_RETRY: Duration = Duration::from_millis\((\d+)\);",
    "idle_ms": r"const IDLE_INTERVAL: Duration = Duration::from_millis\((\d+)\);",
    "uhid_start_timeout_ms": r"const UHID_START_TIMEOUT: Duration = Duration::from_secs\((\d+)\);",
}


@dataclass(frozen=True)
class QualityAudit:
    source_revision: str
    review_statuses: dict[str, str]
    baseline_artifact_sizes: dict[str, int]
    baseline_staged_payload_bytes: int
    relay_polling: dict[str, int]


def _integer(value: Any, label: str, *, positive: bool = True) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"quality audit {label} must be an integer at least {minimum}")
    return value


def _evidence_path(root: Path, value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"quality audit {label} evidence path must be a string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ModelError(f"quality audit {label} evidence path escapes the repository")
    path = root / relative
    if path.is_symlink() or not path.exists():
        raise ModelError(f"quality audit {label} evidence path is unavailable")
    return value


def load_quality_audit(root: Path) -> QualityAudit:
    value = load_json(root / "assurance" / "quality-audit.json")
    if set(value) != {"$schema", "schema", "source_revision", "reviews", "measurements"}:
        raise ModelError("quality audit has missing or unknown top-level fields")
    if value["schema"] != "hyperflux-quality-audit-v1":
        raise ModelError("unsupported quality audit schema")
    revision = value["source_revision"]
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ModelError("quality audit source revision is invalid")
    reviews = value["reviews"]
    if not isinstance(reviews, list) or len(reviews) != len(REVIEW_STATUSES):
        raise ModelError("quality audit review inventory is incomplete")
    statuses: dict[str, str] = {}
    for index, review in enumerate(reviews):
        if not isinstance(review, dict) or set(review) != {
            "id",
            "status",
            "evidence",
            "finding",
            "action",
        }:
            raise ModelError(f"quality audit review {index} has missing or unknown fields")
        identifier = review["id"]
        status = review["status"]
        if not isinstance(identifier, str) or identifier in statuses:
            raise ModelError("quality audit review ids must be unique strings")
        statuses[identifier] = status
        evidence = review["evidence"]
        if not isinstance(evidence, list) or not evidence or len(evidence) > 8:
            raise ModelError(f"quality audit {identifier} evidence is malformed")
        if len(evidence) != len(set(evidence)):
            raise ModelError(f"quality audit {identifier} evidence is duplicated")
        for path in evidence:
            _evidence_path(root, path, identifier)
        for field in ("finding", "action"):
            text = review[field]
            if not isinstance(text, str) or not text.strip() or len(text) > 400:
                raise ModelError(f"quality audit {identifier} {field} is invalid")
    if tuple(statuses.items()) != tuple(REVIEW_STATUSES.items()):
        raise ModelError("quality audit review status or ordering drifted")

    measurements = value["measurements"]
    if not isinstance(measurements, dict) or set(measurements) != {
        "package_baseline",
        "relay_polling",
        "build_profile_trial",
    }:
        raise ModelError("quality audit measurements are incomplete")
    package = measurements["package_baseline"]
    expected_package_fields = {
        "source_revision",
        "manifest_sha256",
        "linux_inventory_sha256",
        "full_inventory_sha256",
        "artifact_sizes_bytes",
        "staged_payload_bytes",
    }
    if not isinstance(package, dict) or set(package) != expected_package_fields:
        raise ModelError("quality audit package baseline is malformed")
    for field, length in (("source_revision", 40), ("manifest_sha256", 64), ("linux_inventory_sha256", 64), ("full_inventory_sha256", 64)):
        item = package[field]
        if not isinstance(item, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", item) is None:
            raise ModelError(f"quality audit package {field} is invalid")
    sizes = package["artifact_sizes_bytes"]
    if not isinstance(sizes, dict) or tuple(sizes) != ARTIFACT_IDS:
        raise ModelError("quality audit artifact-size inventory drifted")
    artifact_sizes = {
        identifier: _integer(size, f"{identifier} size") for identifier, size in sizes.items()
    }
    staged_payload = _integer(package["staged_payload_bytes"], "staged payload size")
    verify_package_performance_budgets(
        load_performance_budgets(root), artifact_sizes, staged_payload
    )

    polling = measurements["relay_polling"]
    if not isinstance(polling, dict) or tuple(polling) != tuple(POLLING_PATTERNS):
        raise ModelError("quality audit relay polling inventory drifted")
    relay_polling = {
        identifier: _integer(interval, f"{identifier} interval")
        for identifier, interval in polling.items()
    }
    source = (root / "crates/hfx-uhid-relay/src/runtime.rs").read_text(encoding="utf-8")
    for identifier, pattern in POLLING_PATTERNS.items():
        matches = re.findall(pattern, source)
        if len(matches) != 1:
            raise ModelError(f"quality audit cannot resolve relay polling constant {identifier}")
        measured = int(matches[0]) * (1000 if identifier == "uhid_start_timeout_ms" else 1)
        if relay_polling[identifier] != measured:
            raise ModelError(f"quality audit relay polling constant {identifier} drifted")

    trial = measurements["build_profile_trial"]
    if not isinstance(trial, dict) or set(trial) != {
        "id",
        "status",
        "minimum_elapsed_seconds",
        "sampled_cpu_percent",
        "artifact_produced",
        "production_change_applied",
        "rationale",
    }:
        raise ModelError("quality audit build-profile trial is malformed")
    if (
        trial["id"] != "bridge-thin-lto-one-codegen-unit"
        or trial["status"] != "rejected-before-artifact"
        or trial["artifact_produced"] is not False
        or trial["production_change_applied"] is not False
    ):
        raise ModelError("quality audit build-profile trial overclaims its result")
    _integer(trial["minimum_elapsed_seconds"], "build trial elapsed time")
    cpu = trial["sampled_cpu_percent"]
    if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not 0 <= cpu <= 100:
        raise ModelError("quality audit build trial CPU sample is invalid")
    if not isinstance(trial["rationale"], str) or not trial["rationale"].strip():
        raise ModelError("quality audit build trial rationale is invalid")

    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    if cargo.get("workspace", {}).get("lints", {}).get("rust", {}).get("unsafe_code") != "deny":
        raise ModelError("quality audit Rust unsafe boundary is not enforced")
    tests = load_test_catalog(root).by_id
    if "openrgb-thread-sanitizer" not in tests or "rust-coverage-contracts" not in tests:
        raise ModelError("quality audit verification lanes are incomplete")
    return QualityAudit(revision, statuses, artifact_sizes, staged_payload, relay_polling)


def verify_pinned_rust_sanitizer_disposition() -> None:
    codegen = _bounded_command(
        ["rustc", "-C", "help"],
        label="pinned Rust codegen capabilities",
        max_output_bytes=65_536,
        timeout_seconds=20,
    )
    if codegen.returncode != 0 or codegen.stderr or "sanitizer" in codegen.stdout:
        raise ModelError("pinned stable Rust sanitizer disposition must be reviewed")
    components = _bounded_command(
        [
            "rustup",
            "component",
            "list",
            "--toolchain",
            "1.95.0-x86_64-unknown-linux-gnu",
        ],
        label="pinned Rust components",
        max_output_bytes=65_536,
        timeout_seconds=20,
    )
    if components.returncode != 0 or components.stderr:
        raise ModelError("pinned Rust component inventory is unavailable")
    if any(line.startswith("miri-") for line in components.stdout.splitlines()):
        raise ModelError("pinned stable Rust now exposes Miri; add the sanitizer lane")


def verify_current_package_measurements(root: Path) -> dict[str, int]:
    package_root = root / "build" / "package-contracts"
    manifest = load_json(package_root / "artifacts" / "package-build-manifest.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACT_IDS):
        raise ModelError("current quality package artifact inventory is incomplete")
    sizes: dict[str, int] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ModelError("current quality package artifact is malformed")
        identifier = artifact.get("build_id")
        if not isinstance(identifier, str) or identifier in sizes:
            raise ModelError("current quality package artifact ids are invalid")
        sizes[identifier] = _integer(artifact.get("size"), f"current {identifier} size")
    if tuple(sorted(sizes)) != ARTIFACT_IDS:
        raise ModelError("current quality package artifact coverage drifted")
    staged_payload = 0
    for component in ("linux", "full"):
        inventory = load_json(package_root / f"{component}-root-a.inventory.json")
        files = inventory.get("files")
        if not isinstance(files, list) or not files:
            raise ModelError(f"current quality {component} inventory is incomplete")
        for item in files:
            if not isinstance(item, dict):
                raise ModelError(f"current quality {component} inventory is malformed")
            staged_payload += _integer(
                item.get("size"), f"current {component} inventory size", positive=False
            )
    verify_package_performance_budgets(
        load_performance_budgets(root), sizes, staged_payload
    )
    return {**sizes, "staged-payload": staged_payload}
