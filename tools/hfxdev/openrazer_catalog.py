# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .model import ModelError, sha256_file
from .openrazer import (
    MAX_OPENRAZER_CLASSES,
    SUPPORTED_POLL_RATES_SOURCE,
    _ClassCatalog,
    _integer,
    _model_name,
    _supported_poll_rates_function,
)


def _catalog_route(class_name: str, docstring: str) -> str:
    text = f"{class_name} {docstring}".lower()
    if "bluetooth" in text:
        return "bluetooth"
    if "wireless" in text or "receiver" in text:
        return "vendor-wireless-receiver"
    return "direct-usb"


def effective_event_regex(source: Path, class_name: str) -> str | None:
    """Resolve one OpenRazer class's inherited event matcher without execution."""

    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as error:
        raise ModelError(f"cannot parse OpenRazer event matcher source: {error}") from error
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    if class_name not in classes:
        raise ModelError(f"OpenRazer class {class_name} is absent from event matcher source")

    def resolve(name: str, visited: set[str]) -> str | None:
        if name in visited:
            raise ModelError("OpenRazer event matcher inheritance contains a cycle")
        node = classes.get(name)
        if node is None:
            return None
        visited = {*visited, name}
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == "EVENT_FILE_REGEX"
                for target in statement.targets
            ):
                continue
            value = statement.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == "re"
                and value.func.attr == "compile"
                and len(value.args) == 1
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                return value.args[0].value
            if isinstance(value, ast.Constant) and value.value is None:
                return None
            raise ModelError(f"OpenRazer class {name} has an unsupported event matcher")
        for base in node.bases:
            if isinstance(base, ast.Name):
                inherited = resolve(base.id, visited)
                if inherited is not None:
                    return inherited
        return None

    return resolve(class_name, set())


def verify_global_event_file_matcher(source: Path) -> None:
    """Verify OpenRazer still scans global input events by model regex.

    The HyperFlux UHID object intentionally exposes only the Feature transport.
    The global ``/dev/input/by-id`` scan is upstream behavior, but does not prove
    a route- or generation-associated event source and must not be treated as
    macro or Ripple support authority. Inspect the pinned source structurally so
    formatting-only upstream changes do not invalidate this limitation.
    """

    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as error:
        raise ModelError(f"cannot parse OpenRazer event binding source: {error}") from error
    device_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RazerDevice"
        ),
        None,
    )
    initializer = next(
        (
            node
            for node in getattr(device_class, "body", ())
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__init__"
        ),
        None,
    )
    if initializer is None:
        raise ModelError("OpenRazer global event binding initializer is missing")

    def dotted_name(node: ast.AST) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    constants = {
        node.value
        for node in ast.walk(initializer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    calls = [node for node in ast.walk(initializer) if isinstance(node, ast.Call)]
    has_global_scan = any(
        dotted_name(call.func) == "os.listdir"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "search_dir"
        for call in calls
    )
    has_upstream_match = any(
        dotted_name(call.func) == "self.EVENT_FILE_REGEX.match"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "event_file"
        for call in calls
    )
    has_global_append = any(
        dotted_name(call.func) == "self.event_files.append"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and dotted_name(call.args[0].func) == "os.path.join"
        and len(call.args[0].args) == 2
        and isinstance(call.args[0].args[0], ast.Name)
        and call.args[0].args[0].id == "search_dir"
        and isinstance(call.args[0].args[1], ast.Name)
        and call.args[0].args[1].id == "event_file"
        for call in calls
    )
    if (
        "/dev/input/by-id/" not in constants
        or not has_global_scan
        or not has_upstream_match
        or not has_global_append
    ):
        raise ModelError(
            "OpenRazer no longer binds device objects through its global event-file matcher"
        )


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
    support_path, _, _ = _supported_poll_rates_function(source_root)
    source_files = [
        *catalog.source_files,
        {
            "path": SUPPORTED_POLL_RATES_SOURCE.as_posix(),
            "sha256": sha256_file(support_path),
        },
    ]
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
            "source_files": sorted(source_files, key=lambda item: item["path"]),
        },
        "records": records,
    }
