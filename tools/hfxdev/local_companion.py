# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import ModelError, load_json


EVIDENCE_STATES = ("active", "sleeping", "disconnected", "unknown")
WRITE_REQUIREMENTS = (
    "explicit-user-confirmation",
    "bounded-scope",
    "expiry",
    "single-local-origin",
)


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label} must contain exactly {', '.join(sorted(keys))}")
    return value


def load_local_companion(root: Path) -> dict[str, Any]:
    value = _exact(
        load_json(root / "runtime" / "local-companion.json"),
        {
            "$schema",
            "schema",
            "base_url",
            "transport",
            "snapshot",
            "write_capabilities",
            "privacy",
        },
        "local companion contract",
    )
    if value["$schema"] != "../schemas/local-companion.schema.json":
        raise ModelError("local companion contract has a non-canonical schema reference")
    if value["schema"] != "hyperflux-local-companion-v1":
        raise ModelError("unsupported local companion contract")
    if value["base_url"] != "http://127.0.0.1:47427":
        raise ModelError("local companion must bind the canonical loopback origin")
    if value["transport"] != "loopback-http-json":
        raise ModelError("local companion transport must remain loopback HTTP JSON")

    snapshot = _exact(
        value["snapshot"],
        {"method", "path", "schema", "evidence_states", "read_only"},
        "local snapshot endpoint",
    )
    if (
        snapshot["method"] != "GET"
        or snapshot["path"] != "/v1/snapshot"
        or snapshot["schema"] != "hyperflux-local-snapshot-v1"
        or tuple(snapshot["evidence_states"]) != EVIDENCE_STATES
        or snapshot["read_only"] is not True
    ):
        raise ModelError("local snapshot endpoint violates the read-only contract")

    capabilities = _exact(
        value["write_capabilities"],
        {"default_state", "grant_path", "maximum_ttl_seconds", "requirements"},
        "local write capabilities",
    )
    ttl = capabilities["maximum_ttl_seconds"]
    if (
        capabilities["default_state"] != "disabled"
        or capabilities["grant_path"] != "/v1/capabilities/lighting"
        or not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or not 1 <= ttl <= 300
        or tuple(capabilities["requirements"]) != WRITE_REQUIREMENTS
    ):
        raise ModelError("local writes require one bounded, confirmed, expiring capability")

    privacy = _exact(
        value["privacy"],
        {
            "hardware_serials_exposed",
            "stable_host_identifiers_exposed",
            "silent_network_uploads",
            "direct_usb_access",
            "webhid_access",
        },
        "local companion privacy boundary",
    )
    if any(setting is not False for setting in privacy.values()):
        raise ModelError("local companion privacy and direct-hardware boundaries must remain disabled")
    return value
