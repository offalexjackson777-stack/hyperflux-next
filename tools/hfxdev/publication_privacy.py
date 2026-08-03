# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Iterable

from .journey import load_journey_policy, load_journey_private_overlay
from .model import ModelError, load_json, require_unique


AUTHORITY_KEYS = {
    "$schema",
    "schema",
    "scope",
    "normalization",
    "private_overlay_sha256",
    "forbidden_term_sha256",
    "allowed_protocol_notation",
}
SCOPE_KEYS = {
    "tracked_and_unignored_files_only",
    "paths_scanned",
    "utf8_text_scanned",
    "binary_payloads_ascii_scanned",
    "maximum_text_file_bytes",
}
NORMALIZATION_KEYS = {
    "unicode_casefold",
    "non_alphanumeric_separator",
    "maximum_words_per_term",
}
TERM_KEYS = {"id", "word_count", "sha256"}
ALLOWED_KEYS = {"tokens", "reason"}
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"private-term-[0-9]{2}")
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise ModelError(f"{label}: missing {missing or 'none'}; unknown {extra or 'none'}")
    return value


def normalized_words(value: str) -> tuple[str, ...]:
    return tuple(TOKEN.findall(value.casefold()))


def normalized_term_sha256(value: str) -> str:
    words = normalized_words(value)
    if not words:
        raise ModelError("publication privacy term normalizes to no words")
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def load_publication_privacy(root: Path) -> dict[str, Any]:
    value = _exact(
        load_json(root / "assurance" / "publication-privacy.json"),
        AUTHORITY_KEYS,
        "publication privacy authority",
    )
    if value["$schema"] != "../schemas/publication-privacy.schema.json":
        raise ModelError("publication privacy authority has a non-canonical schema reference")
    if value["schema"] != "hyperflux-publication-privacy-v1":
        raise ModelError("unsupported publication privacy schema")
    scope = _exact(value["scope"], SCOPE_KEYS, "publication privacy scope")
    if scope != {
        "tracked_and_unignored_files_only": True,
        "paths_scanned": True,
        "utf8_text_scanned": True,
        "binary_payloads_ascii_scanned": True,
        "maximum_text_file_bytes": 33_554_432,
    }:
        raise ModelError("publication privacy scope drifted")
    normalization = _exact(
        value["normalization"], NORMALIZATION_KEYS, "publication privacy normalization"
    )
    if normalization != {
        "unicode_casefold": True,
        "non_alphanumeric_separator": True,
        "maximum_words_per_term": 4,
    }:
        raise ModelError("publication privacy normalization drifted")
    private_overlay_sha256 = value["private_overlay_sha256"]
    if (
        not isinstance(private_overlay_sha256, str)
        or SHA256.fullmatch(private_overlay_sha256) is None
        or private_overlay_sha256 != load_journey_policy(root).private_overlay_sha256
    ):
        raise ModelError("publication privacy private-overlay binding drifted")
    raw_terms = value["forbidden_term_sha256"]
    if not isinstance(raw_terms, list) or not 8 <= len(raw_terms) <= 64:
        raise ModelError("publication privacy authority requires 8 through 64 deny terms")
    terms: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_terms, start=1):
        item = _exact(raw, TERM_KEYS, f"publication privacy term {index}")
        if item["id"] != f"private-term-{index:02d}" or IDENTIFIER.fullmatch(item["id"]) is None:
            raise ModelError("publication privacy term ids must be contiguous and opaque")
        if (
            isinstance(item["word_count"], bool)
            or not isinstance(item["word_count"], int)
            or not 1 <= item["word_count"] <= normalization["maximum_words_per_term"]
            or not isinstance(item["sha256"], str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ModelError(f"publication privacy term {index} is malformed")
        terms.append(item)
    require_unique([item["sha256"] for item in terms], "publication privacy term digest")
    allowed = _exact(
        value["allowed_protocol_notation"], ALLOWED_KEYS, "allowed protocol notation"
    )
    if allowed != {
        "tokens": ["ai"],
        "reason": "D-Bus and OpenRazer signature notation is protocol data, not authorship metadata.",
    }:
        raise ModelError("allowed protocol notation drifted")
    return value


def _tracked_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ModelError(result.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    names = result.stdout.split(b"\0")
    if names and not names[-1]:
        names.pop()
    try:
        relative = sorted({name.decode("utf-8") for name in names})
    except UnicodeDecodeError as error:
        raise ModelError("publishable Git path is not UTF-8") from error
    return tuple(root / name for name in relative)


def _matching_term_ids(
    value: str, terms: Iterable[dict[str, Any]], maximum_words: int
) -> tuple[str, ...]:
    words = normalized_words(value)
    by_digest = {item["sha256"]: item["id"] for item in terms}
    matches: set[str] = set()
    counts = sorted(
        {
            item["word_count"]
            for item in terms
            if 1 <= item["word_count"] <= maximum_words
        }
    )
    for count in counts:
        for index in range(0, len(words) - count + 1):
            digest = hashlib.sha256(
                " ".join(words[index : index + count]).encode("utf-8")
            ).hexdigest()
            if digest in by_digest:
                matches.add(by_digest[digest])
    return tuple(sorted(matches))


def scan_publishable_tree(root: Path) -> dict[str, Any]:
    root = root.resolve()
    authority = load_publication_privacy(root)
    terms = authority["forbidden_term_sha256"]
    maximum_words = authority["normalization"]["maximum_words_per_term"]
    maximum_bytes = authority["scope"]["maximum_text_file_bytes"]
    violations: list[dict[str, str]] = []
    text_files = 0
    binary_files = 0
    paths = _tracked_paths(root)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        for term_id in _matching_term_ids(relative, terms, maximum_words):
            violations.append({"path": relative, "surface": "path", "term_id": term_id})
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            content = os.readlink(path)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > maximum_bytes:
                raise ModelError(f"publishable text candidate exceeds scan bound: {relative}")
            raw = path.read_bytes()
            if b"\0" in raw:
                content = raw.decode("ascii", "replace")
                binary_files += 1
            else:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("ascii", "replace")
                    binary_files += 1
                else:
                    text_files += 1
        else:
            raise ModelError(f"publishable Git entry is neither regular nor symbolic: {relative}")
        if stat.S_ISLNK(metadata.st_mode):
            text_files += 1
        for term_id in _matching_term_ids(content, terms, maximum_words):
            violations.append({"path": relative, "surface": "content", "term_id": term_id})
    return {
        "schema": "hyperflux-publication-privacy-scan-v1",
        "publishable_files": len(paths),
        "utf8_text_files": text_files,
        "binary_files": binary_files,
        "violations": violations,
    }


def verify_publication_privacy(root: Path) -> dict[str, Any]:
    result = scan_publishable_tree(root)
    if result["violations"]:
        first = result["violations"][0]
        raise ModelError(
            "publishable tree contains a private product or authorship reference: "
            f"{first['path']} ({first['surface']}, {first['term_id']})"
        )
    return result


def validate_private_publication_overlay(root: Path, overlay_path: Path) -> None:
    authority = load_publication_privacy(root)
    overlay = load_journey_private_overlay(
        overlay_path, authority["private_overlay_sha256"]
    )
    actual = [
        {
            "id": f"private-term-{index:02d}",
            "word_count": len(normalized_words(term)),
            "sha256": normalized_term_sha256(term),
        }
        for index, term in enumerate(overlay.publication_forbidden_terms, start=1)
    ]
    if actual != authority["forbidden_term_sha256"]:
        raise ModelError("private publication terms do not match the public digest authority")
