# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


PORTAL_KEYS = {"$schema", "schema", "site", "navigation", "routes"}
SITE_KEYS = {
    "title",
    "description",
    "publication_state",
    "canonical_url",
    "repository_url",
    "language",
    "social_image",
}
NAVIGATION_KEYS = {
    "id",
    "title",
    "description",
    "primary_label",
    "landing_route",
    "routes",
}
ROUTE_KEYS = {
    "id",
    "path",
    "title",
    "summary",
    "source",
    "kind",
    "renderer",
    "audience",
    "search_terms",
}
PAGE_KINDS = {"home", "guide", "concept", "reference", "book", "ledger"}
RENDERERS = {
    "home",
    "markdown",
    "book",
    "reference",
    "coverage",
    "device-lab",
    "atlas",
    "state",
}
NAVIGATION_ORDER = ("users", "devices", "developers", "maintainers")
REQUIRED_ROUTES = {"home", "device-lab", "repository-atlas", "repository-state"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class PortalRoute:
    id: str
    path: str
    title: str
    summary: str
    source: str
    audience_id: str
    kind: str
    renderer: str
    search_terms: tuple[str, ...]

    @property
    def url(self) -> str:
        return self.path


@dataclass(frozen=True)
class PortalAudience:
    id: str
    title: str
    description: str
    primary_label: str
    landing_route: str
    pages: tuple[PortalRoute, ...]


@dataclass(frozen=True)
class PortalConfig:
    title: str
    description: str
    publication_state: str
    canonical_url: str
    repository_url: str
    language: str
    social_image: str
    audiences: tuple[PortalAudience, ...]
    routes: tuple[PortalRoute, ...]

    @property
    def pages(self) -> tuple[PortalRoute, ...]:
        return tuple(
            route
            for route in self.routes
            if route.renderer in {"markdown", "book", "reference", "coverage"}
        )

    def route(self, identifier: str) -> PortalRoute:
        try:
            return next(route for route in self.routes if route.id == identifier)
        except StopIteration as error:
            raise ModelError(f"portal route is not registered: {identifier}") from error

    def route_for_path(self, path: str) -> PortalRoute:
        try:
            return next(route for route in self.routes if route.path == path)
        except StopIteration as error:
            raise ModelError(f"portal path is not registered: {path}") from error


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


def _identifier(value: Any, label: str) -> str:
    identifier = _text(value, label, 64)
    if IDENTIFIER.fullmatch(identifier) is None:
        raise ModelError(f"{label}: invalid identifier")
    return identifier


def _repository_path(root: Path, value: Any, label: str) -> str:
    source = _text(value, label, 256)
    pure = PurePosixPath(source)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != source:
        raise ModelError(f"{label}: source must be a safe repository path")
    path = root / pure
    if path.is_symlink() or not path.is_file():
        raise ModelError(f"{label}: source is missing or symbolic: {source}")
    return source


def _route_path(value: Any, label: str) -> str:
    path = _text(value, label, 160)
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != path
        or pure.suffix != ".html"
    ):
        raise ModelError(f"{label}: path must be a safe HTML output path")
    return path


def _string_array(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ModelError(f"{label}: expected a non-empty string array")
    values = tuple(_text(item, label, 80) for item in value)
    require_unique(values, label)
    return values


def load_portal_config(root: Path) -> PortalConfig:
    value = _exact(
        load_json(root / "docs" / "portal.json"), PORTAL_KEYS, "documentation portal"
    )
    if value["schema"] != "hyperflux-documentation-portal-v2":
        raise ModelError("unsupported documentation portal schema")
    if value["$schema"] != "../schemas/documentation-portal.schema.json":
        raise ModelError("documentation portal has a non-canonical schema reference")

    site = _exact(value["site"], SITE_KEYS, "documentation portal site")
    if site["title"] != "HyperFlux Next" or site["publication_state"] != "public-pages-pre-release":
        raise ModelError("documentation portal must remain the reviewed public pre-release surface")
    canonical_url = _text(site["canonical_url"], "portal canonical URL", 240)
    repository_url = _text(site["repository_url"], "portal repository URL", 240)
    if not canonical_url.startswith("https://") or not canonical_url.endswith("/"):
        raise ModelError("portal canonical URL must be an absolute HTTPS directory URL")
    if not repository_url.startswith("https://github.com/"):
        raise ModelError("portal repository URL must be an absolute GitHub URL")

    raw_routes = value["routes"]
    if not isinstance(raw_routes, list) or len(raw_routes) < 8:
        raise ModelError("documentation portal requires at least eight routes")
    routes: list[PortalRoute] = []
    for index, raw_route in enumerate(raw_routes):
        route = _exact(raw_route, ROUTE_KEYS, f"portal route {index}")
        identifier = _identifier(route["id"], f"portal route {index} id")
        audience = _identifier(route["audience"], f"portal route {identifier} audience")
        renderer = _text(route["renderer"], f"portal route {identifier} renderer", 32)
        kind = _text(route["kind"], f"portal route {identifier} kind", 16)
        if audience not in NAVIGATION_ORDER:
            raise ModelError(f"portal route {identifier}: unknown audience")
        if renderer not in RENDERERS or kind not in PAGE_KINDS:
            raise ModelError(f"portal route {identifier}: unsupported renderer or kind")
        routes.append(
            PortalRoute(
                id=identifier,
                path=_route_path(route["path"], f"portal route {identifier}"),
                title=_text(route["title"], f"portal route {identifier} title", 80),
                summary=_text(route["summary"], f"portal route {identifier} summary", 180),
                source=_repository_path(root, route["source"], f"portal route {identifier}"),
                audience_id=audience,
                kind=kind,
                renderer=renderer,
                search_terms=_string_array(
                    route["search_terms"], f"portal route {identifier} search terms", allow_empty=True
                ),
            )
        )
    require_unique([route.id for route in routes], "portal route id")
    require_unique([route.path for route in routes], "portal route path")
    if REQUIRED_ROUTES - {route.id for route in routes}:
        raise ModelError("documentation portal is missing a required public route")
    if next(route for route in routes if route.id == "home").path != "index.html":
        raise ModelError("portal home route must render index.html")
    if sum(route.renderer == "home" for route in routes) != 1:
        raise ModelError("documentation portal requires exactly one home renderer")

    route_by_id = {route.id: route for route in routes}
    raw_navigation = value["navigation"]
    if not isinstance(raw_navigation, list) or len(raw_navigation) != len(NAVIGATION_ORDER):
        raise ModelError("documentation portal requires four navigation paths")
    if [item.get("id") for item in raw_navigation if isinstance(item, dict)] != list(NAVIGATION_ORDER):
        raise ModelError("documentation portal navigation must use the canonical order")
    audiences: list[PortalAudience] = []
    assigned_routes: list[str] = []
    for index, raw_navigation_item in enumerate(raw_navigation):
        item = _exact(raw_navigation_item, NAVIGATION_KEYS, f"portal navigation {index}")
        identifier = item["id"]
        route_ids = _string_array(item["routes"], f"portal navigation {identifier} routes")
        landing_route = _identifier(
            item["landing_route"], f"portal navigation {identifier} landing route"
        )
        if landing_route not in route_ids:
            raise ModelError(f"portal navigation {identifier}: landing route is not listed")
        try:
            pages = tuple(route_by_id[route_id] for route_id in route_ids)
        except KeyError as error:
            raise ModelError(
                f"portal navigation {identifier}: unknown route {error.args[0]}"
            ) from error
        if any(page.audience_id != identifier for page in pages):
            raise ModelError(f"portal navigation {identifier}: route belongs to another audience")
        assigned_routes.extend(route_ids)
        audiences.append(
            PortalAudience(
                id=identifier,
                title=_text(item["title"], f"portal navigation {identifier} title", 48),
                description=_text(
                    item["description"], f"portal navigation {identifier} description", 180
                ),
                primary_label=_text(
                    item["primary_label"], f"portal navigation {identifier} primary label", 20
                ),
                landing_route=landing_route,
                pages=pages,
            )
        )
    require_unique(assigned_routes, "portal navigation route")
    non_home_routes = {route.id for route in routes if route.renderer != "home"}
    if set(assigned_routes) != non_home_routes:
        raise ModelError("every non-home portal route must appear in navigation exactly once")

    return PortalConfig(
        title=site["title"],
        description=_text(site["description"], "portal description", 180),
        publication_state=site["publication_state"],
        canonical_url=canonical_url,
        repository_url=repository_url.rstrip("/"),
        language=_text(site["language"], "portal language", 16),
        social_image=_repository_path(root, site["social_image"], "portal social image"),
        audiences=tuple(audiences),
        routes=tuple(routes),
    )
