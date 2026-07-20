# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ModelError(ValueError):
    """Raised when canonical repository data violates a foundation rule."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ModelError(f"{path}: top-level value must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ModelError(f"duplicate {label}: {', '.join(duplicates)}")


def load_foundation(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    constitution = load_json(root / "architecture" / "constitution.json")
    sources = load_json(root / "migration" / "sources.json")
    ledger = load_json(root / "migration" / "ledger.json")
    return constitution, sources, ledger

