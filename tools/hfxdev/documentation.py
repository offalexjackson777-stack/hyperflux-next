# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Callable

from .model import ModelError, load_json, repository_files, require_unique


CATALOG_KEYS = {"$schema", "schema", "documents", "classified_markdown"}
DOCUMENT_KEYS = {
    "path",
    "title",
    "audience",
    "purpose",
    "owner",
    "navigation_group",
    "mode",
    "canonical_sources",
    "generated_block_ids",
}
AUDIENCES = {
    "users",
    "developers",
    "contributors",
    "maintainers",
    "project-followers",
    "mixed",
}
NAVIGATION_GROUPS = {
    "home",
    "user",
    "developer",
    "project",
    "generated",
    "architecture",
    "assurance",
    "legal",
}
MODES = {"human", "hybrid", "generated"}
MARKDOWN_CLASSIFICATIONS = {
    "evidence-record",
    "generated-front-door",
    "repository-template",
    "vendored-component-note",
}
CLASSIFIED_MARKDOWN_KEYS = {"path", "classification"}
GENERATED_BLOCK_IDS = (
    "release-status",
    "recommended-package",
    "compatibility-summary",
)
BEGIN_RE = re.compile(r"^<!-- HFX-GENERATED-BEGIN: ([a-z][a-z0-9-]*) -->$")
END_RE = re.compile(r"^<!-- HFX-GENERATED-END: ([a-z][a-z0-9-]*) -->$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
RAW_HTML_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)\b[^>]*>")
INLINE_CODE_RE = re.compile(r"(`+)(.*?)\1")
ALLOWED_RAW_HTML_TAGS = {"details", "summary", "strong"}
PUBLIC_PROCESS_PATTERNS = (
    re.compile(r"\breconstruction\b", re.IGNORECASE),
    re.compile(r"\bprivate oracle\b", re.IGNORECASE),
    re.compile(r"\bchat (?:history|source|text)\b", re.IGNORECASE),
    re.compile(r"\bconversation history\b", re.IGNORECASE),
    re.compile(r"\bsession extraction\b", re.IGNORECASE),
    re.compile(r"\binternal gate\b", re.IGNORECASE),
    re.compile(r"\bphase [0-9]+\b", re.IGNORECASE),
    re.compile(r"\bseparately authori[sz]ed\b", re.IGNORECASE),
    re.compile(r"\bexplicit(?:ly)? authori[sz]", re.IGNORECASE),
    re.compile(r"\bseparate (?:retirement )?approval\b", re.IGNORECASE),
    re.compile(r"\b(?:mission-specific|software-only mission)\b", re.IGNORECASE),
)
# Claims that contradict the code. Each entry names the fact the code
# establishes, so a reintroduced claim reports what it contradicts.
STALE_CLAIM_PATTERNS = (
    (
        re.compile(r"hfx verify --changed-from\b"),
        "./hfx verify requires --fast, --full, or --all",
    ),
    (
        re.compile(r"\bnightly (?:builds?|runs?|verification)\b", re.IGNORECASE),
        "no workflow runs nightly",
    ),
    (
        re.compile(r"\b(?:historical benchmarks?|benchmark trends?)\b", re.IGNORECASE),
        "no benchmark history is stored",
    ),
    (
        re.compile(
            r"\binstalled (?:local |loopback )?(?:qualification )?console\b|\bpackage-matched\b",
            re.IGNORECASE,
        ),
        "the qualification console is source-only",
    ),
    (re.compile(r"\bCycloneDX\b"), "the generated SBOM is SPDX only"),
)
STALE_CLAIM_CLASSIFICATIONS = {"generated-front-door", "repository-template"}
LONG_LINE_LIMIT = 100
LINK_ONLY_LINE_RE = re.compile(
    r"^\s*(?:- |\d+\. |Next: |Previous: )?\[[^\]]+\]\([^)]+\)[.,;:]?$"
)
BADGE_LINE_RE = re.compile(r"^\s*(?:\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*)+$")
REVIEW_KEYS = {"$schema", "schema", "common_checks", "pages"}
REVIEW_PAGE_KEYS = {"path", "checks", "authorities"}
REVIEW_PAGES = (
    "README.md",
    "docs/user/installation.md",
    "SUPPORT.md",
    "SECURITY.md",
    "docs/user/hardware.md",
    "docs/user/first-run.md",
    "docs/project/journey.md",
    "docs/project/roadmap.md",
    "docs/project/release-status.md",
)


@dataclass(frozen=True)
class DocumentRecord:
    path: str
    title: str
    audience: str
    purpose: str
    owner: str
    navigation_group: str
    mode: str
    canonical_sources: tuple[str, ...]
    generated_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClassifiedMarkdown:
    path: str
    classification: str


@dataclass(frozen=True)
class MarkdownStructureIssue:
    path: str
    line: int
    reason: str


@dataclass(frozen=True)
class ReviewPage:
    path: str
    checks: tuple[str, ...]
    authorities: tuple[str, ...]


@dataclass(frozen=True)
class DocumentationReview:
    common_checks: tuple[str, ...]
    pages: tuple[ReviewPage, ...]


@dataclass(frozen=True)
class DocumentCatalog:
    documents: tuple[DocumentRecord, ...]
    classified_markdown: tuple[ClassifiedMarkdown, ...]

    @property
    def by_path(self) -> dict[str, DocumentRecord]:
        return {document.path: document for document in self.documents}

    @property
    def covered_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                [document.path for document in self.documents]
                + [document.path for document in self.classified_markdown]
            )
        )


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")
    return value


def _string(value: object, label: str, maximum: int = 320) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ModelError(f"{label}: expected a non-empty trimmed string")
    if len(value) > maximum or "\n" in value or "\r" in value:
        raise ModelError(f"{label}: exceeds its single-line bound")
    return value


def _path(value: object, label: str) -> str:
    result = _string(value, label, 256)
    pure = PurePosixPath(result)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != result:
        raise ModelError(f"{label}: expected a safe repository-relative path")
    return result


def _strings(
    value: object,
    label: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelError(f"{label}: expected at most {maximum} entries")
    result = tuple(_string(item, f"{label} entry", 256) for item in value)
    require_unique(list(result), label)
    return result


def generated_markers(text: str) -> tuple[str, ...]:
    seen: list[str] = []
    active: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        begin = BEGIN_RE.fullmatch(line)
        end = END_RE.fullmatch(line)
        if begin:
            identifier = begin.group(1)
            if identifier not in GENERATED_BLOCK_IDS:
                raise ModelError(
                    f"line {line_number}: unknown generated block {identifier}"
                )
            if active is not None:
                raise ModelError(
                    f"line {line_number}: nested generated block {identifier}"
                )
            if identifier in seen:
                raise ModelError(
                    f"line {line_number}: duplicate generated block {identifier}"
                )
            active = identifier
            seen.append(identifier)
        elif end:
            identifier = end.group(1)
            if active != identifier:
                raise ModelError(
                    f"line {line_number}: generated block end {identifier} "
                    f"does not match {active or 'an open block'}"
                )
            active = None
        elif "HFX-GENERATED-BEGIN:" in line or "HFX-GENERATED-END:" in line:
            raise ModelError(f"line {line_number}: malformed generated block marker")
    if active is not None:
        raise ModelError(f"generated block {active} is missing its end marker")
    return tuple(seen)


def replace_generated_blocks(
    text: str,
    declared: tuple[str, ...],
    renderers: dict[str, Callable[[], str]],
) -> str:
    actual = generated_markers(text)
    if actual != declared:
        raise ModelError(
            "generated blocks do not match the declared order: "
            f"expected {declared}, found {actual}"
        )
    if set(renderers) != set(declared):
        raise ModelError("generated block renderers do not exactly match declarations")

    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        begin = BEGIN_RE.fullmatch(line.rstrip("\r\n"))
        output.append(line)
        if begin is None:
            index += 1
            continue
        identifier = begin.group(1)
        index += 1
        while index < len(lines):
            end = END_RE.fullmatch(lines[index].rstrip("\r\n"))
            if end is not None:
                break
            index += 1
        if index == len(lines):
            raise ModelError(f"generated block {identifier} lost its end marker")
        body = renderers[identifier]().strip("\n")
        if body:
            output.append(body + "\n")
        output.append(lines[index])
        index += 1
    return "".join(output)


def protected_prose(text: str) -> str:
    generated_markers(text)
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    active = False
    for line in lines:
        stripped = line.rstrip("\r\n")
        if BEGIN_RE.fullmatch(stripped):
            active = True
            output.append(line)
        elif END_RE.fullmatch(stripped):
            active = False
            output.append(line)
        elif not active:
            output.append(line)
    return "".join(output)


def _tracked_markdown_paths(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in repository_files(root)
            if path.suffix == ".md"
        )
    )

def load_document_catalog(root: Path) -> DocumentCatalog:
    root = root.resolve()
    value = _exact(
        load_json(root / "docs" / "catalog.json"),
        CATALOG_KEYS,
        "document catalog",
    )
    if value["$schema"] != "../schemas/document-catalog.schema.json":
        raise ModelError("document catalog has a non-canonical schema reference")
    if value["schema"] != "hyperflux-document-catalog-v2":
        raise ModelError("unsupported document catalog schema")
    raw_documents = value["documents"]
    if not isinstance(raw_documents, list) or not 1 <= len(raw_documents) <= 256:
        raise ModelError("document catalog requires 1 through 256 documents")

    documents: list[DocumentRecord] = []
    for index, raw in enumerate(raw_documents):
        item = _exact(raw, DOCUMENT_KEYS, f"document {index}")
        path = _path(item["path"], f"document {index} path")
        document_path = root / path
        if document_path.is_symlink() or not document_path.is_file():
            raise ModelError(f"document {path}: missing or symbolic")
        title = _string(item["title"], f"document {path} title", 120)
        text = document_path.read_text(encoding="utf-8")
        first_heading = next(
            (line.removeprefix("# ") for line in text.splitlines() if line.startswith("# ")),
            None,
        )
        if first_heading != title:
            raise ModelError(
                f"document {path}: title does not match its first level-one heading"
            )
        audience = _string(item["audience"], f"document {path} audience", 32)
        navigation_group = _string(
            item["navigation_group"], f"document {path} navigation group", 32
        )
        mode = _string(item["mode"], f"document {path} mode", 16)
        if audience not in AUDIENCES:
            raise ModelError(f"document {path}: unknown audience {audience}")
        if navigation_group not in NAVIGATION_GROUPS:
            raise ModelError(
                f"document {path}: unknown navigation group {navigation_group}"
            )
        if mode not in MODES:
            raise ModelError(f"document {path}: unknown mode {mode}")
        canonical_sources = tuple(
            _path(source, f"document {path} canonical source")
            for source in _strings(
                item["canonical_sources"],
                f"document {path} canonical sources",
                maximum=16,
            )
        )
        for source in canonical_sources:
            source_path = root / source
            if source_path.is_symlink() or not source_path.is_file():
                raise ModelError(
                    f"document {path}: canonical source is missing or symbolic: {source}"
                )
        blocks = _strings(
            item["generated_block_ids"],
            f"document {path} generated blocks",
            maximum=len(GENERATED_BLOCK_IDS),
        )
        if any(block not in GENERATED_BLOCK_IDS for block in blocks):
            raise ModelError(f"document {path}: unknown generated block declaration")
        actual_blocks = generated_markers(text)
        if mode == "hybrid" and (not blocks or actual_blocks != blocks):
            raise ModelError(
                f"document {path}: hybrid marker order does not match its catalog"
            )
        if mode != "hybrid" and (blocks or actual_blocks):
            raise ModelError(
                f"document {path}: only hybrid documents may contain generated blocks"
            )
        documents.append(
            DocumentRecord(
                path=path,
                title=title,
                audience=audience,
                purpose=_string(item["purpose"], f"document {path} purpose"),
                owner=_string(item["owner"], f"document {path} owner", 64),
                navigation_group=navigation_group,
                mode=mode,
                canonical_sources=canonical_sources,
                generated_block_ids=blocks,
            )
        )
    result = tuple(documents)
    paths = [document.path for document in result]
    require_unique(paths, "document path")
    if paths != sorted(paths):
        raise ModelError("document catalog must be sorted by path")
    raw_classified = value["classified_markdown"]
    if not isinstance(raw_classified, list) or len(raw_classified) > 256:
        raise ModelError("classified Markdown requires at most 256 entries")
    classified: list[ClassifiedMarkdown] = []
    for index, raw in enumerate(raw_classified):
        item = _exact(raw, CLASSIFIED_MARKDOWN_KEYS, f"classified Markdown {index}")
        path = _path(item["path"], f"classified Markdown {index} path")
        if not path.endswith(".md"):
            raise ModelError(f"classified Markdown {path}: path must end in .md")
        document_path = root / path
        if document_path.is_symlink() or not document_path.is_file():
            raise ModelError(f"classified Markdown {path}: missing or symbolic")
        classification = _string(
            item["classification"], f"classified Markdown {path} classification", 64
        )
        if classification not in MARKDOWN_CLASSIFICATIONS:
            raise ModelError(f"classified Markdown {path}: unknown classification")
        classified.append(ClassifiedMarkdown(path=path, classification=classification))
    classified_result = tuple(classified)
    classified_paths = [document.path for document in classified_result]
    require_unique(classified_paths, "classified Markdown path")
    if classified_paths != sorted(classified_paths):
        raise ModelError("classified Markdown must be sorted by path")
    overlap = sorted(set(paths) & set(classified_paths))
    if overlap:
        raise ModelError(f"Markdown paths are both cataloged and classified: {overlap}")
    actual_paths = _tracked_markdown_paths(root)
    covered_paths = tuple(sorted(paths + classified_paths))
    if covered_paths != actual_paths:
        missing = sorted(set(actual_paths) - set(covered_paths))
        extra = sorted(set(covered_paths) - set(actual_paths))
        raise ModelError(
            "tracked Markdown coverage mismatch: "
            f"missing {missing or 'none'}; extra {extra or 'none'}"
        )
    return DocumentCatalog(documents=result, classified_markdown=classified_result)


def markdown_structure_issues(
    root: Path,
    catalog: DocumentCatalog | None = None,
) -> tuple[MarkdownStructureIssue, ...]:
    root = root.resolve()
    catalog = catalog or load_document_catalog(root)
    issues: list[MarkdownStructureIssue] = []
    title_owners: dict[str, str] = {}

    for document in catalog.documents:
        title_key = document.title.casefold()
        prior_owner = title_owners.get(title_key)
        if prior_owner is not None:
            issues.append(
                MarkdownStructureIssue(
                    document.path,
                    1,
                    f"document title duplicates {prior_owner}",
                )
            )
        else:
            title_owners[title_key] = document.path

        lines = (root / document.path).read_text(encoding="utf-8").splitlines()
        headings: list[tuple[int, int]] = []
        fence_character: str | None = None
        fence_length = 0
        fence_line = 0
        for line_number, line in enumerate(lines, start=1):
            fence = FENCE_RE.fullmatch(line)
            if fence is not None:
                marker = fence.group(1)
                suffix = fence.group(2).strip()
                if fence_character is None:
                    fence_character = marker[0]
                    fence_length = len(marker)
                    fence_line = line_number
                    if not suffix:
                        issues.append(
                            MarkdownStructureIssue(
                                document.path,
                                line_number,
                                "fenced block is missing a language",
                            )
                        )
                elif marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
                    fence_line = 0
                continue
            if fence_character is not None:
                continue

            heading = HEADING_RE.fullmatch(line)
            if heading is not None:
                level = len(heading.group(1))
                if headings and level > headings[-1][1] + 1:
                    issues.append(
                        MarkdownStructureIssue(
                            document.path,
                            line_number,
                            "heading hierarchy skips a level",
                        )
                    )
                headings.append((line_number, level))

            stripped = INLINE_CODE_RE.sub("", line)
            if "<!--" in stripped and not (
                stripped.startswith("<!-- HFX-GENERATED-")
                or stripped == "<!-- Generated by ./hfx generate. Do not edit directly. -->"
            ):
                issues.append(
                    MarkdownStructureIssue(
                        document.path,
                        line_number,
                        "raw HTML comment is not an approved generated marker",
                    )
                )
            for raw_tag in RAW_HTML_RE.finditer(stripped):
                if raw_tag.group(1).casefold() not in ALLOWED_RAW_HTML_TAGS:
                    issues.append(
                        MarkdownStructureIssue(
                            document.path,
                            line_number,
                            f"raw HTML tag {raw_tag.group(1)} is not allowed",
                        )
                    )

            if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
                separator_count = sum(
                    character == "|" and (index == 0 or line[index - 1] != "\\")
                    for index, character in enumerate(line)
                )
                if separator_count - 1 > 8:
                    issues.append(
                        MarkdownStructureIssue(
                            document.path,
                            line_number,
                            "Markdown table exceeds eight columns",
                        )
                    )

            if document.mode in {"human", "hybrid"}:
                for pattern in PUBLIC_PROCESS_PATTERNS:
                    if pattern.search(stripped):
                        issues.append(
                            MarkdownStructureIssue(
                                document.path,
                                line_number,
                                "public prose contains private process terminology",
                            )
                        )
                        break

        if fence_character is not None:
            issues.append(
                MarkdownStructureIssue(
                    document.path,
                    fence_line,
                    "fenced block is not closed",
                )
            )
        if sum(level == 1 for _line, level in headings) != 1:
            issues.append(
                MarkdownStructureIssue(
                    document.path,
                    1,
                    "document must contain exactly one level-one heading",
                )
            )

    return tuple(issues)


def stale_claim_issues(
    root: Path,
    catalog: DocumentCatalog | None = None,
) -> tuple[MarkdownStructureIssue, ...]:
    """Find public Markdown lines that repeat a claim the code contradicts."""
    root = root.resolve()
    catalog = catalog or load_document_catalog(root)
    paths = sorted(
        [document.path for document in catalog.documents]
        + [
            item.path
            for item in catalog.classified_markdown
            if item.classification in STALE_CLAIM_CLASSIFICATIONS
        ]
    )
    issues: list[MarkdownStructureIssue] = []
    for path in paths:
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            for pattern, fact in STALE_CLAIM_PATTERNS:
                if pattern.search(line):
                    issues.append(
                        MarkdownStructureIssue(path, line_number, f"stale claim: {fact}")
                    )
    return tuple(issues)


def long_line_issues(
    root: Path,
    catalog: DocumentCatalog | None = None,
) -> tuple[MarkdownStructureIssue, ...]:
    """Report hand-written prose lines too long to read comfortably in source."""
    root = root.resolve()
    catalog = catalog or load_document_catalog(root)
    issues: list[MarkdownStructureIssue] = []
    for document in catalog.documents:
        if document.mode not in {"human", "hybrid"}:
            continue
        fence_character: str | None = None
        fence_length = 0
        lines = (root / document.path).read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            fence = FENCE_RE.fullmatch(line)
            if fence is not None:
                marker = fence.group(1)
                if fence_character is None:
                    fence_character = marker[0]
                    fence_length = len(marker)
                elif marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                continue
            if fence_character is not None or len(line) <= LONG_LINE_LIMIT:
                continue
            if line.lstrip().startswith(("|", ">", "<")):
                continue
            if LINK_ONLY_LINE_RE.match(line) or BADGE_LINE_RE.match(line):
                continue
            issues.append(
                MarkdownStructureIssue(
                    document.path,
                    line_number,
                    f"prose line exceeds {LONG_LINE_LIMIT} characters",
                )
            )
    return tuple(issues)


def load_documentation_review(
    root: Path,
    catalog: DocumentCatalog | None = None,
) -> DocumentationReview:
    """Load the manual review list for the pages readers rely on most."""
    root = root.resolve()
    catalog = catalog or load_document_catalog(root)
    value = _exact(
        load_json(root / "assurance" / "documentation-review.json"),
        REVIEW_KEYS,
        "documentation review",
    )
    if value["$schema"] != "../schemas/documentation-review.schema.json":
        raise ModelError("documentation review has a non-canonical schema reference")
    if value["schema"] != "hyperflux-documentation-review-v1":
        raise ModelError("unsupported documentation review schema")
    common_checks = _strings(value["common_checks"], "documentation review common checks", maximum=8)
    if not common_checks:
        raise ModelError("documentation review requires common checks")
    raw_pages = value["pages"]
    if not isinstance(raw_pages, list):
        raise ModelError("documentation review pages must be a list")
    by_path = catalog.by_path
    pages: list[ReviewPage] = []
    for index, raw in enumerate(raw_pages):
        item = _exact(raw, REVIEW_PAGE_KEYS, f"documentation review page {index}")
        path = _path(item["path"], f"documentation review page {index} path")
        document = by_path.get(path)
        if document is None or document.mode not in {"human", "hybrid"}:
            raise ModelError(f"{path}: reviewed page must be a cataloged human or hybrid document")
        checks = _strings(item["checks"], f"{path} review checks", maximum=6)
        authorities = _strings(item["authorities"], f"{path} review authorities", maximum=6)
        if not checks or not authorities:
            raise ModelError(f"{path}: review requires checks and authorities")
        for authority in authorities:
            _path(authority, f"{path} review authority")
            candidate = root / authority
            if candidate.is_symlink() or not candidate.is_file():
                raise ModelError(f"{path}: review authority {authority} is missing")
        pages.append(ReviewPage(path, checks, authorities))
    if tuple(page.path for page in pages) != REVIEW_PAGES:
        raise ModelError(
            "documentation review must list exactly, in order: " + ", ".join(REVIEW_PAGES)
        )
    return DocumentationReview(common_checks, tuple(pages))

