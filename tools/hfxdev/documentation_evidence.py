# SPDX-License-Identifier: GPL-2.0-only
"""Check, bind and review documentation render evidence.

A render is a directory written by tools/documentation_preview.cjs: result.json plus
screenshots/. `check_render` proves a render describes this exact tree and found no
issue. `bind` copies a checked render into the committed release record. `review`
records, file by file, that a person looked at the critical screenshots that changed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .documentation_preview import (
    AUDIT_PATH,
    AUDIT_SCHEMA,
    RENDER_CONTRACT,
    RENDER_INPUT_FILES,
    SCREENSHOT_MANIFEST_PATH,
    DocumentationRenderPolicy,
    _screenshot_files,
    documentation_render_inputs,
    expected_release_gate_changes,
    load_documentation_render_policy,
    validate_documentation_render_audit,
)
from .model import ModelError, load_json, sha256_file
from .verification_run import git_changed_paths


RESULT_SCHEMA = "hyperflux-documentation-render-result-v2"
RESULT_KEYS = {
    "schema",
    "render_contract",
    "status",
    "policy_sha256",
    "catalog_sha256",
    "renderer_sha256",
    "environment",
    "counts",
    "capture",
    "network",
    "documents",
    "checks",
    "screenshots",
}
RUNTIME_KEYS = ("node", "playwright", "marked", "chromium")
VERSION = re.compile(r"[A-Za-z0-9.+-]{1,64}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
SELECTION_SCHEMA = "hyperflux-documentation-render-selection-v1"
DEFAULT_CHROMIUM = "/usr/bin/chromium"
DEFAULT_NODE_MODULES = ".hfx/documentation-render/node_modules"
DEFAULT_RENDER_TIMEOUT_SECONDS = 2700
# Changing any of these can change what the renderer produces, in addition to the
# rendered documents and RENDER_INPUT_FILES.
RENDER_ENVIRONMENT_FILES = (
    ".devcontainer/Containerfile",
    "toolchains/development-environment.json",
    "toolchains/documentation-render/package.json",
    "toolchains/documentation-render/package-lock.json",
    "tools/hfxdev/documentation_evidence.py",
    "tools/hfxdev/documentation_preview.py",
)


@dataclass(frozen=True)
class CheckedRender:
    directory: Path
    result: dict[str, Any]
    result_sha256: str
    screenshots: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RenderSelection:
    needed: bool
    reason: str
    base_revision: str | None
    changed_inputs: tuple[str, ...]


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label} has missing or unknown fields")
    return value


def _regular_file(path: Path, label: str, max_bytes: int) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ModelError(f"{label} is missing or is not a regular file")
    if path.stat().st_size > max_bytes:
        raise ModelError(f"{label} exceeds its byte bound")
    return path


def check_render(root: Path, render: Path) -> CheckedRender:
    """Fail unless `render` is a clean render of exactly this tree."""
    root = root.resolve()
    render = render.resolve()
    policy = load_documentation_render_policy(root)
    result_path = _regular_file(
        render / "result.json", "render result", policy.limits["max_result_bytes"]
    )
    result = _exact(load_json(result_path), RESULT_KEYS, "render result")
    if result["schema"] != RESULT_SCHEMA:
        raise ModelError("render result schema is unsupported")
    if result["render_contract"] != RENDER_CONTRACT:
        raise ModelError("render result uses another render contract")
    if result["status"] != "passed":
        raise ModelError("render did not pass")

    tree = {
        "policy_sha256": RENDER_INPUT_FILES["policy_sha256"],
        "catalog_sha256": RENDER_INPUT_FILES["catalog_sha256"],
        "renderer_sha256": RENDER_INPUT_FILES["renderer_sha256"],
    }
    for key, relative in tree.items():
        if result[key] != sha256_file(root / relative):
            raise ModelError(f"render used another {relative} than this tree")

    environment = _exact(result["environment"], set(RUNTIME_KEYS), "render environment")
    for name, version in environment.items():
        if not isinstance(version, str) or VERSION.fullmatch(version) is None:
            raise ModelError(f"render environment {name} version is invalid")

    counts = _exact(
        result["counts"], {"documents", "checks", "screenshots", "issues"}, "render counts"
    )
    if counts["issues"] != 0:
        raise ModelError(f"render found {counts['issues']} layout issue(s)")
    if (
        counts["documents"] != len(policy.render_paths)
        or counts["checks"] != policy.check_count
        or counts["screenshots"] != policy.screenshot_count
    ):
        raise ModelError("render does not cover every document, viewport and theme")
    capture = _exact(result["capture"], {"mode", "max_page_height"}, "render capture")
    if capture != {"mode": policy.capture_mode, "max_page_height": policy.max_page_height}:
        raise ModelError("render capture differs from the policy")
    network = _exact(
        result["network"], {"blocked_requests", "placeholder_images"}, "render network"
    )
    if network["blocked_requests"] != 0:
        raise ModelError(
            f"render attempted {network['blocked_requests']} outbound network request(s)"
        )
    placeholders = network["placeholder_images"]
    if isinstance(placeholders, bool) or not isinstance(placeholders, int) or not 1 <= placeholders <= 64:
        raise ModelError("render placeholder image count is outside its bound")

    _check_documents(root, policy, result["documents"])
    _check_checks(policy, result["checks"])
    screenshots = _check_screenshots(render, policy, result["screenshots"])
    return CheckedRender(render, result, sha256_file(result_path), screenshots)


def _check_documents(root: Path, policy: DocumentationRenderPolicy, documents: Any) -> None:
    if not isinstance(documents, list):
        raise ModelError("render document list is invalid")
    paths = []
    for index, raw in enumerate(documents):
        record = _exact(raw, {"path", "sha256", "bytes"}, f"render document {index}")
        paths.append(record["path"])
        if record["path"] not in policy.render_paths:
            raise ModelError(f"render includes an unexpected document: {record['path']}")
        source = root / record["path"]
        if record["sha256"] != sha256_file(source) or record["bytes"] != source.stat().st_size:
            raise ModelError(
                f"render used another version of {record['path']} than this tree"
            )
    if tuple(paths) != policy.render_paths:
        raise ModelError("render document inventory differs from the catalog")


def _check_checks(policy: DocumentationRenderPolicy, checks: Any) -> None:
    if not isinstance(checks, list) or len(checks) != policy.check_count:
        raise ModelError("render check list does not match the policy")
    expected = {
        (path, width, height, theme)
        for path in policy.render_paths
        for width, height in policy.viewports
        for theme in policy.themes
    }
    seen = set()
    for index, raw in enumerate(checks):
        record = _exact(
            raw, {"path", "width", "height", "theme", "issues", "metrics"}, f"render check {index}"
        )
        key = (record["path"], record["width"], record["height"], record["theme"])
        if key not in expected or key in seen:
            raise ModelError("render check inventory differs from the policy")
        seen.add(key)
        if record["issues"] != []:
            raise ModelError(
                f"render found issues in {record['path']} at {record['width']} px "
                f"{record['theme']}: {record['issues']}"
            )
    if seen != expected:
        raise ModelError("render check inventory differs from the policy")


def _check_screenshots(
    render: Path, policy: DocumentationRenderPolicy, screenshots: Any
) -> dict[str, dict[str, Any]]:
    expected = _screenshot_files(policy)
    if not isinstance(screenshots, list) or len(screenshots) != len(expected):
        raise ModelError("render screenshot list does not match the policy")
    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(screenshots):
        record = _exact(
            raw,
            {"path", "width", "height", "theme", "file", "sha256", "bytes"},
            f"render screenshot {index}",
        )
        file = record["file"]
        if not isinstance(file, str) or file not in expected or file in records:
            raise ModelError("render screenshot inventory differs from the policy")
        document, width, height, theme = expected[file]
        if (record["path"], record["width"], record["height"], record["theme"]) != (
            document,
            width,
            height,
            theme,
        ):
            raise ModelError(f"render screenshot {file} has the wrong page or viewport")
        image = _regular_file(
            render / file, f"render screenshot {file}", policy.limits["max_screenshot_bytes"]
        )
        if (
            not isinstance(record["sha256"], str)
            or DIGEST.fullmatch(record["sha256"]) is None
            or sha256_file(image) != record["sha256"]
            or image.stat().st_size != record["bytes"]
        ):
            raise ModelError(f"render screenshot {file} does not match result.json")
        records[file] = record
    return records


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git_head(root: Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot read the source revision: {error}") from error
    if REVISION.fullmatch(revision) is None:
        raise ModelError("source revision is not a Git commit")
    return revision


@dataclass(frozen=True)
class BindOutcome:
    changed_screenshots: tuple[str, ...]
    pending_review: tuple[str, ...]


def bind(root: Path, render: Path, revision: str | None = None) -> BindOutcome:
    """Write a checked render into the committed release record.

    The visual review is left exactly as it was. Critical screenshots whose bytes
    changed stay unreviewed, and the record fails validation, until `review` records
    that a person looked at each of them.
    """
    root = root.resolve()
    checked = check_render(root, render)
    revision = _git_head(root) if revision is None else revision
    if REVISION.fullmatch(revision) is None:
        raise ModelError("bind revision must be a 40-character Git commit")
    policy = load_documentation_render_policy(root)
    manifest_path = root / SCREENSHOT_MANIFEST_PATH
    audit_path = root / AUDIT_PATH
    old_manifest = load_json(manifest_path)
    old_hashes = {
        record.get("file"): record.get("sha256")
        for record in old_manifest.get("screenshots", [])
        if isinstance(record, dict)
    }
    manifest = {
        "$schema": "../schemas/documentation-screenshot-manifest.schema.json",
        "schema": "hyperflux-documentation-screenshot-manifest-v1",
        "source_revision": revision,
        "render_result_sha256": checked.result_sha256,
        "screenshots": [
            {
                "file": file,
                "path": record["path"],
                "width": record["width"],
                "height": record["height"],
                "theme": record["theme"],
                "sha256": record["sha256"],
                "bytes": record["bytes"],
            }
            for file, record in sorted(checked.screenshots.items())
        ],
    }
    changed = tuple(
        file
        for file, record in sorted(checked.screenshots.items())
        if old_hashes.get(file) != record["sha256"]
    )

    audit = load_json(audit_path)
    visual_review = audit.get("visual_review") if isinstance(audit, dict) else None
    if not isinstance(visual_review, dict):
        raise ModelError("the committed documentation audit has no visual review to keep")
    result = checked.result
    viewports = [width for width, _height in policy.viewports]
    new_audit = {
        "$schema": "../schemas/documentation-audit.schema.json",
        "schema": AUDIT_SCHEMA,
        "source_revision": revision,
        "inputs": documentation_render_inputs(root, policy),
        "render": {
            "status": "passed",
            "result_sha256": checked.result_sha256,
            "document_count": result["counts"]["documents"],
            "check_count": result["counts"]["checks"],
            "screenshot_count": result["counts"]["screenshots"],
            "issue_count": result["counts"]["issues"],
            "outbound_network_requests": result["network"]["blocked_requests"],
            "external_image_placeholders": result["network"]["placeholder_images"],
            "viewport_widths": viewports,
            "themes": list(policy.themes),
            "screenshot_manifest_sha256": "",
            "capture_mode": policy.capture_mode,
            "max_page_height": policy.max_page_height,
            "render_contract": RENDER_CONTRACT,
            "runtime": {name: result["environment"][name] for name in RUNTIME_KEYS},
        },
        "visual_review": visual_review,
        "publication": {"release_gate_changes": expected_release_gate_changes(root)},
    }
    _write_json(manifest_path, manifest)
    new_audit["render"]["screenshot_manifest_sha256"] = sha256_file(manifest_path)
    _write_json(audit_path, new_audit)
    return BindOutcome(changed, pending_review(root))


def pending_review(root: Path) -> tuple[str, ...]:
    """Critical screenshots whose reviewed digest differs from the manifest."""
    root = root.resolve()
    manifest = load_json(root / SCREENSHOT_MANIFEST_PATH)
    current = {record["file"]: record["sha256"] for record in manifest["screenshots"]}
    audit = load_json(root / AUDIT_PATH)
    return tuple(
        record["file"]
        for record in audit["visual_review"]["reviewed_screenshots"]
        if current.get(record["file"]) != record["sha256"]
    )


def review(root: Path, render: Path, confirm: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Record a person's review of named screenshots; return those still pending.

    Nothing is marked without being named in `confirm`, and only screenshots that
    changed since their last review may be named.
    """
    root = root.resolve()
    render = render.resolve()
    manifest = load_json(root / SCREENSHOT_MANIFEST_PATH)
    result_path = render / "result.json"
    if (
        result_path.is_symlink()
        or not result_path.is_file()
        or sha256_file(result_path) != manifest["render_result_sha256"]
    ):
        raise ModelError(
            "this render is not the one bound in the screenshot manifest; "
            "run ./hfx docs bind with it first"
        )
    pending = pending_review(root)
    if not confirm:
        return pending
    if len(set(confirm)) != len(confirm):
        raise ModelError("each reviewed screenshot may be named only once")
    unknown = sorted(set(confirm) - set(pending))
    if unknown:
        raise ModelError(
            "only changed, unreviewed critical screenshots can be confirmed: "
            + ", ".join(unknown)
        )
    current = {record["file"]: record["sha256"] for record in manifest["screenshots"]}
    for file in confirm:
        image = render / file
        if image.is_symlink() or not image.is_file() or sha256_file(image) != current[file]:
            raise ModelError(f"{file} in the render differs from the screenshot manifest")
    audit_path = root / AUDIT_PATH
    audit = load_json(audit_path)
    for record in audit["visual_review"]["reviewed_screenshots"]:
        if record["file"] in confirm:
            record["sha256"] = current[record["file"]]
    _write_json(audit_path, audit)
    return pending_review(root)


def render_inputs(root: Path) -> tuple[str, ...]:
    policy = load_documentation_render_policy(root)
    return tuple(
        sorted(
            {
                *policy.render_paths,
                *RENDER_INPUT_FILES.values(),
                *RENDER_ENVIRONMENT_FILES,
            }
        )
    )


def render_selection(root: Path, changed_from: str | None) -> RenderSelection:
    """Decide whether a render is needed; anything unknown selects the render."""
    if changed_from is None or changed_from == "" or changed_from == "0" * 40:
        return RenderSelection(True, "no base revision", None, ())
    try:
        base, changed = git_changed_paths(root, changed_from)
    except ModelError:
        return RenderSelection(True, "base revision is unavailable", None, ())
    inputs = set(render_inputs(root))
    selected = tuple(path for path in changed if path in inputs)
    if selected:
        return RenderSelection(True, "render inputs changed", base, selected)
    return RenderSelection(False, "no render input changed", base, ())


def _summary(
    root: Path,
    selection: RenderSelection,
    checked: CheckedRender | None,
    error: str | None,
) -> str:
    lines = ["# Documentation render", ""]
    if not selection.needed:
        lines.append(f"Skipped: {selection.reason} since `{selection.base_revision}`.")
        return "\n".join(lines) + "\n"
    lines.append(f"Selected: {selection.reason}.")
    if selection.changed_inputs:
        shown = selection.changed_inputs[:20]
        lines.extend(["", "Changed render inputs:", ""])
        lines.extend(f"- `{path}`" for path in shown)
        if len(selection.changed_inputs) > len(shown):
            lines.append(f"- and {len(selection.changed_inputs) - len(shown)} more")
    lines.append("")
    if checked is None:
        lines.append(f"Result: FAILED. {error}")
        return "\n".join(lines) + "\n"
    counts = checked.result["counts"]
    runtime = checked.result["environment"]
    lines.append(
        f"Result: PASSED. {counts['documents']} documents, {counts['checks']} checks, "
        f"{counts['screenshots']} screenshots, 0 issues, 0 outbound requests."
    )
    lines.append(
        f"Runtime: Node {runtime['node']}, Playwright {runtime['playwright']}, "
        f"marked {runtime['marked']}, Chromium {runtime['chromium']}."
    )
    try:
        manifest = load_json(root / SCREENSHOT_MANIFEST_PATH)
        audit = load_json(root / AUDIT_PATH)
        recorded = {record["file"]: record["sha256"] for record in manifest["screenshots"]}
        reviewed = [record["file"] for record in audit["visual_review"]["reviewed_screenshots"]]
    except (ModelError, KeyError, TypeError):
        reviewed, recorded = [], {}
    differing = [
        file for file in reviewed if checked.screenshots[file]["sha256"] != recorded.get(file)
    ]
    lines.extend(["", "Critical screenshots that differ from the release record:", ""])
    if differing:
        lines.extend(f"- `{file}`" for file in differing)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def run_render(
    root: Path,
    output: Path,
    *,
    changed_from: str | None = None,
    chromium: str = DEFAULT_CHROMIUM,
    node_modules: Path | None = None,
    timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> int:
    """Render when inputs changed, check the result, and write a short summary."""
    root = root.resolve()
    output = output if output.is_absolute() else root / output
    if output.exists() or output.is_symlink():
        raise ModelError("documentation render output must not exist yet")
    output.mkdir(parents=True)
    selection = render_selection(root, changed_from)
    _write_json(
        output / "selection.json",
        {
            "schema": SELECTION_SCHEMA,
            "needed": selection.needed,
            "reason": selection.reason,
            "base_revision": selection.base_revision,
            "changed_inputs": list(selection.changed_inputs),
        },
    )
    checked: CheckedRender | None = None
    error: str | None = None
    if selection.needed:
        node = shutil.which("node")
        modules = node_modules if node_modules is not None else root / DEFAULT_NODE_MODULES
        if node is None:
            error = "Node.js is unavailable"
        elif not (modules / "playwright").is_dir() or not (modules / "marked").is_dir():
            error = f"render packages are missing from {modules}; run the render prepare step"
        else:
            command = [
                node,
                str(root / RENDER_INPUT_FILES["renderer_sha256"]),
                "--root",
                str(root),
                "--output",
                str(output / "render"),
                "--chromium",
                chromium,
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env={**os.environ, "NODE_PATH": str(modules.resolve())},
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                error = f"the renderer did not finish within {timeout_seconds} seconds"
            except OSError as failure:
                error = f"cannot run the renderer: {failure}"
            else:
                try:
                    checked = check_render(root, output / "render")
                except ModelError as failure:
                    error = str(failure)
                if checked is None and completed.returncode != 0:
                    error = f"renderer exited with status {completed.returncode}: {error}"
    (output / "summary.md").write_text(
        _summary(root, selection, checked, error), encoding="utf-8"
    )
    if selection.needed and checked is None:
        raise ModelError(f"documentation render failed: {error}")
    return 0


def validate_release_record(root: Path) -> None:
    validate_documentation_render_audit(
        root, load_json(root / AUDIT_PATH), release_evidence=True
    )
