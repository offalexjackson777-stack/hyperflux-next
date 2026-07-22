# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from .model import ModelError
from .portal_model import PortalConfig


MAX_SEARCH_RECORDS = 64
MAX_SEARCH_TEXT = 900
MARKDOWN_HEADING = re.compile(r"^#{1,3}\s+(?P<label>.+)$", re.MULTILINE)
MARKUP = re.compile(r"[`*_>#\[\]()]")


def _source_headings(root: Path, source: str) -> str:
    path = root / source
    if path.suffix != ".md":
        return ""
    text = path.read_text(encoding="utf-8")
    labels = [MARKUP.sub(" ", match.group("label")) for match in MARKDOWN_HEADING.finditer(text)]
    return " ".join(labels[:24])


def build_search_records(
    root: Path,
    config: PortalConfig,
    *,
    chapter_records: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    audience_titles = {audience.id: audience.title for audience in config.audiences}
    records = [
        {
            "title": route.title,
            "audience": audience_titles[route.audience_id],
            "summary": route.summary,
            "url": route.path,
            "search": " ".join(
                (
                    route.title,
                    route.summary,
                    " ".join(route.search_terms),
                    _source_headings(root, route.source),
                )
            ).lower()[:MAX_SEARCH_TEXT],
        }
        for route in config.routes
    ]
    records.extend(
        {
            **record,
            "search": record["search"].strip().lower()[:MAX_SEARCH_TEXT],
        }
        for record in chapter_records
    )
    records.sort(key=lambda record: (record["audience"], record["title"]))
    verify_search_quality(records)
    return records


def verify_search_quality(records: list[dict[str, str]]) -> None:
    if not records or len(records) > MAX_SEARCH_RECORDS:
        raise ModelError(
            f"portal search must contain 1 through {MAX_SEARCH_RECORDS} task-oriented records"
        )
    keys = {"title", "audience", "summary", "url", "search"}
    identities: list[tuple[str, str]] = []
    for index, record in enumerate(records):
        if set(record) != keys or any(
            not isinstance(record[key], str) or not record[key].strip() for key in keys
        ):
            raise ModelError(f"portal search record {index} is malformed")
        if len(record["search"]) > MAX_SEARCH_TEXT:
            raise ModelError(f"portal search record {index} exceeds its text budget")
        if record["search"] != record["search"].lower():
            raise ModelError(f"portal search record {index} is not normalized")
        if "reference/" in record["url"]:
            raise ModelError("portal search may not index a mirrored repository reference")
        identities.append((record["title"], record["url"]))
    if len(set(identities)) != len(identities):
        raise ModelError("portal search contains duplicate title and URL records")
