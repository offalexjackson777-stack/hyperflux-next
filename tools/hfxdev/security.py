# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .ci import _bounded_command
from .model import ModelError
from .supply_chain import DependencyInventory, load_dependency_inventory


MAX_AUDIT_OUTPUT_BYTES = 1_048_576
MAX_GIT_OUTPUT_BYTES = 65_536


@dataclass(frozen=True)
class RustAuditResult:
    advisory_count: int
    dependency_count: int
    vulnerability_count: int
    warning_count: int


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelError(f"Rust dependency audit {label} is invalid")
    return value


def parse_rust_audit_result(
    text: str,
    inventory: DependencyInventory,
) -> RustAuditResult:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelError("Rust dependency audit output is not valid JSON") from error
    if not isinstance(value, dict):
        raise ModelError("Rust dependency audit output must be an object")
    database = value.get("database")
    lockfile = value.get("lockfile")
    vulnerabilities = value.get("vulnerabilities")
    warnings = value.get("warnings")
    if not all(
        isinstance(item, dict)
        for item in (database, lockfile, vulnerabilities, warnings)
    ):
        raise ModelError("Rust dependency audit output is incomplete")

    advisory_count = _nonnegative_integer(
        database.get("advisory-count"), "advisory count"
    )
    dependency_count = _nonnegative_integer(
        lockfile.get("dependency-count"), "dependency count"
    )
    vulnerability_count = _nonnegative_integer(
        vulnerabilities.get("count"), "vulnerability count"
    )
    vulnerability_list = vulnerabilities.get("list")
    if (
        not isinstance(vulnerability_list, list)
        or len(vulnerability_list) != vulnerability_count
    ):
        raise ModelError("Rust dependency audit vulnerability details are inconsistent")
    warning_lists = tuple(warnings.values())
    if not all(isinstance(items, list) for items in warning_lists):
        raise ModelError("Rust dependency audit warning details are malformed")
    warning_count = sum(len(items) for items in warning_lists)

    policy = inventory.rust_security
    if advisory_count != policy.expected_advisory_count:
        raise ModelError("Rust dependency audit advisory database count drifted")
    expected_dependencies = len(inventory.workspace_packages) + len(inventory.rust_packages)
    if dependency_count != expected_dependencies:
        raise ModelError("Rust dependency audit lockfile count drifted")
    if vulnerability_count != 0 and policy.deny_vulnerabilities:
        raise ModelError("Rust dependency audit found a vulnerability")
    if warning_count != 0 and policy.deny_warnings:
        raise ModelError("Rust dependency audit found an unreviewed warning")
    return RustAuditResult(
        advisory_count=advisory_count,
        dependency_count=dependency_count,
        vulnerability_count=vulnerability_count,
        warning_count=warning_count,
    )


def run_rust_dependency_audit(root: Path) -> RustAuditResult:
    root = root.resolve()
    inventory = load_dependency_inventory(root)
    policy = inventory.rust_security
    database = Path(policy.advisory_database_path)
    if database.is_symlink() or not database.is_dir():
        raise ModelError("pinned Rust advisory database is unavailable")

    revision = _bounded_command(
        [
            "git",
            "-c",
            f"safe.directory={database}",
            "-C",
            str(database),
            "rev-parse",
            "HEAD",
        ],
        label="Rust advisory database revision",
        max_output_bytes=MAX_GIT_OUTPUT_BYTES,
    )
    if (
        revision.returncode != 0
        or revision.stderr
        or revision.stdout.strip() != policy.advisory_database_revision
    ):
        raise ModelError("pinned Rust advisory database revision drifted")
    status = _bounded_command(
        [
            "git",
            "-c",
            f"safe.directory={database}",
            "-C",
            str(database),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        label="Rust advisory database status",
        max_output_bytes=MAX_GIT_OUTPUT_BYTES,
    )
    if status.returncode != 0 or status.stderr or status.stdout:
        raise ModelError("pinned Rust advisory database is dirty")

    audit = _bounded_command(
        [
            "cargo",
            "audit",
            "--no-fetch",
            "--db",
            str(database),
            "--file",
            "Cargo.lock",
            "--json",
        ],
        label="Rust dependency audit",
        max_output_bytes=MAX_AUDIT_OUTPUT_BYTES,
        timeout_seconds=120,
        cwd=root,
        environment={"CARGO_NET_OFFLINE": "true"},
    )
    if audit.returncode != 0:
        raise ModelError(
            "Rust dependency audit command failed: " + audit.stderr.strip()[:512]
        )
    return parse_rust_audit_result(audit.stdout, inventory)
