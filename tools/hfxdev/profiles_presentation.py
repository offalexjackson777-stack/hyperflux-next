# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from typing import Any

from .model import ModelError
from .profiles_common import (
    GIT_COMMIT_PATTERN,
    PRESENTATION_KEYS,
    PRESENTATION_KEY_PATTERN,
    PRESENTATION_VARIANT_ROUTES,
    UPSTREAM_ID_PATTERN,
    _expect_keys,
    _expect_nonempty_string,
)


def _validate_presentation(profile: dict[str, Any]) -> None:
    identifier = profile["profile_id"]
    presentation = profile.get("presentation")
    if profile["kind"] != "child":
        if presentation is not None:
            raise ModelError(f"{identifier}: only child profiles may declare application presentation")
        return
    if not isinstance(presentation, dict):
        raise ModelError(f"{identifier}: child presentation metadata is required")
    _expect_keys(presentation, PRESENTATION_KEYS, f"{identifier} presentation")
    missing = sorted(PRESENTATION_KEYS - set(presentation))
    if missing:
        raise ModelError(f"{identifier}: incomplete presentation metadata: {', '.join(missing)}")

    upstream_id = _expect_nonempty_string(
        presentation["upstream_id"], f"{identifier} presentation upstream"
    )
    if UPSTREAM_ID_PATTERN.fullmatch(upstream_id) is None:
        raise ModelError(f"{identifier}: invalid presentation upstream id")
    _expect_nonempty_string(presentation["owner"], f"{identifier} presentation owner")
    _expect_nonempty_string(
        presentation["project_version"], f"{identifier} presentation project version"
    )
    source_commit = _expect_nonempty_string(
        presentation["source_commit"], f"{identifier} presentation source commit"
    )
    if GIT_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ModelError(f"{identifier}: presentation source commit must be 40 lowercase hex characters")
    model_key = _expect_nonempty_string(
        presentation["model_key"], f"{identifier} presentation model key"
    )
    if PRESENTATION_KEY_PATTERN.fullmatch(model_key) is None:
        raise ModelError(f"{identifier}: invalid presentation model key")
    layout_key = presentation["layout_key"]
    if layout_key is not None and (
        not isinstance(layout_key, str)
        or PRESENTATION_KEY_PATTERN.fullmatch(layout_key) is None
    ):
        raise ModelError(f"{identifier}: invalid presentation layout key")
    variant = presentation["transport_variant"]
    expected_route = PRESENTATION_VARIANT_ROUTES.get(variant)
    if expected_route is None:
        raise ModelError(f"{identifier}: invalid presentation transport variant")
    if expected_route not in profile["compatibility"]["routes"]:
        raise ModelError(
            f"{identifier}: presentation transport variant has no matching device route"
        )


def _validate_presentation_sources(
    profiles: list[dict[str, Any]], upstreams: dict[str, dict[str, Any]]
) -> None:
    for profile in profiles:
        presentation = profile.get("presentation")
        if presentation is None:
            continue
        upstream = upstreams.get(presentation["upstream_id"])
        if upstream is None:
            raise ModelError(
                f"{profile['profile_id']}: presentation references an unknown upstream"
            )
        expected = {
            "owner": upstream["name"],
            "project_version": upstream["version"],
            "source_commit": upstream["commit"],
        }
        mismatches = [
            field for field, value in expected.items() if presentation[field] != value
        ]
        if mismatches:
            raise ModelError(
                f"{profile['profile_id']}: presentation pin differs from the integration catalog: "
                + ", ".join(mismatches)
            )
