# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


IGNORED_DIRECTORIES = {".git", ".hfx", "build", "target"}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_LINK = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
EXPLICIT_ANCHOR = re.compile(
    r"<(?:a|span)\s+[^>]*(?:id|name)=[\"']([^\"']+)[\"']", re.IGNORECASE
)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class LinkIssue:
    source: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.target}: {self.reason}"


def markdown_files(root: Path) -> tuple[Path, ...]:
    root = root.resolve()
    return tuple(
        sorted(
            path
            for path in root.rglob("*.md")
            if not IGNORED_DIRECTORIES.intersection(path.relative_to(root).parts)
        )
    )


def _visible_lines(text: str) -> tuple[tuple[int, str], ...]:
    visible: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = line.lstrip()[:3]
        if marker in {"```", "~~~"}:
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None:
            visible.append((number, line))
    return tuple(visible)


def _heading_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    return unescape(value.replace("`", "").strip())


def _github_slug(value: str) -> str:
    value = _heading_text(value).lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def markdown_anchors(path: Path) -> frozenset[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in _visible_lines(path.read_text(encoding="utf-8")):
        anchors.update(unescape(value) for value in EXPLICIT_ANCHOR.findall(line))
        match = HEADING.match(line)
        if match is None:
            continue
        base = _github_slug(match.group(1))
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return frozenset(anchors)


def _raw_targets(line: str) -> tuple[str, ...]:
    return tuple(MARKDOWN_LINK.findall(line) + HTML_LINK.findall(line))


def _clean_target(raw: str) -> str:
    target = unescape(raw.strip())
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target.split(' "', 1)[0].split(" '", 1)[0]


def repository_link_issues(root: Path) -> tuple[LinkIssue, ...]:
    root = root.resolve()
    issues: list[LinkIssue] = []
    anchor_cache: dict[Path, frozenset[str]] = {}
    for source in markdown_files(root):
        relative_source = source.relative_to(root).as_posix()
        for line_number, line in _visible_lines(source.read_text(encoding="utf-8")):
            for raw in _raw_targets(line):
                target = _clean_target(raw)
                parsed = urlsplit(target)
                if parsed.scheme in {"http", "https", "mailto", "data", "tel"}:
                    continue
                if parsed.scheme or parsed.netloc:
                    issues.append(
                        LinkIssue(relative_source, line_number, raw, "unsupported link scheme")
                    )
                    continue
                target_path = unquote(parsed.path)
                destination = (
                    root / target_path.lstrip("/")
                    if target_path.startswith("/")
                    else source.parent / target_path
                    if target_path
                    else source
                ).resolve()
                if destination != root and root not in destination.parents:
                    issues.append(
                        LinkIssue(relative_source, line_number, raw, "target escapes repository")
                    )
                    continue
                if not destination.exists():
                    issues.append(
                        LinkIssue(relative_source, line_number, raw, "local target does not exist")
                    )
                    continue
                if not parsed.fragment:
                    continue
                anchor_document = destination / "README.md" if destination.is_dir() else destination
                if anchor_document.suffix.lower() != ".md" or not anchor_document.is_file():
                    issues.append(
                        LinkIssue(relative_source, line_number, raw, "anchor target is not Markdown")
                    )
                    continue
                anchors = anchor_cache.setdefault(
                    anchor_document, markdown_anchors(anchor_document)
                )
                fragment = unquote(parsed.fragment)
                if fragment not in anchors:
                    issues.append(
                        LinkIssue(relative_source, line_number, raw, "Markdown anchor does not exist")
                    )
    return tuple(issues)
