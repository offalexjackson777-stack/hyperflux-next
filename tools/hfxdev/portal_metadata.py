# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
import json
from pathlib import PurePosixPath
from urllib.parse import urljoin

from .portal_model import PortalConfig
from .portal_routing import relative_url


def canonical_url(config: PortalConfig, path: str) -> str:
    public_path = path
    if path == "index.html":
        public_path = ""
    elif path.endswith("/index.html"):
        public_path = path[: -len("index.html")]
    return urljoin(config.canonical_url, public_path)


def social_image_path(config: PortalConfig) -> str:
    return f"assets/{PurePosixPath(config.social_image).name}"


def head_metadata(
    config: PortalConfig,
    *,
    current_url: str,
    title: str,
    description: str,
) -> str:
    canonical = canonical_url(config, current_url)
    social_image = urljoin(config.canonical_url, social_image_path(config))
    page_title = f"{title} | {config.title}"
    favicon = relative_url(current_url, "assets/favicon.svg")
    manifest = relative_url(current_url, "site.webmanifest")
    return f"""  <meta name="description" content="{escape(description)}">
  <meta name="theme-color" content="#10151a">
  <link rel="canonical" href="{escape(canonical)}">
  <link rel="icon" href="{escape(favicon)}" type="image/svg+xml">
  <link rel="manifest" href="{escape(manifest)}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{escape(config.title)}">
  <meta property="og:locale" content="en_US">
  <meta property="og:title" content="{escape(page_title)}">
  <meta property="og:description" content="{escape(description)}">
  <meta property="og:url" content="{escape(canonical)}">
  <meta property="og:image" content="{escape(social_image)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{escape(page_title)}">
  <meta name="twitter:description" content="{escape(description)}">
  <meta name="twitter:image" content="{escape(social_image)}">"""


def favicon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="8" fill="#10151a"/>
  <path d="M14 15h8v13h20V15h8v34h-8V36H22v13h-8z" fill="#7ee0c3"/>
</svg>
"""


def web_manifest(config: PortalConfig) -> str:
    value = {
        "name": config.title,
        "short_name": "HyperFlux",
        "description": config.description,
        "id": "./",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#10151a",
        "theme_color": "#10151a",
        "icons": [
            {
                "src": "assets/favicon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def sitemap_xml(config: PortalConfig) -> str:
    locations = "\n".join(
        f"  <url><loc>{escape(canonical_url(config, route.path))}</loc></url>"
        for route in config.routes
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locations}\n"
        "</urlset>\n"
    )


def robots_txt(config: PortalConfig) -> str:
    return f"User-agent: *\nAllow: /\nSitemap: {urljoin(config.canonical_url, 'sitemap.xml')}\n"


def not_found_content(config: PortalConfig) -> str:
    return f"""<article class="document document--concept">
  <header class="page-hero"><p class="page-kicker">404</p><h1>Page not found</h1>
  <p class="lede">That documentation route does not exist or has moved.</p></header>
  <div class="document-body"><p>Return to the <a href="index.html">{escape(config.title)} documentation home</a>, use search, or inspect the <a href="atlas/index.html">Repository Atlas</a>.</p></div>
</article>"""
