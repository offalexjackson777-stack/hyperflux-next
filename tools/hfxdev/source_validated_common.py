# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from .model import ModelError, sha256_file


SCHEMA = "hyperflux-source-validated-device-catalog-v1"
SCHEMA_REF = "../../schemas/source-validated-device-catalog.schema.json"
CATALOG_PATH = Path("integrations/native-route/source-validated.json")
REPORT_FAMILY = "razer-feature-report-v1"
REPORT_BYTES = 90
RAZER_VENDOR_ID = 0x1532
SUPPORTED_KINDS = {"keyboard", "mouse"}
SOURCE_VALIDATED = "source-validated"
BACKEND_REQUIRED = "backend-required"
ABSENT = "absent"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _checkout_commit(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot identify pinned upstream checkout {path}: {error}") from error
    return result.stdout.strip()


def _validate_checkout(path: Path, source: dict[str, Any]) -> Path:
    if path.is_symlink():
        raise ModelError(f"{source['upstream_id']}: source checkout may not be symbolic")
    resolved = path.resolve()
    if not resolved.is_dir() or _checkout_commit(resolved) != source["commit"]:
        raise ModelError(f"{source['upstream_id']}: checkout differs from the source-bound catalog")
    for record in source["source_files"]:
        candidate = resolved / record["path"]
        if candidate.is_symlink() or not candidate.is_file():
            raise ModelError(
                f"{source['upstream_id']}: source file is missing or symbolic: {record['path']}"
            )
        if sha256_file(candidate) != record["sha256"]:
            raise ModelError(
                f"{source['upstream_id']}: source file differs from the catalog: {record['path']}"
            )
    return resolved


def _identity(record: dict[str, Any]) -> tuple[int, int, str] | None:
    identity = record["usb_identity"]
    vendor_id = identity["vendor_id"]
    product_id = identity["product_id"]
    kind = record["device_kind"]
    if (
        vendor_id != RAZER_VENDOR_ID
        or not isinstance(product_id, int)
        or not 1 <= product_id <= 0xFFFF
        or kind not in SUPPORTED_KINDS
    ):
        return None
    return vendor_id, product_id, kind


def _route_id(identity: tuple[int, int, str]) -> str:
    vendor_id, product_id, kind = identity
    return f"razer.{vendor_id:04x}.{product_id:04x}.{kind}"


def _wireless_records(catalog: dict[str, Any]) -> dict[tuple[int, int, str], list[dict[str, Any]]]:
    records: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for record in catalog["records"]:
        if record["source_route"] != "vendor-wireless-receiver":
            continue
        identity = _identity(record)
        if identity is not None:
            records.setdefault(identity, []).append(record)
    return records


def _classification(policy: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for method in policy["classifications"]["read_only"]:
        result[method] = "read"
    for item in policy["classifications"]["reversible_with_readback"]:
        result[item["method"]] = "write"
    for method in policy["classifications"]["unsafe"]:
        result[method] = "write"
    for method in policy["classifications"]["software_only"]:
        result[method] = "software"
    return result


def _merge_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rule in rules:
        normalized = deepcopy(rule)
        if "data_sizes" not in normalized:
            normalized["data_sizes"] = [normalized.pop("data_size")]
        signature = _digest(
            {
                key: normalized[key]
                for key in (
                    "direction",
                    "command_class",
                    "command_id",
                    "data_sizes",
                    "matcher",
                )
            }
        )
        existing = merged.get(signature)
        if existing is None:
            merged[signature] = normalized
            continue
        existing["integrations"] = sorted(
            set(existing["integrations"]) | set(normalized["integrations"])
        )
        existing["methods"] = sorted(
            set(existing["methods"]) | set(normalized["methods"])
        )
        if "transaction_ids" in existing or "transaction_ids" in normalized:
            existing["transaction_ids"] = sorted(
                set(existing.get("transaction_ids", []))
                | set(normalized.get("transaction_ids", []))
            )
        source_keys = {
            (source["upstream"], source["path"], source["function"]): source
            for source in existing["sources"] + normalized["sources"]
        }
        existing["sources"] = [source_keys[key] for key in sorted(source_keys)]
        existing["id"] = "+".join(existing["integrations"]) + "-" + min(
            existing["id"].split("-", 1)[-1], normalized["id"].split("-", 1)[-1]
        )
    result = sorted(merged.values(), key=lambda rule: rule["id"])
    ids = [rule["id"] for rule in result]
    if len(ids) != len(set(ids)):
        for index, rule in enumerate(result):
            rule["id"] = f"{rule['id']}-{index:02d}"
    return result


def _require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label}: fields are invalid")
    return value
