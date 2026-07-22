# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .model import ModelError, load_foundation, load_json, sha256_file
from .toolchains import toolchain_environment
from .verification_run import source_identity


SHADOW_FIXTURE_SCHEMA = "hyperflux-shadow-comparison-fixture-v1"
SHADOW_RESULT_SCHEMA = "hyperflux-shadow-comparison-result-v1"
SHADOW_EVIDENCE_SCHEMA = "hyperflux-shadow-comparison-evidence-v1"
SHADOW_DOMAIN_FIELDS = (
    ("profile-selection", "selected_profiles"),
    ("presence-state", "presence_states"),
    ("capabilities", "capabilities"),
    ("transaction-validation", "transaction_validation"),
    ("diagnostic-findings", "diagnostic_findings"),
)
MAX_SHADOW_FIXTURE_BYTES = 1_048_576


@dataclass(frozen=True)
class ShadowRun:
    status: str
    output: Path
    comparison: Path
    evidence: Path


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ModelError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def capture_inventory(root: Path, source_id: str, source_path: Path) -> Path:
    _, sources, _ = load_foundation(root)
    matches = [source for source in sources["sources"] if source["id"] == source_id]
    if len(matches) != 1 or matches[0]["kind"] != "git":
        raise ModelError(f"{source_id}: expected exactly one git source")
    source = matches[0]
    commit = _git(source_path, "rev-parse", f"{source['commit']}^{{commit}}").strip()
    if not commit.startswith(source["commit"]):
        raise ModelError(f"{source_id}: resolved commit {commit} does not match {source['commit']}")
    raw = _git(source_path, "ls-tree", "-r", commit)
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_type, blob = metadata.split(" ")
        entries.append({"path": path, "mode": mode, "type": object_type, "object": blob})
    inventory: dict[str, Any] = {
        "schema": "hyperflux-source-inventory-v1",
        "source": source_id,
        "commit": commit,
        "entry_count": len(entries),
        "entries": entries,
    }
    destination = root / source["inventory"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def load_shadow_fixture(root: Path, fixture_path: Path) -> dict[str, Any]:
    """Load one safe shadow fixture and bind every legacy record to inventory."""
    path = fixture_path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ModelError("shadow fixture must be one regular, non-symbolic-link file")
    if path.stat().st_size > MAX_SHADOW_FIXTURE_BYTES:
        raise ModelError("shadow fixture exceeds the 1 MiB input limit")
    fixture = load_json(path)
    if fixture.get("schema") != SHADOW_FIXTURE_SCHEMA:
        raise ModelError("unsupported shadow comparison fixture schema")
    provenance = fixture.get("provenance")
    if not isinstance(provenance, dict):
        raise ModelError("shadow fixture provenance is missing")
    required_safety = {
        "comparison_mode": "recorded-decisions-only",
        "boundary": {"test_fixture": True, "read_only": True},
        "authority": {
            "hardware_claim_authority": False,
            "publication_authorized": False,
        },
        "side_effects": {
            "private_identifiers_exported": False,
            "hardware_queried": False,
            "hardware_writes_executed": False,
        },
        "sanitization": "no-private-identifiers-v1",
    }
    for field, expected in required_safety.items():
        if provenance.get(field) != expected:
            raise ModelError(f"shadow fixture has unsafe provenance field: {field}")

    _, sources, _ = load_foundation(root)
    source_id = provenance.get("source_id")
    source = next(
        (
            candidate
            for candidate in sources["sources"]
            if candidate["id"] == source_id and candidate["kind"] == "git"
        ),
        None,
    )
    if source is None:
        raise ModelError("shadow fixture does not name one cataloged Git source")
    inventory_path = root / source["inventory"]
    inventory = load_json(inventory_path)
    source_commit = provenance.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or source_commit != inventory.get("commit")
        or not source_commit.startswith(source["commit"])
    ):
        raise ModelError("shadow fixture legacy commit does not match its frozen inventory")
    inventory_records = {
        entry["path"]: entry["object"]
        for entry in inventory.get("entries", [])
        if entry.get("type") == "blob"
    }
    records = provenance.get("source_records")
    if not isinstance(records, list) or not records:
        raise ModelError("shadow fixture has no legacy source records")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ModelError("shadow fixture has a malformed legacy source record")
        record_path = record.get("path")
        object_id = record.get("object")
        if not isinstance(record_path, str) or inventory_records.get(record_path) != object_id:
            raise ModelError("shadow fixture legacy source record does not match inventory")
        paths.append(record_path)
    if paths != sorted(set(paths)):
        raise ModelError("shadow fixture legacy source records are not unique and sorted")
    return fixture


def execute_shadow_comparison(root: Path, fixture_path: Path) -> dict[str, Any]:
    """Run the bounded Rust comparator and validate its non-authoritative result."""
    fixture = load_shadow_fixture(root, fixture_path)
    try:
        result = subprocess.run(
            [
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "-p",
                "hfx-sim",
                "--bin",
                "hfx-shadow",
                "--",
                str(fixture_path.resolve()),
            ],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            env=toolchain_environment(root),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"shadow comparator could not run: {error}") from error
    if result.returncode != 0:
        message = " ".join(result.stderr.split())[:512]
        raise ModelError(f"shadow comparator failed: {message}")
    try:
        comparison = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ModelError("shadow comparator emitted malformed JSON") from error
    validate_shadow_result(comparison, fixture)
    return comparison


def validate_shadow_result(
    comparison: object,
    fixture: dict[str, Any],
) -> None:
    """Independently verify every semantic claim emitted by the Rust comparator."""
    if not isinstance(comparison, dict):
        raise ModelError("shadow comparator result must be one object")
    _require_exact_keys(
        comparison,
        {
            "schema",
            "comparison_id",
            "scenario_id",
            "status",
            "boundary",
            "authority",
            "side_effects",
            "legacy_source",
            "simulator_content_sha256",
            "domains",
            "checkpoints",
            "differences",
            "content_sha256",
        },
        "shadow result",
    )
    if comparison.get("schema") != SHADOW_RESULT_SCHEMA:
        raise ModelError("shadow comparator emitted an unsupported result")
    if comparison.get("comparison_id") != fixture.get("comparison_id"):
        raise ModelError("shadow comparator changed the comparison identity")
    scenario = fixture.get("scenario")
    if not isinstance(scenario, dict) or comparison.get("scenario_id") != scenario.get(
        "scenario_id"
    ):
        raise ModelError("shadow comparator changed the scenario identity")
    if comparison.get("legacy_source") != fixture.get("provenance"):
        raise ModelError("shadow comparator changed the frozen legacy source binding")
    if comparison.get("status") not in {"matched", "diverged"}:
        raise ModelError("shadow comparator emitted an unknown status")
    required_safety = {
        "boundary": {"test_fixture": True, "read_only": True},
        "authority": {
            "hardware_claim_authority": False,
            "publication_authorized": False,
        },
        "side_effects": {
            "private_identifiers_exported": False,
            "hardware_queried": False,
            "hardware_writes_executed": False,
        },
    }
    for field, expected in required_safety.items():
        if comparison.get(field) != expected:
            raise ModelError(f"shadow result violates its safety contract: {field}")
    _require_sha256(comparison.get("simulator_content_sha256"), "simulator content")
    expected_domains, expected_differences = _validate_shadow_checkpoints(
        comparison.get("checkpoints"), scenario
    )
    if comparison.get("domains") != expected_domains:
        raise ModelError("shadow result domain summaries contradict its checkpoints")
    if comparison.get("differences") != expected_differences:
        raise ModelError("shadow result differences contradict its checkpoints")
    expected_status = "matched" if not expected_differences else "diverged"
    if comparison.get("status") != expected_status:
        raise ModelError("shadow result status contradicts its semantic comparisons")
    content_sha256 = comparison.get("content_sha256")
    _require_sha256(content_sha256, "shadow result content")
    unhashed = deepcopy(comparison)
    unhashed["content_sha256"] = ""
    expected_digest = hashlib.sha256(
        json.dumps(unhashed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if content_sha256 != expected_digest:
        raise ModelError("shadow result content digest does not bind its semantic result")


def _validate_shadow_checkpoints(
    checkpoints: object,
    scenario: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) > 256:
        raise ModelError("shadow result checkpoints must contain 1..=256 entries")
    events = scenario.get("events")
    if not isinstance(events, list):
        raise ModelError("shadow fixture scenario events are missing")
    domain_counts = {domain: [0, 0] for domain, _ in SHADOW_DOMAIN_FIELDS}
    differences: list[dict[str, Any]] = []
    sequences: set[int] = set()
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            raise ModelError("shadow result contains a malformed checkpoint")
        required = {"sequence", "event_kind", "matched"}
        allowed = required | {field for _, field in SHADOW_DOMAIN_FIELDS}
        if not required.issubset(checkpoint) or not set(checkpoint).issubset(allowed):
            raise ModelError("shadow result checkpoint has missing or unknown fields")
        sequence = checkpoint["sequence"]
        if type(sequence) is not int or not 0 <= sequence < len(events):
            raise ModelError("shadow result checkpoint sequence is outside the scenario")
        if sequence in sequences:
            raise ModelError("shadow result checkpoint sequences must be unique")
        sequences.add(sequence)
        event = events[sequence]
        event_kind = event.get("event", {}).get("kind") if isinstance(event, dict) else None
        if checkpoint["event_kind"] != event_kind:
            raise ModelError("shadow result checkpoint names the wrong event kind")
        comparison_states: list[bool] = []
        for domain, field in SHADOW_DOMAIN_FIELDS:
            if field not in checkpoint:
                continue
            value = checkpoint[field]
            if not isinstance(value, dict) or set(value) != {"matched", "legacy", "next"}:
                raise ModelError(f"shadow result has a malformed {domain} comparison")
            matched = value["matched"]
            if type(matched) is not bool or matched != (value["legacy"] == value["next"]):
                raise ModelError(f"shadow result has a contradictory {domain} comparison")
            comparison_states.append(matched)
            domain_counts[domain][0] += 1
            if not matched:
                domain_counts[domain][1] += 1
                differences.append(
                    {
                        "sequence": sequence,
                        "domain": domain,
                        "description": (
                            f"legacy and next {domain} decisions differ at event {sequence}"
                        ),
                    }
                )
        if not comparison_states:
            raise ModelError("shadow result checkpoint compares no semantic domain")
        checkpoint_matched = checkpoint["matched"]
        if type(checkpoint_matched) is not bool or checkpoint_matched != all(
            comparison_states
        ):
            raise ModelError("shadow result checkpoint match state is contradictory")

    summaries: list[dict[str, Any]] = []
    for domain, _ in SHADOW_DOMAIN_FIELDS:
        compared, mismatches = domain_counts[domain]
        if compared == 0:
            raise ModelError("shadow result does not report all five semantic domains")
        summaries.append(
            {
                "domain": domain,
                "compared_checkpoints": compared,
                "mismatches": mismatches,
                "matched": mismatches == 0,
            }
        )
    return summaries, differences


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ModelError(f"{label} has missing or unknown fields")


def _require_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelError(f"{label} digest is not canonical SHA-256")


def run_shadow_comparison(
    root: Path,
    fixture_path: Path,
    output: Path,
) -> ShadowRun:
    """Write one source-bound shadow result and privacy-safe evidence envelope."""
    fixture_path = fixture_path.expanduser().resolve()
    source = source_identity(root)
    fixture = load_shadow_fixture(root, fixture_path)
    comparison = execute_shadow_comparison(root, fixture_path)
    destination = _prepare_shadow_output(root, output)
    comparison_path = destination / "comparison.json"
    _write_atomic(comparison_path, _json_payload(comparison))

    _, sources, _ = load_foundation(root)
    source_record = next(
        candidate
        for candidate in sources["sources"]
        if candidate["id"] == fixture["provenance"]["source_id"]
    )
    fixture_material_path = _evidence_path(root, fixture_path)
    materials = [
        {
            "id": "legacy-decision-fixture",
            "path": fixture_material_path,
            "sha256": sha256_file(fixture_path),
        },
        {
            "id": "legacy-source-inventory",
            "path": source_record["inventory"],
            "sha256": sha256_file(root / source_record["inventory"]),
        },
        {
            "id": "generated-profile-catalog",
            "path": "generated/profiles/catalog.json",
            "sha256": sha256_file(root / "generated/profiles/catalog.json"),
        },
    ]
    constitution, _, _ = load_foundation(root)
    if constitution["publication_interlock"].get("publication_authorized") is not False:
        raise ModelError("shadow evidence requires publication authorization to remain false")
    evidence = {
        "$schema": "https://hyperflux.dev/schemas/shadow-comparison-evidence-v1.json",
        "schema": SHADOW_EVIDENCE_SCHEMA,
        "run_id": f"shadow-{comparison['comparison_id']}-{source['revision'][:12]}",
        "comparison": "comparison.json",
        "comparison_sha256": sha256_file(comparison_path),
        "comparison_status": comparison["status"],
        "source": source,
        "materials": materials,
        "hardware": {"queried": False, "writes_executed": False, "generations": []},
        "publication_authorized": False,
    }
    evidence_path = destination / "evidence.json"
    _write_atomic(evidence_path, _json_payload(evidence))
    return ShadowRun(
        status=str(comparison["status"]),
        output=destination,
        comparison=comparison_path,
        evidence=evidence_path,
    )


def _prepare_shadow_output(root: Path, output: Path) -> Path:
    candidate = output.expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise ModelError("shadow comparison output may not be a symbolic link")
    candidate = candidate.resolve()
    if candidate in {Path("/"), Path.home().resolve(), root.resolve()}:
        raise ModelError("refusing unsafe shadow comparison output directory")
    if candidate.exists():
        if not candidate.is_dir() or any(candidate.iterdir()):
            raise ModelError("shadow comparison output must be absent or empty")
    else:
        candidate.mkdir(parents=True, mode=0o755)
    return candidate


def _evidence_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "external-sanitized-fixture"


def _json_payload(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n").encode()


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def summary(root: Path) -> str:
    _, sources, ledger = load_foundation(root)
    source_count = len(sources["sources"])
    decisions: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for entry in ledger["entries"]:
        decisions[entry["disposition"]] = decisions.get(entry["disposition"], 0) + 1
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    lines = [
        "HyperFlux Next migration",
        f"Sources: {source_count}",
        f"Subsystem decisions: {len(ledger['entries'])}",
        "Decisions: " + ", ".join(f"{name}={count}" for name, count in sorted(decisions.items())),
        "Status: " + ", ".join(f"{name}={count}" for name, count in sorted(statuses.items())),
        f"Default: {ledger['default_disposition']}",
    ]
    return "\n".join(lines)
