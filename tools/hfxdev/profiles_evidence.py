# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .model import ModelError, load_json, require_unique
from .profiles_common import (
    PROFILE_DIRECTORIES,
    PUBLIC_EVIDENCE,
    _canonical_bytes,
    _expect_nonempty_string,
    _expect_string_list,
)


def _source_digest(documents: list[tuple[str, dict[str, Any]]]) -> str:
    digest = hashlib.sha256()
    for relative, document in sorted(documents):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_bytes(document))
        digest.update(b"\0")
    return digest.hexdigest()


def _source_inventory(root: Path, sources: dict[str, Any]) -> dict[str, set[str]]:
    inventory: dict[str, set[str]] = {}
    for source in sources["sources"]:
        if source["kind"] == "git":
            content = load_json(root / source["inventory"])
            inventory[source["id"]] = {
                entry["path"]
                for entry in content["entries"]
                if isinstance(entry.get("path"), str)
            }
        elif source["kind"] == "imported-document":
            inventory[source["id"]] = {source["imported_path"]}
    return inventory


def _load_documents(root: Path) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[tuple[str, dict[str, Any]]] = []
    profiles: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for directory in PROFILE_DIRECTORIES:
        for path in sorted((root / "profiles" / directory).glob("*.json")):
            value = load_json(path)
            relative = path.relative_to(root).as_posix()
            value["_source_path"] = relative
            profiles.append(value)
            documents.append((relative, {key: item for key, item in value.items() if key != "_source_path"}))
    for path in sorted((root / "profiles" / "candidates").glob("*.json")):
        value = load_json(path)
        value["_source_path"] = path.relative_to(root).as_posix()
        candidates.append(value)
        documents.append((value["_source_path"], {key: item for key, item in value.items() if key != "_source_path"}))
    return documents, profiles, candidates


def _validate_evidence(
    root: Path,
    registry: dict[str, Any],
    sources: dict[str, Any],
    capability_ids: set[str],
    target_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if registry.get("schema") != "hyperflux-evidence-registry-v1":
        raise ModelError("unsupported evidence registry schema")
    claims = registry.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ModelError("evidence registry is empty")
    identifiers = [_expect_nonempty_string(claim.get("id"), "evidence claim id") for claim in claims]
    require_unique(identifiers, "evidence claim id")
    if identifiers != sorted(identifiers):
        raise ModelError("evidence registry must be sorted by claim id")
    inventories = _source_inventory(root, sources)
    source_ids = set(inventories)
    for claim in claims:
        identifier = claim["id"]
        _expect_nonempty_string(claim.get("summary"), f"{identifier} summary")
        if claim.get("privacy") not in PUBLIC_EVIDENCE:
            raise ModelError(f"{identifier}: product evidence must be public or a public summary")
        profiles = set(_expect_string_list(claim.get("profile_ids"), f"{identifier} profile ids", allow_empty=False))
        unknown_targets = profiles - target_ids
        if unknown_targets:
            raise ModelError(f"{identifier}: unknown evidence targets: {', '.join(sorted(unknown_targets))}")
        qualified = set(_expect_string_list(claim.get("qualifies_capabilities"), f"{identifier} capabilities"))
        unknown_capabilities = qualified - capability_ids
        if unknown_capabilities:
            raise ModelError(f"{identifier}: unknown capabilities: {', '.join(sorted(unknown_capabilities))}")
        refs = claim.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise ModelError(f"{identifier}: source refs are required")
        for ref in refs:
            source_id = ref.get("source_id")
            if source_id not in source_ids:
                raise ModelError(f"{identifier}: unknown source {source_id}")
            paths = _expect_string_list(ref.get("paths"), f"{identifier} source paths", allow_empty=False)
            for path in paths:
                if path.startswith("/") or ".." in Path(path).parts:
                    raise ModelError(f"{identifier}: unsafe evidence path {path}")
                if path not in inventories[source_id]:
                    raise ModelError(f"{identifier}: source path is absent from frozen inventory: {path}")
        _expect_string_list(claim.get("limitations"), f"{identifier} limitations")
    return {claim["id"]: claim for claim in claims}
