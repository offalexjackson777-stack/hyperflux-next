# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape

from .portal_metadata import head_metadata
from .portal_model import PortalAudience, PortalConfig
from .portal_routing import relative_url


def _active_audience(config: PortalConfig, current_url: str) -> PortalAudience:
    if current_url in {"index.html", "404.html"}:
        return config.audiences[0]
    if current_url.startswith("developers/design-book/"):
        return next(item for item in config.audiences if item.id == "developers")
    route = config.route_for_path(current_url)
    return next(item for item in config.audiences if item.id == route.audience_id)


def _active_link(current_url: str, target: str, label: str) -> str:
    active = ' class="active" aria-current="page"' if current_url == target else ""
    return (
        f'<a{active} href="{escape(relative_url(current_url, target))}">'
        f"{escape(label)}</a>"
    )


def _primary_navigation(config: PortalConfig, current_url: str) -> str:
    current_audience = _active_audience(config, current_url)
    links = []
    for audience in config.audiences:
        target = config.route(audience.landing_route).path
        active = (
            ' class="active" aria-current="page"'
            if audience.id == current_audience.id and current_url not in {"index.html", "404.html"}
            else ""
        )
        links.append(
            f'<a{active} href="{escape(relative_url(current_url, target))}">'
            f"{escape(audience.primary_label)}</a>"
        )
    return "".join(links)


def _navigation(config: PortalConfig, current_url: str) -> str:
    audience = _active_audience(config, current_url)
    page_links = "".join(
        _active_link(current_url, page.path, page.title) for page in audience.pages
    )
    cross_links = "".join(
        _active_link(
            current_url,
            config.route(item.landing_route).path,
            item.title,
        )
        for item in config.audiences
        if item.id != audience.id
    )
    return (
        f'<div class="nav-context"><span class="nav-label">{escape(audience.title)}</span>'
        f'<p>{escape(audience.description)}</p></div>{page_links}'
        '<div class="nav-context nav-context--secondary"><span class="nav-label">Other paths</span></div>'
        f"{cross_links}"
    )


def shell(
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
    css = relative_url(current_url, "assets/site.css")
    script = relative_url(current_url, "assets/portal.js")
    search_index = relative_url(current_url, "assets/search-index.json")
    root_url = relative_url(current_url, "index.html")
    extra_script_tags = "".join(
        f'<script src="{escape(relative_url(current_url, value))}" defer></script>'
        for value in extra_scripts
    )
    metadata = head_metadata(
        config,
        current_url=current_url,
        title=title,
        description=description,
    )
    return f"""<!doctype html>
<html lang="{escape(config.language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
{metadata}
  <title>{escape(title)} | {escape(config.title)}</title>
  <link rel="stylesheet" href="{escape(css)}">
</head>
<body class="page-kind-{escape(page_kind)}" data-search-index="{escape(search_index)}" data-portal-root="{escape(root_url)}">
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <a class="brand" href="{escape(relative_url(current_url, 'index.html'))}">
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
    <a href="{escape(relative_url(current_url, config.route('release-gates').path))}">Release gates</a>
  </footer>
  <script src="{escape(script)}" defer></script>
  {extra_script_tags}
</body>
</html>
"""
