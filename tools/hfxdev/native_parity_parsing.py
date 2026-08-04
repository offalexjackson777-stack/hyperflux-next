# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from typing import Any

from .model import ModelError, sha256_file
from .native_probe import REPORT_SIZE, load_native_probe_catalog
from .private_artifact import write_new_private_json
from .profiles import load_profile_inputs


EVIDENCE_SCHEMA = "hyperflux-native-transport-parity-evidence-v1"
RAW_QUALIFICATION_SCHEMA = "hyperflux-native-qualification-raw-v1"
USBMON_LIMIT_BYTES = 16 * 1024 * 1024
USBMON_LIMIT_LINES = 65536
PIPE_PATTERN = re.compile(r"C[oi]:(?P<bus>[0-9]+):(?P<device>[0-9]+):(?P<endpoint>[0-9]+)\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
METHOD_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
PROFILE_PATTERN = re.compile(r"child\.[a-z0-9.-]+\Z")
BUSY_STATUS = 1
SUCCESS_STATUS = 2
RESPONSE_STATUS_MINIMUM = 1
RESPONSE_STATUS_MAXIMUM = 5
COMPARISONS = {"exact-semantic", "domain-valid", "redacted-equality"}
QUALIFICATION_CERTAINTIES = {"not-sent", "may-have-been-sent", "matched-response"}


@dataclass(frozen=True)
class UsbmonExchange:
    timestamp_us: int
    bus: int
    device: int
    interface: int
    request: bytes
    request_length: int
    response: bytes
    response_length: int


@dataclass(frozen=True)
class MatchedExchange:
    method: str
    probe: dict[str, Any]
    exchange: UsbmonExchange
    canonical_request: bytes
    request_complete: bool
    request_exact: bool | None
    response_structural: bool
    response_complete: bool
    summary: dict[str, Any]
    semantic_value: Any
    serial_value: bytes | None


@dataclass(frozen=True)
class DirectSemanticReference:
    """Sanitized direct evidence plus process-local comparison material."""

    evidence: dict[str, Any]
    serial_values: dict[str, bytes] = field(repr=False)


def _bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _read_bounded_text(path: Path) -> str:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ModelError("native parity input may not be a symbolic link")
    try:
        metadata = candidate.stat()
    except OSError as error:
        raise ModelError(f"cannot inspect native parity input: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ModelError("native parity input must be a regular file")
    if metadata.st_size > USBMON_LIMIT_BYTES:
        raise ModelError("native parity input exceeds the bounded size limit")
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ModelError(f"cannot read native parity input: {error}") from error


def _pipe_identity(value: str) -> tuple[int, int, int] | None:
    match = PIPE_PATTERN.fullmatch(value)
    if match is None:
        return None
    return tuple(int(match.group(name)) for name in ("bus", "device", "endpoint"))


def _payload(tokens: list[str]) -> bytes:
    try:
        marker = tokens.index("=")
    except ValueError:
        return b""
    chunks: list[str] = []
    for token in tokens[marker + 1 :]:
        if re.fullmatch(r"[0-9a-fA-F]+", token) is None or len(token) % 2:
            break
        chunks.append(token)
    try:
        value = bytes.fromhex("".join(chunks))
    except ValueError as error:
        raise ModelError("usbmon capture contains malformed hexadecimal payload") from error
    if len(value) > REPORT_SIZE:
        raise ModelError("usbmon capture contains an oversized Razer report")
    return value


def parse_usbmon_capture(path: Path) -> tuple[UsbmonExchange, ...]:
    lines = _read_bounded_text(path).splitlines()
    if len(lines) > USBMON_LIMIT_LINES:
        raise ModelError("usbmon capture exceeds the bounded line limit")
    pending: dict[tuple[int, int], list[tuple[int, int, bytes, int]]] = {}
    exchanges: list[UsbmonExchange] = []
    for line_number, line in enumerate(lines, start=1):
        tokens = line.split()
        if len(tokens) < 4:
            continue
        identity = _pipe_identity(tokens[3])
        if identity is None:
            continue
        bus, device, _endpoint = identity
        try:
            timestamp_us = int(tokens[1])
        except ValueError as error:
            raise ModelError(f"usbmon line {line_number} has an invalid timestamp") from error

        if tokens[2] == "S" and tokens[3].startswith("Co:"):
            if len(tokens) < 11 or tokens[4] != "s":
                continue
            try:
                request_type = int(tokens[5], 16)
                request_id = int(tokens[6], 16)
                value = int(tokens[7], 16)
                interface = int(tokens[8], 16)
                setup_length = int(tokens[9], 16)
                report_length = int(tokens[10], 10)
            except ValueError as error:
                raise ModelError(f"usbmon line {line_number} has invalid control setup") from error
            if (
                request_type != 0x21
                or request_id != 0x09
                or value != 0x0300
                or setup_length != REPORT_SIZE
                or report_length != REPORT_SIZE
            ):
                continue
            request = _payload(tokens)
            if len(request) < 8:
                raise ModelError("usbmon Razer request prefix is shorter than its header")
            pending.setdefault((bus, device), []).append(
                (timestamp_us, interface, request, report_length)
            )
            continue

        if tokens[2] != "C" or not tokens[3].startswith("Ci:"):
            continue
        response = _payload(tokens)
        if not response:
            continue
        queue = pending.get((bus, device))
        if not queue:
            raise ModelError("usbmon response has no preceding standard Razer request")
        request_timestamp, interface, request, request_length = queue.pop(0)
        try:
            response_length = int(tokens[5], 10)
        except (IndexError, ValueError) as error:
            raise ModelError(f"usbmon line {line_number} has invalid response length") from error
        if response_length != REPORT_SIZE or len(response) < 8:
            raise ModelError("usbmon response is not a standard 90-byte Razer report")
        exchanges.append(
            UsbmonExchange(
                timestamp_us=request_timestamp,
                bus=bus,
                device=device,
                interface=interface,
                request=request,
                request_length=request_length,
                response=response,
                response_length=response_length,
            )
        )
    if any(queue for queue in pending.values()):
        raise ModelError("usbmon capture ends with an unmatched Razer request")
    return tuple(exchanges)


def _base_request(probe: dict[str, Any]) -> bytearray:
    request = probe["request"]
    report = bytearray(REPORT_SIZE)
    report[5] = request["data_size"]
    report[6] = request["command_class"]
    report[7] = request["command_id"]
    arguments = bytes(request["arguments"])
    report[8 : 8 + len(arguments)] = arguments
    return report


def _report_crc(report: bytes | bytearray) -> int:
    value = 0
    for byte in report[2:88]:
        value ^= byte
    return value


def canonical_request(probe: dict[str, Any], transaction_id: int) -> bytes:
    if not 0 <= transaction_id <= 255:
        raise ModelError("Razer transaction identifier is outside one byte")
    report = _base_request(probe)
    report[1] = transaction_id
    report[88] = _report_crc(report)
    return bytes(report)


def _request_matches_signature(probe: dict[str, Any], request: bytes) -> bool:
    if len(request) < 8 or request[0] != 0:
        return False
    canonical = canonical_request(probe, request[1])
    semantic_end = min(len(request), 8 + canonical[5])
    return request[5:semantic_end] == canonical[5:semantic_end]


def _response_is_structural(request: bytes, response: bytes) -> bool:
    if len(request) < 8 or len(response) < 8:
        return False
    if not RESPONSE_STATUS_MINIMUM <= response[0] <= RESPONSE_STATUS_MAXIMUM:
        return False
    if response[1] != request[1] or response[2:4] != request[2:4]:
        return False
    if response[6] != request[6] or response[7] != request[7]:
        return False
    if len(response) == REPORT_SIZE and response[88] != _report_crc(response):
        return False
    return True


def _response_arguments(response: bytes) -> bytes:
    if len(response) < 8:
        return b""
    data_size = min(response[5], 80)
    return response[8 : min(len(response), 8 + data_size)]


def _serial_comparison_value(value: bytes) -> bytes | None:
    normalized = value.rstrip(b"\x00\t\n\r\x0b\x0c ")
    if not normalized or len(normalized) > 80 or b"\x00" in normalized:
        return None
    return normalized


def _semantic_summary(
    summary_kind: str,
    response: bytes,
) -> tuple[dict[str, Any], Any, bytes | None]:
    arguments = _response_arguments(response)
    expected = response[5] if len(response) >= 6 else 0
    complete_arguments = len(arguments) == expected
    summary: dict[str, Any] = {"kind": summary_kind}

    if summary_kind == "redacted-serial-equality":
        serial = _serial_comparison_value(bytes(arguments)) if complete_arguments else None
        summary["redacted"] = True
        summary["available"] = serial is not None
        return summary, None, serial

    value: Any = None
    if summary_kind == "firmware-version" and len(arguments) >= 2:
        value = (arguments[0], arguments[1])
        summary.update({"major": value[0], "minor": value[1]})
    elif summary_kind == "battery-u8" and arguments:
        raw = arguments[-1]
        value = raw
        summary.update({"raw_level": raw, "percent_floor": raw * 100 // 255})
    elif summary_kind in {"boolean-u8", "led-state", "led-effect", "u8-value"} and arguments:
        value = arguments[-1]
        summary["value"] = value
        if summary_kind == "boolean-u8":
            summary["enabled"] = bool(value)
    elif summary_kind == "be16-seconds" and len(arguments) >= 2:
        value = int.from_bytes(arguments[:2], "big")
        summary["seconds"] = value
    elif summary_kind == "device-mode" and len(response) >= 10:
        # OpenRazer's device_mode readers consume arguments[0] and
        # arguments[1] directly.  Some devices report a one-byte response
        # length while still returning both fixed-position bytes.
        value = (response[8], response[9])
        summary.update({"mode": value[0], "parameter": value[1]})
    elif summary_kind == "dpi-xy" and len(arguments) >= 5:
        value = (
            int.from_bytes(arguments[1:3], "big"),
            int.from_bytes(arguments[3:5], "big"),
        )
        summary.update({"x": value[0], "y": value[1]})
    elif summary_kind == "dpi-stages" and len(arguments) >= 3:
        active, count = arguments[1], arguments[2]
        required = count * 7 + 1
        complete = len(arguments) >= required
        stages = [
            {
                "x": int.from_bytes(arguments[4 + index * 7 : 6 + index * 7], "big"),
                "y": int.from_bytes(arguments[6 + index * 7 : 8 + index * 7], "big"),
            }
            for index in range(count)
            if 8 + index * 7 <= len(arguments)
        ]
        value = (active, tuple((stage["x"], stage["y"]) for stage in stages)) if complete else None
        summary.update(
            {
                "active_stage": active,
                "stage_count": count,
                "stages": stages,
                "complete_stage_table": complete,
            }
        )
    elif summary_kind == "keyboard-layout" and len(arguments) >= 2:
        value = arguments[0]
        summary["layout_code"] = value
    elif summary_kind == "polling-rate-code" and arguments:
        value = arguments[0]
        rate = {1: 1000, 2: 500, 3: 125}.get(value)
        summary.update({"code": value, "hertz": rate})
    summary["available"] = value is not None
    return summary, value, None


def _profile(catalog: dict[str, Any], profile_id: str) -> dict[str, Any]:
    matches = [value for value in catalog["devices"] if value["profile_id"] == profile_id]
    if len(matches) != 1:
        raise ModelError(f"unknown OpenRazer native parity profile: {profile_id}")
    return matches[0]


def _declared_receiver_selector(root: Path, device_kind: str) -> int:
    lane = {"mouse": "pointer_lane", "keyboard": "keyboard_lane"}.get(device_kind)
    if lane is None:
        raise ModelError("native parity device kind has no receiver endpoint lane")
    inputs = load_profile_inputs(root)
    receivers = [profile for profile in inputs.profiles if profile["kind"] == "receiver"]
    if len(receivers) != 1:
        raise ModelError("native parity requires exactly one canonical receiver profile")
    route_selector = (
        receivers[0].get("transport", {})
        .get("native_exchange", {})
        .get("route_selector")
    )
    if (
        not isinstance(route_selector, dict)
        or route_selector.get("field") != "transaction-id"
        or not _bounded_int(route_selector.get(lane), 1, 255)
    ):
        raise ModelError("native parity receiver route selector is unavailable")
    return route_selector[lane]


def _route_translation(
    *,
    application_selector: int,
    receiver_selector: int,
    response_selector: int | None,
    response_restored_for_application: bool | None,
    authority: str,
) -> dict[str, Any]:
    return {
        "field": "transaction-id",
        "application_selector": application_selector,
        "receiver_selector": receiver_selector,
        "applied": application_selector != receiver_selector,
        "response_selector": response_selector,
        "response_restored_for_application": response_restored_for_application,
        "authority": authority,
    }


def _match_exchanges(
    profile: dict[str, Any],
    exchanges: tuple[UsbmonExchange, ...],
) -> dict[str, list[MatchedExchange]]:
    matched: dict[str, list[MatchedExchange]] = {}
    for exchange in exchanges:
        candidates = [
            probe
            for probe in profile["probes"]
            if _request_matches_signature(probe, exchange.request)
        ]
        if not candidates:
            continue
        if len(candidates) != 1:
            raise ModelError("captured Razer request is ambiguous in the source-bound catalog")
        probe = candidates[0]
        request_complete = len(exchange.request) == REPORT_SIZE
        canonical = canonical_request(probe, exchange.request[1])
        request_exact = exchange.request == canonical if request_complete else None
        response_complete = len(exchange.response) == REPORT_SIZE
        response_structural = _response_is_structural(exchange.request, exchange.response)
        summary, semantic, serial = _semantic_summary(
            probe["response_summary"], exchange.response
        )
        item = MatchedExchange(
            method=probe["method"],
            probe=probe,
            exchange=exchange,
            canonical_request=canonical,
            request_complete=request_complete,
            request_exact=request_exact,
            response_structural=response_structural,
            response_complete=response_complete,
            summary=summary,
            semantic_value=semantic,
            serial_value=serial,
        )
        matched.setdefault(item.method, []).append(item)
    return matched
