# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
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
from .integrations import compiled_catalog as compiled_integration_catalog
from .model import ModelError, load_json, require_unique, sha256_file
from .profiles import compiled_catalog as compiled_profile_catalog


PORTAL_KEYS = {"$schema", "schema", "site", "audiences"}
SITE_KEYS = {"title", "description", "publication_state"}
AUDIENCE_KEYS = {"id", "title", "description", "pages"}
PAGE_KEYS = {"id", "title", "summary", "source"}
AUDIENCE_ORDER = ("users", "developers", "maintainers")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
LINK_ATTRIBUTE = re.compile(r'(?P<attribute>href|src)="(?P<url>[^"]+)"')
PRIVATE_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/")
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(?P<source>.*?)\n```", re.DOTALL)
MERMAID_NODE = re.compile(
    r'^(?P<id>[A-Za-z][A-Za-z0-9_]*)\s*(?:\["(?P<label>[^"]+)"\])?$'
)
MERMAID_EDGE = re.compile(r'\s*-->(?:\|"([^"]+)"\|)?\s*')
MERMAID_PARTICIPANT = re.compile(
    r"^participant\s+(?P<id>[A-Za-z][A-Za-z0-9_]*)\s+as\s+(?P<label>.+)$"
)
MERMAID_MESSAGE = re.compile(
    r"^(?P<source>[A-Za-z][A-Za-z0-9_]*)(?P<arrow>-{1,2}>>)"
    r"(?P<target>[A-Za-z][A-Za-z0-9_]*):\s*(?P<label>.+)$"
)


@dataclass(frozen=True)
class PortalPage:
    id: str
    title: str
    summary: str
    source: str
    audience_id: str

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
    if site["title"] != "HyperFlux Next" or site["publication_state"] != "local-artifact-only":
        raise ModelError("documentation portal must remain a local unpublished HyperFlux artifact")

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
                )
            )
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


def _render_flowchart(lines: list[str]) -> str:
    header = lines[0].split()
    if len(header) != 2 or header[1] not in {"LR", "TB"}:
        raise ModelError("portal supports only LR and TB Mermaid flowcharts")
    labels: dict[str, str] = {}
    edges: list[tuple[str, str, str | None]] = []
    for line in lines[1:]:
        if "-->" not in line:
            _mermaid_node(line, labels)
            continue
        parts = MERMAID_EDGE.split(line)
        if len(parts) < 3 or len(parts) % 2 == 0:
            raise ModelError(f"unsupported Mermaid flowchart edge: {line}")
        identifiers = [_mermaid_node(parts[index], labels) for index in range(0, len(parts), 2)]
        edge_labels = [parts[index] or None for index in range(1, len(parts), 2)]
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


def _navigation(config: PortalConfig, current_url: str) -> str:
    sections: list[str] = []
    home_class = ' class="active" aria-current="page"' if current_url == "index.html" else ""
    sections.append(
        f'<a{home_class} href="{escape(_relative_url(current_url, "index.html"))}">Home</a>'
    )
    for audience in config.audiences:
        links = []
        for page in audience.pages:
            active = ' class="active" aria-current="page"' if page.url == current_url else ""
            links.append(
                f'<a{active} href="{escape(_relative_url(current_url, page.url))}">'
                f"{escape(page.title)}</a>"
            )
        sections.append(
            f'<section class="nav-group"><h2>{escape(audience.title)}</h2>{"".join(links)}</section>'
        )
    return "".join(sections)


def _shell(
    config: PortalConfig,
    *,
    current_url: str,
    title: str,
    description: str,
    content: str,
    search_records: list[dict[str, str]],
) -> str:
    css = _relative_url(current_url, "assets/site.css")
    script = _relative_url(current_url, "assets/portal.js")
    search_json = json.dumps(search_records, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
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
<body>
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <a class="brand" href="{escape(_relative_url(current_url, "index.html"))}">
      <span class="brand-mark" aria-hidden="true">HF</span>
      <span><strong>HyperFlux Next</strong><small>Linux receiver foundation</small></span>
    </a>
    <div class="search-box">
      <label class="sr-only" for="portal-search">Search documentation</label>
      <input id="portal-search" type="search" placeholder="Search documentation" autocomplete="off">
      <div id="search-results" class="search-results" hidden></div>
    </div>
    <span class="phase">Local reconstruction</span>
  </header>
  <div class="site-grid">
    <nav class="side-nav desktop-nav" aria-label="Documentation">{_navigation(config, current_url)}</nav>
    <details class="mobile-nav">
      <summary>Browse documentation</summary>
      <nav class="mobile-nav-links" aria-label="Mobile documentation">{_navigation(config, current_url)}</nav>
    </details>
    <main id="main-content" tabindex="-1">{content}</main>
  </div>
  <footer>
    <span>Schema-first. Evidence-bound. Publication locked.</span>
    <a href="{escape(_relative_url(current_url, "maintainers/release-gates.html"))}">Release gates</a>
  </footer>
  <script id="search-index" type="application/json">{search_json}</script>
  <script src="{escape(script)}" defer></script>
</body>
</html>
"""


def _architecture_svg() -> str:
    labels = ("Applications", "SDK", "Bridge", "Kernel", "Receiver")
    colors = ("#6ccff6", "#b7df50", "#43d6b5", "#ffd166", "#ff7d6e")
    boxes = []
    arrows = []
    for index, (label, color) in enumerate(zip(labels, colors, strict=True)):
        x = 24 + index * 174
        boxes.append(
            f'<rect x="{x}" y="45" width="138" height="70" rx="6" fill="#1c2229" '
            f'stroke="{color}" stroke-width="2"/><text x="{x + 69}" y="86" '
            f'text-anchor="middle" fill="#edf3f2" font-family="sans-serif" font-size="16">'
            f"{label}</text>"
        )
        if index < len(labels) - 1:
            arrows.append(
                f'<path d="M {x + 140} 80 H {x + 168}" stroke="#93a4aa" stroke-width="2" '
                'marker-end="url(#arrow)"/>'
            )
    return """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="160" viewBox="0 0 900 160" role="img" aria-labelledby="title description">
<title id="title">HyperFlux responsibility flow</title>
<desc id="description">Applications use the SDK, which talks to the bridge, kernel, and receiver in one direction.</desc>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#93a4aa"/></marker></defs>
<rect width="900" height="160" fill="#151a20"/>
""" + "".join(boxes + arrows) + "\n</svg>\n"


SITE_CSS = """:root {
  color-scheme: dark;
  --bg: #11151a;
  --surface: #1c2229;
  --surface-strong: #252d35;
  --line: #39444d;
  --ink: #edf3f2;
  --muted: #aebbc0;
  --teal: #43d6b5;
  --lime: #b7df50;
  --coral: #ff7d6e;
  --yellow: #ffd166;
  --cyan: #6ccff6;
}
* { box-sizing: border-box; }
html { background: var(--bg); scroll-behavior: smooth; }
body { margin: 0; color: var(--ink); background: var(--bg); font: 15px/1.62 Inter, ui-sans-serif, system-ui, sans-serif; }
a { color: var(--cyan); text-underline-offset: 3px; }
a:hover { color: var(--teal); }
a:focus-visible, input:focus-visible, summary:focus-visible { outline: 2px solid var(--yellow); outline-offset: 3px; }
.skip-link { position: fixed; top: 8px; left: 8px; z-index: 20; padding: 8px 12px; background: var(--yellow); color: #11151a; transform: translateY(-180%); }
.skip-link:focus { transform: translateY(0); }
.site-header { position: sticky; top: 0; z-index: 10; min-height: 72px; display: grid; grid-template-columns: minmax(220px, 320px) minmax(260px, 620px) auto; align-items: center; gap: 24px; padding: 10px 24px; border-bottom: 1px solid var(--line); background: #151a20; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--ink); text-decoration: none; }
.brand-mark { display: grid; place-items: center; width: 38px; height: 38px; border: 1px solid var(--teal); border-radius: 6px; color: var(--teal); font-weight: 800; }
.brand strong, .brand small { display: block; }
.brand small { color: var(--muted); font-size: 12px; }
.search-box { position: relative; }
.search-box input { width: 100%; min-height: 40px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); background: var(--surface); font: inherit; }
.search-results { position: absolute; top: 46px; right: 0; left: 0; max-height: 360px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-strong); box-shadow: 0 12px 28px rgb(0 0 0 / 35%); }
.search-results a { display: block; padding: 10px 12px; border-bottom: 1px solid var(--line); color: var(--ink); text-decoration: none; }
.search-results small { display: block; color: var(--muted); }
.phase { justify-self: end; color: var(--yellow); font-size: 13px; }
.site-grid { display: grid; grid-template-columns: 250px minmax(0, 1fr); min-height: calc(100vh - 122px); }
.side-nav { position: sticky; top: 72px; align-self: start; height: calc(100vh - 72px); overflow: auto; padding: 24px 16px; border-right: 1px solid var(--line); background: #151a20; scrollbar-color: var(--line) #151a20; scrollbar-width: thin; }
.side-nav > a, .nav-group a { display: block; min-height: 34px; padding: 6px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; }
.nav-group { margin-top: 20px; }
.nav-group h2 { margin: 0 10px 6px; color: var(--ink); font-size: 12px; text-transform: uppercase; }
.side-nav a:hover, .side-nav a.active { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
.mobile-nav { display: none; }
main { width: min(1120px, 100%); min-width: 0; padding: 38px clamp(24px, 5%, 72px) 72px; }
.breadcrumb { margin-bottom: 12px; color: var(--muted); font-size: 13px; }
.document > h1, .home-intro h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.2; }
.lede { max-width: 760px; color: var(--muted); font-size: 18px; }
.system-map { display: block; width: 100%; max-width: 900px; height: auto; margin: 28px 0; border: 1px solid var(--line); border-radius: 6px; }
.audience-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 28px 0 38px; }
.audience-card { min-width: 0; padding: 18px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.audience-card:nth-child(1) { border-top-color: var(--cyan); }
.audience-card:nth-child(2) { border-top-color: var(--lime); }
.audience-card:nth-child(3) { border-top-color: var(--coral); }
.audience-card h2 { margin: 0 0 6px; font-size: 18px; }
.audience-card p { color: var(--muted); }
.status-band { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 24px 0; border: 1px solid var(--line); background: var(--line); }
.status-band div { min-width: 0; padding: 16px; background: var(--surface); }
.status-band strong { display: block; color: var(--teal); font-size: 24px; }
.status-band span { color: var(--muted); }
.notice { margin: 24px 0; padding: 14px 16px; border-left: 3px solid var(--yellow); background: var(--surface); }
.document { overflow-wrap: anywhere; }
.document h2 { margin-top: 38px; padding-bottom: 8px; border-bottom: 1px solid var(--line); font-size: 23px; }
.document h3 { margin-top: 28px; font-size: 18px; }
.document p, .document li { max-width: 860px; }
.document table { display: block; width: 100%; max-width: 100%; overflow-x: auto; margin: 18px 0; border-collapse: collapse; font-size: 14px; }
.document th, .document td { padding: 9px 10px; border: 1px solid var(--line); text-align: left; vertical-align: top; }
.document th { background: var(--surface-strong); }
.document code { padding: 2px 5px; border-radius: 4px; color: var(--lime); background: var(--surface); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.document pre { max-width: 100%; overflow: auto; padding: 16px; border: 1px solid var(--line); border-radius: 6px; background: #0c1014; }
.document pre code { padding: 0; color: var(--ink); background: transparent; }
.document blockquote { margin-left: 0; padding: 10px 16px; border-left: 3px solid var(--yellow); color: var(--muted); background: var(--surface); }
.compiled-diagram { margin: 22px 0; padding: 16px; border: 1px solid var(--line); border-radius: 6px; background: #151a20; }
.compiled-diagram figcaption { margin-bottom: 14px; color: var(--muted); font-size: 13px; font-weight: 700; text-transform: uppercase; }
.diagram-pipeline, .sequence-participants { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.diagram-step { display: inline-flex; align-items: center; gap: 10px; }
.diagram-node { display: inline-flex; min-height: 42px; min-width: 112px; align-items: center; justify-content: center; padding: 8px 12px; border: 1px solid var(--teal); border-radius: 6px; color: var(--ink); background: var(--surface); text-align: center; }
.diagram-link { display: inline-grid; min-width: 44px; justify-items: center; gap: 2px; }
.diagram-label { max-width: 170px; color: var(--muted); text-align: center; }
.diagram-arrow { position: relative; display: block; width: 38px; height: 2px; background: var(--muted); }
.diagram-arrow::after { position: absolute; top: -4px; right: -1px; width: 8px; height: 8px; border-top: 2px solid var(--muted); border-right: 2px solid var(--muted); content: ""; transform: rotate(45deg); }
.diagram-edges { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.diagram-edge { display: grid; grid-template-columns: minmax(110px, 1fr) 54px minmax(110px, 1fr); align-items: center; gap: 8px; }
.sequence-messages { display: grid; gap: 8px; margin-top: 16px; }
.sequence-message { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(160px, 2fr) minmax(120px, 1fr); align-items: center; gap: 10px; padding: 8px 0; border-top: 1px solid var(--line); }
.sequence-message > span:first-child, .sequence-message > span:last-child { font-weight: 700; }
.sequence-track { display: grid; justify-items: center; gap: 4px; color: var(--muted); text-align: center; }
.sequence-track.response .diagram-arrow { background: var(--yellow); }
.sequence-track.response .diagram-arrow::after { border-color: var(--yellow); }
footer { min-height: 50px; display: flex; justify-content: space-between; gap: 18px; padding: 14px 24px; border-top: 1px solid var(--line); color: var(--muted); background: #151a20; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 900px) {
  .site-header { position: static; grid-template-columns: 1fr; gap: 10px; }
  .phase { justify-self: start; }
  .site-grid { grid-template-columns: 1fr; }
  .desktop-nav { display: none; }
  .mobile-nav { display: block; margin: 16px 20px 0; border: 1px solid var(--line); border-radius: 6px; background: #151a20; }
  .mobile-nav summary { min-height: 44px; padding: 9px 14px; color: var(--ink); cursor: pointer; font-weight: 700; }
  .mobile-nav[open] summary { border-bottom: 1px solid var(--line); }
  .mobile-nav-links { columns: 2; padding: 4px 8px 16px; }
  .mobile-nav-links > a, .mobile-nav-links .nav-group a { display: block; min-height: 34px; padding: 6px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; }
  .mobile-nav-links a:hover, .mobile-nav-links a.active { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
  .nav-group { break-inside: avoid; }
  .audience-grid, .status-band { grid-template-columns: 1fr; }
  .diagram-edges { grid-template-columns: 1fr; }
  .sequence-message { grid-template-columns: minmax(84px, 1fr) minmax(100px, 1.5fr) minmax(84px, 1fr); font-size: 13px; }
  main { padding: 28px 20px 56px; }
  footer { flex-wrap: wrap; }
}
@media (max-width: 520px) {
  .mobile-nav-links { columns: 1; }
  .diagram-pipeline { display: grid; grid-template-columns: 1fr; justify-items: stretch; }
  .diagram-pipeline > .diagram-node, .diagram-step .diagram-node { width: 100%; }
  .diagram-step { display: grid; justify-items: center; }
  .diagram-step .diagram-arrow { transform: rotate(90deg); }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
"""


PORTAL_JS = """(() => {
  const input = document.getElementById('portal-search');
  const results = document.getElementById('search-results');
  const source = document.getElementById('search-index');
  if (!input || !results || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const close = () => { results.hidden = true; results.replaceChildren(); };
  input.addEventListener('input', () => {
    const query = input.value.trim().toLocaleLowerCase();
    if (query.length < 2) { close(); return; }
    const matches = records.filter((record) => record.search.includes(query)).slice(0, 8);
    results.replaceChildren(...matches.map((record) => {
      const link = document.createElement('a');
      link.href = record.url;
      const title = document.createElement('strong');
      title.textContent = record.title;
      const detail = document.createElement('small');
      detail.textContent = `${record.audience} · ${record.summary}`;
      link.append(title, detail);
      return link;
    }));
    results.hidden = matches.length === 0;
  });
  input.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-box')) close(); });
})();
"""


def _home_content(
    config: PortalConfig,
    coverage: tuple[Any, ...],
    profiles: dict[str, Any],
    integrations: dict[str, Any],
) -> str:
    verified = sum(entry.status == "software-verified" for entry in coverage)
    release_blocking = sum(entry.release_blocking for entry in coverage)
    qualified_profiles = sum(
        profile.get("support_level") == "qualified" for profile in profiles["profiles"]
    )
    adapters = len(integrations["adapters"])
    cards = []
    for audience in config.audiences:
        first = audience.pages[0]
        cards.append(
            f'<section class="audience-card"><h2>{escape(audience.title)}</h2>'
            f'<p>{escape(audience.description)}</p><a href="{escape(first.url)}">'
            f"Open {escape(audience.title.lower())}</a></section>"
        )
    return f"""<section class="home-intro">
  <p class="breadcrumb">Documentation portal</p>
  <h1>HyperFlux Next</h1>
  <p class="lede">{escape(config.description)}</p>
  <div class="notice"><strong>Publication remains locked.</strong> This portal is a verified local artifact, not a released driver or public support promise.</div>
  <img class="system-map" src="assets/system-map.svg" alt="Applications flow through the SDK, bridge, kernel, and receiver">
</section>
<section class="audience-grid" aria-label="Documentation audiences">{"".join(cards)}</section>
<section aria-labelledby="truth-heading">
  <h2 id="truth-heading">Repository truth</h2>
  <div class="status-band">
    <div><strong>{len(coverage)}</strong><span>design sections tracked</span></div>
    <div><strong>{verified}</strong><span>software-verified sections</span></div>
    <div><strong>{qualified_profiles}</strong><span>qualified hardware profiles</span></div>
    <div><strong>{adapters}</strong><span>application adapters modeled</span></div>
  </div>
  <p>{release_blocking} sections still carry a release-blocking condition. The <a href="maintainers/coverage.html">coverage ledger</a> names each one without converting missing evidence into a green claim.</p>
</section>"""


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
    source_urls = {page.source: page.url for page in config.pages}
    search_records: list[dict[str, str]] = []
    for page in config.pages:
        source_text = (root / page.source).read_text(encoding="utf-8")
        search_records.append(
            {
                "title": page.title,
                "audience": page.audience_id.title(),
                "summary": page.summary,
                "url": page.url,
                "search": f"{page.title} {page.summary} {_plain_markdown(source_text)}".lower()[:12_000],
            }
        )
    search_records.sort(key=lambda record: (record["audience"], record["title"]))

    assets = output / "assets"
    assets.mkdir()
    (assets / "site.css").write_text(SITE_CSS, encoding="utf-8")
    (assets / "portal.js").write_text(PORTAL_JS, encoding="utf-8")
    (assets / "system-map.svg").write_text(_architecture_svg(), encoding="utf-8")
    (assets / "search-index.json").write_text(
        json.dumps(search_records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    copied_references: set[str] = set()
    renderer = markdown.Markdown(extensions=["fenced_code", "sane_lists", "tables", "toc"])
    for page in config.pages:
        source = root / page.source
        body = renderer.reset().convert(_compile_mermaid(source.read_text(encoding="utf-8")))
        body = _rewrite_links(
            body,
            root=root,
            output=output,
            source=source,
            current_url=page.url,
            source_urls=source_urls,
            copied_references=copied_references,
        )
        body = re.sub(r"<h1(?: [^>]*)?>.*?</h1>", "", body, count=1, flags=re.DOTALL)
        audience = next(item for item in config.audiences if item.id == page.audience_id)
        content = (
            f'<p class="breadcrumb">{escape(audience.title)} / {escape(page.title)}</p>'
            f'<article class="document"><h1>{escape(page.title)}</h1>'
            f'<p class="lede">{escape(page.summary)}</p>{body}</article>'
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
                search_records=[
                    {**record, "url": _relative_url(page.url, record["url"])}
                    for record in search_records
                ],
            ),
            encoding="utf-8",
        )

    coverage = load_design_coverage(root)
    profiles = compiled_profile_catalog(root)
    integrations = compiled_integration_catalog(root)
    (output / "index.html").write_text(
        _shell(
            config,
            current_url="index.html",
            title="Documentation",
            description=config.description,
            content=_home_content(config, coverage, profiles, integrations),
            search_records=search_records,
        ),
        encoding="utf-8",
    )

    material_paths = {
        "assurance/design-coverage.json",
        "docs/portal.json",
        "generated/integrations/catalog.json",
        "generated/profiles/catalog.json",
        "tools/hfxdev/portal.py",
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
        "schema": "hyperflux-documentation-portal-build-v1",
        "publication_authorized": False,
        "external_runtime_dependencies": False,
        "source_tree_sha256": source_digest,
        "materials": materials,
        "pages": len(config.pages) + 1,
        "files": files,
    }
    manifest = output / "portal-build-manifest.json"
    manifest.write_text(
        json.dumps(manifest_value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return PortalBuild(output=output, manifest=manifest, pages=len(config.pages) + 1, files=len(files))


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
        "publication_authorized",
        "external_runtime_dependencies",
        "source_tree_sha256",
        "materials",
        "pages",
        "files",
    }
    if set(value) != expected_keys or value["schema"] != "hyperflux-documentation-portal-build-v1":
        raise ModelError("portal build manifest is malformed")
    if value["publication_authorized"] is not False or value["external_runtime_dependencies"] is not False:
        raise ModelError("portal build violates its publication or runtime boundary")
    expected_files = _file_inventory(site)
    if value["files"] != expected_files:
        raise ModelError("portal file inventory differs from its manifest")
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
    if any(token in javascript for token in ("fetch(", "XMLHttpRequest", "WebSocket")):
        raise ModelError("portal JavaScript may not depend on network access")
    if value["pages"] != len(html_inspectors):
        raise ModelError("portal page count differs from rendered HTML")
    return value
