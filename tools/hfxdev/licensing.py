# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


SPDX = re.compile(r"SPDX-License-Identifier:\s*(?P<expression>[^*\n\r\"]+)")
EXPRESSIONS = {
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-2.0 WITH Linux-syscall-note",
    "GPL-3.0-only",
    "MIT",
}
POLICY_KEYS = {
    "$schema",
    "schema",
    "default_expression",
    "rationale",
    "rules",
    "license_texts",
}
RULE_KEYS = {"path", "expression", "reason"}


def _safe_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ModelError(f"{label} must be a non-empty repository path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ModelError(f"{label} must be a safe repository path")
    return value


def load_licensing_policy(root: Path) -> dict[str, Any]:
    value = load_json(root / "governance" / "licensing.json")
    if set(value) != POLICY_KEYS:
        raise ModelError("licensing policy has missing or unknown fields")
    if value["$schema"] != "../schemas/licensing-policy.schema.json":
        raise ModelError("licensing policy has a non-canonical schema reference")
    if value.get("schema") != "hyperflux-licensing-policy-v1":
        raise ModelError("unsupported licensing policy")
    if value["default_expression"] != "GPL-2.0-only":
        raise ModelError("licensing policy must retain GPL-2.0-only as the project default")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ModelError("licensing policy requires a rationale")
    rules = value.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ModelError("licensing policy requires path rules")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) != RULE_KEYS:
            raise ModelError(f"licensing rule {index} has missing or unknown fields")
        _safe_path(rule["path"], f"licensing rule {index}")
        if not isinstance(rule["reason"], str) or not rule["reason"].strip():
            raise ModelError(f"licensing rule {index} requires a reason")
    if rules[-1]["path"] != "**":
        raise ModelError("licensing policy must end with the project default")
    require_unique([rule["path"] for rule in rules], "licensing path rule")
    if any(rule.get("expression") not in EXPRESSIONS for rule in rules):
        raise ModelError("licensing policy contains an unsupported expression")
    texts = value.get("license_texts")
    if not isinstance(texts, dict) or set(texts) != EXPRESSIONS:
        raise ModelError("licensing policy text inventory is incomplete")
    for expression, relative in texts.items():
        _safe_path(relative, f"license text for {expression}")
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 100:
            raise ModelError(f"license text is missing for {expression}")
    return value


def expression_for_path(policy: dict[str, Any], path: str) -> str:
    for rule in policy["rules"]:
        if fnmatch.fnmatchcase(path, rule["path"]):
            return rule["expression"]
    raise ModelError(f"no license rule covers {path}")


def verify_licensing_policy(root: Path) -> dict[str, Any]:
    policy = load_licensing_policy(root)
    root_license = root / "LICENSE"
    if root_license.is_symlink() or not root_license.is_file():
        raise ModelError("the repository must have one regular root LICENSE file")
    ambiguous = sorted(
        path.name
        for path in root.iterdir()
        if path.name.startswith("LICENSE") and path.name not in {"LICENSE", "LICENSES"}
    )
    if ambiguous:
        raise ModelError("ambiguous root license documents: " + ", ".join(ambiguous))
    canonical_gpl = root / policy["license_texts"]["GPL-2.0-only"]
    if root_license.read_bytes() != canonical_gpl.read_bytes():
        raise ModelError("root LICENSE does not match the declared GPL-2.0-only text")
    checked = 0
    mismatches: list[str] = []
    ignored_roots = {".git", ".hfx", "build", "LICENSES"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in ignored_roots:
            continue
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8").splitlines()[:10])
        except (OSError, UnicodeError):
            continue
        match = SPDX.search(first_lines)
        if match is None:
            continue
        checked += 1
        actual = match.group("expression").strip()
        expected = expression_for_path(policy, relative.as_posix())
        if actual != expected:
            mismatches.append(f"{relative}: expected {expected}, found {actual}")
    if mismatches:
        raise ModelError("license policy mismatch: " + "; ".join(mismatches[:8]))
    return {
        "schema": "hyperflux-license-verification-v1",
        "status": "PASS",
        "checked_spdx_files": checked,
        "expressions": sorted(policy["license_texts"]),
        "root_license": "GPL-2.0-only",
        "unknown_license_files": 0,
    }
