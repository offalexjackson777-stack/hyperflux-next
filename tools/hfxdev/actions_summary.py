# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from .model import ModelError
from .performance import load_performance_budgets
from .release import load_release_gates


REVISION = re.compile(r"^[0-9a-f]{40}$")
RUN_STATUSES = {"running", "passed", "failed"}
NODE_STATUSES = {"pending", "running", "passed", "failed", "blocked"}


def _duration(value: Any) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelError("verification summary contains an invalid duration")
    if value < 1000:
        return f"{value} ms"
    return f"{value / 1000:.2f} s"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelError(f"cannot read structured verification result: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != "hyperflux-verification-run-v1":
        raise ModelError("unsupported structured verification result")
    if value.get("status") not in RUN_STATUSES:
        raise ModelError("structured verification result has an invalid status")
    source = value.get("source")
    nodes = value.get("nodes")
    selection = value.get("selection")
    if not isinstance(source, dict) or REVISION.fullmatch(str(source.get("revision", ""))) is None:
        raise ModelError("structured verification result has an invalid source revision")
    if not isinstance(selection, dict) or not isinstance(selection.get("mode"), str):
        raise ModelError("structured verification result has an invalid selection")
    if not isinstance(nodes, list) or not nodes:
        raise ModelError("structured verification result has no selected nodes")
    seen: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ModelError(f"structured verification node {index} is invalid")
        identifier = node.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ModelError(f"structured verification node {index} has an invalid id")
        seen.add(identifier)
        if node.get("status") not in NODE_STATUSES:
            raise ModelError(f"structured verification node {identifier} has an invalid status")
        _duration(node.get("duration_ms"))
    return value


def render_actions_summary(
    root: Path,
    result_path: Path,
    *,
    expected_revision: str | None = None,
) -> str:
    if expected_revision is not None and REVISION.fullmatch(expected_revision) is None:
        raise ModelError("Actions summary expected revision must be a full Git commit")
    result = _load_result(result_path)
    gates = load_release_gates(root)
    gate_counts = Counter(gate.status for gate in gates)
    performance = load_performance_budgets(root)
    performance_counts = Counter(metric.status for metric in performance)

    if result is None:
        revision = expected_revision or "unavailable"
        lines = [
            "# HyperFlux Next verification",
            "",
            "| Field | Result |",
            "| --- | --- |",
            "| Status | Result unavailable |",
            f"| Source revision | `{revision}` |",
            "| Selection | Verification ended before `result.json` was emitted |",
            "",
            "> No structured node timings or failure records were available. The job remains governed by the failing workflow step.",
        ]
    else:
        revision = result["source"]["revision"]
        if expected_revision is not None and revision != expected_revision:
            raise ModelError("Actions summary source revision does not match the workflow revision")
        nodes = result["nodes"]
        failed = [node for node in nodes if node["status"] in {"failed", "blocked"}]
        generated = next(
            (node for node in nodes if node["id"] == "generated-freshness"), None
        )
        changed = result["selection"].get("changed_paths", [])
        unmatched = result["selection"].get("unmatched_paths", [])
        lines = [
            "# HyperFlux Next verification",
            "",
            "| Field | Result |",
            "| --- | --- |",
            f"| Status | **{_cell(str(result['status']).upper())}** |",
            f"| Source revision | `{revision}` |",
            f"| Lane | `{_cell(result['lane'])}` |",
            f"| Selection | `{_cell(result['selection']['mode'])}`; {len(nodes)} node(s); {len(changed)} changed path(s); {len(unmatched)} unmatched path(s) |",
            f"| Total time | {_duration(result.get('duration_ms'))} |",
            f"| Generated freshness | {_cell(generated['status'] if generated else 'not selected')} |",
            f"| Affected domains | {_cell(', '.join(sorted({node['domain'] for node in nodes})))} |",
            "",
            "## Selected nodes and timings",
            "",
            "| Node | Domain | Status | Time |",
            "| --- | --- | --- | ---: |",
        ]
        lines.extend(
            f"| `{_cell(node['id'])}` | {_cell(node['domain'])} | {_cell(node['status'])} | {_duration(node.get('duration_ms'))} |"
            for node in nodes
        )
        lines.extend(["", "## Failures", ""])
        if failed:
            lines.extend(
                f"- `{_cell(node['id'])}`: {_cell(node['title'])} ({_cell(node['status'])})"
                for node in failed
            )
        else:
            lines.append("No failed or blocked selected nodes.")

    lines.extend(
        [
            "",
            "## Performance budgets",
            "",
            f"- Software-enforced budgets: {performance_counts['enforced-software']}",
            f"- Awaiting physical measurements: {performance_counts['blocked-by-physical-evidence']}",
            "- Canonical authority: `assurance/performance-budgets.json`",
        ]
    )
    lines.extend(
        [
            "",
            "## Release-gate impact",
            "",
            "This job validates evidence but cannot mutate canonical release-gate state, authorize hardware access, or authorize publication.",
            "",
            f"- Software satisfied: {gate_counts['software-satisfied']} of {len(gates)}",
            f"- Blocked by physical evidence: {gate_counts['blocked-by-physical-evidence']}",
            f"- Blocked by lifecycle evidence: {gate_counts['blocked-by-lifecycle-evidence']}",
            f"- Publication locked: {gate_counts['publication-locked']}",
            "",
        ]
    )
    return "\n".join(lines)


def render_pages_summary(
    root: Path,
    manifest_path: Path,
    *,
    expected_revision: str | None = None,
) -> str:
    if expected_revision is not None and REVISION.fullmatch(expected_revision) is None:
        raise ModelError("Pages summary expected revision must be a full Git commit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelError(f"cannot read portal build manifest: {error}") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "hyperflux-documentation-portal-build-v2"
        or manifest.get("source_publication_state") != "public-pages-pre-release"
        or manifest.get("product_publication_authorized") is not False
        or manifest.get("external_runtime_dependencies") is not False
    ):
        raise ModelError("Pages summary refuses an unbounded portal manifest")
    files = manifest.get("files")
    pages = manifest.get("pages")
    materials = manifest.get("materials")
    if not isinstance(files, list) or not isinstance(materials, list) or not isinstance(pages, int):
        raise ModelError("Pages summary portal inventory is malformed")
    revision = expected_revision or "unavailable"
    return "\n".join(
        [
            "# HyperFlux Next documentation",
            "",
            "| Field | Result |",
            "| --- | --- |",
            "| Portal artifact | **READY FOR PAGES** |",
            f"| Source revision | `{revision}` |",
            f"| Generated pages | {pages} |",
            f"| Published files | {len(files)} |",
            f"| Canonical materials | {len(materials)} |",
            f"| Source digest | `{manifest.get('source_tree_sha256', 'unavailable')}` |",
            "| Runtime dependencies | None |",
            "| Product release authority | Locked |",
            "",
            "The Pages artifact was regenerated from canonical repository data and verified before upload. Deploying documentation does not create a package, tag, release, hardware claim, or supported-product promise.",
            "",
        ]
    )


def write_actions_summary(
    root: Path,
    result_path: Path,
    output: Path,
    *,
    expected_revision: str | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_actions_summary(
            root,
            result_path,
            expected_revision=expected_revision,
        ),
        encoding="utf-8",
    )


def write_pages_summary(
    root: Path,
    manifest_path: Path,
    output: Path,
    *,
    expected_revision: str | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_pages_summary(
            root,
            manifest_path,
            expected_revision=expected_revision,
        ),
        encoding="utf-8",
    )
