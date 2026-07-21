# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique, sha256_file
from .profiles import load_profile_inputs


IMPORT_SCHEMA = "hyperflux-upstream-metadata-import-v1"
METADATA_SCHEMA = "hyperflux-imported-device-metadata-v1"
IMPORT_KEYS = {"$schema", "schema", "upstream_id", "source_commit", "devices"}
DEVICE_KEYS = {
    "profile_id",
    "source_path",
    "class_name",
    "device_kind",
    "transport_variant",
}
METADATA_KEYS = {"$schema", "schema", "upstream", "selection_sha256", "devices"}
UPSTREAM_KEYS = {"id", "repository", "version", "commit", "license_expression"}
RECORD_KEYS = {"profile_id", "source", "identity", "presentation", "advertised_methods"}
SOURCE_KEYS = {"path", "class_name", "sha256"}
IDENTITY_KEYS = {
    "vendor_id",
    "product_id",
    "model_name",
    "device_kind",
    "transport_variant",
}
PRESENTATION_KEYS = {"image_url", "has_matrix", "matrix_rows", "matrix_columns"}
PROFILE_ID_PATTERN = re.compile(r"child\.[a-z0-9.-]+\Z")
CLASS_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
METHOD_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
HEX_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
INHERITED_FIELDS = {
    "DEVICE_IMAGE",
    "HAS_MATRIX",
    "MATRIX_DIMS",
    "METHODS",
    "USB_PID",
    "USB_VID",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _nonempty_string(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ModelError(f"{label}: expected a non-empty string of at most {maximum} characters")
    return value


def _safe_source_path(value: Any, label: str) -> str:
    source = _nonempty_string(value, label)
    path = Path(source)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
        raise ModelError(f"{label}: unsafe source path")
    return source


def load_import_selection(root: Path) -> dict[str, Any]:
    selection = load_json(root / "integrations" / "openrazer" / "import.json")
    _exact_keys(selection, IMPORT_KEYS, "OpenRazer import selection")
    if selection["$schema"] != "../../schemas/upstream-metadata-import.schema.json":
        raise ModelError("OpenRazer import selection schema reference is not canonical")
    if selection["schema"] != IMPORT_SCHEMA:
        raise ModelError("unsupported OpenRazer import selection schema")
    upstream = upstream_index(root).get(selection["upstream_id"])
    if upstream is None or upstream["id"] != "openrazer":
        raise ModelError("OpenRazer import selection names an unknown upstream")
    if selection["source_commit"] != upstream["commit"]:
        raise ModelError("OpenRazer import selection does not match the catalog pin")
    devices = selection["devices"]
    if not isinstance(devices, list) or not devices:
        raise ModelError("OpenRazer import selection is empty")
    profile_ids: list[str] = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ModelError(f"OpenRazer import device {index}: expected an object")
        _exact_keys(device, DEVICE_KEYS, f"OpenRazer import device {index}")
        for field in DEVICE_KEYS:
            if not isinstance(device[field], str) or not device[field]:
                raise ModelError(f"OpenRazer import device {index}: {field} is empty")
        if PROFILE_ID_PATTERN.fullmatch(device["profile_id"]) is None:
            raise ModelError(f"OpenRazer import device {index}: invalid profile id")
        _safe_source_path(
            device["source_path"], f"OpenRazer import device {index} source_path"
        )
        if CLASS_NAME_PATTERN.fullmatch(device["class_name"]) is None:
            raise ModelError(f"OpenRazer import device {index}: invalid class name")
        if device["device_kind"] not in {"keyboard", "mouse"}:
            raise ModelError(f"OpenRazer import device {index}: unsupported device kind")
        if device["transport_variant"] not in {"wireless", "wired", "bluetooth"}:
            raise ModelError(f"OpenRazer import device {index}: unsupported transport variant")
        profile_ids.append(device["profile_id"])
    require_unique(profile_ids, "OpenRazer import profile id")
    if profile_ids != sorted(profile_ids):
        raise ModelError("OpenRazer import devices must be sorted by profile id")
    return selection


class _ClassCatalog:
    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        try:
            source = source_path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(source_path))
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            raise ModelError(f"cannot parse pinned OpenRazer source {source_path}: {error}") from error
        self.classes = {
            node.name: node for node in module.body if isinstance(node, ast.ClassDef)
        }
        self._linearizations: dict[str, tuple[str, ...]] = {}

    def _linearization(self, class_name: str, active: tuple[str, ...] = ()) -> tuple[str, ...]:
        cached = self._linearizations.get(class_name)
        if cached is not None:
            return cached
        if class_name in active:
            chain = " -> ".join((*active, class_name))
            raise ModelError(f"OpenRazer class inheritance cycle: {chain}")
        node = self.classes.get(class_name)
        if node is None:
            raise ModelError(f"OpenRazer class {class_name} is absent from {self.source_path}")
        local_bases = [
            base.id
            for base in node.bases
            if isinstance(base, ast.Name) and base.id in self.classes
        ]
        sequences = [
            list(self._linearization(base, (*active, class_name))) for base in local_bases
        ]
        sequences.append(list(local_bases))
        merged: list[str] = []
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(sequence[0] not in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                raise ModelError(
                    f"OpenRazer class {class_name} has inconsistent local inheritance"
                )
            merged.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        result = (class_name, *merged)
        self._linearizations[class_name] = result
        return result

    def resolve(self, class_name: str) -> tuple[ast.ClassDef, dict[str, Any]]:
        node = self.classes.get(class_name)
        if node is None:
            raise ModelError(f"OpenRazer class {class_name} is absent from {self.source_path}")
        values: dict[str, Any] = {}
        for name in reversed(self._linearization(class_name)):
            current = self.classes[name]
            for statement in current.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                if not isinstance(target, ast.Name) or target.id not in INHERITED_FIELDS:
                    continue
                try:
                    values[target.id] = ast.literal_eval(statement.value)
                except (ValueError, TypeError) as error:
                    raise ModelError(
                        f"OpenRazer {name}.{target.id} is not literal metadata"
                    ) from error
        return node, values


def _model_name(node: ast.ClassDef) -> str:
    doc = ast.get_docstring(node, clean=True)
    if doc is None:
        raise ModelError(f"OpenRazer class {node.name} has no model description")
    line = doc.splitlines()[0].strip().rstrip(".")
    prefix = "Class for the "
    if not line.startswith(prefix) or len(line) <= len(prefix):
        raise ModelError(f"OpenRazer class {node.name} has an unsupported model description")
    return line[len(prefix) :]


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65_535:
        raise ModelError(f"{label} is not a 16-bit integer")
    return value


def _record(
    source_root: Path,
    selected: dict[str, str],
) -> dict[str, Any]:
    source_path = source_root / selected["source_path"]
    catalog = _ClassCatalog(source_path)
    node, values = catalog.resolve(selected["class_name"])
    missing = sorted(INHERITED_FIELDS - set(values))
    if missing:
        raise ModelError(
            f"OpenRazer class {node.name} lacks required metadata: {', '.join(missing)}"
        )
    dimensions = values["MATRIX_DIMS"]
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in dimensions)
        or any(not 0 <= value <= 128 for value in dimensions)
    ):
        raise ModelError(f"OpenRazer class {node.name} has invalid matrix dimensions")
    methods = values["METHODS"]
    if not isinstance(methods, list) or any(not isinstance(value, str) for value in methods):
        raise ModelError(f"OpenRazer class {node.name} has invalid method metadata")
    methods = sorted(set(methods))
    image_url = values["DEVICE_IMAGE"]
    if not isinstance(image_url, str) or not image_url.startswith("https://"):
        raise ModelError(f"OpenRazer class {node.name} has an unsafe image URL")
    has_matrix = values["HAS_MATRIX"]
    if not isinstance(has_matrix, bool):
        raise ModelError(f"OpenRazer class {node.name} has invalid matrix metadata")
    return {
        "profile_id": selected["profile_id"],
        "source": {
            "path": selected["source_path"],
            "class_name": selected["class_name"],
            "sha256": sha256_file(source_path),
        },
        "identity": {
            "vendor_id": _integer(values["USB_VID"], f"OpenRazer {node.name}.USB_VID"),
            "product_id": _integer(values["USB_PID"], f"OpenRazer {node.name}.USB_PID"),
            "model_name": _model_name(node),
            "device_kind": selected["device_kind"],
            "transport_variant": selected["transport_variant"],
        },
        "presentation": {
            "image_url": image_url,
            "has_matrix": has_matrix,
            "matrix_rows": dimensions[0],
            "matrix_columns": dimensions[1],
        },
        "advertised_methods": methods,
    }


def _source_commit(source_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot identify pinned OpenRazer checkout: {error}") from error
    return result.stdout.strip()


def transformed_metadata(root: Path, source_root: Path) -> dict[str, Any]:
    selection = load_import_selection(root)
    source_root = source_root.resolve()
    if _source_commit(source_root) != selection["source_commit"]:
        raise ModelError("OpenRazer checkout does not match the selected immutable commit")
    upstream = upstream_index(root)["openrazer"]
    selected_devices = deepcopy(selection["devices"])
    records = [_record(source_root, selected) for selected in selected_devices]
    _validate_profile_authority(root, records)
    canonical_selection = {key: value for key, value in selection.items() if key != "$schema"}
    return {
        "$schema": "../../schemas/imported-device-metadata.schema.json",
        "schema": METADATA_SCHEMA,
        "upstream": {
            "id": upstream["id"],
            "repository": upstream["repository"],
            "version": upstream["version"],
            "commit": upstream["commit"],
            "license_expression": upstream["license_expression"],
        },
        "selection_sha256": hashlib.sha256(_canonical_bytes(canonical_selection)).hexdigest(),
        "devices": records,
    }


def _validate_profile_authority(root: Path, records: list[dict[str, Any]]) -> None:
    profiles = {
        profile["profile_id"]: profile
        for profile in load_profile_inputs(root).profiles
        if profile["kind"] == "child"
    }
    for record in records:
        profile = profiles.get(record["profile_id"])
        if profile is None:
            raise ModelError(f"OpenRazer metadata names unknown profile {record['profile_id']}")
        identity = record["identity"]
        if (
            profile["device_kind"] != identity["device_kind"]
            or profile["identity"].get("vendor_id") != identity["vendor_id"]
            or profile["identity"].get("product_id") != identity["product_id"]
        ):
            raise ModelError(f"OpenRazer metadata identity drifts from {record['profile_id']}")
        lighting = profile["transport"]["lighting"]
        presentation = record["presentation"]
        if (
            lighting["rows"] != presentation["matrix_rows"]
            or lighting["columns"] != presentation["matrix_columns"]
        ):
            raise ModelError(f"OpenRazer matrix dimensions drift from {record['profile_id']}")


def load_imported_metadata(root: Path) -> dict[str, Any]:
    path = root / "integrations" / "openrazer" / "metadata.json"
    metadata = load_json(path)
    _exact_keys(metadata, METADATA_KEYS, "imported OpenRazer metadata")
    if metadata["$schema"] != "../../schemas/imported-device-metadata.schema.json":
        raise ModelError("imported OpenRazer metadata schema reference is not canonical")
    if metadata["schema"] != METADATA_SCHEMA:
        raise ModelError("unsupported imported OpenRazer metadata schema")
    selection = load_import_selection(root)
    canonical_selection = {key: value for key, value in selection.items() if key != "$schema"}
    expected_digest = hashlib.sha256(_canonical_bytes(canonical_selection)).hexdigest()
    if metadata["selection_sha256"] != expected_digest:
        raise ModelError("imported OpenRazer metadata selection is stale")
    upstream = upstream_index(root)["openrazer"]
    expected_upstream = {
        key: upstream[key]
        for key in ("id", "repository", "version", "commit", "license_expression")
    }
    if not isinstance(metadata["upstream"], dict):
        raise ModelError("imported OpenRazer metadata upstream is not an object")
    _exact_keys(metadata["upstream"], UPSTREAM_KEYS, "imported OpenRazer upstream")
    if metadata["upstream"] != expected_upstream:
        raise ModelError("imported OpenRazer metadata provenance drifts from the catalog")
    devices = metadata["devices"]
    if not isinstance(devices, list) or not 1 <= len(devices) <= 128:
        raise ModelError("imported OpenRazer metadata is empty")
    selected_by_profile = {device["profile_id"]: device for device in selection["devices"]}
    profile_ids: list[str] = []
    for index, record in enumerate(devices):
        label = f"imported OpenRazer device {index}"
        if not isinstance(record, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(record, RECORD_KEYS, label)
        profile_id = _nonempty_string(record["profile_id"], f"{label} profile_id", maximum=128)
        if PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
            raise ModelError(f"{label}: invalid profile id")
        selected = selected_by_profile.get(profile_id)
        if selected is None:
            raise ModelError(f"{label}: profile was not selected for import")
        profile_ids.append(profile_id)

        source = record["source"]
        if not isinstance(source, dict):
            raise ModelError(f"{label} source: expected an object")
        _exact_keys(source, SOURCE_KEYS, f"{label} source")
        if _safe_source_path(source["path"], f"{label} source path") != selected["source_path"]:
            raise ModelError(f"{label}: source path drifts from the import selection")
        if (
            not isinstance(source["class_name"], str)
            or CLASS_NAME_PATTERN.fullmatch(source["class_name"]) is None
            or source["class_name"] != selected["class_name"]
        ):
            raise ModelError(f"{label}: source class drifts from the import selection")
        if not isinstance(source["sha256"], str) or HEX_SHA256_PATTERN.fullmatch(source["sha256"]) is None:
            raise ModelError(f"{label}: invalid source digest")

        identity = record["identity"]
        if not isinstance(identity, dict):
            raise ModelError(f"{label} identity: expected an object")
        _exact_keys(identity, IDENTITY_KEYS, f"{label} identity")
        _integer(identity["vendor_id"], f"{label} vendor_id")
        _integer(identity["product_id"], f"{label} product_id")
        _nonempty_string(identity["model_name"], f"{label} model_name", maximum=128)
        if identity["device_kind"] != selected["device_kind"]:
            raise ModelError(f"{label}: device kind drifts from the import selection")
        if identity["transport_variant"] != selected["transport_variant"]:
            raise ModelError(f"{label}: transport variant drifts from the import selection")

        presentation = record["presentation"]
        if not isinstance(presentation, dict):
            raise ModelError(f"{label} presentation: expected an object")
        _exact_keys(presentation, PRESENTATION_KEYS, f"{label} presentation")
        if (
            not isinstance(presentation["image_url"], str)
            or not presentation["image_url"].startswith("https://")
        ):
            raise ModelError(f"{label}: invalid image URL")
        if not isinstance(presentation["has_matrix"], bool):
            raise ModelError(f"{label}: invalid matrix declaration")
        _integer(presentation["matrix_rows"], f"{label} matrix rows")
        _integer(presentation["matrix_columns"], f"{label} matrix columns")
        if presentation["matrix_rows"] > 128 or presentation["matrix_columns"] > 128:
            raise ModelError(f"{label}: matrix dimensions exceed the import bound")

        methods = record["advertised_methods"]
        if (
            not isinstance(methods, list)
            or len(methods) > 256
            or any(not isinstance(method, str) or METHOD_PATTERN.fullmatch(method) is None for method in methods)
            or methods != sorted(set(methods))
        ):
            raise ModelError(f"{label}: invalid advertised method metadata")
    require_unique(profile_ids, "imported OpenRazer profile id")
    if profile_ids != sorted(profile_ids):
        raise ModelError("imported OpenRazer metadata must be sorted by profile id")
    selected_ids = [record["profile_id"] for record in selection["devices"]]
    if profile_ids != selected_ids:
        raise ModelError("imported OpenRazer metadata does not match the selected profiles")
    _validate_profile_authority(root, devices)
    return metadata


def write_imported_metadata(root: Path, source_root: Path) -> Path:
    destination = root / "integrations" / "openrazer" / "metadata.json"
    value = transformed_metadata(root, source_root)
    destination.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return destination
