# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
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
OPENRAZER_SOURCE_FILES = (
    ("keyboard", Path("daemon/openrazer_daemon/hardware/keyboards.py")),
    ("mouse", Path("daemon/openrazer_daemon/hardware/mouse.py")),
)
SELECTED_FIELDS = {
    "DEVICE_IMAGE",
    "HAS_MATRIX",
    "MATRIX_DIMS",
    "METHODS",
    "USB_PID",
    "USB_VID",
}
CATALOG_FIELDS = SELECTED_FIELDS | {
    "DEDICATED_MACRO_KEYS",
    "DPI_MAX",
    "POLL_RATES",
    "WAVE_DIRS",
}
MAX_OPENRAZER_SOURCE_BYTES = 8 * 1024 * 1024
MAX_OPENRAZER_CLASSES = 2_048


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


@dataclass(frozen=True)
class _ParsedClass:
    name: str
    bases: tuple[str, ...]
    assignments: dict[str, ast.expr]
    docstring: str
    line: int
    source_file: str
    device_kind: str


class _ClassCatalog:
    def __init__(self, source_root: Path) -> None:
        requested = source_root.resolve()
        if requested.is_file():
            self.source_root = requested.parent
            source_files = (("unknown", Path(requested.name)),)
        else:
            self.source_root = requested
            source_files = OPENRAZER_SOURCE_FILES
        self.classes: dict[str, _ParsedClass] = {}
        self.source_files: list[dict[str, str]] = []
        self._linearizations: dict[str, tuple[str, ...]] = {}
        self._field_cache: dict[tuple[str, str], Any] = {}
        self._active_fields: set[tuple[str, str]] = set()
        for device_kind, relative in source_files:
            source_path = self.source_root / relative
            if source_path.is_symlink() or not source_path.is_file():
                raise ModelError(f"OpenRazer source is missing or symbolic: {relative.as_posix()}")
            if source_path.stat().st_size > MAX_OPENRAZER_SOURCE_BYTES:
                raise ModelError(f"OpenRazer source exceeds its size bound: {relative.as_posix()}")
            try:
                source = source_path.read_text(encoding="utf-8")
                module = ast.parse(source, filename=relative.as_posix())
            except (OSError, SyntaxError, UnicodeDecodeError) as error:
                raise ModelError(
                    f"cannot parse pinned OpenRazer source {relative.as_posix()}: {error}"
                ) from error
            self.source_files.append(
                {"path": relative.as_posix(), "sha256": sha256_file(source_path)}
            )
            for node in module.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                if node.name in self.classes:
                    raise ModelError(f"duplicate OpenRazer hardware class: {node.name}")
                assignments: dict[str, ast.expr] = {}
                for statement in node.body:
                    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                        continue
                    target = statement.targets[0]
                    if isinstance(target, ast.Name) and target.id in CATALOG_FIELDS:
                        assignments[target.id] = statement.value
                self.classes[node.name] = _ParsedClass(
                    name=node.name,
                    bases=tuple(
                        base.id for base in node.bases if isinstance(base, ast.Name)
                    ),
                    assignments=assignments,
                    docstring=ast.get_docstring(node, clean=True) or "",
                    line=node.lineno,
                    source_file=relative.as_posix(),
                    device_kind=device_kind,
                )
        if not self.classes or len(self.classes) > MAX_OPENRAZER_CLASSES:
            raise ModelError("OpenRazer class catalog is empty or exceeds its bound")

    def _linearization(self, class_name: str, active: tuple[str, ...] = ()) -> tuple[str, ...]:
        cached = self._linearizations.get(class_name)
        if cached is not None:
            return cached
        if class_name in active:
            chain = " -> ".join((*active, class_name))
            raise ModelError(f"OpenRazer class inheritance cycle: {chain}")
        parsed = self.classes.get(class_name)
        if parsed is None:
            raise ModelError(f"OpenRazer class {class_name} is absent from the pinned catalog")
        local_bases = [base for base in parsed.bases if base in self.classes]
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

    def _expression(self, value: ast.expr) -> Any:
        if isinstance(value, ast.Constant) and isinstance(value.value, (bool, int, str)):
            return value.value
        if isinstance(value, (ast.List, ast.Tuple)):
            return [self._expression(item) for item in value.elts]
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return self.field(value.value.id, value.attr)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            left = self._expression(value.left)
            right = self._expression(value.right)
            if isinstance(left, list) and isinstance(right, list):
                return left + right
        raise ModelError(
            f"unsupported OpenRazer metadata expression at line {getattr(value, 'lineno', '?')}"
        )

    def field(self, class_name: str, field: str) -> Any:
        key = (class_name, field)
        if key in self._field_cache:
            return self._field_cache[key]
        if key in self._active_fields:
            raise ModelError(f"OpenRazer metadata has a cyclic reference: {class_name}.{field}")
        self._active_fields.add(key)
        try:
            for name in self._linearization(class_name):
                expression = self.classes[name].assignments.get(field)
                if expression is not None:
                    result = self._expression(expression)
                    self._field_cache[key] = result
                    return result
        finally:
            self._active_fields.remove(key)
        raise KeyError(key)

    def resolve(
        self, class_name: str, fields: set[str] | None = None
    ) -> tuple[_ParsedClass, dict[str, Any]]:
        parsed = self.classes.get(class_name)
        if parsed is None:
            raise ModelError(f"OpenRazer class {class_name} is absent from the pinned catalog")
        values: dict[str, Any] = {}
        for field in fields or SELECTED_FIELDS:
            try:
                values[field] = self.field(class_name, field)
            except KeyError:
                continue
        return parsed, values


def _model_name(parsed: _ParsedClass, *, strict: bool) -> str:
    line = parsed.docstring.splitlines()[0].strip().rstrip(".") if parsed.docstring else ""
    prefix = "Class for the "
    if not line.startswith(prefix) or len(line) <= len(prefix):
        if strict:
            raise ModelError(f"OpenRazer class {parsed.name} has an unsupported model description")
        return parsed.name
    return line[len(prefix) :].strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65_535:
        raise ModelError(f"{label} is not a 16-bit integer")
    return value


def _record(
    catalog: _ClassCatalog,
    source_root: Path,
    selected: dict[str, str],
) -> dict[str, Any]:
    source_path = source_root / selected["source_path"]
    parsed, values = catalog.resolve(selected["class_name"], SELECTED_FIELDS)
    if parsed.source_file != selected["source_path"]:
        raise ModelError(
            f"OpenRazer class {parsed.name} source drifts from the import selection"
        )
    missing = sorted(SELECTED_FIELDS - set(values))
    if missing:
        raise ModelError(
            f"OpenRazer class {parsed.name} lacks required metadata: {', '.join(missing)}"
        )
    dimensions = values["MATRIX_DIMS"]
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in dimensions)
        or any(not 0 <= value <= 128 for value in dimensions)
    ):
        raise ModelError(f"OpenRazer class {parsed.name} has invalid matrix dimensions")
    methods = values["METHODS"]
    if not isinstance(methods, list) or any(not isinstance(value, str) for value in methods):
        raise ModelError(f"OpenRazer class {parsed.name} has invalid method metadata")
    methods = sorted(set(methods))
    image_url = values["DEVICE_IMAGE"]
    if not isinstance(image_url, str) or not image_url.startswith("https://"):
        raise ModelError(f"OpenRazer class {parsed.name} has an unsafe image URL")
    has_matrix = values["HAS_MATRIX"]
    if not isinstance(has_matrix, bool):
        raise ModelError(f"OpenRazer class {parsed.name} has invalid matrix metadata")
    return {
        "profile_id": selected["profile_id"],
        "source": {
            "path": selected["source_path"],
            "class_name": selected["class_name"],
            "sha256": sha256_file(source_path),
        },
        "identity": {
            "vendor_id": _integer(values["USB_VID"], f"OpenRazer {parsed.name}.USB_VID"),
            "product_id": _integer(values["USB_PID"], f"OpenRazer {parsed.name}.USB_PID"),
            "model_name": _model_name(parsed, strict=True),
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
    catalog = _ClassCatalog(source_root)
    records = [_record(catalog, source_root, selected) for selected in selected_devices]
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


def _catalog_route(class_name: str, docstring: str) -> str:
    text = f"{class_name} {docstring}".lower()
    if "bluetooth" in text:
        return "bluetooth"
    if "wireless" in text or "receiver" in text:
        return "vendor-wireless-receiver"
    return "direct-usb"


def extract_openrazer_catalog(
    source_root: Path,
    *,
    repository: str,
    commit: str,
    version: str,
    license_expression: str,
) -> dict[str, Any]:
    """Parse the complete pinned OpenRazer registry without importing its code."""

    catalog = _ClassCatalog(source_root)
    records: list[dict[str, Any]] = []
    for class_name, parsed in sorted(catalog.classes.items()):
        try:
            product_id = catalog.field(class_name, "USB_PID")
        except KeyError:
            continue
        product_id = _integer(product_id, f"OpenRazer {class_name}.USB_PID")
        try:
            vendor_id = _integer(
                catalog.field(class_name, "USB_VID"),
                f"OpenRazer {class_name}.USB_VID",
            )
        except KeyError:
            vendor_id = None
        try:
            methods = catalog.field(class_name, "METHODS")
        except KeyError as error:
            raise ModelError(f"OpenRazer {class_name}.METHODS is missing") from error
        if not isinstance(methods, list) or any(not isinstance(item, str) for item in methods):
            raise ModelError(f"OpenRazer {class_name}.METHODS is not a string list")

        facts: dict[str, Any] = {}
        for source_name, target_name in (
            ("DEDICATED_MACRO_KEYS", "dedicated_macro_keys"),
            ("DPI_MAX", "dpi_max"),
            ("HAS_MATRIX", "has_matrix"),
            ("MATRIX_DIMS", "matrix_dimensions"),
            ("POLL_RATES", "poll_rates_hz"),
            ("WAVE_DIRS", "wave_directions"),
        ):
            try:
                facts[target_name] = catalog.field(class_name, source_name)
            except KeyError:
                continue
        dimensions = facts.get("matrix_dimensions")
        if dimensions is not None and (
            not isinstance(dimensions, list)
            or len(dimensions) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 256
                for value in dimensions
            )
        ):
            raise ModelError(f"OpenRazer {class_name}.MATRIX_DIMS is invalid")
        records.append(
            {
                "record_id": f"openrazer:{class_name}",
                "source_device_key": class_name,
                "model_name": _model_name(parsed, strict=False),
                "device_kind": parsed.device_kind,
                "source_route": _catalog_route(class_name, parsed.docstring),
                "usb_identity": {
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                },
                "lighting_topology": (
                    {"matrix_dimensions": dimensions}
                    if dimensions is not None
                    else None
                ),
                "settings_methods": sorted(set(methods)),
                "facts": facts,
                "source_location": {
                    "path": parsed.source_file,
                    "line": parsed.line,
                },
            }
        )
    if not records or len(records) > MAX_OPENRAZER_CLASSES:
        raise ModelError("normalized OpenRazer records are empty or exceed their bound")
    return {
        "$schema": "../../schemas/upstream-device-catalog.schema.json",
        "schema": "hyperflux-upstream-device-catalog-v1",
        "source": {
            "upstream_id": "openrazer",
            "repository": repository,
            "version": version,
            "commit": commit,
            "license_expression": license_expression,
            "extractor": "python-ast-v1",
            "source_files": sorted(catalog.source_files, key=lambda item: item["path"]),
        },
        "records": records,
    }
