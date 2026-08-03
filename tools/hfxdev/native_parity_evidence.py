# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import re

from .native_parity_evidence_build import (
    _apply_legacy_firmware_result,
    _direct_observation,
    _git_revision,
    _receiver_observation,
    _request_evidence,
    build_direct_semantic_reference,
    build_live_parity_evidence,
    build_offline_parity_evidence,
)
from .native_parity_evidence_validation import validate_parity_evidence
from .private_artifact import write_new_private_json


def _default_cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "hyperflux-next" / "native-transport-parity"


def write_parity_evidence(
    value: dict[str, Any],
    *,
    cache_root: Path | None = None,
) -> Path:
    validate_parity_evidence(value)
    stamp = re.sub(r"[^0-9TZ]", "", value["created_at"])
    profile = re.sub(r"[^a-z0-9.-]", "-", value["profile_id"])
    return write_new_private_json(
        cache_root if cache_root is not None else _default_cache_root(),
        f"{stamp}-{profile}.json",
        value,
    )
