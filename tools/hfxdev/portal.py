# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
from typing import Any
from urllib.parse import unquote, urlsplit

import markdown

from .assurance import load_design_coverage
from .atlas import load_repository_atlas
from .governance import GitHubGovernance, load_github_governance
from .integrations import compiled_catalog as compiled_integration_catalog
from .knowledge import compiled_knowledge_catalog
from .model import ModelError, load_json, require_unique, sha256_file
from .portal_device_lab import (
    DEVICE_LAB_CSS,
    DEVICE_LAB_SCRIPT,
    render_device_lab,
)
from .portal_atlas import ATLAS_CSS, ATLAS_SCRIPT, render_repository_atlas
from .portal_assets import PORTAL_JS, SITE_CSS, architecture_svg
from .portal_book import (
    chapter_markdown,
    chapter_search_text,
    parse_design_book,
    render_book_index,
)
from .portal_coverage import COVERAGE_CSS, COVERAGE_SCRIPT, render_coverage_browser
from .portal_reference import (
    REFERENCE_CSS,
    REFERENCE_SCRIPT,
    parse_reference,
    render_reference_browser,
)
from .profiles import compiled_catalog as compiled_profile_catalog
from .portal_state import (
    REPOSITORY_STATE_CSS,
    REPOSITORY_STATE_SCRIPT,
    render_repository_state,
)


PORTAL_KEYS = {"$schema", "schema", "site", "audiences"}
SITE_KEYS = {"title", "description", "publication_state"}
AUDIENCE_KEYS = {"id", "title", "description", "pages"}
PAGE_KEYS = {"id", "title", "summary", "source", "kind"}
PAGE_KINDS = {"guide", "concept", "reference", "book", "ledger"}
MAX_PORTAL_BYTES = 4 * 1024 * 1024
MAX_HTML_BYTES = 256 * 1024
MAX_SEARCH_INDEX_BYTES = 512 * 1024
MAX_STYLESHEET_BYTES = 96 * 1024
MAX_JAVASCRIPT_BYTES = 64 * 1024
AUDIENCE_ORDER = ("users", "developers", "maintainers")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
LINK_ATTRIBUTE = re.compile(r'(?P<attribute>href|src)="(?P<url>[^"]+)"')
PRIVATE_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/")
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(?P<source>.*?)\n```", re.DOTALL)
MERMAID_NODE = re.compile(
    r'^(?P<id>[A-Za-z][A-Za-z0-9_]*)\s*(?:\["(?P<label>[^"]+)"\])?$'
)
MERMAID_PARTICIPANT = re.compile(
    r"^participant\s+(?P<id>[A-Za-z][A-Za-z0-9_]*)\s+as\s+(?P<label>.+)$"
)
MERMAID_MESSAGE = re.compile(
    r"^(?P<source>[A-Za-z][A-Za-z0-9_]*)(?P<arrow>-{1,2}>>)"
    r"(?P<target>[A-Za-z][A-Za-z0-9_]*):\s*(?P<label>.+)$"
)
HTML_HEADING = re.compile(
    r'<h(?P<level>[23]) id="(?P<id>[^"]+)">(?P<label>.*?)</h(?P=level)>',
    re.DOTALL,
)
HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class PortalPage:
    id: str
    title: str
    summary: str
    source: str
    audience_id: str
    kind: str

    @property
    def url(self) -> str:
        return f"{self.audience_id}/{self.id}.html"


@dataclass(frozen=True)
class PortalAudience:
    id: str
    title: str
    description: str
    pages: tuple[PortalPage, ...]


@dataclass(frozen=True)
class PortalConfig:
    title: str
    description: str
    publication_state: str
    audiences: tuple[PortalAudience, ...]

    @property
    def pages(self) -> tuple[PortalPage, ...]:
        return tuple(page for audience in self.audiences for page in audience.pages)


@dataclass(frozen=True)
class PortalBuild:
    output: Path
    manifest: Path
    pages: int
    files: int


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ModelError(f"{label}: expected 1 through {maximum} characters")
    return value.strip()


def _source_path(root: Path, value: Any, label: str) -> str:
    source = _text(value, label, 256)
    pure = PurePosixPath(source)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != source or pure.suffix != ".md":
        raise ModelError(f"{label}: source must be a safe repository Markdown path")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise ModelError(f"{label}: source is missing or symbolic: {source}")
    return source


def load_portal_config(root: Path) -> PortalConfig:
    value = _exact(load_json(root / "docs" / "portal.json"), PORTAL_KEYS, "documentation portal")
    if value["schema"] != "hyperflux-documentation-portal-v1":
        raise ModelError("unsupported documentation portal schema")
    if value["$schema"] != "../schemas/documentation-portal.schema.json":
        raise ModelError("documentation portal has a non-canonical schema reference")
    site = _exact(value["site"], SITE_KEYS, "documentation portal site")
    if site["title"] != "HyperFlux Next" or site["publication_state"] != "public-pages-pre-release":
        raise ModelError("documentation portal must remain the reviewed public pre-release surface")

    raw_audiences = value["audiences"]
    if not isinstance(raw_audiences, list) or len(raw_audiences) != 3:
        raise ModelError("documentation portal requires exactly three audiences")
    if [item.get("id") for item in raw_audiences if isinstance(item, dict)] != list(AUDIENCE_ORDER):
        raise ModelError("documentation portal audiences must use the canonical order")
    audiences: list[PortalAudience] = []
    all_page_ids: list[str] = []
    all_sources: list[str] = []
    for audience_index, raw_audience in enumerate(raw_audiences):
        audience = _exact(raw_audience, AUDIENCE_KEYS, f"portal audience {audience_index}")
        identifier = audience["id"]
        if identifier not in AUDIENCE_ORDER:
            raise ModelError(f"portal audience {audience_index}: invalid id")
        raw_pages = audience["pages"]
        if not isinstance(raw_pages, list) or not 4 <= len(raw_pages) <= 16:
            raise ModelError(f"portal audience {identifier}: expected 4 through 16 pages")
        pages: list[PortalPage] = []
        for page_index, raw_page in enumerate(raw_pages):
            page = _exact(raw_page, PAGE_KEYS, f"portal page {identifier}/{page_index}")
            page_id = _text(page["id"], f"portal page {identifier}/{page_index} id", 64)
            if IDENTIFIER.fullmatch(page_id) is None:
                raise ModelError(f"portal page {identifier}/{page_index}: invalid id")
            source = _source_path(
                root,
                page["source"],
                f"portal page {identifier}/{page_id}",
            )
            pages.append(
                PortalPage(
                    id=page_id,
                    title=_text(page["title"], f"portal page {identifier}/{page_id} title", 80),
                    summary=_text(
                        page["summary"], f"portal page {identifier}/{page_id} summary", 180
                    ),
                    source=source,
                    audience_id=identifier,
                    kind=_text(page["kind"], f"portal page {identifier}/{page_id} kind", 16),
                )
            )
            if pages[-1].kind not in PAGE_KINDS:
                raise ModelError(f"portal page {identifier}/{page_id}: invalid kind")
            all_page_ids.append(f"{identifier}/{page_id}")
            all_sources.append(source)
        audiences.append(
            PortalAudience(
                id=identifier,
                title=_text(audience["title"], f"portal audience {identifier} title", 48),
                description=_text(
                    audience["description"], f"portal audience {identifier} description", 180
                ),
                pages=tuple(pages),
            )
        )
    require_unique(all_page_ids, "portal page id")
    require_unique(all_sources, "portal page source")
    return PortalConfig(
        title=site["title"],
        description=_text(site["description"], "portal site description", 180),
        publication_state=site["publication_state"],
        audiences=tuple(audiences),
    )


def _relative_url(current: str, target: str) -> str:
    directory = posixpath.dirname(current) or "."
    return posixpath.relpath(target, directory)


def _plain_markdown(value: str) -> str:
    text = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?(?:\[([^]]*)\])\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_|~-]", " ", text)
    return " ".join(text.split())


def _mermaid_node(token: str, labels: dict[str, str]) -> str:
    match = MERMAID_NODE.fullmatch(token.strip())
    if match is None:
        raise ModelError(f"unsupported Mermaid node: {token.strip()}")
    identifier = match.group("id")
    label = match.group("label")
    if label is not None:
        previous = labels.setdefault(identifier, label)
        if previous != label:
            raise ModelError(f"conflicting Mermaid labels for {identifier}")
    else:
        labels.setdefault(identifier, identifier)
    return identifier


def _linear_path(
    labels: dict[str, str], edges: list[tuple[str, str, str | None]]
) -> list[tuple[str, str | None]] | None:
    if len(edges) != len(labels) - 1:
        return None
    outgoing: dict[str, tuple[str, str | None]] = {}
    incoming: dict[str, int] = {identifier: 0 for identifier in labels}
    for source, target, edge_label in edges:
        if source in outgoing or target not in incoming:
            return None
        outgoing[source] = (target, edge_label)
        incoming[target] += 1
        if incoming[target] > 1:
            return None
    starts = [identifier for identifier, count in incoming.items() if count == 0]
    if len(starts) != 1:
        return None
    path: list[tuple[str, str | None]] = [(starts[0], None)]
    seen = {starts[0]}
    current = starts[0]
    while current in outgoing:
        target, edge_label = outgoing[current]
        if target in seen:
            return None
        path.append((target, edge_label))
        seen.add(target)
        current = target
    return path if len(path) == len(labels) else None


def _unquoted_arrow(value: str, start: int) -> int | None:
    quoted = False
    index = start
    while index < len(value):
        if value[index] == '"':
            quoted = not quoted
            index += 1
            continue
        if not quoted and value.startswith("-->", index):
            return index
        index += 1
    return None


def _flowchart_edge_chain(value: str) -> tuple[list[str], list[str | None]] | None:
    arrow = _unquoted_arrow(value, 0)
    if arrow is None:
        return None

    nodes: list[str] = []
    labels: list[str | None] = []
    cursor = 0
    while arrow is not None:
        node = value[cursor:arrow].strip()
        if not node:
            raise ModelError(f"unsupported Mermaid flowchart edge: {value}")
        nodes.append(node)

        cursor = arrow + 3
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1

        label: str | None = None
        if cursor < len(value) and value[cursor] == "|":
            if not value.startswith('|"', cursor):
                raise ModelError(f"unsupported Mermaid flowchart edge: {value}")
            closing = value.find('"|', cursor + 2)
            if closing == -1 or closing == cursor + 2:
                raise ModelError(f"unsupported Mermaid flowchart edge: {value}")
            label = value[cursor + 2 : closing]
            cursor = closing + 2
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
        labels.append(label)
        arrow = _unquoted_arrow(value, cursor)

    target = value[cursor:].strip()
    if not target:
        raise ModelError(f"unsupported Mermaid flowchart edge: {value}")
    nodes.append(target)
    return nodes, labels


def _render_flowchart(lines: list[str]) -> str:
    header = lines[0].split()
    if len(header) != 2 or header[1] not in {"LR", "TB"}:
        raise ModelError("portal supports only LR and TB Mermaid flowcharts")
    labels: dict[str, str] = {}
    edges: list[tuple[str, str, str | None]] = []
    for line in lines[1:]:
        chain = _flowchart_edge_chain(line)
        if chain is None:
            _mermaid_node(line, labels)
            continue
        node_tokens, edge_labels = chain
        identifiers = [_mermaid_node(token, labels) for token in node_tokens]
        edges.extend(
            (identifiers[index], identifiers[index + 1], edge_labels[index])
            for index in range(len(edge_labels))
        )
    if not labels or not edges:
        raise ModelError("Mermaid flowchart must declare nodes and edges")

    path = _linear_path(labels, edges)
    if path is not None:
        pieces = [f'<span class="diagram-node">{escape(labels[path[0][0]])}</span>']
        for identifier, edge_label in path[1:]:
            label = (
                f'<small class="diagram-label">{escape(edge_label)}</small>'
                if edge_label
                else ""
            )
            pieces.append(
                '<span class="diagram-step">'
                f'<span class="diagram-link">{label}<span class="diagram-arrow" '
                'aria-hidden="true"></span></span>'
                f'<span class="diagram-node">{escape(labels[identifier])}</span></span>'
            )
        body = f'<div class="diagram-pipeline">{"".join(pieces)}</div>'
    else:
        rows = []
        for source, target, edge_label in edges:
            label = (
                f'<small class="diagram-label">{escape(edge_label)}</small>'
                if edge_label
                else ""
            )
            rows.append(
                '<div class="diagram-edge">'
                f'<span class="diagram-node">{escape(labels[source])}</span>'
                f'<span class="diagram-link">{label}<span class="diagram-arrow" '
                'aria-hidden="true"></span></span>'
                f'<span class="diagram-node">{escape(labels[target])}</span></div>'
            )
        body = f'<div class="diagram-edges">{"".join(rows)}</div>'
    return (
        '<figure class="compiled-diagram">'
        '<figcaption>Responsibility flow</figcaption>'
        f"{body}</figure>"
    )


def _render_sequence(lines: list[str]) -> str:
    participants: dict[str, str] = {}
    messages: list[tuple[str, str, str, bool]] = []
    for line in lines[1:]:
        participant = MERMAID_PARTICIPANT.fullmatch(line)
        if participant is not None:
            participants[participant.group("id")] = participant.group("label")
            continue
        message = MERMAID_MESSAGE.fullmatch(line)
        if message is None:
            raise ModelError(f"unsupported Mermaid sequence statement: {line}")
        source = message.group("source")
        target = message.group("target")
        if source not in participants or target not in participants:
            raise ModelError("Mermaid sequence message references an unknown participant")
        messages.append(
            (source, target, message.group("label"), message.group("arrow").startswith("--"))
        )
    if len(participants) < 2 or not messages:
        raise ModelError("Mermaid sequence requires participants and messages")
    participant_html = "".join(
        f'<span class="diagram-node">{escape(label)}</span>' for label in participants.values()
    )
    message_html = "".join(
        '<div class="sequence-message">'
        f'<span>{escape(participants[source])}</span>'
        f'<span class="sequence-track{" response" if response else ""}">'
        f'<small>{escape(label)}</small><span class="diagram-arrow" aria-hidden="true"></span></span>'
        f'<span>{escape(participants[target])}</span></div>'
        for source, target, label, response in messages
    )
    return (
        '<figure class="compiled-diagram">'
        '<figcaption>Request sequence</figcaption>'
        f'<div class="sequence-participants">{participant_html}</div>'
        f'<div class="sequence-messages">{message_html}</div></figure>'
    )


def _render_state_diagram(lines: list[str]) -> str:
    edges: list[tuple[str, str]] = []
    for line in lines[1:]:
        parts = [part.strip() for part in line.split("-->")]
        if len(parts) != 2 or not all(parts):
            raise ModelError(f"unsupported Mermaid state transition: {line}")
        edges.append((parts[0], parts[1]))
    if not edges:
        raise ModelError("Mermaid state diagram requires transitions")

    def state_label(value: str) -> str:
        return "Start" if value == "[*]" else value

    rows = "".join(
        '<div class="diagram-edge">'
        f'<span class="diagram-node">{escape(state_label(source))}</span>'
        '<span class="diagram-link"><span class="diagram-arrow" aria-hidden="true"></span></span>'
        f'<span class="diagram-node">{escape(state_label(target))}</span></div>'
        for source, target in edges
    )
    return (
        '<figure class="compiled-diagram">'
        '<figcaption>State transitions</figcaption>'
        f'<div class="diagram-edges">{rows}</div></figure>'
    )


def _render_mermaid(source: str) -> str:
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if not lines:
        raise ModelError("empty Mermaid block")
    if lines[0].startswith("flowchart "):
        return _render_flowchart(lines)
    if lines[0] == "sequenceDiagram":
        return _render_sequence(lines)
    if lines[0] == "stateDiagram-v2":
        return _render_state_diagram(lines)
    raise ModelError(f"unsupported Mermaid diagram type: {lines[0]}")


def _compile_mermaid(value: str) -> str:
    return MERMAID_BLOCK.sub(lambda match: _render_mermaid(match.group("source")), value)


def _active_audience(config: PortalConfig, current_url: str) -> PortalAudience:
    for audience in config.audiences:
        if current_url.startswith(f"{audience.id}/"):
            return audience
    mapping = {
        "devices/index.html": "users",
        "atlas/index.html": "developers",
        "state/index.html": "maintainers",
    }
    identifier = mapping.get(current_url, "users")
    return next(audience for audience in config.audiences if audience.id == identifier)


def _active_link(current_url: str, target: str, label: str) -> str:
    active = ' class="active" aria-current="page"' if current_url == target else ""
    return (
        f'<a{active} href="{escape(_relative_url(current_url, target))}">'
        f"{escape(label)}</a>"
    )


def _primary_navigation(config: PortalConfig, current_url: str) -> str:
    targets = (
        ("users", config.audiences[0].pages[0].url, "Use"),
        ("devices", "devices/index.html", "Devices"),
        ("developers", config.audiences[1].pages[0].url, "Develop"),
        ("maintainers", "state/index.html", "Readiness"),
    )
    links = []
    for identifier, target, label in targets:
        selected = (
            current_url == target
            or (identifier in AUDIENCE_ORDER and current_url.startswith(f"{identifier}/"))
            or (identifier == "devices" and current_url == "devices/index.html")
            or (identifier == "maintainers" and current_url == "state/index.html")
        )
        active = ' class="active" aria-current="page"' if selected else ""
        links.append(
            f'<a{active} href="{escape(_relative_url(current_url, target))}">{escape(label)}</a>'
        )
    return "".join(links)


def _navigation(config: PortalConfig, current_url: str) -> str:
    audience = _active_audience(config, current_url)
    page_links = "".join(
        _active_link(current_url, page.url, page.title) for page in audience.pages
    )
    workbench = "".join(
        (
            _active_link(current_url, "devices/index.html", "Device Lab"),
            _active_link(current_url, "atlas/index.html", "Repository Atlas"),
            _active_link(current_url, "state/index.html", "Repository State"),
        )
    )
    return (
        f'<div class="nav-context"><span class="nav-label">{escape(audience.title)}</span>'
        f'<p>{escape(audience.description)}</p></div>{page_links}'
        f'<div class="nav-context nav-context--secondary"><span class="nav-label">Explore</span></div>'
        f"{workbench}"
    )


def _shell(
    config: PortalConfig,
    *,
    current_url: str,
    title: str,
    description: str,
    content: str,
    page_kind: str = "concept",
    outline: str = "",
    extra_scripts: tuple[str, ...] = (),
) -> str:
    css = _relative_url(current_url, "assets/site.css")
    script = _relative_url(current_url, "assets/portal.js")
    search_index = _relative_url(current_url, "assets/search-index.json")
    root_url = _relative_url(current_url, "index.html")
    extra_script_tags = "".join(
        f'<script src="{escape(_relative_url(current_url, value))}" defer></script>'
        for value in extra_scripts
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{escape(description)}">
  <title>{escape(title)} | {escape(config.title)}</title>
  <link rel="stylesheet" href="{escape(css)}">
</head>
<body class="page-kind-{escape(page_kind)}" data-search-index="{escape(search_index)}" data-portal-root="{escape(root_url)}">
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <a class="brand" href="{escape(_relative_url(current_url, "index.html"))}">
      <span class="brand-mark" aria-hidden="true">HF</span>
      <span><strong>HyperFlux Next</strong><small>Linux receiver foundation</small></span>
    </a>
    <nav class="primary-nav" aria-label="Primary">{_primary_navigation(config, current_url)}</nav>
    <div class="search-box">
      <label class="sr-only" for="portal-search">Search documentation</label>
      <input id="portal-search" type="search" placeholder="Search HyperFlux" autocomplete="off" aria-keyshortcuts="/">
      <div id="search-results" class="search-results" hidden></div>
    </div>
    <div class="header-tools">
      <span class="phase">Public pre-release</span>
      <button class="theme-cycle" id="theme-cycle" type="button" title="Change color theme">Theme: System</button>
    </div>
  </header>
  <div class="site-grid">
    <nav class="side-nav desktop-nav" aria-label="Documentation">{_navigation(config, current_url)}</nav>
    <details class="mobile-nav">
      <summary>Browse documentation</summary>
      <nav class="mobile-nav-links" aria-label="Mobile documentation">{_navigation(config, current_url)}</nav>
    </details>
    <div class="page-frame"><main id="main-content" tabindex="-1">{content}</main>{outline}</div>
  </div>
  <footer>
    <span>Generated from canonical repository data. Product release remains locked.</span>
    <a href="{escape(_relative_url(current_url, "maintainers/release-gates.html"))}">Release gates</a>
  </footer>
  <script src="{escape(script)}" defer></script>
  {extra_script_tags}
</body>
</html>
"""


def _home_content(
    config: PortalConfig,
    coverage: tuple[Any, ...],
    profiles: dict[str, Any],
    integrations: dict[str, Any],
    knowledge: dict[str, Any],
) -> str:
    verified = sum(entry.status == "software-verified" for entry in coverage)
    release_blocking = sum(entry.release_blocking for entry in coverage)
    qualified_profiles = sum(
        profile.get("support_level") == "qualified" for profile in profiles["profiles"]
    )
    adapters = len(integrations["adapters"])
    route_qualified = sum(
        candidate.get("hyperflux_support") == "route-qualified"
        for candidate in knowledge["candidates"]
    )
    return f"""<article class="home">
  <section class="home-hero"><div class="home-copy"><p class="page-kicker">Linux receiver foundation</p><h1>HyperFlux Next</h1><p class="home-lede">{escape(config.description)}</p><p class="home-boundary">Public source for review; product unreleased. Support claims stay bounded to recorded evidence.</p><div class="home-actions"><a class="button button--primary" href="devices/index.html">Explore tested hardware</a><a class="button" href="users/overview.html">Understand the system</a></div></div><div class="home-signal" aria-label="Current project state"><span>Current phase</span><strong>Software foundation built</strong><p>Hardware qualification and the product release decision remain separate gates.</p><a href="state/index.html">Inspect readiness</a></div></section>
  <section class="flow-section" aria-labelledby="flow-title"><div class="section-intro"><p class="page-kicker">One direction of responsibility</p><h2 id="flow-title">Applications stay separate from hardware transport</h2><p>Integrations express intent through the SDK. One bridge validates policy and reaches the receiver through the minimal kernel driver.</p></div><img class="system-map" src="assets/system-map.svg" alt="Applications flow through the SDK, bridge, kernel, and receiver"></section>
  <section class="path-section" aria-labelledby="path-title"><div class="section-intro"><p class="page-kicker">Choose a path</p><h2 id="path-title">Start with what you need</h2></div><div class="path-grid"><a href="users/overview.html"><span>01</span><strong>Use HyperFlux</strong><p>Installation, applications, supported devices, privacy, and troubleshooting.</p></a><a href="developers/architecture.html"><span>02</span><strong>Build with HyperFlux</strong><p>Architecture, SDK contracts, protocol, development, and verification.</p></a><a href="state/index.html"><span>03</span><strong>Review readiness</strong><p>Release gates, migration decisions, verification, and performance boundaries.</p></a></div></section>
  <section class="truth-section" aria-labelledby="truth-heading"><div class="section-intro"><p class="page-kicker">Generated repository state</p><h2 id="truth-heading">Evidence without inflated promises</h2><p>These values are compiled from canonical ledgers whenever the portal is built.</p></div><div class="status-band"><div><strong>{route_qualified}</strong><span>receiver routes with physical evidence</span></div><div><strong>{verified}/{len(coverage)}</strong><span>design sections software verified</span></div><div><strong>{adapters}</strong><span>application adapters registered</span></div><div><strong>{release_blocking}</strong><span>release-blocking sections</span></div></div><p class="truth-note">There are {qualified_profiles} whole-product profiles marked fully qualified. Capability-scoped route evidence remains visible in the <a href="devices/index.html">Device Lab</a> without becoming a broader support claim.</p></section>
</article>"""


def _reading_minutes(source: str) -> int:
    words = len(_plain_markdown(source).split())
    return max(1, round(words / 220))


def _github_source_url(governance: GitHubGovernance, source: str, *, edit: bool) -> str:
    action = "edit" if edit else "blob"
    return (
        f"https://github.com/{governance.owner}/{governance.repository}/"
        f"{action}/{governance.default_branch}/{source}"
    )


def _source_details(
    governance: GitHubGovernance,
    *,
    source: str,
    current_url: str,
    generated: bool | None = None,
) -> str:
    is_generated = (
        source.startswith("docs/generated/") or source.startswith("generated/")
        if generated is None
        else generated
    )
    source_url = _github_source_url(governance, source, edit=not is_generated)
    if is_generated:
        description = (
            f'<strong>Generated projection.</strong> <a href="{escape(source_url)}">View the source projection</a>. '
            f'Use the <a href="{escape(_relative_url(current_url, "atlas/index.html"))}">Repository Atlas</a> '
            "to find its canonical authority and generator."
        )
    else:
        description = (
            f'<strong>Canonical source.</strong> <a href="{escape(source_url)}">Edit this source on GitHub</a>. '
            "The deployed HTML is generated and must not be edited directly."
        )
    return (
        '<details class="source-note"><summary>Source and generation details</summary>'
        f"<p>{description}</p></details>"
    )


def _outline(body: str) -> str:
    links = []
    for match in HTML_HEADING.finditer(body):
        label = unescape(HTML_TAG.sub("", match.group("label"))).strip()
        if not label:
            continue
        links.append(
            f'<a class="outline-level-{match.group("level")}" href="#{escape(match.group("id"))}">{escape(label)}</a>'
        )
    if len(links) < 2:
        return ""
    return (
        '<aside class="page-outline" aria-label="On this page"><span>On this page</span>'
        f'{"".join(links)}<a class="outline-top" href="#main-content">Back to top</a></aside>'
    )


def _document_content(
    config: PortalConfig,
    page: PortalPage,
    governance: GitHubGovernance,
    *,
    body: str,
    source_text: str,
) -> tuple[str, str]:
    audience = next(item for item in config.audiences if item.id == page.audience_id)
    index = audience.pages.index(page)
    previous = audience.pages[index - 1] if index > 0 else None
    following = audience.pages[index + 1] if index + 1 < len(audience.pages) else None
    crumbs = (
        f'<nav class="breadcrumb" aria-label="Breadcrumb"><a href="{escape(_relative_url(page.url, "index.html"))}">Home</a>'
        f'<a href="{escape(_relative_url(page.url, audience.pages[0].url))}">{escape(audience.title)}</a>'
        f'<span>{escape(page.title)}</span></nav>'
    )
    origin = "Generated reference" if "/generated/" in page.source else "Canonical documentation"
    pager = []
    if previous is not None:
        pager.append(
            f'<a rel="prev" href="{escape(_relative_url(page.url, previous.url))}"><span>Previous</span><strong>{escape(previous.title)}</strong></a>'
        )
    if following is not None:
        pager.append(
            f'<a rel="next" href="{escape(_relative_url(page.url, following.url))}"><span>Next</span><strong>{escape(following.title)}</strong></a>'
        )
    content = (
        f'<article class="document document--{escape(page.kind)}">{crumbs}'
        f'<header class="page-hero"><p class="page-kicker">{escape(page.kind.title())}</p><h1>{escape(page.title)}</h1>'
        f'<p class="lede">{escape(page.summary)}</p><div class="page-meta"><span>{escape(origin)}</span>'
        f'<span>{_reading_minutes(source_text)} min read</span></div></header>'
        f'<div class="document-body">{body}</div>{_source_details(governance, source=page.source, current_url=page.url)}'
        f'<nav class="page-pager" aria-label="Adjacent pages">{"".join(pager)}</nav></article>'
    )
    return content, _outline(body)


def _rewrite_links(
    html: str,
    *,
    root: Path,
    output: Path,
    source: Path,
    current_url: str,
    source_urls: dict[str, str],
    copied_references: set[str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        attribute = match.group("attribute")
        raw_url = match.group("url")
        parsed = urlsplit(raw_url)
        if parsed.scheme or raw_url.startswith("//"):
            if attribute == "src" or parsed.scheme not in {"https", "mailto"}:
                raise ModelError(f"portal source {source.relative_to(root)} uses forbidden URL {raw_url}")
            return match.group(0)
        if parsed.query or parsed.path.startswith("/"):
            raise ModelError(f"portal source {source.relative_to(root)} uses an unsafe local URL")
        if not parsed.path:
            return match.group(0)
        decoded = unquote(parsed.path)
        target = (source.parent / decoded).resolve()
        try:
            relative = target.relative_to(root.resolve()).as_posix()
        except ValueError as error:
            raise ModelError(f"portal source {source.relative_to(root)} links outside the repository") from error
        if target.is_symlink() or not target.exists():
            raise ModelError(f"portal source {source.relative_to(root)} has a broken link: {decoded}")
        if target.is_dir():
            target_url = f"reference/{relative}/directory-index.txt"
            destination = output / target_url
            destination.parent.mkdir(parents=True, exist_ok=True)
            if target_url not in copied_references:
                entries = [
                    f"{path.name}/" if path.is_dir() else path.name
                    for path in sorted(target.iterdir(), key=lambda path: path.name)
                    if not path.is_symlink() and not path.name.startswith(".")
                ]
                destination.write_text(
                    f"Repository directory: {relative}\n\n" + "\n".join(entries) + "\n",
                    encoding="utf-8",
                )
                copied_references.add(target_url)
        elif relative in source_urls:
            target_url = source_urls[relative]
        else:
            target_url = f"reference/{relative}"
            destination = output / target_url
            destination.parent.mkdir(parents=True, exist_ok=True)
            if target_url not in copied_references:
                shutil.copyfile(target, destination)
                copied_references.add(target_url)
        rewritten = _relative_url(current_url, target_url)
        if parsed.fragment:
            rewritten += f"#{parsed.fragment}"
        return f'{attribute}="{escape(rewritten, quote=True)}"'

    return LINK_ATTRIBUTE.sub(replace, html)


def _file_inventory(output: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ModelError(f"portal output contains symbolic link: {path.relative_to(output)}")
        if path.is_file() and path.name != "portal-build-manifest.json":
            files.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
    return files


def build_portal(root: Path, output: Path) -> PortalBuild:
    root = root.resolve()
    output = output.expanduser()
    if not output.is_absolute():
        output = root / output
    if output.is_symlink():
        raise ModelError("portal output may not be a symbolic link")
    if output.exists() and any(output.iterdir()):
        raise ModelError("portal output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)

    config = load_portal_config(root)
    governance = load_github_governance(root)
    coverage = load_design_coverage(root)
    knowledge = compiled_knowledge_catalog(root)
    device_lab = render_device_lab(knowledge)
    repository_atlas = load_repository_atlas(root)
    atlas_page = render_repository_atlas(repository_atlas)
    state_page = render_repository_state(root)
    book_source = (root / "docs" / "architecture" / "design-book.md").read_text(
        encoding="utf-8"
    )
    book = parse_design_book(book_source)
    protocol_page = next(page for page in config.pages if page.id == "protocol")
    protocol_source = (root / protocol_page.source).read_text(encoding="utf-8")
    protocol_reference = parse_reference(protocol_source)
    source_urls = {page.source: page.url for page in config.pages}
    special_pages = {"design-book", "protocol", "coverage"}
    search_records: list[dict[str, str]] = []
    for page in config.pages:
        source_text = (root / page.source).read_text(encoding="utf-8")
        if page.id not in special_pages:
            search_records.append(
                {
                    "title": page.title,
                    "audience": page.audience_id.title(),
                    "summary": page.summary,
                    "url": page.url,
                    "search": f"{page.title} {page.summary} {_plain_markdown(source_text)}".lower()[:12_000],
                }
            )
    search_records.append(
        {
            "title": "Design book",
            "audience": "Developers",
            "summary": "Twelve chapters and sixty-seven product and engineering decisions.",
            "url": "developers/design-book.html",
            "search": "design book specification architecture product engineering chapters",
        }
    )
    search_records.extend(
        {
            "title": f"Chapter {chapter.roman}: {chapter.title}",
            "audience": "Design book",
            "summary": f"Sections {chapter.section_range}",
            "url": f"developers/design-book/{chapter.slug}.html",
            "search": chapter_search_text(chapter),
        }
        for chapter in book.chapters
    )
    search_records.append(
        {
            "title": "Bridge protocol",
            "audience": "Developers",
            "summary": protocol_page.summary,
            "url": protocol_page.url,
            "search": _plain_markdown(protocol_source).lower()[:12_000],
        }
    )
    search_records.extend(
        {
            "title": entry.title,
            "audience": f"Protocol / {entry.group_title}",
            "summary": entry.group_title,
            "url": f"{protocol_page.url}#ref-{entry.id}",
            "search": f"{entry.group_title} {entry.title} {_plain_markdown(entry.markdown)}".lower(),
        }
        for entry in protocol_reference.entries
    )
    search_records.append(
        {
            "title": "Design coverage",
            "audience": "Maintainers",
            "summary": "Implementation and release state for every design section.",
            "url": "maintainers/coverage.html",
            "search": "design coverage implementation status release blocking physical evidence",
        }
    )
    search_records.extend(
        {
            "title": f"{entry.section}. {entry.title}",
            "audience": "Design coverage",
            "summary": entry.status.replace("-", " "),
            "url": f"maintainers/coverage.html#coverage-section-{entry.section}",
            "search": f"{entry.section} {entry.title} {entry.owner} {entry.status} {' '.join(entry.remaining)}".lower(),
        }
        for entry in coverage
    )
    search_records.append(
        {
            "title": "Device Lab",
            "audience": "Research",
            "summary": "Search, compare, and trace provenance-bound device knowledge.",
            "url": "devices/index.html",
            "search": "device lab compatibility candidates capability heatmap evidence provenance conflicts unknowns qualification",
        }
    )
    search_records.extend(device_lab.search_records)
    search_records.append(
        {
            "title": "Repository Atlas",
            "audience": "Architecture",
            "summary": "Search subsystem ownership, dependencies, projections, and change impact.",
            "url": "atlas/index.html",
            "search": "repository atlas architecture ownership dependencies used by canonical generated change impact",
        }
    )
    search_records.extend(atlas_page.search_records)
    search_records.append(
        {
            "title": "Repository State",
            "audience": "Assurance",
            "summary": "Inspect release gates, migration state, verification budgets, and performance limits.",
            "url": "state/index.html",
            "search": "repository state release gates migration verification timings performance budgets publication lock",
        }
    )
    search_records.extend(state_page.search_records)
    search_records.sort(key=lambda record: (record["audience"], record["title"]))

    assets = output / "assets"
    assets.mkdir()
    (assets / "site.css").write_text(
        SITE_CSS
        + DEVICE_LAB_CSS
        + ATLAS_CSS
        + REPOSITORY_STATE_CSS
        + REFERENCE_CSS
        + COVERAGE_CSS,
        encoding="utf-8",
    )
    (assets / "portal.js").write_text(PORTAL_JS, encoding="utf-8")
    (assets / "device-lab.js").write_text(DEVICE_LAB_SCRIPT, encoding="utf-8")
    (assets / "atlas.js").write_text(ATLAS_SCRIPT, encoding="utf-8")
    (assets / "repository-state.js").write_text(
        REPOSITORY_STATE_SCRIPT, encoding="utf-8"
    )
    (assets / "reference.js").write_text(REFERENCE_SCRIPT, encoding="utf-8")
    (assets / "coverage.js").write_text(COVERAGE_SCRIPT, encoding="utf-8")
    (assets / "system-map.svg").write_text(architecture_svg(), encoding="utf-8")
    shutil.copyfile(root / "docs" / "assets" / "social-preview.png", assets / "social-preview.png")
    (assets / "search-index.json").write_text(
        json.dumps(search_records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    copied_references: set[str] = set()
    renderer = markdown.Markdown(extensions=["fenced_code", "sane_lists", "tables", "toc"])

    def render_markdown(value: str, *, source: Path, current_url: str) -> str:
        body = renderer.reset().convert(_compile_mermaid(value))
        body = _rewrite_links(
            body,
            root=root,
            output=output,
            source=source,
            current_url=current_url,
            source_urls=source_urls,
            copied_references=copied_references,
        )
        return re.sub(r"<h1(?: [^>]*)?>.*?</h1>", "", body, count=1, flags=re.DOTALL)

    for page in config.pages:
        if page.id in special_pages:
            continue
        source = root / page.source
        source_text = source.read_text(encoding="utf-8")
        body = render_markdown(source_text, source=source, current_url=page.url)
        content, outline = _document_content(
            config, page, governance, body=body, source_text=source_text
        )
        destination = output / page.url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _shell(
                config,
                current_url=page.url,
                title=page.title,
                description=page.summary,
                content=content,
                page_kind=page.kind,
                outline=outline,
            ),
            encoding="utf-8",
        )

    book_page = next(page for page in config.pages if page.id == "design-book")
    book_destination = output / book_page.url
    book_destination.parent.mkdir(parents=True, exist_ok=True)
    book_destination.write_text(
        _shell(
            config,
            current_url=book_page.url,
            title=book_page.title,
            description=book_page.summary,
            content=(
                render_book_index(book, coverage)
                + _source_details(
                    governance,
                    source=book_page.source,
                    current_url=book_page.url,
                    generated=False,
                )
            ),
            page_kind="book",
        ),
        encoding="utf-8",
    )
    book_source_path = root / book_page.source
    for chapter_index, chapter in enumerate(book.chapters):
        chapter_url = f"developers/design-book/{chapter.slug}.html"
        body = render_markdown(
            chapter_markdown(chapter), source=book_source_path, current_url=chapter_url
        )
        previous = book.chapters[chapter_index - 1] if chapter_index else None
        following = (
            book.chapters[chapter_index + 1]
            if chapter_index + 1 < len(book.chapters)
            else None
        )
        pager = []
        if previous is not None:
            pager.append(
                f'<a rel="prev" href="{escape(previous.slug)}.html"><span>Previous chapter</span><strong>{escape(previous.title)}</strong></a>'
            )
        if following is not None:
            pager.append(
                f'<a rel="next" href="{escape(following.slug)}.html"><span>Next chapter</span><strong>{escape(following.title)}</strong></a>'
            )
        content = f"""<article class="document document--book">
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="../../index.html">Home</a><a href="../design-book.html">Design book</a><span>Chapter {escape(chapter.roman)}</span></nav>
  <header class="page-hero page-hero--book"><p class="page-kicker">Chapter {escape(chapter.roman)} | Sections {escape(chapter.section_range)}</p><h1>{escape(chapter.title)}</h1><p class="lede">Part of the canonical HyperFlux Next product and engineering specification.</p><div class="page-meta"><span>{len(chapter.sections)} sections</span><span>{_reading_minutes(chapter_search_text(chapter))} min read</span></div></header>
  <div class="document-body">{body}</div>{_source_details(governance, source=book_page.source, current_url=chapter_url, generated=False)}<nav class="page-pager" aria-label="Adjacent chapters">{''.join(pager)}</nav>
</article>"""
        destination = output / chapter_url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _shell(
                config,
                current_url=chapter_url,
                title=f"Chapter {chapter.roman}: {chapter.title}",
                description=f"Design book sections {chapter.section_range}.",
                content=content,
                page_kind="book",
                outline=_outline(body),
            ),
            encoding="utf-8",
        )

    protocol_content = render_reference_browser(
        protocol_reference,
        title=protocol_page.title,
        summary=protocol_page.summary,
        render_markdown=lambda value: render_markdown(
            value,
            source=root / protocol_page.source,
            current_url=protocol_page.url,
        ),
    ) + _source_details(
        governance,
        source=protocol_page.source,
        current_url=protocol_page.url,
        generated=True,
    )
    protocol_destination = output / protocol_page.url
    protocol_destination.parent.mkdir(parents=True, exist_ok=True)
    protocol_destination.write_text(
        _shell(
            config,
            current_url=protocol_page.url,
            title=protocol_page.title,
            description=protocol_page.summary,
            content=protocol_content,
            page_kind="reference",
            extra_scripts=("assets/reference.js",),
        ),
        encoding="utf-8",
    )

    coverage_page = next(page for page in config.pages if page.id == "coverage")
    coverage_destination = output / coverage_page.url
    coverage_destination.parent.mkdir(parents=True, exist_ok=True)
    coverage_destination.write_text(
        _shell(
            config,
            current_url=coverage_page.url,
            title=coverage_page.title,
            description=coverage_page.summary,
            content=(
                render_coverage_browser(coverage, book)
                + _source_details(
                    governance,
                    source=coverage_page.source,
                    current_url=coverage_page.url,
                    generated=True,
                )
            ),
            page_kind="ledger",
            extra_scripts=("assets/coverage.js",),
        ),
        encoding="utf-8",
    )

    device_url = "devices/index.html"
    device_destination = output / device_url
    device_destination.parent.mkdir(parents=True, exist_ok=True)
    device_destination.write_text(
        _shell(
            config,
            current_url=device_url,
            title="Device Lab",
            description="Search, compare, and inspect provenance-bound HyperFlux device knowledge.",
            content=(
                device_lab.content
                + _source_details(
                    governance,
                    source="generated/knowledge/catalog.json",
                    current_url=device_url,
                    generated=True,
                )
            ),
            page_kind="reference",
            extra_scripts=("assets/device-lab.js",),
        ),
        encoding="utf-8",
    )

    atlas_url = "atlas/index.html"
    atlas_destination = output / atlas_url
    atlas_destination.parent.mkdir(parents=True, exist_ok=True)
    atlas_destination.write_text(
        _shell(
            config,
            current_url=atlas_url,
            title="Repository Atlas",
            description="Search repository ownership, dependencies, generated projections, and safe change impact.",
            content=(
                atlas_page.content
                + _source_details(
                    governance,
                    source="architecture/repository-atlas.json",
                    current_url=atlas_url,
                    generated=False,
                )
            ),
            page_kind="reference",
            extra_scripts=("assets/atlas.js",),
        ),
        encoding="utf-8",
    )

    state_url = "state/index.html"
    state_destination = output / state_url
    state_destination.parent.mkdir(parents=True, exist_ok=True)
    state_destination.write_text(
        _shell(
            config,
            current_url=state_url,
            title="Repository State",
            description="Generated release gates, migration decisions, verification budgets, and performance limits.",
            content=(
                state_page.content
                + _source_details(
                    governance,
                    source="assurance/release-gates.json",
                    current_url=state_url,
                    generated=False,
                )
            ),
            page_kind="ledger",
            extra_scripts=("assets/repository-state.js",),
        ),
        encoding="utf-8",
    )

    profiles = compiled_profile_catalog(root)
    integrations = compiled_integration_catalog(root)
    (output / "index.html").write_text(
        _shell(
            config,
            current_url="index.html",
            title="Documentation",
            description=config.description,
            content=_home_content(config, coverage, profiles, integrations, knowledge),
            page_kind="home",
        ),
        encoding="utf-8",
    )

    material_paths = {
        "assurance/design-coverage.json",
        "assurance/performance-budgets.json",
        "assurance/release-gates.json",
        "architecture/repository-atlas.json",
        "docs/portal.json",
        "schemas/documentation-portal.schema.json",
        "generated/integrations/catalog.json",
        "generated/knowledge/catalog.json",
        "generated/profiles/catalog.json",
        "governance/github.json",
        "tools/hfxdev/portal.py",
        "tools/hfxdev/portal_assets.py",
        "tools/hfxdev/portal_device_lab.py",
        "tools/hfxdev/portal_atlas.py",
        "tools/hfxdev/atlas.py",
        "tools/hfxdev/generators/atlas.py",
        "schemas/repository-atlas.schema.json",
        "migration/ledger.json",
        "verification/tests.json",
        "tools/hfxdev/portal_state.py",
        "tools/hfxdev/portal_book.py",
        "tools/hfxdev/portal_reference.py",
        "tools/hfxdev/portal_coverage.py",
        *(page.source for page in config.pages),
    }
    materials = [
        {"path": path, "sha256": sha256_file(root / path)} for path in sorted(material_paths)
    ]
    source_digest = hashlib.sha256(
        "".join(f"{item['path']}\0{item['sha256']}\n" for item in materials).encode("ascii")
    ).hexdigest()
    files = _file_inventory(output)
    manifest_value = {
        "schema": "hyperflux-documentation-portal-build-v2",
        "source_publication_state": config.publication_state,
        "product_publication_authorized": False,
        "external_runtime_dependencies": False,
        "source_tree_sha256": source_digest,
        "materials": materials,
        "pages": len(config.pages) + 4 + len(book.chapters),
        "files": files,
    }
    manifest = output / "portal-build-manifest.json"
    manifest.write_text(
        json.dumps(manifest_value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return PortalBuild(
        output=output,
        manifest=manifest,
        pages=len(config.pages) + 4 + len(book.chapters),
        files=len(files),
    )


class _PortalHtmlInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[tuple[str, str]] = []
        self.h1_count = 0
        self.has_main = False
        self.has_viewport = False
        self.has_search_label = False
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value for key, value in attrs}
        identifier = values.get("id")
        if identifier:
            if identifier in self.ids:
                self.errors.append(f"duplicate id {identifier}")
            self.ids.add(identifier)
        if tag == "h1":
            self.h1_count += 1
        if tag == "main" and identifier == "main-content":
            self.has_main = True
        if tag == "meta" and values.get("name") == "viewport":
            self.has_viewport = True
        if tag == "label" and values.get("for") == "portal-search":
            self.has_search_label = True
        if tag == "img" and not values.get("alt"):
            self.errors.append("image has no non-empty alt text")
        for attribute in ("href", "src"):
            url = values.get(attribute)
            if url:
                self.links.append((attribute, url))
        if any(key.lower().startswith("on") for key, _ in attrs):
            self.errors.append("inline event handler is forbidden")


def verify_portal(root: Path, site: Path) -> dict[str, Any]:
    root = root.resolve()
    site = site.resolve()
    manifest_path = site / "portal-build-manifest.json"
    value = load_json(manifest_path)
    expected_keys = {
        "schema",
        "source_publication_state",
        "product_publication_authorized",
        "external_runtime_dependencies",
        "source_tree_sha256",
        "materials",
        "pages",
        "files",
    }
    if set(value) != expected_keys or value["schema"] != "hyperflux-documentation-portal-build-v2":
        raise ModelError("portal build manifest is malformed")
    if (
        value["source_publication_state"] != "public-pages-pre-release"
        or value["product_publication_authorized"] is not False
        or value["external_runtime_dependencies"] is not False
    ):
        raise ModelError("portal build violates its publication or runtime boundary")
    expected_files = _file_inventory(site)
    if value["files"] != expected_files:
        raise ModelError("portal file inventory differs from its manifest")
    total_size = sum(file["size"] for file in expected_files)
    if total_size > MAX_PORTAL_BYTES:
        raise ModelError(
            f"portal exceeds its {MAX_PORTAL_BYTES}-byte uncompressed size budget"
        )
    for file in expected_files:
        path = file["path"]
        size = file["size"]
        if path.endswith(".html") and size > MAX_HTML_BYTES:
            raise ModelError(f"portal HTML exceeds its size budget: {path}")
        if path.endswith(".js") and size > MAX_JAVASCRIPT_BYTES:
            raise ModelError(f"portal JavaScript exceeds its size budget: {path}")
        if path == "assets/site.css" and size > MAX_STYLESHEET_BYTES:
            raise ModelError("portal stylesheet exceeds its size budget")
        if path == "assets/search-index.json" and size > MAX_SEARCH_INDEX_BYTES:
            raise ModelError("portal search index exceeds its size budget")

    search_path = site / "assets" / "search-index.json"
    try:
        search_records = json.loads(search_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelError("portal search index is not valid UTF-8 JSON") from error
    if not isinstance(search_records, list) or not search_records:
        raise ModelError("portal search index must contain records")
    search_keys = {"title", "audience", "summary", "url", "search"}
    for index, record in enumerate(search_records):
        if not isinstance(record, dict) or set(record) != search_keys:
            raise ModelError(f"portal search record {index} is malformed")
        if any(not isinstance(record[key], str) or not record[key] for key in search_keys):
            raise ModelError(f"portal search record {index} contains empty text")
        raw_url = record["url"]
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme
            or raw_url.startswith("//")
            or parsed.path.startswith("/")
            or parsed.query
        ):
            raise ModelError(f"portal search record {index} has an unsafe URL")
        target = (site / unquote(parsed.path)).resolve()
        try:
            target.relative_to(site)
        except ValueError as error:
            raise ModelError(
                f"portal search record {index} escapes the site"
            ) from error
        if not target.is_file():
            raise ModelError(f"portal search record {index} has a missing target")
    for material in value["materials"]:
        if set(material) != {"path", "sha256"}:
            raise ModelError("portal material entry is malformed")
        path = root / material["path"]
        if not path.is_file() or sha256_file(path) != material["sha256"]:
            raise ModelError(f"portal material drifted: {material['path']}")
    source_digest = hashlib.sha256(
        "".join(
            f"{item['path']}\0{item['sha256']}\n" for item in value["materials"]
        ).encode("ascii")
    ).hexdigest()
    if source_digest != value["source_tree_sha256"]:
        raise ModelError("portal source-tree digest is invalid")

    html_inspectors: dict[Path, _PortalHtmlInspector] = {}
    for file in expected_files:
        path = site / file["path"]
        if path.suffix != ".html":
            continue
        text = path.read_text(encoding="utf-8")
        if PRIVATE_PATH.search(text):
            raise ModelError(f"portal HTML leaks a private path: {file['path']}")
        inspector = _PortalHtmlInspector()
        inspector.feed(text)
        if (
            inspector.errors
            or inspector.h1_count != 1
            or not inspector.has_main
            or not inspector.has_viewport
            or not inspector.has_search_label
        ):
            details = ", ".join(inspector.errors) or "required landmark or heading is missing"
            raise ModelError(f"portal accessibility contract failed for {file['path']}: {details}")
        html_inspectors[path] = inspector

    for source, inspector in html_inspectors.items():
        for attribute, raw_url in inspector.links:
            parsed = urlsplit(raw_url)
            if parsed.scheme:
                if attribute == "src" or parsed.scheme not in {"https", "mailto"}:
                    raise ModelError(f"portal has forbidden external runtime URL: {raw_url}")
                continue
            if raw_url.startswith("//") or parsed.query:
                raise ModelError(f"portal has unsafe URL: {raw_url}")
            target = source if not parsed.path else (source.parent / unquote(parsed.path)).resolve()
            try:
                target.relative_to(site)
            except ValueError as error:
                raise ModelError(f"portal link escapes the site: {raw_url}") from error
            if not target.is_file():
                raise ModelError(f"portal has a broken local link: {source.relative_to(site)} -> {raw_url}")
            if parsed.fragment and target.suffix == ".html":
                target_inspector = html_inspectors.get(target)
                if target_inspector is None or parsed.fragment not in target_inspector.ids:
                    raise ModelError(f"portal has a broken fragment link: {raw_url}")

    css = (site / "assets" / "site.css").read_text(encoding="utf-8")
    javascript = (site / "assets" / "portal.js").read_text(encoding="utf-8")
    if "gradient" in css.lower() or re.search(r"letter-spacing\s*:\s*-", css):
        raise ModelError("portal styling violates the visual-system contract")
    external_runtime_javascript = javascript.replace(
        "fetch(document.body.dataset.searchIndex", "loadLocalSearchIndex("
    )
    if any(
        token in external_runtime_javascript
        for token in ("fetch(", "XMLHttpRequest", "WebSocket")
    ):
        raise ModelError("portal JavaScript may not depend on external network access")
    if value["pages"] != len(html_inspectors):
        raise ModelError("portal page count differs from rendered HTML")
    return value
