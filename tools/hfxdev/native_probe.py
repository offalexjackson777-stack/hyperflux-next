# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique, sha256_file
from .native_control import (
    load_native_control_policy,
    load_native_method_inventory,
)
from .openrazer import _source_commit


POLICY_SCHEMA = "hyperflux-openrazer-native-probe-policy-v1"
CATALOG_SCHEMA = "hyperflux-openrazer-native-probe-catalog-v1"
REPORT_SIZE = 90
METHOD_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
FUNCTION_PATTERN = re.compile(r"razer_attr_read_[a-z0-9_]+\Z")
EXPRESSION_PATTERN = re.compile(
    r"(?P<function>razer_chroma_[a-z0-9_]+|get_razer_report)"
    r"\((?P<arguments>[A-Z0-9_x, ]*)\)\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

POLICY_KEYS = {
    "$schema",
    "schema",
    "upstream_id",
    "source_commit",
    "report_size",
    "busy_retry",
    "bindings",
}
BINDING_KEYS = {
    "method",
    "device_kinds",
    "driver_attribute",
    "request_owner",
    "request_expression",
    "response_summary",
    "comparison",
}
CATALOG_KEYS = {
    "$schema",
    "schema",
    "upstream",
    "policy_sha256",
    "inventory_sha256",
    "report_size",
    "busy_retry",
    "source_files",
    "devices",
}
DEVICE_KEYS = {
    "profile_id",
    "wireless_identity",
    "direct_usb_reference",
    "probes",
}
PROBE_KEYS = {
    "method",
    "privacy",
    "driver",
    "request",
    "response_summary",
    "comparison",
}
DRIVER_KEYS = {
    "attribute",
    "source_path",
    "reader",
    "request_owner",
    "source_line",
    "source_sha256",
}
REQUEST_KEYS = {
    "command_class",
    "command_id",
    "data_size",
    "arguments",
    "normalized_sha256",
    "transaction_id_from_upstream",
    "crc_recomputed_by_upstream",
}
SUMMARY_KINDS = {
    "battery-u8",
    "be16-seconds",
    "boolean-u8",
    "device-mode",
    "dpi-stages",
    "dpi-xy",
    "firmware-version",
    "keyboard-layout",
    "led-effect",
    "led-state",
    "polling-rate-code",
    "redacted-serial-equality",
    "u8-value",
}
COMPARISON_KINDS = {"exact-semantic", "domain-valid", "redacted-equality"}
DRIVER_SOURCES = {
    "keyboard": "driver/razerkbd_driver.c",
    "mouse": "driver/razermouse_driver.c",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _policy_digest(policy: dict[str, Any]) -> str:
    return _digest({key: value for key, value in policy.items() if key != "$schema"})


def _inventory_digest(inventory: dict[str, Any]) -> str:
    return _digest({key: value for key, value in inventory.items() if key != "$schema"})


def native_probe_binding(
    policy: dict[str, Any], method: str, device_kind: str
) -> dict[str, Any]:
    matches = [
        binding
        for binding in policy["bindings"]
        if binding["method"] == method and device_kind in binding["device_kinds"]
    ]
    if len(matches) != 1:
        raise ModelError(
            f"OpenRazer native probe {method} has no unique {device_kind} binding"
        )
    return matches[0]


def load_native_probe_policy(root: Path) -> dict[str, Any]:
    policy = load_json(root / "integrations" / "openrazer" / "native-probe-policy.json")
    _exact_keys(policy, POLICY_KEYS, "OpenRazer native probe policy")
    if policy["$schema"] != "../../schemas/openrazer-native-probe-policy.schema.json":
        raise ModelError("OpenRazer native probe policy schema reference is not canonical")
    if policy["schema"] != POLICY_SCHEMA:
        raise ModelError("unsupported OpenRazer native probe policy schema")

    upstream = upstream_index(root).get(policy["upstream_id"])
    if upstream is None or upstream["id"] != "openrazer":
        raise ModelError("OpenRazer native probe policy names an unknown upstream")
    if policy["source_commit"] != upstream["commit"]:
        raise ModelError("OpenRazer native probe policy differs from the upstream pin")
    if policy["report_size"] != REPORT_SIZE:
        raise ModelError("OpenRazer native probes require exact 90-byte reports")

    busy = policy["busy_retry"]
    if not isinstance(busy, dict):
        raise ModelError("OpenRazer native probe Busy policy must be an object")
    _exact_keys(busy, {"status", "maximum_attempts"}, "OpenRazer Busy policy")
    if busy["status"] != 1 or not _bounded_int(busy["maximum_attempts"], 1, 8):
        raise ModelError("OpenRazer native probe Busy policy is invalid")

    native_policy = load_native_control_policy(root)
    inventory = load_native_method_inventory(root)
    read_only = set(native_policy["classifications"]["read_only"])
    expected_by_kind: dict[str, set[str]] = {"keyboard": set(), "mouse": set()}
    for device in inventory["devices"]:
        expected_by_kind[device["wireless_identity"]["device_kind"]].update(
            device["qualification_probes"]
        )

    bindings = policy["bindings"]
    if not isinstance(bindings, list) or not bindings or len(bindings) > 128:
        raise ModelError("OpenRazer native probe bindings must be a bounded list")
    binding_order: list[tuple[str, tuple[str, ...]]] = []
    method_kind_keys: list[str] = []
    covered_by_kind: dict[str, set[str]] = {"keyboard": set(), "mouse": set()}
    for index, binding in enumerate(bindings):
        label = f"OpenRazer native probe binding {index}"
        if not isinstance(binding, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(binding, BINDING_KEYS, label)
        method = binding["method"]
        if not isinstance(method, str) or METHOD_PATTERN.fullmatch(method) is None:
            raise ModelError(f"{label}: invalid method")
        if method not in read_only:
            raise ModelError(f"{label}: method is not classified read-only")
        kinds = binding["device_kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or len(kinds) > 2
            or any(kind not in {"keyboard", "mouse"} for kind in kinds)
        ):
            raise ModelError(f"{label}: invalid device kinds")
        require_unique(kinds, f"{label} device kind")
        if kinds != sorted(kinds):
            raise ModelError(f"{label}: device kinds must be sorted")
        attribute = binding["driver_attribute"]
        owner = binding["request_owner"]
        expression = binding["request_expression"]
        if (
            not isinstance(attribute, str)
            or METHOD_PATTERN.fullmatch(attribute) is None
            or not isinstance(owner, str)
            or FUNCTION_PATTERN.fullmatch(owner) is None
            or not isinstance(expression, str)
            or EXPRESSION_PATTERN.fullmatch(expression) is None
            or binding["response_summary"] not in SUMMARY_KINDS
            or binding["comparison"] not in COMPARISON_KINDS
        ):
            raise ModelError(f"{label}: invalid source binding")
        if (method == "get_serial") != (binding["comparison"] == "redacted-equality"):
            raise ModelError(f"{label}: serial comparison policy is invalid")
        if binding["comparison"] == "domain-valid" and method not in {
            "get_battery",
            "is_charging",
        }:
            raise ModelError(f"{label}: dynamic comparison is not approved for this method")
        for kind in kinds:
            if method not in expected_by_kind[kind]:
                raise ModelError(f"{label}: method is not a qualification probe for {kind}")
            covered_by_kind[kind].add(method)
            method_kind_keys.append(f"{method}:{kind}")
        binding_order.append((method, tuple(kinds)))
    require_unique(method_kind_keys, "OpenRazer native probe method/kind")
    if binding_order != sorted(binding_order):
        raise ModelError("OpenRazer native probe bindings must be sorted")
    if covered_by_kind != expected_by_kind:
        details = []
        for kind in ("keyboard", "mouse"):
            missing = sorted(expected_by_kind[kind] - covered_by_kind[kind])
            extra = sorted(covered_by_kind[kind] - expected_by_kind[kind])
            if missing:
                details.append(f"{kind} missing: {', '.join(missing)}")
            if extra:
                details.append(f"{kind} extra: {', '.join(extra)}")
        raise ModelError("OpenRazer native probe coverage drift: " + "; ".join(details))
    return policy


def _skip_c_quoted(source: str, index: int, quote: str) -> int:
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == quote:
            return index + 1
        index += 1
    raise ModelError("unterminated quoted string in pinned OpenRazer source")


def c_function_source(source: str, name: str) -> tuple[str, int]:
    pattern = re.compile(
        rf"(?m)^(?:static\s+)?(?:int|ssize_t|unsigned\s+char|unsigned\s+short|"
        rf"struct\s+razer_report)\s+{re.escape(name)}\s*\("
    )
    match = pattern.search(source)
    if match is None:
        raise ModelError(f"pinned OpenRazer source lacks function {name}")
    brace = source.find("{", match.end())
    if brace < 0:
        raise ModelError(f"pinned OpenRazer function {name} has no body")
    depth = 0
    index = brace
    while index < len(source):
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ModelError(f"unterminated comment in OpenRazer function {name}")
            index = end + 2
            continue
        character = source[index]
        if character in {'"', "'"}:
            index = _skip_c_quoted(source, index, character)
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                return source[match.start() : end], source.count("\n", 0, match.start()) + 1
        index += 1
    raise ModelError(f"unterminated OpenRazer function {name}")


def _device_attribute_readers(source: str) -> dict[str, str]:
    pattern = re.compile(
        r"(?m)^static\s+DEVICE_ATTR\(\s*(?P<attribute>[a-z0-9_]+)\s*,"
        r"\s*[0-7]+\s*,\s*(?P<reader>razer_attr_read_[a-z0-9_]+|NULL)\s*,"
    )
    readers: dict[str, str] = {}
    for match in pattern.finditer(source):
        attribute, reader = match.group("attribute"), match.group("reader")
        if attribute in readers:
            raise ModelError(f"duplicate OpenRazer driver attribute {attribute}")
        if reader != "NULL":
            readers[attribute] = reader
    return readers


def simple_c_defines(source: str) -> dict[str, int]:
    values: dict[str, int] = {}
    pattern = re.compile(
        r"(?m)^#define\s+(?P<name>[A-Z][A-Z0-9_]*)\s+(?P<value>0x[0-9A-Fa-f]+|[0-9]+)\s*(?://.*)?$"
    )
    for match in pattern.finditer(source):
        values[match.group("name")] = int(match.group("value"), 0)
    return values


def _expression_function(expression: str) -> str:
    match = EXPRESSION_PATTERN.fullmatch(expression)
    if match is None:
        raise ModelError(f"unsafe OpenRazer request expression: {expression}")
    return match.group("function")


def _compile_report_expressions(
    root: Path,
    source_root: Path,
    expressions: list[str],
) -> dict[str, bytes]:
    chroma_path = source_root / "driver" / "razerchromacommon.c"
    common_header = (source_root / "driver" / "razercommon.h").read_text(encoding="utf-8")
    chroma_source = chroma_path.read_text(encoding="utf-8")
    constants = simple_c_defines(common_header)
    required_constants = sorted(
        {
            token
            for expression in expressions
            for token in re.findall(r"\b[A-Z][A-Z0-9_]*\b", expression)
        }
    )
    missing_constants = [name for name in required_constants if name not in constants]
    if missing_constants:
        raise ModelError(
            "OpenRazer request expressions use unknown constants: "
            + ", ".join(missing_constants)
        )

    functions = sorted(
        {
            _expression_function(expression)
            for expression in expressions
            if _expression_function(expression) != "get_razer_report"
        }
    )
    function_sources = [c_function_source(chroma_source, name)[0] for name in functions]
    constant_lines = "\n".join(
        f"#define {name} {constants[name]}" for name in required_constants
    )
    emit_lines = []
    for expression in expressions:
        emit_lines.append(
            "    report = " + expression + ";\n"
            "    for(size_t index = 0; index < sizeof(report); ++index)\n"
            "        printf(\"%02x\", ((const unsigned char*)&report)[index]);\n"
            "    putchar('\\n');"
        )
    harness = f"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t __be16;
union transaction_id_union {{
    u8 id;
    struct {{ u8 device : 3; u8 id : 5; }} parts;
}};
union command_id_union {{
    u8 id;
    struct {{ u8 direction : 1; u8 id : 7; }} parts;
}};
struct razer_report {{
    u8 status;
    union transaction_id_union transaction_id;
    __be16 remaining_packets;
    u8 protocol_type;
    u8 data_size;
    u8 command_class;
    union command_id_union command_id;
    u8 arguments[80];
    u8 crc;
    u8 reserved;
}};
_Static_assert(sizeof(struct razer_report) == 90, "unexpected report layout");
{constant_lines}

struct razer_report get_razer_report(unsigned char command_class,
                                     unsigned char command_id,
                                     unsigned char data_size)
{{
    struct razer_report report = {{0}};
    report.command_class = command_class;
    report.command_id.id = command_id;
    report.data_size = data_size;
    return report;
}}

{os.linesep.join(function_sources)}

int main(void)
{{
    struct razer_report report;
{os.linesep.join(emit_lines)}
    return 0;
}}
"""
    compiler = os.environ.get("CC", "cc")
    scratch_root = root / "build"
    scratch_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix="hyperflux-openrazer-probe-",
        dir=scratch_root,
    ) as directory:
        executable = Path(directory) / "probe"
        try:
            compiled = subprocess.run(
                [compiler, "-x", "c", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(executable), "-"],
                input=harness,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot compile pinned OpenRazer probe constructors: {error}") from error
        if compiled.returncode != 0:
            raise ModelError(
                "pinned OpenRazer probe constructor compilation failed: "
                + compiled.stderr.strip()
            )
        try:
            rendered = subprocess.run(
                [str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot execute pinned OpenRazer probe constructors: {error}") from error
        if rendered.returncode != 0 or rendered.stderr:
            raise ModelError("pinned OpenRazer probe constructor harness failed")
    lines = rendered.stdout.splitlines()
    if len(lines) != len(expressions):
        raise ModelError("pinned OpenRazer probe constructor output is incomplete")
    reports: dict[str, bytes] = {}
    for expression, line in zip(expressions, lines, strict=True):
        try:
            report = bytes.fromhex(line)
        except ValueError as error:
            raise ModelError("pinned OpenRazer probe constructor emitted invalid hex") from error
        if len(report) != REPORT_SIZE:
            raise ModelError("pinned OpenRazer probe constructor emitted a non-90-byte report")
        reports[expression] = report
    return reports


def _identity_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "vendor_id",
            "product_id",
            "model_name",
            "device_kind",
            "transport_variant",
        )
    }


def transformed_native_probe_catalog(root: Path, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    policy = load_native_probe_policy(root)
    inventory = load_native_method_inventory(root)
    if _source_commit(source_root) != policy["source_commit"]:
        raise ModelError("OpenRazer native probe checkout does not match the immutable pin")

    expressions = sorted({value["request_expression"] for value in policy["bindings"]})
    reports = _compile_report_expressions(root, source_root, expressions)
    source_cache: dict[str, str] = {}
    reader_cache: dict[str, dict[str, str]] = {}
    devices: list[dict[str, Any]] = []

    for device in inventory["devices"]:
        kind = device["wireless_identity"]["device_kind"]
        source_path = DRIVER_SOURCES[kind]
        if source_path not in source_cache:
            source_cache[source_path] = (source_root / source_path).read_text(
                encoding="utf-8"
            )
        source = source_cache[source_path]
        readers = reader_cache.setdefault(source_path, _device_attribute_readers(source))
        probes: list[dict[str, Any]] = []
        methods = {value["id"]: value for value in device["methods"]}
        for method in device["qualification_probes"]:
            binding = native_probe_binding(policy, method, kind)
            attribute = binding["driver_attribute"]
            reader = readers.get(attribute)
            if reader is None:
                raise ModelError(f"OpenRazer driver lacks readable attribute {attribute}")
            reader_source, _ = c_function_source(source, reader)
            owner = binding["request_owner"]
            owner_source, owner_line = c_function_source(source, owner)
            if reader != owner and owner not in reader_source:
                raise ModelError(
                    f"OpenRazer attribute {attribute} does not delegate to {owner}"
                )
            constructor = _expression_function(binding["request_expression"])
            if constructor not in owner_source:
                raise ModelError(
                    f"OpenRazer request owner {owner} does not use {constructor}"
                )
            report = reports[binding["request_expression"]]
            data_size = report[5]
            if report[0] != 0 or report[1] != 0 or report[4] != 0 or report[89] != 0:
                raise ModelError(f"OpenRazer constructor for {method} has an invalid request header")
            if report[7] < 0x80 or data_size > 80:
                raise ModelError(f"OpenRazer qualification probe {method} is not a read command")
            probes.append(
                {
                    "method": method,
                    "privacy": methods[method]["privacy"],
                    "driver": {
                        "attribute": attribute,
                        "source_path": source_path,
                        "reader": reader,
                        "request_owner": owner,
                        "source_line": owner_line,
                        "source_sha256": hashlib.sha256(owner_source.encode()).hexdigest(),
                    },
                    "request": {
                        "command_class": report[6],
                        "command_id": report[7],
                        "data_size": data_size,
                        "arguments": list(report[8 : 8 + data_size]),
                        "normalized_sha256": hashlib.sha256(report).hexdigest(),
                        "transaction_id_from_upstream": True,
                        "crc_recomputed_by_upstream": True,
                    },
                    "response_summary": binding["response_summary"],
                    "comparison": binding["comparison"],
                }
            )
        devices.append(
            {
                "profile_id": device["profile_id"],
                "wireless_identity": _identity_projection(device["wireless_identity"]),
                "direct_usb_reference": _identity_projection(device["direct_usb_reference"]),
                "probes": probes,
            }
        )

    upstream = upstream_index(root)["openrazer"]
    relevant_paths = sorted(
        {
            "driver/razercommon.h",
            "driver/razerchromacommon.c",
            *DRIVER_SOURCES.values(),
        }
    )
    return {
        "$schema": "../../schemas/openrazer-native-probe-catalog.schema.json",
        "schema": CATALOG_SCHEMA,
        "upstream": {
            key: upstream[key]
            for key in ("id", "repository", "version", "commit", "license_expression")
        },
        "policy_sha256": _policy_digest(policy),
        "inventory_sha256": _inventory_digest(inventory),
        "report_size": REPORT_SIZE,
        "busy_retry": deepcopy(policy["busy_retry"]),
        "source_files": [
            {"path": path, "sha256": sha256_file(source_root / path)}
            for path in relevant_paths
        ],
        "devices": devices,
    }


def load_native_probe_catalog(root: Path) -> dict[str, Any]:
    catalog = load_json(root / "integrations" / "openrazer" / "native-probe-catalog.json")
    _exact_keys(catalog, CATALOG_KEYS, "OpenRazer native probe catalog")
    if catalog["$schema"] != "../../schemas/openrazer-native-probe-catalog.schema.json":
        raise ModelError("OpenRazer native probe catalog schema reference is not canonical")
    if catalog["schema"] != CATALOG_SCHEMA:
        raise ModelError("unsupported OpenRazer native probe catalog schema")
    policy = load_native_probe_policy(root)
    inventory = load_native_method_inventory(root)
    upstream = upstream_index(root)["openrazer"]
    expected_upstream = {
        key: upstream[key]
        for key in ("id", "repository", "version", "commit", "license_expression")
    }
    if catalog["upstream"] != expected_upstream:
        raise ModelError("OpenRazer native probe catalog upstream binding has drifted")
    if catalog["policy_sha256"] != _policy_digest(policy):
        raise ModelError("OpenRazer native probe catalog policy is stale")
    if catalog["inventory_sha256"] != _inventory_digest(inventory):
        raise ModelError("OpenRazer native probe catalog inventory is stale")
    if catalog["report_size"] != REPORT_SIZE or catalog["busy_retry"] != policy["busy_retry"]:
        raise ModelError("OpenRazer native probe catalog transport policy has drifted")

    source_paths: list[str] = []
    for index, source in enumerate(catalog["source_files"]):
        label = f"OpenRazer native probe source {index}"
        if not isinstance(source, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(source, {"path", "sha256"}, label)
        path = source["path"]
        if (
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(source["sha256"], str)
            or SHA256_PATTERN.fullmatch(source["sha256"]) is None
        ):
            raise ModelError(f"{label}: invalid source binding")
        source_paths.append(path)
    require_unique(source_paths, "OpenRazer native probe source path")
    expected_source_paths = sorted(
        {
            "driver/razercommon.h",
            "driver/razerchromacommon.c",
            *DRIVER_SOURCES.values(),
        }
    )
    if source_paths != expected_source_paths:
        raise ModelError("OpenRazer native probe source set has drifted")

    inventory_devices = {value["profile_id"]: value for value in inventory["devices"]}
    if not isinstance(catalog["devices"], list) or not 1 <= len(catalog["devices"]) <= 128:
        raise ModelError("OpenRazer native probe devices must be a bounded list")
    profile_ids: list[str] = []
    for index, device in enumerate(catalog["devices"]):
        label = f"OpenRazer native probe device {index}"
        if not isinstance(device, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(device, DEVICE_KEYS, label)
        profile_id = device["profile_id"]
        expected = inventory_devices.get(profile_id)
        if expected is None:
            raise ModelError(f"{label}: unknown profile")
        if device["wireless_identity"] != _identity_projection(expected["wireless_identity"]):
            raise ModelError(f"{label}: wireless identity has drifted")
        if device["direct_usb_reference"] != _identity_projection(expected["direct_usb_reference"]):
            raise ModelError(f"{label}: direct USB reference has drifted")
        expected_methods = expected["qualification_probes"]
        actual_methods: list[str] = []
        signatures: list[tuple[Any, ...]] = []
        method_records = {value["id"]: value for value in expected["methods"]}
        for probe_index, probe in enumerate(device["probes"]):
            probe_label = f"{label} probe {probe_index}"
            if not isinstance(probe, dict):
                raise ModelError(f"{probe_label}: expected an object")
            _exact_keys(probe, PROBE_KEYS, probe_label)
            method = probe["method"]
            binding = native_probe_binding(
                policy, method, expected["wireless_identity"]["device_kind"]
            )
            if probe["privacy"] != method_records[method]["privacy"]:
                raise ModelError(f"{probe_label}: privacy policy has drifted")
            if probe["response_summary"] != binding["response_summary"]:
                raise ModelError(f"{probe_label}: response summary has drifted")
            if probe["comparison"] != binding["comparison"]:
                raise ModelError(f"{probe_label}: comparison policy has drifted")
            driver = probe["driver"]
            request = probe["request"]
            if not isinstance(driver, dict) or not isinstance(request, dict):
                raise ModelError(f"{probe_label}: invalid source or request binding")
            _exact_keys(driver, DRIVER_KEYS, f"{probe_label} driver")
            _exact_keys(request, REQUEST_KEYS, f"{probe_label} request")
            if (
                driver["attribute"] != binding["driver_attribute"]
                or driver["request_owner"] != binding["request_owner"]
                or driver["source_path"] != DRIVER_SOURCES[expected["wireless_identity"]["device_kind"]]
                or not isinstance(driver["reader"], str)
                or FUNCTION_PATTERN.fullmatch(driver["reader"]) is None
                or not _bounded_int(driver["source_line"], 1, 2**31 - 1)
                or not isinstance(driver["source_sha256"], str)
                or SHA256_PATTERN.fullmatch(driver["source_sha256"]) is None
            ):
                raise ModelError(f"{probe_label}: invalid driver binding")
            arguments = request["arguments"]
            if (
                not _bounded_int(request["command_class"], 0, 0xFF)
                or not _bounded_int(request["command_id"], 0x80, 0xFF)
                or not _bounded_int(request["data_size"], 0, 80)
                or not isinstance(arguments, list)
                or len(arguments) != request["data_size"]
                or any(not _bounded_int(value, 0, 255) for value in arguments)
                or not isinstance(request["normalized_sha256"], str)
                or SHA256_PATTERN.fullmatch(request["normalized_sha256"]) is None
                or request["transaction_id_from_upstream"] is not True
                or request["crc_recomputed_by_upstream"] is not True
            ):
                raise ModelError(f"{probe_label}: invalid request signature")
            normalized = bytearray(REPORT_SIZE)
            normalized[5] = request["data_size"]
            normalized[6] = request["command_class"]
            normalized[7] = request["command_id"]
            normalized[8 : 8 + len(arguments)] = bytes(arguments)
            if hashlib.sha256(normalized).hexdigest() != request["normalized_sha256"]:
                raise ModelError(f"{probe_label}: normalized request digest has drifted")
            signatures.append(
                (
                    request["command_class"],
                    request["command_id"],
                    request["data_size"],
                    tuple(arguments),
                )
            )
            actual_methods.append(method)
        require_unique(actual_methods, f"{label} method")
        require_unique(signatures, f"{label} request signature")
        if actual_methods != expected_methods:
            raise ModelError(f"{label}: qualification probe order has drifted")
        profile_ids.append(profile_id)
    require_unique(profile_ids, "OpenRazer native probe profile")
    if profile_ids != sorted(inventory_devices):
        raise ModelError("OpenRazer native probe catalog does not cover the exact selection")
    return catalog


def write_native_probe_catalog(root: Path, source_root: Path) -> Path:
    destination = root / "integrations" / "openrazer" / "native-probe-catalog.json"
    value = transformed_native_probe_catalog(root, source_root)
    destination.write_text(
        json.dumps(value, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return destination
