# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit

from .governance import GitHubGovernance
from .model import ModelError


LINK_ATTRIBUTE = re.compile(r'(?P<attribute>href|src)="(?P<url>[^"]+)"')


def relative_url(current: str, target: str) -> str:
    base = posixpath.dirname(current) or "."
    return posixpath.relpath(target, base)


def repository_url(
    governance: GitHubGovernance,
    path: str,
    *,
    directory: bool = False,
    edit: bool = False,
) -> str:
    action = "tree" if directory else ("edit" if edit else "blob")
    return (
        f"https://github.com/{governance.owner}/{governance.repository}/"
        f"{action}/{governance.default_branch}/{path}"
    )


def rewrite_links(
    html: str,
    *,
    root: Path,
    source: Path,
    current_url: str,
    source_urls: dict[str, str],
    governance: GitHubGovernance,
) -> str:
    """Resolve portal routes locally and link all other repository material to GitHub."""

    def replace(match: re.Match[str]) -> str:
        attribute = match.group("attribute")
        raw_url = match.group("url")
        parsed = urlsplit(raw_url)
        if parsed.scheme or raw_url.startswith("//"):
            if attribute == "src" or parsed.scheme not in {"https", "mailto"}:
                raise ModelError(
                    f"portal source {source.relative_to(root)} uses forbidden URL {raw_url}"
                )
            return match.group(0)
        if parsed.query or parsed.path.startswith("/"):
            raise ModelError(
                f"portal source {source.relative_to(root)} uses an unsafe local URL"
            )
        if not parsed.path:
            return match.group(0)
        decoded = unquote(parsed.path)
        target = (source.parent / decoded).resolve()
        try:
            relative = target.relative_to(root.resolve()).as_posix()
        except ValueError as error:
            raise ModelError(
                f"portal source {source.relative_to(root)} links outside the repository"
            ) from error
        if target.is_symlink() or not target.exists():
            raise ModelError(
                f"portal source {source.relative_to(root)} has a broken link: {decoded}"
            )
        if relative in source_urls:
            target_url = relative_url(current_url, source_urls[relative])
            if parsed.fragment:
                target_url += f"#{parsed.fragment}"
        else:
            if attribute == "src":
                raise ModelError(
                    f"portal source {source.relative_to(root)} references an undeclared local asset: {decoded}"
                )
            target_url = repository_url(
                governance, relative, directory=target.is_dir(), edit=False
            )
            if parsed.fragment:
                target_url += f"#{parsed.fragment}"
        return f'{attribute}="{escape(target_url, quote=True)}"'

    return LINK_ATTRIBUTE.sub(replace, html)
