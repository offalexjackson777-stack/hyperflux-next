# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any

from .model import ModelError, sha256_file
from .source_validated_common import BACKEND_REQUIRED, SOURCE_VALIDATED


OPENRGB_CONTROLLER_CPP = Path(
    "Controllers/RazerController/RazerController/RazerController.cpp"
)
OPENRGB_CONTROLLER_HEADER = Path(
    "Controllers/RazerController/RazerController/RazerController.h"
)
OPENRGB_DEVICES_HEADER = Path("Controllers/RazerController/RazerDevices.h")

OPENRGB_EXTENDED_FUNCTIONS = {
    "read-device-mode": "razer_get_device_mode",
    "read-firmware": "razer_get_firmware",
    "read-keyboard-info": "razer_get_keyboard_info",
    "read-serial": "razer_get_serial",
    "write-brightness": "razer_create_brightness_extended_matrix_report",
    "write-breathing-dual": "razer_create_mode_breathing_two_colors_extended_matrix_report",
    "write-breathing-random": "razer_create_mode_breathing_random_extended_matrix_report",
    "write-breathing-single": "razer_create_mode_breathing_one_color_extended_matrix_report",
    "write-custom-frame": "razer_create_custom_frame_extended_matrix_report",
    "write-custom-mode": "razer_create_mode_custom_extended_matrix_report",
    "write-device-mode": "razer_create_device_mode_report",
    "write-none": "razer_create_mode_none_extended_matrix_report",
    "write-spectrum": "razer_create_mode_spectrum_cycle_extended_matrix_report",
    "write-static": "razer_create_mode_static_extended_matrix_report",
    "write-wave": "razer_create_mode_wave_extended_matrix_report",
}


def _cpp_function_source(source: str, name: str) -> tuple[str, int]:
    pattern = re.compile(
        rf"(?m)^(?:[A-Za-z_][A-Za-z0-9_:<>*& ]+\s+)?RazerController::{re.escape(name)}\s*\("
    )
    match = pattern.search(source)
    if match is None:
        raise ModelError(f"OpenRGB function {name} is absent from pinned source")
    brace = source.find("{", match.end())
    if brace < 0:
        raise ModelError(f"OpenRGB function {name} has no body")
    depth = 0
    cursor = brace
    while cursor < len(source):
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
            if depth == 0:
                body = source[match.start() : cursor + 1]
                return body, source.count("\n", 0, match.start()) + 1
        cursor += 1
    raise ModelError(f"OpenRGB function {name} body is unterminated")


def _openrgb_led_selectors(source_root: Path) -> tuple[dict[int, int], int]:
    cpp = (source_root / OPENRGB_CONTROLLER_CPP).read_text(encoding="utf-8")
    header = (source_root / OPENRGB_DEVICES_HEADER).read_text(encoding="utf-8")
    defines = {
        name: int(value, 0)
        for name, value in re.findall(
            r"(?m)^\s*#define\s+([A-Z][A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+|[0-9]+)\s*$",
            header,
        )
    }
    marker = "Determine LED ID for device"
    start = cpp.find(marker)
    end = cpp.find("Determine matrix type for device", start)
    if start < 0 or end < 0:
        raise ModelError("OpenRGB LED selector switch is absent")
    block = cpp[start:end]
    selector_values = {"RAZER_LED_ID_ZERO": 0, "RAZER_LED_ID_BACKLIGHT": 5}
    result: dict[int, int] = {}
    pending: list[str] = []
    default_value: int | None = None
    token_pattern = re.compile(
        r"case\s+([A-Z][A-Z0-9_]+)\s*:|default\s*:|dev_led_id\s*=\s*(RAZER_LED_ID_ZERO|RAZER_LED_ID_BACKLIGHT)\s*;"
    )
    default_pending = False
    for match in token_pattern.finditer(block):
        if match.group(1):
            pending.append(match.group(1))
            default_pending = False
        elif match.group(2):
            value = selector_values[match.group(2)]
            if default_pending:
                default_value = value
            for macro in pending:
                if macro not in defines:
                    raise ModelError(f"OpenRGB LED selector names unknown PID macro {macro}")
                result[defines[macro]] = value
            pending.clear()
            default_pending = False
        else:
            default_pending = True
    if default_value is None:
        raise ModelError("OpenRGB LED selector switch has no default")
    return result, default_value


def _openrgb_source_binding(source: str, name: str) -> dict[str, Any]:
    body, line = _cpp_function_source(source, name)
    return {
        "upstream": "openrgb",
        "path": OPENRGB_CONTROLLER_CPP.as_posix(),
        "function": name,
        "line": line,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


def _openrgb_extended_rules(
    source_root: Path,
    record: dict[str, Any],
    led_selector: int,
) -> list[dict[str, Any]]:
    source = (source_root / OPENRGB_CONTROLLER_CPP).read_text(encoding="utf-8")
    rows = record["lighting_topology"]["rows"]
    columns = record["lighting_topology"]["columns"]
    source_for = lambda identifier: [_openrgb_source_binding(source, OPENRGB_EXTENDED_FUNCTIONS[identifier])]
    rules = [
        {
            "id": "openrgb-read-device-mode",
            "integrations": ["openrgb"],
            "methods": ["get-device-mode"],
            "direction": "read",
            "command_class": 0,
            "command_id": 0x84,
            "data_size": 2,
            "matcher": {"kind": "fields", "fields": [{"kind": "exact", "offset": 0, "bytes": [0, 0]}]},
            "sources": source_for("read-device-mode"),
        },
        {
            "id": "openrgb-read-firmware",
            "integrations": ["openrgb"],
            "methods": ["get-firmware"],
            "direction": "read",
            "command_class": 0,
            "command_id": 0x81,
            "data_size": 2,
            "matcher": {"kind": "fields", "fields": [{"kind": "exact", "offset": 0, "bytes": [0, 0]}]},
            "sources": source_for("read-firmware"),
        },
        {
            "id": "openrgb-read-serial",
            "integrations": ["openrgb"],
            "methods": ["get-serial"],
            "direction": "read",
            "command_class": 0,
            "command_id": 0x82,
            "data_size": 22,
            "matcher": {"kind": "fields", "fields": [{"kind": "exact", "offset": 0, "bytes": [0] * 22}]},
            "sources": source_for("read-serial"),
        },
        {
            "id": "openrgb-write-brightness",
            "integrations": ["openrgb"],
            "methods": ["set-brightness"],
            "direction": "write",
            "command_class": 0x0F,
            "command_id": 0x04,
            "data_size": 3,
            "matcher": {"kind": "fields", "fields": [
                {"kind": "exact", "offset": 0, "bytes": [0, led_selector]},
                {"kind": "u8-range", "offset": 2, "minimum": 0, "maximum": 255},
            ]},
            "sources": source_for("write-brightness"),
        },
        {
            "id": "openrgb-write-custom-frame",
            "integrations": ["openrgb"],
            "methods": ["set-custom-frame"],
            "direction": "write",
            "command_class": 0x0F,
            "command_id": 0x03,
            "data_sizes": [5 + pixels * 3 for pixels in range(1, columns + 1)],
            "matcher": {
                "kind": "custom-frame",
                "rows": rows,
                "columns": columns,
                "length_mode": "exact",
            },
            "sources": source_for("write-custom-frame"),
        },
        {
            "id": "openrgb-write-custom-mode",
            "integrations": ["openrgb"],
            "methods": ["set-custom-mode"],
            "direction": "write",
            "command_class": 0x0F,
            "command_id": 0x02,
            "data_size": 12,
            "matcher": {"kind": "fields", "fields": [{"kind": "exact", "offset": 0, "bytes": [0, 0, 8, 0, 0, 0, 0, 0, 0, 0, 0, 0]}]},
            "sources": source_for("write-custom-mode"),
        },
        {
            "id": "openrgb-write-device-mode",
            "integrations": ["openrgb"],
            "methods": ["set-device-mode"],
            "direction": "write",
            "command_class": 0,
            "command_id": 4,
            "data_size": 2,
            "matcher": {"kind": "fields", "fields": [
                {"kind": "one-of", "offset": 0, "values": [0, 3]},
                {"kind": "exact", "offset": 1, "bytes": [0]},
            ]},
            "sources": source_for("write-device-mode"),
        },
    ]
    if record["device_kind"] == "keyboard":
        rules.append(
            {
                "id": "openrgb-read-keyboard-info",
                "integrations": ["openrgb"],
                "methods": ["get-keyboard-info"],
                "direction": "read",
                "command_class": 0,
                "command_id": 0x86,
                "data_size": 2,
                "matcher": {"kind": "fields", "fields": [{"kind": "exact", "offset": 0, "bytes": [0, 0]}]},
                "sources": source_for("read-keyboard-info"),
            }
        )
    effect_specs = [
        ("breathing-dual", 12, [0, led_selector, 2, 2, 0, 2], 2),
        ("breathing-random", 6, [0, led_selector, 2, 0, 0, 0], 0),
        ("breathing-single", 9, [0, led_selector, 2, 1, 0, 1], 1),
        ("none", 6, [0, led_selector, 0, 0, 0, 0], 0),
        ("spectrum", 6, [0, led_selector, 3, 0, 0, 0], 0),
        ("static", 9, [0, led_selector, 1, 0, 0, 1], 1),
    ]
    for name, data_size, prefix, rgb_pixels in effect_specs:
        fields: list[dict[str, Any]] = [{"kind": "exact", "offset": 0, "bytes": prefix}]
        if rgb_pixels:
            fields.append({"kind": "rgb", "offset": 6, "pixels": rgb_pixels})
        rules.append(
            {
                "id": "openrgb-write-" + name,
                "integrations": ["openrgb"],
                "methods": ["set-" + name],
                "direction": "write",
                "command_class": 0x0F,
                "command_id": 0x02,
                "data_size": data_size,
                "matcher": {"kind": "fields", "fields": fields},
                "sources": source_for("write-" + name),
            }
        )
    rules.append(
        {
            "id": "openrgb-write-wave",
            "integrations": ["openrgb"],
            "methods": ["set-wave"],
            "direction": "write",
            "command_class": 0x0F,
            "command_id": 0x02,
            "data_size": 6,
            "matcher": {"kind": "fields", "fields": [
                {"kind": "exact", "offset": 0, "bytes": [0, led_selector, 4]},
                {"kind": "u8-range", "offset": 3, "minimum": 0, "maximum": 2},
                {"kind": "exact", "offset": 4, "bytes": [40, 0]},
            ]},
            "sources": source_for("write-wave"),
        }
    )
    return sorted(rules, key=lambda rule: rule["id"])


def _openrgb_integration(
    source_root: Path,
    record: dict[str, Any],
    selectors: dict[int, int],
    default_selector: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    topology = record["lighting_topology"]
    if not record["facts"].get("detector_registered"):
        return ({"state": BACKEND_REQUIRED, "reason": "detector-not-registered", "record_id": record["record_id"]}, [])
    if topology["matrix_type"] != "RAZER_MATRIX_TYPE_EXTENDED":
        return ({"state": BACKEND_REQUIRED, "reason": "unsupported-matrix-family:" + topology["matrix_type"], "record_id": record["record_id"]}, [])
    if not (1 <= topology["rows"] <= 32 and 1 <= topology["columns"] <= 25):
        return ({"state": BACKEND_REQUIRED, "reason": "unsupported-matrix-geometry", "record_id": record["record_id"]}, [])
    product_id = record["usb_identity"]["product_id"]
    selector = selectors.get(product_id, default_selector)
    source_path = record["source_location"]["path"]
    return (
        {
            "state": SOURCE_VALIDATED,
            "reason": None,
            "record_id": record["record_id"],
            "source_device_key": record["source_device_key"],
            "source": {
                "path": source_path,
                "line": record["source_location"]["line"],
                "sha256": sha256_file(source_root / source_path),
            },
            "transaction_id": record["facts"]["transaction_id"],
            "led_selector": selector,
            "lighting_topology": deepcopy(topology),
        },
        _openrgb_extended_rules(source_root, record, selector),
    )
