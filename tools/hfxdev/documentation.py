# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Callable

from .model import ModelError, load_json, require_unique


CATALOG_KEYS = {"$schema", "schema", "documents"}
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
GENERATED_BLOCK_IDS = (
    "release-status",
    "recommended-package",
    "compatibility-summary",
)
PRESERVED_LEGACY_TITLE_PATHS = {"docs/architecture/design-book.md"}
BEGIN_RE = re.compile(r"^<!-- HFX-GENERATED-BEGIN: ([a-z][a-z0-9-]*) -->$")
END_RE = re.compile(r"^<!-- HFX-GENERATED-END: ([a-z][a-z0-9-]*) -->$")


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
class DocumentCatalog:
    documents: tuple[DocumentRecord, ...]

    @property
    def by_path(self) -> dict[str, DocumentRecord]:
        return {document.path: document for document in self.documents}


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


def _document_paths(root: Path) -> tuple[str, ...]:
    paths = ["README.md"]
    paths.extend(
        path.relative_to(root).as_posix()
        for path in (root / "docs").rglob("*.md")
        if path.is_file() and not path.is_symlink()
    )
    return tuple(sorted(paths))


def load_document_catalog(root: Path) -> DocumentCatalog:
    root = root.resolve()
    value = _exact(
        load_json(root / "docs" / "catalog.json"),
        CATALOG_KEYS,
        "document catalog",
    )
    if value["$schema"] != "../schemas/document-catalog.schema.json":
        raise ModelError("document catalog has a non-canonical schema reference")
    if value["schema"] != "hyperflux-document-catalog-v1":
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
        if path in PRESERVED_LEGACY_TITLE_PATHS:
            first_heading = next(
                (line for line in text.splitlines() if line.strip()),
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
    actual_paths = _document_paths(root)
    if tuple(paths) != actual_paths:
        missing = sorted(set(actual_paths) - set(paths))
        extra = sorted(set(paths) - set(actual_paths))
        raise ModelError(
            "document catalog coverage mismatch: "
            f"missing {missing or 'none'}; extra {extra or 'none'}"
        )
    return DocumentCatalog(documents=result)
