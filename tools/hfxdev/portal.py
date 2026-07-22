# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import markdown

from .assurance import load_design_coverage
from .atlas import load_repository_atlas
from .governance import load_github_governance
from .knowledge import compiled_knowledge_catalog
from .model import ModelError, load_json, sha256_file
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
from .portal_state import (
    REPOSITORY_STATE_CSS,
    REPOSITORY_STATE_SCRIPT,
    render_repository_state,
)
from .portal_content import (
    _reading_minutes,
    document_content as _document_content,
    home_content as _home_content,
    outline as _outline,
    source_details as _source_details,
)
from .portal_layout import shell as _shell
from .portal_metadata import (
    canonical_url,
    favicon_svg,
    not_found_content,
    robots_txt,
    social_image_path,
    sitemap_xml,
    web_manifest,
)
from .portal_model import load_portal_config
from .portal_routing import relative_url as _relative_url, rewrite_links
from .portal_search import build_search_records, verify_search_quality
from .public_readiness import public_readiness


MAX_PORTAL_BYTES = 4 * 1024 * 1024
MAX_HTML_BYTES = 256 * 1024
MAX_SEARCH_INDEX_BYTES = 512 * 1024
MAX_STYLESHEET_BYTES = 96 * 1024
MAX_JAVASCRIPT_BYTES = 64 * 1024
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
@dataclass(frozen=True)
class PortalBuild:
    output: Path
    manifest: Path
    pages: int
    files: int


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
    readiness = public_readiness(root)
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
    protocol_page = config.route("protocol")
    protocol_source = (root / protocol_page.source).read_text(encoding="utf-8")
    protocol_reference = parse_reference(protocol_source)
    source_urls = {route.source: route.url for route in config.routes}
    special_pages = {
        page.id for page in config.pages if page.renderer in {"book", "reference", "coverage"}
    }
    search_records = build_search_records(
        root,
        config,
        chapter_records=(
            {
            "title": f"Chapter {chapter.roman}: {chapter.title}",
            "audience": "Design book",
            "summary": f"Sections {chapter.section_range}",
            "url": f"developers/design-book/{chapter.slug}.html",
            "search": chapter_search_text(chapter)[:900],
            }
            for chapter in book.chapters
        ),
    )

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
    (assets / "favicon.svg").write_text(favicon_svg(), encoding="utf-8")
    social_asset = social_image_path(config)
    shutil.copyfile(root / config.social_image, output / social_asset)
    (assets / "search-index.json").write_text(
        json.dumps(search_records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "site.webmanifest").write_text(web_manifest(config), encoding="utf-8")
    (output / "sitemap.xml").write_text(sitemap_xml(config), encoding="utf-8")
    (output / "robots.txt").write_text(robots_txt(config), encoding="utf-8")

    renderer = markdown.Markdown(extensions=["fenced_code", "sane_lists", "tables", "toc"])

    def render_markdown(value: str, *, source: Path, current_url: str) -> str:
        body = renderer.reset().convert(_compile_mermaid(value))
        body = rewrite_links(
            body,
            root=root,
            source=source,
            current_url=current_url,
            source_urls=source_urls,
            governance=governance,
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

    coverage_page = config.route("coverage")
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

    device_route = config.route("device-lab")
    device_url = device_route.path
    device_destination = output / device_url
    device_destination.parent.mkdir(parents=True, exist_ok=True)
    device_destination.write_text(
        _shell(
            config,
            current_url=device_url,
            title=device_route.title,
            description=device_route.summary,
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

    atlas_route = config.route("repository-atlas")
    atlas_url = atlas_route.path
    atlas_destination = output / atlas_url
    atlas_destination.parent.mkdir(parents=True, exist_ok=True)
    atlas_destination.write_text(
        _shell(
            config,
            current_url=atlas_url,
            title=atlas_route.title,
            description=atlas_route.summary,
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

    state_route = config.route("repository-state")
    state_url = state_route.path
    state_destination = output / state_url
    state_destination.parent.mkdir(parents=True, exist_ok=True)
    state_destination.write_text(
        _shell(
            config,
            current_url=state_url,
            title=state_route.title,
            description=state_route.summary,
            content=(
                state_page.content
                + _source_details(
                    governance,
                    source=state_route.source,
                    current_url=state_url,
                    generated=True,
                )
            ),
            page_kind="ledger",
            extra_scripts=("assets/repository-state.js",),
        ),
        encoding="utf-8",
    )

    home_route = config.route("home")
    (output / home_route.path).write_text(
        _shell(
            config,
            current_url=home_route.path,
            title=home_route.title,
            description=home_route.summary,
            content=_home_content(config, readiness),
            page_kind="home",
        ),
        encoding="utf-8",
    )
    (output / "404.html").write_text(
        _shell(
            config,
            current_url="404.html",
            title="Page not found",
            description="The requested HyperFlux Next documentation route does not exist.",
            content=not_found_content(config),
            page_kind="concept",
        ),
        encoding="utf-8",
    )

    material_paths = {
        "assurance/design-coverage.json",
        "assurance/performance-budgets.json",
        "assurance/release-gates.json",
        "architecture/repository-atlas.json",
        "docs/portal.json",
        "generated/public-readiness.json",
        "runtime/local-companion.json",
        "schemas/local-companion.schema.json",
        "schemas/local-snapshot.schema.json",
        "schemas/public-readiness.schema.json",
        "schemas/documentation-portal.schema.json",
        "generated/integrations/catalog.json",
        "generated/knowledge/catalog.json",
        "generated/profiles/catalog.json",
        "governance/github.json",
        config.social_image,
        "tools/hfxdev/portal.py",
        "tools/hfxdev/portal_content.py",
        "tools/hfxdev/portal_layout.py",
        "tools/hfxdev/portal_metadata.py",
        "tools/hfxdev/portal_model.py",
        "tools/hfxdev/portal_routing.py",
        "tools/hfxdev/portal_search.py",
        "tools/hfxdev/public_readiness.py",
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
        *(route.source for route in config.routes),
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
        "pages": len(config.routes) + len(book.chapters) + 1,
        "files": files,
    }
    manifest = output / "portal-build-manifest.json"
    manifest.write_text(
        json.dumps(manifest_value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return PortalBuild(
        output=output,
        manifest=manifest,
        pages=len(config.routes) + len(book.chapters) + 1,
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
        self.has_description = False
        self.has_search_label = False
        self.canonical_urls: list[str] = []
        self.has_manifest = False
        self.has_icon = False
        self.open_graph: set[str] = set()
        self.twitter: set[str] = set()
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
        if tag == "meta" and values.get("name") == "description" and values.get("content"):
            self.has_description = True
        if tag == "meta" and values.get("property", "").startswith("og:"):
            self.open_graph.add(values["property"] or "")
        if tag == "meta" and values.get("name", "").startswith("twitter:"):
            self.twitter.add(values["name"] or "")
        if tag == "link" and values.get("rel") == "canonical" and values.get("href"):
            self.canonical_urls.append(values["href"] or "")
        if tag == "link" and values.get("rel") == "manifest":
            self.has_manifest = True
        if tag == "link" and values.get("rel") == "icon":
            self.has_icon = True
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
    verify_search_quality(search_records)
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

    config = load_portal_config(root)
    required_site_files = {
        "404.html",
        "assets/favicon.svg",
        social_image_path(config),
        "robots.txt",
        "site.webmanifest",
        "sitemap.xml",
    }
    inventory_paths = {item["path"] for item in expected_files}
    if not required_site_files <= inventory_paths or any(
        path.startswith("reference/") for path in inventory_paths
    ):
        raise ModelError("portal metadata files are incomplete or a reference mirror was emitted")
    manifest = load_json(site / "site.webmanifest")
    if manifest.get("name") != config.title or manifest.get("start_url") != "./":
        raise ModelError("portal web manifest is malformed")
    try:
        sitemap_root = ET.fromstring(
            (site / "sitemap.xml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ET.ParseError) as error:
        raise ModelError("portal sitemap is not valid UTF-8 XML") from error
    namespace = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls = {
        element.text
        for element in sitemap_root.findall("sitemap:url/sitemap:loc", namespace)
        if element.text
    }
    expected_sitemap_urls = {
        canonical_url(config, route.path) for route in config.routes
    }
    if sitemap_urls != expected_sitemap_urls:
        raise ModelError("portal sitemap differs from the canonical route registry")
    if config.canonical_url not in (site / "robots.txt").read_text(encoding="utf-8"):
        raise ModelError("portal robots policy does not name the canonical sitemap")
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
        expected_canonical = canonical_url(config, file["path"])
        if (
            inspector.errors
            or inspector.h1_count != 1
            or not inspector.has_main
            or not inspector.has_viewport
            or not inspector.has_description
            or not inspector.has_search_label
            or inspector.canonical_urls != [expected_canonical]
            or not inspector.has_manifest
            or not inspector.has_icon
            or not {"og:title", "og:description", "og:url", "og:image"} <= inspector.open_graph
            or not {"twitter:card", "twitter:title", "twitter:description", "twitter:image"} <= inspector.twitter
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
    if "@media" not in css or "max-width" not in css:
        raise ModelError("portal stylesheet lacks its responsive-layout contract")
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
