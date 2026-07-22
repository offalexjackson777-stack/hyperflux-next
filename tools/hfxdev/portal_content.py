# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape, unescape
import re
from typing import Any

from .governance import GitHubGovernance
from .portal_model import PortalConfig, PortalRoute
from .portal_routing import relative_url, repository_url


HTML_HEADING = re.compile(
    r'<h(?P<level>[23]) id="(?P<id>[^"]+)">(?P<label>.*?)</h(?P=level)>',
    re.DOTALL,
)
HTML_TAG = re.compile(r"<[^>]+>")


def _reading_minutes(source: str) -> int:
    words = len(re.sub(r"[^A-Za-z0-9]+", " ", source).split())
    return max(1, round(words / 220))


def source_details(
    governance: GitHubGovernance,
    *,
    source: str,
    current_url: str,
    generated: bool | None = None,
    atlas_url: str = "atlas/index.html",
) -> str:
    is_generated = (
        source.startswith("docs/generated/") or source.startswith("generated/")
        if generated is None
        else generated
    )
    source_url = repository_url(governance, source, edit=not is_generated)
    if is_generated:
        description = (
            f'<strong>Generated projection.</strong> <a href="{escape(source_url)}">View the source projection</a>. '
            f'Use the <a href="{escape(relative_url(current_url, atlas_url))}">Repository Atlas</a> '
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


def outline(body: str) -> str:
    links = []
    for match in HTML_HEADING.finditer(body):
        label = unescape(HTML_TAG.sub("", match.group("label"))).strip()
        if label:
            links.append(
                f'<a class="outline-level-{match.group("level")}" href="#{escape(match.group("id"))}">{escape(label)}</a>'
            )
    if len(links) < 2:
        return ""
    return (
        '<aside class="page-outline" aria-label="On this page"><span>On this page</span>'
        f'{"".join(links)}<a class="outline-top" href="#main-content">Back to top</a></aside>'
    )


def document_content(
    config: PortalConfig,
    page: PortalRoute,
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
        f'<nav class="breadcrumb" aria-label="Breadcrumb"><a href="{escape(relative_url(page.url, "index.html"))}">Home</a>'
        f'<a href="{escape(relative_url(page.url, config.route(audience.landing_route).path))}">{escape(audience.title)}</a>'
        f'<span>{escape(page.title)}</span></nav>'
    )
    origin = "Generated reference" if "/generated/" in page.source else "Canonical documentation"
    pager = []
    if previous is not None:
        pager.append(
            f'<a rel="prev" href="{escape(relative_url(page.url, previous.url))}"><span>Previous</span><strong>{escape(previous.title)}</strong></a>'
        )
    if following is not None:
        pager.append(
            f'<a rel="next" href="{escape(relative_url(page.url, following.url))}"><span>Next</span><strong>{escape(following.title)}</strong></a>'
        )
    content = (
        f'<article class="document document--{escape(page.kind)}">{crumbs}'
        f'<header class="page-hero"><p class="page-kicker">{escape(page.kind.title())}</p><h1>{escape(page.title)}</h1>'
        f'<p class="lede">{escape(page.summary)}</p><div class="page-meta"><span>{escape(origin)}</span>'
        f'<span>{_reading_minutes(source_text)} min read</span></div></header>'
        f'<div class="document-body">{body}</div>'
        f'{source_details(governance, source=page.source, current_url=page.url, atlas_url=config.route("repository-atlas").path)}'
        f'<nav class="page-pager" aria-label="Adjacent pages">{"".join(pager)}</nav></article>'
    )
    return content, outline(body)


def home_content(config: PortalConfig, readiness: dict[str, Any]) -> str:
    devices = config.route("device-lab").path
    overview = config.route("overview").path
    state = config.route("repository-state").path
    architecture = config.route("architecture").path
    software = readiness["software"]
    hardware = readiness["hardware"]
    evidence = readiness["evidence"]
    repository = readiness["repository"]
    return f"""<article class="home">
  <section class="home-hero"><div class="home-copy"><p class="page-kicker">Linux receiver foundation</p><h1>HyperFlux Next</h1><p class="home-lede">{escape(config.description)}</p><p class="home-boundary">Public source for review; product unreleased. Support claims stay bounded to recorded evidence.</p><div class="home-actions"><a class="button button--primary" href="{escape(devices)}">Explore tested hardware</a><a class="button" href="{escape(overview)}">Understand the system</a></div></div><div class="home-signal" aria-label="Current project state"><span>Current phase</span><strong>{escape(readiness['publication']['label'])}</strong><p>{escape(readiness['publication']['summary'])}</p><a href="{escape(state)}">Inspect readiness</a></div></section>
  <section class="flow-section" aria-labelledby="flow-title"><div class="section-intro"><p class="page-kicker">One direction of responsibility</p><h2 id="flow-title">Applications stay separate from hardware transport</h2><p>Integrations express intent through the SDK. One bridge validates policy and reaches the receiver through the minimal kernel driver.</p></div><img class="system-map" src="assets/system-map.svg" alt="Applications flow through the SDK, bridge, kernel, and receiver"></section>
  <section class="path-section" aria-labelledby="path-title"><div class="section-intro"><p class="page-kicker">Choose a path</p><h2 id="path-title">Start with what you need</h2></div><div class="path-grid"><a href="{escape(overview)}"><span>01</span><strong>Use HyperFlux</strong><p>Installation, applications, supported devices, privacy, and troubleshooting.</p></a><a href="{escape(architecture)}"><span>02</span><strong>Build with HyperFlux</strong><p>Architecture, SDK contracts, protocol, development, and verification.</p></a><a href="{escape(state)}"><span>03</span><strong>Review readiness</strong><p>Release gates, migration decisions, verification, and performance boundaries.</p></a></div></section>
  <section class="truth-section" aria-labelledby="truth-heading"><div class="section-intro"><p class="page-kicker">Generated public readiness</p><h2 id="truth-heading">One vocabulary, one projection</h2><p>README and Pages consume the same generated readiness record.</p></div><div class="status-band"><div><strong>{software['gates_ready']}/{software['gates_total']}</strong><span>release gates ready in software</span></div><div><strong>{hardware['qualified_routes']}</strong><span>routes with physical evidence</span></div><div><strong>{evidence['hardware_gates'] + evidence['lifecycle_gates']}</strong><span>evidence gates remaining</span></div><div><strong>{repository['atlas_subsystems']}</strong><span>Atlas subsystem records</span></div></div><p class="truth-note">{escape(evidence['summary'])} The portal performs no live device query and has no hardware-write access.</p></section>
</article>"""
