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
    "capture",
    "limits",
}
CAPTURE_KEYS = {"mode", "max_page_height"}
LIMIT_KEYS = {
    "max_documents",
    "max_markdown_bytes",
    "max_rendered_html_bytes",
    "max_issues",
    "max_screenshots",
    "max_screenshot_bytes",
    "max_result_bytes",
    "max_table_columns",
}
EXPECTED_VIEWPORTS = ((375, 812), (768, 1024), (1280, 900))
EXPECTED_THEMES = ("light", "dark")
EXPECTED_MODES = ("human", "hybrid", "generated")
EXPECTED_CAPTURE = {"mode": "full-page", "max_page_height": 32_768}
EXPECTED_SCREENSHOT_DOCUMENTS = (
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "docs/README.md",
    "docs/architecture/design-book.md",
    "docs/developer/architecture.md",
    "docs/generated/distributions.md",
    "docs/generated/installation.md",
    "docs/generated/repository-atlas.md",
    "docs/generated/supported-hardware.md",
    "docs/generated/verification.md",
    "docs/project/journey.md",
    "docs/project/release-status.md",
    "docs/user/first-run.md",
    "docs/user/hardware.md",
    "docs/user/installation.md",
    "docs/user/troubleshooting.md",
)
EXPECTED_LIMITS = {
    "max_documents": 96,
    "max_markdown_bytes": 1_048_576,
    "max_rendered_html_bytes": 4_194_304,
    "max_issues": 128,
    "max_screenshots": 108,
    "max_screenshot_bytes": 8_388_608,
    "max_result_bytes": 2_097_152,
    "max_table_columns": 8,
}
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
SCREENSHOT_MANIFEST_KEYS = {
    "$schema",
    "schema",
    "source_revision",
    "render_result_sha256",
    "screenshots",
}
SCREENSHOT_RECORD_KEYS = {
    "file",
    "path",
    "width",
    "height",
    "theme",
    "sha256",
    "bytes",
}


@dataclass(frozen=True)
class DocumentationRenderPolicy:
    render_paths: tuple[str, ...]
    viewports: tuple[tuple[int, int], ...]
    themes: tuple[str, ...]
    screenshot_documents: tuple[str, ...]
    capture_mode: str
    max_page_height: int
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
    if value["schema"] != "hyperflux-documentation-render-policy-v2":
        raise ModelError("unsupported documentation render policy schema")
    if tuple(value["render_modes"]) != EXPECTED_MODES:
        raise ModelError("documentation render policy must cover every cataloged document")
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

    capture = _exact(value["capture"], CAPTURE_KEYS, "documentation render capture")
    if capture != EXPECTED_CAPTURE:
        raise ModelError("documentation render capture must cover complete pages")

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
    if screenshot_documents != EXPECTED_SCREENSHOT_DOCUMENTS:
        raise ModelError("documentation screenshot inventory drifted")
    if not set(screenshot_documents).issubset(render_paths):
        raise ModelError("documentation screenshots must select rendered documents")

    policy = DocumentationRenderPolicy(
        render_paths=render_paths,
        viewports=tuple(viewports),
        themes=EXPECTED_THEMES,
        screenshot_documents=screenshot_documents,
        capture_mode=capture["mode"],
        max_page_height=capture["max_page_height"],
        limits=dict(limits),
    )
    if policy.screenshot_count != limits["max_screenshots"]:
        raise ModelError("documentation screenshot count does not match its exact bound")
    return policy


@dataclass(frozen=True)
class DocumentationRenderAudit:
    source_revision: str
    result_sha256: str
    document_count: int
    check_count: int
    screenshot_count: int
    capture_mode: str
    release_gate_changes: tuple[str, ...]


RELEASE_EVIDENCE_ENVIRONMENT = "HFX_RELEASE_EVIDENCE"
RELEASE_EVIDENCE_MODES = ("structure", "required")
AUDIT_SCHEMA = "hyperflux-documentation-audit-v3"
AUDIT_PATH = "assurance/documentation-audit.json"
SCREENSHOT_MANIFEST_PATH = "assurance/documentation-screenshot-manifest.json"
RENDER_CONTRACT = "github-compatible-gfm-v2"
PUBLICATION_GATE = "HFX-GATE-PUBLICATION-DECISION"
# Files the audit binds by digest, in addition to the rendered documents.
RENDER_INPUT_FILES = {
    "catalog_sha256": "docs/catalog.json",
    "policy_sha256": "docs/render-preview.json",
    "renderer_sha256": "tools/documentation_preview.cjs",
    "journey_source_coverage_sha256": "assurance/journey-source-coverage.json",
}


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


def _screenshot_files(
    policy: DocumentationRenderPolicy,
) -> dict[str, tuple[str, int, int, str]]:
    files: dict[str, tuple[str, int, int, str]] = {}
    for document in policy.screenshot_documents:
        stem = re.sub(r"[^A-Za-z0-9]+", "__", document.removesuffix(".md"))
        for width, height in policy.viewports:
            for theme in policy.themes:
                files[f"screenshots/{stem}--{width}--{theme}.png"] = (
                    document,
                    width,
                    height,
                    theme,
                )
    return files


def _documentation_screenshot_manifest(
    root: Path,
    policy: DocumentationRenderPolicy,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    value = _exact(
        load_json(root / SCREENSHOT_MANIFEST_PATH),
        SCREENSHOT_MANIFEST_KEYS,
        "documentation screenshot manifest",
    )
    if value["$schema"] != "../schemas/documentation-screenshot-manifest.schema.json":
        raise ModelError("documentation screenshot manifest has a non-canonical schema reference")
    if value["schema"] != "hyperflux-documentation-screenshot-manifest-v1":
        raise ModelError("unsupported documentation screenshot manifest schema")
    revision = value["source_revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise ModelError("documentation screenshot manifest revision is invalid")
    _digest(value["render_result_sha256"], "screenshot manifest render result")
    screenshots = value["screenshots"]
    if not isinstance(screenshots, list) or len(screenshots) != policy.screenshot_count:
        raise ModelError("documentation screenshot manifest count drifted")
    expected = _screenshot_files(policy)
    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(screenshots):
        record = _exact(raw, SCREENSHOT_RECORD_KEYS, f"screenshot manifest record {index}")
        file = record["file"]
        if not isinstance(file, str) or file not in expected or file in records:
            raise ModelError("documentation screenshot manifest file inventory drifted")
        document, width, height, theme = expected[file]
        if (
            record["path"] != document
            or record["width"] != width
            or record["height"] != height
            or record["theme"] != theme
        ):
            raise ModelError("documentation screenshot manifest dimensions drifted")
        _digest(record["sha256"], f"screenshot manifest {file}")
        _positive_integer(
            record["bytes"],
            f"screenshot manifest {file} bytes",
            policy.limits["max_screenshot_bytes"],
        )
        records[file] = record
    if list(records) != sorted(expected) or set(records) != set(expected):
        raise ModelError("documentation screenshot manifest ordering drifted")
    return value, records


def documentation_render_inputs(
    root: Path, policy: DocumentationRenderPolicy
) -> dict[str, str]:
    """Return the input digests a render of this tree would be bound to."""
    inputs = {
        key: sha256_file(root / relative) for key, relative in RENDER_INPUT_FILES.items()
    }
    inputs["rendered_documents_sha256"] = rendered_documents_sha256(root, policy)
    return inputs


def expected_release_gate_changes(root: Path) -> list[str]:
    authorized = load_json(root / "architecture" / "constitution.json")[
        "publication_interlock"
    ]["publication_authorized"]
    return [PUBLICATION_GATE] if authorized else []


def validate_documentation_render_audit(
    root: Path,
    value: Any,
    *,
    release_evidence: bool = False,
) -> DocumentationRenderAudit:
    """Validate the committed render record.

    The record is made once per release. Every pull request checks its structure, its
    binding to the screenshot manifest, the visual review and the publication interlock.
    Only release mode also requires it to describe the documents in this exact tree.
    """
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
            "publication",
        },
        "documentation audit",
    )
    if audit["$schema"] != "../schemas/documentation-audit.schema.json":
        raise ModelError("documentation audit has a non-canonical schema reference")
    if audit["schema"] != AUDIT_SCHEMA:
        raise ModelError("unsupported documentation audit schema")
    revision = audit["source_revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise ModelError("documentation audit source revision is invalid")

    policy = load_documentation_render_policy(root)
    screenshot_manifest, screenshot_records = _documentation_screenshot_manifest(
        root, policy
    )
    inputs = _exact(
        audit["inputs"],
        {*RENDER_INPUT_FILES, "rendered_documents_sha256"},
        "documentation audit inputs",
    )
    for key, digest in inputs.items():
        _digest(digest, key)
    if release_evidence:
        for key, expected in documentation_render_inputs(root, policy).items():
            if inputs[key] != expected:
                raise ModelError(
                    f"documentation audit {key} is stale; render the documentation "
                    "and run ./hfx docs bind for this release"
                )

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
            "capture_mode",
            "max_page_height",
            "render_contract",
            "runtime",
        },
        "documentation audit render",
    )
    if render["status"] != "passed":
        raise ModelError("documentation render audit did not pass")
    document_count = _positive_integer(
        render["document_count"], "document count", policy.limits["max_documents"]
    )
    if render["check_count"] != document_count * len(policy.viewports) * len(
        policy.themes
    ):
        raise ModelError("documentation render check count does not cover every document")
    if release_evidence and (
        document_count != len(policy.render_paths)
        or render["check_count"] != policy.check_count
    ):
        raise ModelError("documentation render document count is stale for this release")
    if render["screenshot_count"] != policy.screenshot_count:
        raise ModelError("documentation render screenshot count drifted")
    if (
        render["capture_mode"] != policy.capture_mode
        or render["max_page_height"] != policy.max_page_height
    ):
        raise ModelError("documentation render capture coverage drifted")
    if render["render_contract"] != RENDER_CONTRACT:
        raise ModelError("documentation render compatibility contract drifted")
    for field in ("issue_count", "outbound_network_requests"):
        if render[field] != 0:
            raise ModelError(f"documentation render {field} must be zero")
    if (
        render["viewport_widths"] != [width for width, _height in policy.viewports]
        or tuple(render["themes"]) != policy.themes
    ):
        raise ModelError("documentation render viewport or theme coverage drifted")
    _digest(render["result_sha256"], "result")
    if _digest(
        render["screenshot_manifest_sha256"], "screenshot manifest"
    ) != sha256_file(root / SCREENSHOT_MANIFEST_PATH):
        raise ModelError("documentation audit screenshot manifest is stale")
    if (
        screenshot_manifest["source_revision"] != revision
        or screenshot_manifest["render_result_sha256"] != render["result_sha256"]
    ):
        raise ModelError("documentation screenshot manifest source binding drifted")
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
    if not isinstance(reviewed, list) or len(reviewed) != len(
        policy.screenshot_documents
    ):
        raise ModelError("documentation visual review must cover every critical document")
    allowed_files = _screenshot_files(policy)
    seen: set[str] = set()
    reviewed_documents: set[str] = set()
    reviewed_widths: set[int] = set()
    reviewed_themes: set[str] = set()
    for index, raw in enumerate(reviewed):
        record = _exact(raw, {"file", "sha256"}, f"reviewed screenshot {index}")
        file = record["file"]
        if not isinstance(file, str) or file not in allowed_files or file in seen:
            raise ModelError("documentation visual review has an invalid screenshot")
        seen.add(file)
        document, width, _height, theme = allowed_files[file]
        if document in reviewed_documents:
            raise ModelError("documentation visual review repeats a critical document")
        reviewed_documents.add(document)
        reviewed_widths.add(width)
        reviewed_themes.add(theme)
        if _digest(
            record["sha256"], f"reviewed screenshot {file}"
        ) != screenshot_records[file]["sha256"]:
            raise ModelError(
                f"documentation visual review of {file} does not match the screenshot "
                "manifest; look at the new screenshot and run ./hfx docs review"
            )
    if (
        reviewed_documents != set(policy.screenshot_documents)
        or reviewed_widths != {width for width, _height in policy.viewports}
        or reviewed_themes != set(policy.themes)
    ):
        raise ModelError("documentation visual review coverage drifted")

    publication = _exact(
        audit["publication"], {"release_gate_changes"}, "documentation audit publication"
    )
    if publication["release_gate_changes"] != expected_release_gate_changes(root):
        raise ModelError("documentation audit publication boundary drifted")
    return DocumentationRenderAudit(
        source_revision=revision,
        result_sha256=render["result_sha256"],
        document_count=document_count,
        check_count=render["check_count"],
        screenshot_count=render["screenshot_count"],
        capture_mode=render["capture_mode"],
        release_gate_changes=tuple(publication["release_gate_changes"]),
    )


def load_documentation_render_audit(
    root: Path, *, release_evidence: bool = False
) -> DocumentationRenderAudit:
    return validate_documentation_render_audit(
        root,
        load_json(root / AUDIT_PATH),
        release_evidence=release_evidence,
    )
