# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .model import ModelError


DEVICES_CPP = Path("Controllers/RazerController/RazerDevices.cpp")
DEVICES_HEADER = Path("Controllers/RazerController/RazerDevices.h")
DETECTOR_CPP = Path("Controllers/RazerController/RazerControllerDetect.cpp")
MAX_OPENRGB_SOURCE_BYTES = 8 * 1024 * 1024
MAX_OPENRGB_TOKENS = 2_000_000
MAX_OPENRGB_RECORDS = 2_048
DEFINE_PATTERN = re.compile(r"^\s*#define\s+([A-Z][A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+|[0-9]+)\s*$", re.MULTILINE)
TOKEN_PATTERN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<block_comment>/\*.*?\*/)"
    r"|(?P<line_comment>//[^\n]*)"
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<number>0x[0-9A-Fa-f]+|[0-9]+)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<symbol>[{}&,;=])"
    r"|(?P<other>.)",
    re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


@dataclass(frozen=True)
class Reference:
    name: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tokens(text: str) -> list[Token]:
    if len(text.encode("utf-8")) > MAX_OPENRGB_SOURCE_BYTES:
        raise ModelError("OpenRGB source exceeds its size bound")
    result: list[Token] = []
    position = 0
    line = 1
    for match in TOKEN_PATTERN.finditer(text):
        if match.start() != position:
            fragment = text[position : match.start()]
            if fragment.strip():
                raise ModelError(f"unsupported OpenRGB token near line {line}: {fragment[:32]!r}")
        value = match.group(0)
        kind = match.lastgroup or ""
        if kind not in {"space", "block_comment", "line_comment"}:
            result.append(Token(kind, value, line))
        line += value.count("\n")
        position = match.end()
    if text[position:].strip():
        raise ModelError(f"unsupported OpenRGB trailing token near line {line}")
    if len(result) > MAX_OPENRGB_TOKENS:
        raise ModelError("OpenRGB source exceeds its token bound")
    return result


class _InitializerParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0

    def value(self) -> Any:
        token = self._next()
        if token.value == "{":
            values: list[Any] = []
            while self._peek().value != "}":
                values.append(self.value())
                if self._peek().value == ",":
                    self._next()
                elif self._peek().value != "}":
                    raise ModelError(f"OpenRGB initializer expects ',' at line {self._peek().line}")
            self._next()
            return values
        if token.value == "&":
            target = self._next()
            if target.kind != "identifier":
                raise ModelError(f"OpenRGB reference expects an identifier at line {target.line}")
            return Reference(target.value)
        if token.kind == "string":
            return json.loads(token.value)
        if token.kind == "number":
            return int(token.value, 0)
        if token.kind == "identifier":
            return token.value
        raise ModelError(f"unsupported OpenRGB initializer value at line {token.line}")

    def _peek(self) -> Token:
        if self.index >= len(self.tokens):
            raise ModelError("unexpected end of OpenRGB initializer")
        return self.tokens[self.index]

    def _next(self) -> Token:
        value = self._peek()
        self.index += 1
        return value


def _declarations(text: str, type_name: str) -> list[tuple[str, list[Any], int]]:
    tokens = _tokens(text)
    declarations: list[tuple[str, list[Any], int]] = []
    index = 0
    signature = ("static", "const", type_name)
    while index + 5 < len(tokens):
        if tuple(token.value for token in tokens[index : index + 3]) != signature:
            index += 1
            continue
        name = tokens[index + 3]
        equals = tokens[index + 4]
        if name.kind != "identifier" or equals.value != "=":
            index += 1
            continue
        parser = _InitializerParser(tokens[index + 5 :])
        value = parser.value()
        if not isinstance(value, list):
            raise ModelError(f"OpenRGB {type_name} {name.value} does not use a braced initializer")
        declarations.append((name.value, value, name.line))
        index += 5 + parser.index
    return declarations


def _route(model_name: str) -> str:
    lower = model_name.lower()
    if "bluetooth" in lower:
        return "bluetooth"
    if "wireless" in lower:
        return "vendor-wireless-receiver"
    return "direct-usb"


def _reference_name(value: Any) -> str | None:
    if isinstance(value, Reference):
        return value.name
    if value == "NULL":
        return None
    raise ModelError(f"OpenRGB expected a reference or NULL, got {value!r}")


def _detector_bindings(text: str, defines: dict[str, int]) -> dict[str, set[int]]:
    tokens = _tokens(text)
    bindings: dict[str, set[int]] = {}
    index = 0
    while index + 1 < len(tokens):
        macro = tokens[index]
        if (
            macro.kind != "identifier"
            or not macro.value.startswith("REGISTER_")
            or tokens[index + 1].value != "("
        ):
            index += 1
            continue
        arguments: list[list[Token]] = [[]]
        depth = 1
        cursor = index + 2
        while cursor < len(tokens) and depth:
            token = tokens[cursor]
            if token.value == "(":
                depth += 1
                arguments[-1].append(token)
            elif token.value == ")":
                depth -= 1
                if depth:
                    arguments[-1].append(token)
            elif token.value == "," and depth == 1:
                arguments.append([])
            else:
                arguments[-1].append(token)
            cursor += 1
        if depth:
            raise ModelError(f"unterminated OpenRGB detector registration at line {macro.line}")
        if len(arguments) >= 4:
            vendor = arguments[2]
            product = arguments[3]
            if (
                len(vendor) == 1
                and vendor[0].kind == "identifier"
                and len(product) == 1
                and product[0].kind == "identifier"
                and vendor[0].value in defines
                and product[0].value in defines
            ):
                bindings.setdefault(product[0].value, set()).add(defines[vendor[0].value])
        index = cursor
    return bindings


def extract_openrgb_catalog(
    source_root: Path,
    *,
    repository: str,
    commit: str,
    version: str,
    license_expression: str,
) -> dict[str, Any]:
    """Parse the pinned Razer registry without compiling or executing C++."""

    cpp_path = source_root / DEVICES_CPP
    header_path = source_root / DEVICES_HEADER
    detector_path = source_root / DETECTOR_CPP
    source_paths = (cpp_path, header_path, detector_path)
    if any(path.is_symlink() or not path.is_file() for path in source_paths):
        raise ModelError("OpenRGB Razer device registry is incomplete")
    if any(path.stat().st_size > MAX_OPENRGB_SOURCE_BYTES for path in source_paths):
        raise ModelError("OpenRGB Razer device registry exceeds its size bound")
    cpp = cpp_path.read_text(encoding="utf-8")
    header = header_path.read_text(encoding="utf-8")
    detector = detector_path.read_text(encoding="utf-8")
    defines = {name: int(value, 0) for name, value in DEFINE_PATTERN.findall(header)}
    detector_bindings = _detector_bindings(detector, defines)
    zones: dict[str, dict[str, Any]] = {}
    for name, values, line in _declarations(cpp, "razer_zone"):
        if len(values) != 4:
            raise ModelError(f"OpenRGB zone {name} has {len(values)} fields, expected 4")
        zone_name, zone_type, rows, columns = values
        if not isinstance(zone_name, str) or not isinstance(zone_type, str):
            raise ModelError(f"OpenRGB zone {name} has an invalid name or type")
        if not isinstance(rows, int) or not isinstance(columns, int):
            raise ModelError(f"OpenRGB zone {name} has invalid dimensions")
        zones[name] = {
            "name": zone_name,
            "type": zone_type,
            "rows": rows,
            "columns": columns,
            "source_line": line,
        }

    records: list[dict[str, Any]] = []
    for key, values, line in _declarations(cpp, "razer_device"):
        if len(values) != 9:
            raise ModelError(f"OpenRGB device {key} has {len(values)} fields, expected 9")
        model_name, pid_value, device_type, matrix_type, transaction_id, rows, columns, zone_values, layout = values
        if not isinstance(model_name, str) or not isinstance(pid_value, str):
            raise ModelError(f"OpenRGB device {key} has invalid identity fields")
        product_id = defines.get(pid_value)
        if product_id is None:
            raise ModelError(f"OpenRGB device {key} references unknown PID macro {pid_value}")
        if device_type not in {"DEVICE_TYPE_KEYBOARD", "DEVICE_TYPE_MOUSE"}:
            continue
        if not isinstance(zone_values, list):
            raise ModelError(f"OpenRGB device {key} zones are not a list")
        resolved_zones: list[dict[str, Any]] = []
        for zone_value in zone_values:
            zone_key = _reference_name(zone_value)
            if zone_key is None:
                continue
            zone = zones.get(zone_key)
            if zone is None:
                raise ModelError(f"OpenRGB device {key} references unknown zone {zone_key}")
            resolved_zones.append({item: value for item, value in zone.items() if item != "source_line"})
        source_route = _route(model_name)
        registered_vendors = sorted(detector_bindings.get(pid_value, set()))
        records.append(
            {
                "record_id": f"openrgb:{key}",
                "source_device_key": key,
                "model_name": model_name,
                "device_kind": "keyboard" if device_type == "DEVICE_TYPE_KEYBOARD" else "mouse",
                "source_route": source_route,
                "usb_identity": {
                    "vendor_id": registered_vendors[0] if len(registered_vendors) == 1 else None,
                    "product_id": product_id,
                },
                "lighting_topology": {
                    "matrix_type": matrix_type,
                    "rows": rows,
                    "columns": columns,
                    "application_slot_count": rows * columns,
                    "zones": resolved_zones,
                    "layout_key": _reference_name(layout),
                },
                "settings_methods": [],
                "facts": {
                    "detector_registered": bool(registered_vendors),
                    "transaction_id": transaction_id,
                },
                "source_location": {"path": DEVICES_CPP.as_posix(), "line": line},
            }
        )
    if not records or len(records) > MAX_OPENRGB_RECORDS:
        raise ModelError("normalized OpenRGB records are empty or exceed their bound")
    return {
        "$schema": "../../schemas/upstream-device-catalog.schema.json",
        "schema": "hyperflux-upstream-device-catalog-v1",
        "source": {
            "upstream_id": "openrgb",
            "repository": repository,
            "version": version,
            "commit": commit,
            "license_expression": license_expression,
            "extractor": "cpp-initializer-v1",
            "source_files": sorted(
                [
                    {"path": DEVICES_CPP.as_posix(), "sha256": _sha256(cpp_path)},
                    {"path": DEVICES_HEADER.as_posix(), "sha256": _sha256(header_path)},
                    {"path": DETECTOR_CPP.as_posix(), "sha256": _sha256(detector_path)},
                ],
                key=lambda item: item["path"],
            ),
        },
        "records": sorted(records, key=lambda item: item["record_id"]),
    }
