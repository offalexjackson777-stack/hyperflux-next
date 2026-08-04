# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path

from .model import ModelError, load_json, require_unique


COVERAGE_KEYS = {
    "$schema",
    "schema",
    "public_document",
    "publication",
    "prior_art_review",
    "eras",
}
PUBLICATION_KEYS = {
    "voice",
    "technical_appendix",
    "direct_quotes",
    "public_sources_only",
    "privacy_review",
}
PRIOR_ART_KEYS = {
    "scope",
    "outcome",
    "absolute_novelty_claims",
    "sources",
}
ERA_KEYS = {"id", "title", "order", "technical_sources"}
SOURCE_KEYS = {"kind", "reference", "supports"}
EXPECTED_ERAS = (
    ("receiver-problem", "The Receiver Problem"),
    ("transport-discovery", "Learning The Transport"),
    ("bridge-foundation", "From Prototype To Bridge"),
    ("application-compatibility", "Preserving Application Ownership"),
    ("evidence-recovery", "Evidence And Recovery"),
    ("smaller-2-0", "Building A Smaller 2.0"),
)


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
        raise ModelError(f"{label}: expected a public repository path")
    resolved = root / candidate
    if resolved.is_symlink() or not resolved.is_file():
        raise ModelError(f"{label}: missing or symbolic authority")
    return path


def load_journey_source_coverage(root: Path) -> dict[str, object]:
    root = root.resolve()
    value = _exact(
        load_json(root / "assurance" / "journey-source-coverage.json"),
        COVERAGE_KEYS,
        "journey source coverage",
    )
    if value["$schema"] != "../schemas/journey-source-coverage.schema.json":
        raise ModelError("journey source coverage has a non-canonical schema reference")
    if value["schema"] != "hyperflux-journey-source-coverage-v3":
        raise ModelError("unsupported journey source coverage schema")
    if value["public_document"] != "docs/project/journey.md":
        raise ModelError("journey source coverage points to the wrong public document")

    publication = _exact(value["publication"], PUBLICATION_KEYS, "journey publication")
    if publication != {
        "voice": "first-person-founder",
        "technical_appendix": True,
        "direct_quotes": False,
        "public_sources_only": True,
        "privacy_review": "passed",
    }:
        raise ModelError("journey publication weakens voice, privacy, or source policy")

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
    if not isinstance(raw_eras, list) or len(raw_eras) != len(EXPECTED_ERAS):
        raise ModelError("journey source coverage requires exactly six eras")
    for index, (raw, expected) in enumerate(zip(raw_eras, EXPECTED_ERAS), start=1):
        era = _exact(raw, ERA_KEYS, f"journey source coverage era {index}")
        if era["id"] != expected[0] or era["title"] != expected[1]:
            raise ModelError("journey source coverage era identity differs")
        if _integer(era["order"], f"journey era {index} order", 1, 6) != index:
            raise ModelError("journey source coverage eras must use contiguous order")
        sources = era["technical_sources"]
        if not isinstance(sources, list) or not 2 <= len(sources) <= 16:
            raise ModelError("each journey era requires 2 through 16 technical sources")
        identities: list[str] = []
        kinds: set[str] = set()
        for source_index, raw_source in enumerate(sources):
            source = _exact(raw_source, SOURCE_KEYS, f"journey era {index} source {source_index}")
            kind = _text(source["kind"], f"journey era {index} source kind", 16)
            if kind not in {"authority", "test", "upstream"}:
                raise ModelError("journey technical source has an unsupported kind")
            reference = _public_source_path(
                root, source["reference"], f"journey era {index} source reference"
            )
            _text(source["supports"], f"journey era {index} source claim", 240)
            identities.append(f"{kind}:{reference}")
            kinds.add(kind)
        require_unique(identities, f"journey era {index} technical source")
        if "authority" not in kinds or "test" not in kinds:
            raise ModelError("each journey era requires public authority and test sources")
    return value
