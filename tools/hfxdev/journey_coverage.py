# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import re

from .journey import load_journey_policy
from .model import ModelError, load_json, require_unique


COVERAGE_KEYS = {
    "$schema",
    "schema",
    "public_document",
    "publication",
    "private_evidence",
    "prior_art_review",
    "eras",
}
PUBLICATION_KEYS = {
    "voice",
    "technical_appendix",
    "publication_mode",
    "direct_quotes_approved",
    "raw_project_records_published",
    "privacy_review",
}
PRIVATE_KEYS = {
    "pipeline_overlay_sha256",
    "source_snapshot_sha256",
    "source_manifest_sha256",
    "source_ledger_sha256",
    "curation_plan_sha256",
    "curation_manifest_sha256",
    "curated_ledger_sha256",
    "extracted_event_count",
    "curated_event_count",
}
PRIOR_ART_KEYS = {
    "scope",
    "outcome",
    "absolute_novelty_claims",
    "sources",
}
ERA_KEYS = {
    "id",
    "title",
    "order",
    "motivation_event_count",
    "technical_sources",
}
SOURCE_KEYS = {"kind", "reference", "supports"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise ModelError(
            f"{label}: missing {missing or 'none'}; unknown {extra or 'none'}"
        )
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{label}: expected an integer")
    if not minimum <= value <= maximum:
        raise ModelError(f"{label}: outside {minimum} through {maximum}")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ModelError(f"{label}: expected a non-empty trimmed string")
    if len(value) > maximum or "\n" in value or "\r" in value:
        raise ModelError(f"{label}: exceeds its single-line bound")
    return value


def _public_source_path(root: Path, value: object, label: str) -> str:
    path = _text(value, label, 256)
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.parts[0] == "build":
        raise ModelError(f"{label}: private or non-relative path")
    resolved = root / candidate
    if resolved.is_symlink() or not resolved.is_file():
        raise ModelError(f"{label}: missing or symbolic authority")
    return path


def _sha256(value: object, label: str) -> str:
    digest = _text(value, label, 64)
    if SHA256_RE.fullmatch(digest) is None:
        raise ModelError(f"{label}: expected a lowercase SHA-256")
    return digest


def load_journey_source_coverage(root: Path) -> dict[str, object]:
    root = root.resolve()
    value = _exact(
        load_json(root / "assurance" / "journey-source-coverage.json"),
        COVERAGE_KEYS,
        "journey source coverage",
    )
    if value["$schema"] != "../schemas/journey-source-coverage.schema.json":
        raise ModelError("journey source coverage has a non-canonical schema reference")
    if value["schema"] != "hyperflux-journey-source-coverage-v2":
        raise ModelError("unsupported journey source coverage schema")
    if value["public_document"] != "docs/project/journey.md":
        raise ModelError("journey source coverage points to the wrong public document")

    publication = _exact(value["publication"], PUBLICATION_KEYS, "journey publication")
    expected_publication = {
        "voice": "first-person-founder",
        "technical_appendix": True,
        "publication_mode": "paraphrase-only",
        "direct_quotes_approved": 0,
        "raw_project_records_published": False,
        "privacy_review": "passed",
    }
    if publication != expected_publication:
        raise ModelError("journey publication weakens voice, privacy, or quotation policy")

    private = _exact(value["private_evidence"], PRIVATE_KEYS, "private journey evidence")
    hash_keys = PRIVATE_KEYS - {"extracted_event_count", "curated_event_count"}
    for key in sorted(hash_keys):
        _sha256(private[key], f"private journey evidence {key}")
    policy = load_journey_policy(root)
    if private["pipeline_overlay_sha256"] != policy.private_overlay_sha256:
        raise ModelError("journey private overlay binding differs from extraction policy")
    extracted = _integer(
        private["extracted_event_count"], "extracted journey event count", 1, 100000
    )
    curated = _integer(
        private["curated_event_count"], "curated journey event count", 6, extracted
    )

    prior_art = _exact(value["prior_art_review"], PRIOR_ART_KEYS, "journey prior-art review")
    if (
        prior_art["scope"] != "bounded-pinned-authority-review"
        or prior_art["outcome"] != "no-absolute-novelty-claim"
        or prior_art["absolute_novelty_claims"] != 0
    ):
        raise ModelError("journey prior-art review cannot authorize an absolute novelty claim")
    prior_sources = prior_art["sources"]
    if not isinstance(prior_sources, list) or not 3 <= len(prior_sources) <= 12:
        raise ModelError("journey prior-art review requires 3 through 12 sources")
    normalized_prior = [
        _public_source_path(root, source, "journey prior-art source")
        for source in prior_sources
    ]
    require_unique(normalized_prior, "journey prior-art source")

    raw_eras = value["eras"]
    if not isinstance(raw_eras, list) or len(raw_eras) != 6:
        raise ModelError("journey source coverage requires exactly six eras")
    motivation_count = 0
    for index, (raw, expected) in enumerate(zip(raw_eras, policy.eras), start=1):
        era = _exact(raw, ERA_KEYS, f"journey source coverage era {index}")
        expected_id, expected_title = expected
        if era["id"] != expected_id or era["title"] != expected_title:
            raise ModelError("journey source coverage era identity differs from policy")
        order = _integer(
            era["order"], f"journey source coverage era {index} order", 1, 6
        )
        if order != index:
            raise ModelError("journey source coverage eras must use contiguous order")
        motivation_count += _integer(
            era["motivation_event_count"],
            f"journey source coverage era {index} motivation count",
            1,
            curated,
        )
        sources = era["technical_sources"]
        if not isinstance(sources, list) or not 2 <= len(sources) <= 16:
            raise ModelError("each journey era requires 2 through 16 technical sources")
        identities: list[str] = []
        source_kinds: set[str] = set()
        for source_index, raw_source in enumerate(sources):
            source = _exact(
                raw_source,
                SOURCE_KEYS,
                f"journey era {index} source {source_index}",
            )
            kind = source["kind"]
            if kind not in {"commit", "authority", "evidence"}:
                raise ModelError("journey technical source has an unsupported kind")
            reference = _text(
                source["reference"], f"journey era {index} source reference", 256
            )
            if kind == "commit":
                if COMMIT_RE.fullmatch(reference) is None:
                    raise ModelError("journey commit source is not a full revision")
            else:
                _public_source_path(
                    root, reference, f"journey era {index} source reference"
                )
            _text(source["supports"], f"journey era {index} source claim", 240)
            identities.append(f"{kind}:{reference}")
            source_kinds.add(kind)
        require_unique(identities, f"journey era {index} technical source")
        if "commit" not in source_kinds or not ({"authority", "evidence"} & source_kinds):
            raise ModelError("each journey era requires commit and repository authority")
    if motivation_count != curated:
        raise ModelError("journey era counts do not cover the curated private ledger")
    return value
