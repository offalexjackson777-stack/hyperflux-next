# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Callable
import xml.etree.ElementTree as ElementTree

from .integrations import load_integration_catalog
from .model import ModelError, load_json, sha256_file
from .testgraph import TestCatalog, TestNode, TestSelection, select_tests
from .toolchains import load_toolchain_pins


Runner = Callable[[Path, TestNode], None]
IGNORED_PARTS = {
    ".git",
    ".hfx",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "target",
}
MATERIALS = (
    ("architecture-constitution", "architecture/constitution.json", "file"),
    ("cargo-lock", "Cargo.lock", "file"),
    ("dependency-policy", "assurance/dependencies.json", "file"),
    ("design-coverage", "assurance/design-coverage.json", "file"),
    ("error-catalog", "errors/catalog.json", "file"),
    ("formal-model", "assurance/formal-model.json", "file"),
    ("generated-profiles", "generated/profiles/catalog.json", "file"),
    ("integration-catalog", "integrations/catalog.json", "file"),
    ("performance-budgets", "assurance/performance-budgets.json", "file"),
    ("protocol-catalog", "protocol/v5/catalog.json", "file"),
    ("release-gates", "assurance/release-gates.json", "file"),
    ("replay-fixtures", "tests/fixtures/replay", "tree"),
    ("shadow-fixtures", "tests/fixtures/shadow", "tree"),
    ("schema-tree", "schemas", "tree"),
    ("source-sbom", "assurance/generated/hyperflux-next.spdx.json", "file"),
    ("test-catalog", "verification/tests.json", "file"),
    ("toolchain-pins", "toolchains/pins.json", "file"),
)


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    output: Path
    passed_titles: tuple[str, ...]
    failed_nodes: tuple[str, ...]


def _git_text(root: Path, command: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect {label}: {error}") from error
    if result.returncode != 0:
        raise ModelError(f"cannot inspect {label}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_paths(root: Path, command: list[str], label: str) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect {label}: {error}") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ModelError(f"cannot inspect {label}: {message}")
    try:
        return tuple(
            value.decode("utf-8")
            for value in result.stdout.split(b"\0")
            if value
        )
    except UnicodeDecodeError as error:
        raise ModelError(f"{label} contains a non-UTF-8 repository path") from error


def git_changed_paths(root: Path, base: str) -> tuple[str, tuple[str, ...]]:
    revision = _git_text(
        root,
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
        "changed-path base revision",
    )
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ModelError("changed-path base did not resolve to a commit")
    paths: set[str] = set()
    commands = (
        (["git", "diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB", f"{revision}...HEAD"], "committed changes"),
        (["git", "diff", "--name-only", "-z", "--diff-filter=ACDMRTUXB"], "working-tree changes"),
        (["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRTUXB"], "index changes"),
        (["git", "ls-files", "--others", "--exclude-standard", "-z"], "untracked changes"),
    )
    for command, label in commands:
        paths.update(_git_paths(root, command, label))
    return revision, tuple(sorted(paths))


def source_identity(root: Path) -> dict[str, object]:
    """Return the exact Git source and working-tree identity for evidence."""
    revision = _git_text(root, ["git", "rev-parse", "HEAD"], "source revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ModelError("source revision is not a lowercase Git commit")
    epoch_text = _git_text(
        root, ["git", "show", "-s", "--format=%ct", revision], "source timestamp"
    )
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise ModelError("source timestamp is not an integer") from error
    status = _git_paths(
        root,
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        "source worktree",
    )
    status_payload = "\0".join(status).encode("utf-8")
    return {
        "revision": revision,
        "commit_epoch": epoch,
        "worktree": "dirty" if status else "clean",
        "worktree_sha256": hashlib.sha256(status_payload).hexdigest(),
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_message(root: Path, error: BaseException) -> str:
    message = " ".join(str(error).split()) or error.__class__.__name__
    message = message.replace(str(root.resolve()), ".")
    message = re.sub(
        r"/(?:home/[^/ ]+|tmp|var/tmp|run/user/[0-9]+)(?:/[^ ]*)?",
        "[private-path]",
        message,
    )
    return message[:512]


def _repository_files(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if any(part in IGNORED_PARTS for part in path.relative_to(root).parts):
                continue
            if path.is_symlink():
                raise ModelError(f"verification input may not be a symbolic link: {path.relative_to(root)}")
            if path.is_file():
                files.add(path)
    if not files:
        raise ModelError("verification input selection resolved to no regular files")
    return tuple(sorted(files))


def _files_digest(root: Path, files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(4, "big"))
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _input_digest(root: Path, node: TestNode) -> str:
    return _files_digest(root, _repository_files(root, node.cache_inputs))


def _material(root: Path, material_id: str, value: str, kind: str) -> dict[str, object]:
    path = root / value
    if kind == "file":
        if not path.is_file() or path.is_symlink():
            raise ModelError(f"verification material is missing: {value}")
        digest = sha256_file(path)
    else:
        if not path.is_dir() or path.is_symlink():
            raise ModelError(f"verification material tree is missing: {value}")
        candidates = tuple(path.rglob("*"))
        symbolic_links = tuple(candidate for candidate in candidates if candidate.is_symlink())
        if symbolic_links:
            relative = symbolic_links[0].relative_to(root)
            raise ModelError(f"verification material tree contains a symbolic link: {relative}")
        files = tuple(sorted(candidate for candidate in candidates if candidate.is_file()))
        if not files:
            raise ModelError(f"verification material tree is empty: {value}")
        digest = _files_digest(root, files)
    return {"id": material_id, "path": value, "kind": kind, "sha256": digest}


def _toolchain(root: Path) -> dict[str, str]:
    return asdict(load_toolchain_pins(root))


def _prepare_output(root: Path, output: Path | None, run_id: str) -> Path:
    candidate = output if output is not None else root / "build" / "verification" / run_id
    candidate = candidate.expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise ModelError("verification output may not be a symbolic link")
    candidate = candidate.resolve()
    if candidate in {Path("/"), Path.home().resolve(), root.resolve()}:
        raise ModelError("refusing unsafe verification output directory")
    if candidate.exists():
        if not candidate.is_dir() or any(candidate.iterdir()):
            raise ModelError("verification output directory must be absent or empty")
    else:
        candidate.mkdir(parents=True, mode=0o755)
    return candidate


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


def _json_payload(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n").encode()


def _junit(result: dict[str, object]) -> bytes:
    nodes = result["nodes"]
    assert isinstance(nodes, list)
    failures = sum(node["status"] == "failed" for node in nodes)
    skipped = sum(node["status"] in {"pending", "running", "blocked"} for node in nodes)
    duration = (result["duration_ms"] or 0) / 1000
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": f"hyperflux-{result['lane']}",
            "tests": str(len(nodes)),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{duration:.3f}",
        },
    )
    for node in nodes:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "name": str(node["id"]),
                "classname": f"hyperflux.{node['domain']}",
                "time": f"{(node['duration_ms'] or 0) / 1000:.3f}",
            },
        )
        if node["status"] == "failed":
            failure = ElementTree.SubElement(case, "failure", {"message": str(node["error"])})
            failure.text = str(node["error"])
        elif node["status"] in {"pending", "running", "blocked"}:
            ElementTree.SubElement(case, "skipped", {"message": str(node["error"] or node["status"])})
        evidence = ElementTree.SubElement(case, "system-out")
        evidence.text = ",".join(node["produced_evidence"])
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True)


def _annotations(result: dict[str, object]) -> list[dict[str, object]]:
    annotations: list[dict[str, object]] = []
    for node in result["nodes"]:
        if node["status"] not in {"failed", "blocked"}:
            continue
        annotations.append(
            {
                "path": "verification/tests.json",
                "start_line": 1,
                "end_line": 1,
                "annotation_level": "failure" if node["status"] == "failed" else "warning",
                "title": node["title"],
                "message": node["error"],
            }
        )
    return annotations


def _summary(result: dict[str, object], output: Path) -> str:
    lines = [
        "# HyperFlux Verification",
        "",
        f"- Run: `{result['run_id']}`",
        f"- Lane: `{result['lane']}`",
        f"- Status: **{str(result['status']).upper()}**",
        f"- Evidence: `{output.name}/evidence.json`",
        "",
        "| Node | Domain | Status | Duration |",
        "| --- | --- | --- | ---: |",
    ]
    for node in result["nodes"]:
        duration = "-" if node["duration_ms"] is None else f"{node['duration_ms']} ms"
        lines.append(f"| `{node['id']}` | `{node['domain']}` | `{node['status']}` | {duration} |")
    lines.append("")
    return "\n".join(lines)


def _package_artifacts(root: Path, node: TestNode) -> list[dict[str, object]]:
    if node.id != "package-contracts":
        return []
    workspace = root / "build" / "package-contracts"
    manifest_path = workspace / "artifacts" / "package-build-manifest.json"
    manifest = load_json(manifest_path)
    records = [
        {
            "id": "package-build-manifest",
            "node_id": node.id,
            "path": manifest_path.relative_to(root).as_posix(),
            "sha256": sha256_file(manifest_path),
            "size": manifest_path.stat().st_size,
        }
    ]
    for artifact in manifest.get("artifacts", []):
        path = manifest_path.parent / artifact["path"]
        if sha256_file(path) != artifact["sha256"]:
            raise ModelError(f"structured evidence found a changed package artifact: {artifact['build_id']}")
        records.append(
            {
                "id": f"package-{artifact['build_id']}",
                "node_id": node.id,
                "path": path.relative_to(root).as_posix(),
                "sha256": artifact["sha256"],
                "size": artifact["size"],
            }
        )
    inventory = workspace / "root-a" / "usr/share/hyperflux-next/installed-files.json"
    if not inventory.is_file() or inventory.is_symlink():
        raise ModelError("structured evidence cannot find the installed-files inventory")
    records.append(
        {
            "id": "installed-files-inventory",
            "node_id": node.id,
            "path": inventory.relative_to(root).as_posix(),
            "sha256": sha256_file(inventory),
            "size": inventory.stat().st_size,
        }
    )
    return sorted(records, key=lambda item: str(item["id"]))


def _evidence(
    root: Path,
    result: dict[str, object],
    source: dict[str, object],
    materials: list[dict[str, object]],
    artifacts: list[dict[str, object]],
) -> dict[str, object]:
    catalog = load_integration_catalog(root)
    constitution = load_json(root / "architecture" / "constitution.json")
    publication = constitution.get("publication_interlock", {}).get("publication_authorized")
    if publication is not False:
        raise ModelError("verification evidence requires publication authorization to remain false")
    claims = [
        {"node_id": node["id"], "evidence_id": evidence_id, "status": node["status"]}
        for node in result["nodes"]
        for evidence_id in node["produced_evidence"]
    ]
    measurements = [
        {
            "id": f"duration-{node['id']}",
            "node_id": node["id"],
            "value": node["duration_ms"],
            "unit": "milliseconds",
        }
        for node in result["nodes"]
        if node["duration_ms"] is not None
    ]
    return {
        "$schema": "https://hyperflux.dev/schemas/verification-evidence-v1.json",
        "schema": "hyperflux-verification-evidence-v1",
        "run_id": result["run_id"],
        "result": "result.json",
        "source": source,
        "materials": materials,
        "toolchain": _toolchain(root),
        "upstreams": [
            {
                "id": upstream["id"],
                "repository": upstream["repository"],
                "revision": upstream["commit"],
            }
            for upstream in sorted(catalog["upstreams"], key=lambda item: item["id"])
        ],
        "hardware": {"queried": False, "writes_executed": False, "generations": []},
        "publication_authorized": False,
        "claims": claims,
        "artifacts": artifacts,
        "measurements": measurements,
    }


def _persist(
    root: Path,
    output: Path,
    result: dict[str, object],
    source: dict[str, object],
    materials: list[dict[str, object]],
    artifacts: list[dict[str, object]],
) -> None:
    evidence = _evidence(root, result, source, materials, artifacts)
    _write_atomic(output / "result.json", _json_payload(result))
    _write_atomic(output / "evidence.json", _json_payload(evidence))
    _write_atomic(output / "junit.xml", _junit(result))
    _write_atomic(output / "annotations.json", _json_payload(_annotations(result)))
    _write_atomic(output / "summary.md", _summary(result, output).encode("utf-8"))


def run_verification(
    root: Path,
    catalog: TestCatalog,
    runners: dict[str, Runner],
    *,
    lane: str,
    output: Path | None = None,
    changed_from: str | None = None,
) -> VerificationOutcome:
    root = root.resolve()
    source = source_identity(root)
    base_revision: str | None = None
    changed_paths: tuple[str, ...] | None = None
    if changed_from is not None:
        base_revision, changed_paths = git_changed_paths(root, changed_from)
    selection: TestSelection = select_tests(catalog, lane, changed_paths)
    unsafe_nodes = tuple(
        node.id
        for node in selection.nodes
        if node.hardware_requirement != "none" or node.writes_hardware
    )
    if unsafe_nodes:
        raise ModelError(
            "software verification selected hardware-authorized nodes: "
            + ", ".join(unsafe_nodes)
        )
    started_at = _timestamp()
    start_ns = time.monotonic_ns()
    identity = json.dumps(
        [source["revision"], lane, started_at, time.time_ns(), selection.changed_paths],
        separators=(",", ":"),
    ).encode()
    run_id = f"hfxv-{hashlib.sha256(identity).hexdigest()[:20]}"
    output = _prepare_output(root, output, run_id)
    materials = [
        _material(root, material_id, path, kind)
        for material_id, path, kind in MATERIALS
    ]
    nodes = [
        {
            "id": node.id,
            "title": node.title,
            "domain": node.owned_domain,
            "runner": node.runner,
            "dependencies": list(node.dependencies),
            "input_sha256": _input_digest(root, node),
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "produced_evidence": list(node.produced_evidence),
            "error": None,
        }
        for node in selection.nodes
    ]
    result: dict[str, object] = {
        "$schema": "https://hyperflux.dev/schemas/verification-run-v1.json",
        "schema": "hyperflux-verification-run-v1",
        "run_id": run_id,
        "lane": lane,
        "selection": {
            "mode": selection.mode,
            "base_revision": base_revision,
            "changed_paths": list(selection.changed_paths),
            "unmatched_paths": list(selection.unmatched_paths),
        },
        "source": source,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "duration_ms": None,
        "nodes": nodes,
    }
    artifacts: list[dict[str, object]] = []
    _persist(root, output, result, source, materials, artifacts)

    states = {node["id"]: node for node in nodes}
    for specification, node in zip(selection.nodes, nodes, strict=True):
        unavailable = [
            dependency
            for dependency in specification.dependencies
            if dependency in states and states[dependency]["status"] != "passed"
        ]
        if unavailable:
            node["status"] = "blocked"
            node["error"] = f"dependency did not pass: {', '.join(unavailable)}"
            _persist(root, output, result, source, materials, artifacts)
            continue
        runner = runners.get(specification.runner)
        if runner is None:
            node["status"] = "failed"
            node["error"] = f"trusted runner is unavailable: {specification.runner}"
            _persist(root, output, result, source, materials, artifacts)
            continue
        print(f"[{specification.id}] {specification.title}", flush=True)
        node_started_ns = time.monotonic_ns()
        node["started_at"] = _timestamp()
        node["status"] = "running"
        _persist(root, output, result, source, materials, artifacts)
        try:
            runner(root, specification)
            artifacts.extend(_package_artifacts(root, specification))
            node["status"] = "passed"
        except Exception as error:
            node["status"] = "failed"
            node["error"] = _safe_message(root, error)
        node["finished_at"] = _timestamp()
        node["duration_ms"] = max(0, (time.monotonic_ns() - node_started_ns) // 1_000_000)
        _persist(root, output, result, source, materials, artifacts)

    failed = tuple(node["id"] for node in nodes if node["status"] in {"failed", "blocked"})
    result["status"] = "failed" if failed else "passed"
    result["finished_at"] = _timestamp()
    result["duration_ms"] = max(0, (time.monotonic_ns() - start_ns) // 1_000_000)
    _persist(root, output, result, source, materials, artifacts)
    return VerificationOutcome(
        status=str(result["status"]),
        output=output,
        passed_titles=tuple(node["title"] for node in nodes if node["status"] == "passed"),
        failed_nodes=failed,
    )
