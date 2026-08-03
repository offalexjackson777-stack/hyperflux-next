# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .documentation import load_document_catalog
from .model import ModelError, load_json, sha256_file


POLICY_KEYS = {
    "$schema",
    "schema",
    "render_modes",
    "viewports",
    "themes",
    "screenshot_documents",
    "limits",
}
LIMIT_KEYS = {
    "max_documents",
    "max_markdown_bytes",
    "max_rendered_html_bytes",
    "max_issues",
    "max_screenshots",
    "max_screenshot_bytes",
    "max_result_bytes",
}
EXPECTED_VIEWPORTS = ((375, 812), (768, 1024), (1280, 900))
EXPECTED_THEMES = ("light", "dark")
EXPECTED_MODES = ("human", "hybrid")
EXPECTED_LIMITS = {
    "max_documents": 64,
    "max_markdown_bytes": 1_048_576,
    "max_rendered_html_bytes": 4_194_304,
    "max_issues": 128,
    "max_screenshots": 36,
    "max_screenshot_bytes": 2_097_152,
    "max_result_bytes": 2_097_152,
}
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class DocumentationRenderPolicy:
    render_paths: tuple[str, ...]
    viewports: tuple[tuple[int, int], ...]
    themes: tuple[str, ...]
    screenshot_documents: tuple[str, ...]
    limits: dict[str, int]

    @property
    def check_count(self) -> int:
        return len(self.render_paths) * len(self.viewports) * len(self.themes)

    @property
    def screenshot_count(self) -> int:
        return len(self.screenshot_documents) * len(self.viewports) * len(self.themes)


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label} has missing or unknown fields")
    return value


def _paths(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ModelError(f"{label} must be a non-empty array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256:
            raise ModelError(f"{label} contains an invalid path")
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != item:
            raise ModelError(f"{label} contains a path outside the repository")
        if item in result:
            raise ModelError(f"{label} contains duplicate paths")
        result.append(item)
    return tuple(result)


def load_documentation_render_policy(root: Path) -> DocumentationRenderPolicy:
    root = root.resolve()
    value = _exact(
        load_json(root / "docs" / "render-preview.json"),
        POLICY_KEYS,
        "documentation render policy",
    )
    if value["$schema"] != "../schemas/documentation-render-policy.schema.json":
        raise ModelError("documentation render policy has a non-canonical schema reference")
    if value["schema"] != "hyperflux-documentation-render-policy-v1":
        raise ModelError("unsupported documentation render policy schema")
    if tuple(value["render_modes"]) != EXPECTED_MODES:
        raise ModelError("documentation render policy must cover human and hybrid documents")
    if tuple(value["themes"]) != EXPECTED_THEMES:
        raise ModelError("documentation render policy must cover light and dark themes")

    raw_viewports = value["viewports"]
    if not isinstance(raw_viewports, list):
        raise ModelError("documentation render policy viewports must be an array")
    viewports: list[tuple[int, int]] = []
    for index, raw in enumerate(raw_viewports):
        viewport = _exact(raw, {"width", "height"}, f"render viewport {index}")
        width = viewport["width"]
        height = viewport["height"]
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or isinstance(height, bool)
            or not isinstance(height, int)
        ):
            raise ModelError("documentation render viewport dimensions must be integers")
        viewports.append((width, height))
    if tuple(viewports) != EXPECTED_VIEWPORTS:
        raise ModelError("documentation render policy viewport inventory drifted")

    limits = _exact(value["limits"], LIMIT_KEYS, "documentation render limits")
    if limits != EXPECTED_LIMITS:
        raise ModelError("documentation render limits drifted")

    catalog = load_document_catalog(root)
    render_paths = tuple(
        document.path
        for document in catalog.documents
        if document.mode in EXPECTED_MODES
    )
    if not render_paths or len(render_paths) > limits["max_documents"]:
        raise ModelError("documentation render inventory exceeds its bound")
    screenshot_documents = _paths(
        value["screenshot_documents"], "documentation screenshot inventory"
    )
    if tuple(sorted(screenshot_documents)) != screenshot_documents:
        raise ModelError("documentation screenshot inventory must be sorted")
    if not set(screenshot_documents).issubset(render_paths):
        raise ModelError("documentation screenshots must select rendered documents")

    policy = DocumentationRenderPolicy(
        render_paths=render_paths,
        viewports=tuple(viewports),
        themes=EXPECTED_THEMES,
        screenshot_documents=screenshot_documents,
        limits=dict(limits),
    )
    if policy.screenshot_count != limits["max_screenshots"]:
        raise ModelError("documentation screenshot count does not match its exact bound")
    return policy


@dataclass(frozen=True)
class DocumentationRenderAudit:
    source_revision: str
    result_sha256: str
    check_count: int
    screenshot_count: int
    fast_run_id: str
    full_run_id: str
    full_source_revision: str


def rendered_documents_sha256(root: Path, policy: DocumentationRenderPolicy) -> str:
    digest = hashlib.sha256()
    for relative in policy.render_paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(root / relative)))
        digest.update(b"\0")
    return digest.hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ModelError(f"documentation audit {label} is not a SHA-256 digest")
    return value


def _positive_integer(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ModelError(f"documentation audit {label} is outside its bound")
    return value


def _screenshot_files(policy: DocumentationRenderPolicy) -> set[str]:
    files: set[str] = set()
    for document in policy.screenshot_documents:
        stem = re.sub(r"[^A-Za-z0-9]+", "__", document.removesuffix(".md"))
        for width, _height in policy.viewports:
            for theme in policy.themes:
                files.add(f"screenshots/{stem}--{width}--{theme}.png")
    return files


def validate_documentation_render_audit(
    root: Path,
    value: Any,
) -> DocumentationRenderAudit:
    root = root.resolve()
    audit = _exact(
        value,
        {
            "$schema",
            "schema",
            "source_revision",
            "inputs",
            "render",
            "visual_review",
            "verification",
        },
        "documentation audit",
    )
    if audit["$schema"] != "../schemas/documentation-audit.schema.json":
        raise ModelError("documentation audit has a non-canonical schema reference")
    if audit["schema"] != "hyperflux-documentation-audit-v1":
        raise ModelError("unsupported documentation audit schema")
    revision = audit["source_revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise ModelError("documentation audit source revision is invalid")

    policy = load_documentation_render_policy(root)
    inputs = _exact(
        audit["inputs"],
        {
            "catalog_sha256",
            "policy_sha256",
            "renderer_sha256",
            "journey_source_coverage_sha256",
            "rendered_documents_sha256",
        },
        "documentation audit inputs",
    )
    expected_inputs = {
        "catalog_sha256": sha256_file(root / "docs" / "catalog.json"),
        "policy_sha256": sha256_file(root / "docs" / "render-preview.json"),
        "renderer_sha256": sha256_file(root / "tools" / "documentation_preview.cjs"),
        "journey_source_coverage_sha256": sha256_file(
            root / "assurance" / "journey-source-coverage.json"
        ),
        "rendered_documents_sha256": rendered_documents_sha256(root, policy),
    }
    for key, expected in expected_inputs.items():
        if _digest(inputs[key], key) != expected:
            raise ModelError(f"documentation audit {key} is stale")

    render = _exact(
        audit["render"],
        {
            "status",
            "result_sha256",
            "document_count",
            "check_count",
            "screenshot_count",
            "issue_count",
            "outbound_network_requests",
            "external_image_placeholders",
            "viewport_widths",
            "themes",
            "screenshot_manifest_sha256",
            "runtime",
        },
        "documentation audit render",
    )
    if render["status"] != "passed":
        raise ModelError("documentation render audit did not pass")
    if render["document_count"] != len(policy.render_paths):
        raise ModelError("documentation render document count drifted")
    if render["check_count"] != policy.check_count:
        raise ModelError("documentation render check count drifted")
    if render["screenshot_count"] != policy.screenshot_count:
        raise ModelError("documentation render screenshot count drifted")
    for field in ("issue_count", "outbound_network_requests"):
        if render[field] != 0:
            raise ModelError(f"documentation render {field} must be zero")
    if (
        render["viewport_widths"] != [width for width, _height in policy.viewports]
        or tuple(render["themes"]) != policy.themes
    ):
        raise ModelError("documentation render viewport or theme coverage drifted")
    _digest(render["result_sha256"], "result")
    _digest(render["screenshot_manifest_sha256"], "screenshot manifest")
    _positive_integer(render["external_image_placeholders"], "placeholder count", 64)
    runtime = _exact(
        render["runtime"], {"node", "playwright", "marked", "chromium"}, "render runtime"
    )
    for name, version in runtime.items():
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 64
            or re.fullmatch(r"[A-Za-z0-9.+-]+", version) is None
        ):
            raise ModelError(f"documentation render runtime {name} is invalid")

    visual = _exact(
        audit["visual_review"], {"status", "reviewed_screenshots"}, "visual review"
    )
    if visual["status"] != "passed":
        raise ModelError("documentation visual review did not pass")
    reviewed = visual["reviewed_screenshots"]
    if not isinstance(reviewed, list) or len(reviewed) != 5:
        raise ModelError("documentation visual review must bind five screenshots")
    allowed_files = _screenshot_files(policy)
    seen: set[str] = set()
    for index, raw in enumerate(reviewed):
        record = _exact(raw, {"file", "sha256"}, f"reviewed screenshot {index}")
        file = record["file"]
        if not isinstance(file, str) or file not in allowed_files or file in seen:
            raise ModelError("documentation visual review has an invalid screenshot")
        seen.add(file)
        _digest(record["sha256"], f"reviewed screenshot {file}")

    verification = _exact(
        audit["verification"],
        {
            "fast_run_id",
            "fast_evidence_sha256",
            "fast_result_sha256",
            "full_run_id",
            "full_source_revision",
            "full_evidence_sha256",
            "full_result_sha256",
            "second_generation_identical",
            "links",
            "privacy",
            "release_gate_changes",
        },
        "documentation audit verification",
    )
    run_id = verification["fast_run_id"]
    if not isinstance(run_id, str) or re.fullmatch(r"hfxv-[0-9a-f]{20}", run_id) is None:
        raise ModelError("documentation audit Fast run id is invalid")
    _digest(verification["fast_evidence_sha256"], "Fast evidence")
    _digest(verification["fast_result_sha256"], "Fast result")
    full_run_id = verification["full_run_id"]
    full_revision = verification["full_source_revision"]
    if (
        not isinstance(full_run_id, str)
        or re.fullmatch(r"hfxv-[0-9a-f]{20}", full_run_id) is None
        or not isinstance(full_revision, str)
        or REVISION.fullmatch(full_revision) is None
    ):
        raise ModelError("documentation audit Full run identity is invalid")
    _digest(verification["full_evidence_sha256"], "Full evidence")
    _digest(verification["full_result_sha256"], "Full result")
    if (
        verification["second_generation_identical"] is not True
        or verification["links"] != "passed"
        or verification["privacy"] != "passed"
        or verification["release_gate_changes"] != []
    ):
        raise ModelError("documentation audit verification boundary drifted")
    return DocumentationRenderAudit(
        source_revision=revision,
        result_sha256=render["result_sha256"],
        check_count=render["check_count"],
        screenshot_count=render["screenshot_count"],
        fast_run_id=run_id,
        full_run_id=full_run_id,
        full_source_revision=full_revision,
    )


def load_documentation_render_audit(root: Path) -> DocumentationRenderAudit:
    return validate_documentation_render_audit(
        root,
        load_json(root / "assurance" / "documentation-audit.json"),
    )
