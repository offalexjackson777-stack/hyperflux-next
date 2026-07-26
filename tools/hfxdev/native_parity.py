# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
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


def _semantic_summary(
    summary_kind: str,
    response: bytes,
) -> tuple[dict[str, Any], Any, bytes | None]:
    arguments = _response_arguments(response)
    expected = response[5] if len(response) >= 6 else 0
    complete_arguments = len(arguments) == expected
    summary: dict[str, Any] = {"kind": summary_kind}

    if summary_kind == "redacted-serial-equality":
        summary["redacted"] = True
        summary["available"] = complete_arguments
        return summary, None, bytes(arguments) if complete_arguments else None

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
    elif summary_kind == "device-mode" and len(arguments) >= 2:
        value = (arguments[0], arguments[1])
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
        value = (active, count) if len(arguments) >= required else None
        summary.update(
            {
                "active_stage": active,
                "stage_count": count,
                "complete_stage_table": len(arguments) >= required,
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


def _validate_raw_qualification(
    value: dict[str, Any],
    *,
    profile_id: str,
    source_revision: str,
    package_sha256: str,
    receiver_generation: int,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "source_revision",
        "package_sha256",
        "profile_id",
        "receiver_generation",
        "read_only",
        "completed",
        "records",
    }:
        raise ModelError("native qualification raw capture is malformed")
    if (
        value["schema"] != RAW_QUALIFICATION_SCHEMA
        or value["profile_id"] != profile_id
        or value["source_revision"] != source_revision
        or value["package_sha256"] != package_sha256
        or value["receiver_generation"] != receiver_generation
        or value["read_only"] is not True
        or not isinstance(value["completed"], bool)
    ):
        raise ModelError("native qualification raw capture binding is invalid")
    records = value["records"]
    if not isinstance(records, list) or len(records) > 128:
        raise ModelError("native qualification raw capture is not bounded")
    expected_sequence = 1
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "sequence",
            "method",
            "attempt",
            "request_hex",
            "certainty",
            "transport_errno",
            "response_hex",
        }:
            raise ModelError("native qualification raw record is malformed")
        method = record["method"]
        request_hex = record["request_hex"]
        response_hex = record["response_hex"]
        if (
            record["sequence"] != expected_sequence
            or (method is not None and (
                not isinstance(method, str) or METHOD_PATTERN.fullmatch(method) is None
            ))
            or not _bounded_int(record["attempt"], 0, 6)
            or not isinstance(request_hex, str)
            or re.fullmatch(r"[0-9a-f]{180}", request_hex) is None
            or record["certainty"] not in QUALIFICATION_CERTAINTIES
            or not isinstance(record["transport_errno"], int)
            or not -(2**31) <= record["transport_errno"] < 2**31
            or (
                response_hex is not None
                and (
                    not isinstance(response_hex, str)
                    or re.fullmatch(r"[0-9a-f]{180}", response_hex) is None
                )
            )
        ):
            raise ModelError("native qualification raw record value is invalid")
        if method is None and (
            record["certainty"] != "not-sent" or response_hex is not None
        ):
            raise ModelError("an unclassified qualification request crossed transport")
        if record["certainty"] == "matched-response" and response_hex is None:
            raise ModelError("matched qualification response is absent")
        if record["certainty"] != "matched-response" and response_hex is not None:
            raise ModelError("non-matched qualification record contains a response")
        expected_sequence += 1


def _raw_receiver_matches(
    profile: dict[str, Any], raw: dict[str, Any]
) -> dict[str, list[MatchedExchange]]:
    probes = {probe["method"]: probe for probe in profile["probes"]}
    matched: dict[str, list[MatchedExchange]] = {}
    for record in raw["records"]:
        method = record["method"]
        if method is None or record["certainty"] != "matched-response":
            continue
        probe = probes.get(method)
        if probe is None:
            raise ModelError("native qualification raw method is not source-bound")
        request = bytes.fromhex(record["request_hex"])
        response = bytes.fromhex(record["response_hex"])
        if not _request_matches_signature(probe, request):
            raise ModelError("native qualification request differs from its upstream signature")
        canonical = canonical_request(probe, request[1])
        if request != canonical:
            raise ModelError("native qualification request was rewritten")
        summary, semantic, serial = _semantic_summary(probe["response_summary"], response)
        item = MatchedExchange(
            method=method,
            probe=probe,
            exchange=UsbmonExchange(
                timestamp_us=record["sequence"],
                bus=0,
                device=0,
                interface=0,
                request=request,
                request_length=REPORT_SIZE,
                response=response,
                response_length=REPORT_SIZE,
            ),
            canonical_request=canonical,
            request_complete=True,
            request_exact=True,
            response_structural=_response_is_structural(request, response),
            response_complete=True,
            summary=summary,
            semantic_value=semantic,
            serial_value=serial,
        )
        matched.setdefault(method, []).append(item)
    return matched


def _standard_terminal_response(item: MatchedExchange) -> bool | None:
    if not item.response_complete:
        return None
    if not item.response_structural:
        return False
    if item.exchange.response[0] == BUSY_STATUS:
        return None
    return True


def _semantic_summary_parity(
    probe: dict[str, Any],
    *,
    direct_status: int,
    direct_structural: bool,
    direct_summary: dict[str, Any],
    receiver_status: int,
    receiver_structural: bool,
    receiver_summary: dict[str, Any],
) -> bool | None:
    if not direct_structural or not receiver_structural:
        return False
    if BUSY_STATUS in {direct_status, receiver_status}:
        return None
    if direct_status != receiver_status:
        return False

    comparison = probe["comparison"]
    if comparison == "redacted-equality":
        return None
    if direct_status != SUCCESS_STATUS:
        if comparison == "domain-valid":
            return True
        if comparison == "exact-semantic":
            return direct_summary == receiver_summary
        raise ModelError("native qualification comparison policy is unsupported")
    if comparison == "domain-valid":
        if (
            direct_summary.get("available") is not True
            or receiver_summary.get("available") is not True
        ):
            return None
        return True
    if comparison == "exact-semantic":
        if (
            direct_summary.get("available") is not True
            or receiver_summary.get("available") is not True
        ):
            return None
        return direct_summary == receiver_summary
    raise ModelError("native qualification comparison policy is unsupported")


def _semantic_parity_from_evidence(
    probe: dict[str, Any],
    direct: dict[str, Any] | None,
    receiver: MatchedExchange,
) -> bool | None:
    if direct is None or not receiver.response_complete:
        return None
    response = direct["response"]
    return _semantic_summary_parity(
        probe,
        direct_status=response["status"],
        direct_structural=response["structural_match"],
        direct_summary=response["summary"],
        receiver_status=receiver.exchange.response[0],
        receiver_structural=receiver.response_structural,
        receiver_summary=receiver.summary,
    )


def build_live_parity_evidence(
    root: Path,
    *,
    profile_id: str,
    direct_evidence: dict[str, Any],
    raw_qualification: dict[str, Any],
    package_sha256: str,
    receiver_generation: int,
    created_at: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Sanitize one ephemeral receiver qualification into canonical parity evidence."""

    catalog = load_native_probe_catalog(root)
    profile = _profile(catalog, profile_id)
    revision = source_revision or _git_revision(root)
    if SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ModelError("native live parity source revision is invalid")
    if SHA256_PATTERN.fullmatch(package_sha256) is None:
        raise ModelError("native live parity package digest is invalid")
    if not _bounded_int(receiver_generation, 1, 2**63 - 1):
        raise ModelError("native live parity receiver generation is invalid")
    validate_parity_evidence(direct_evidence)
    if (
        direct_evidence["profile_id"] != profile_id
        or direct_evidence["upstream_commit"] != catalog["upstream"]["commit"]
        or direct_evidence["catalog_sha256"]
        != sha256_file(root / "integrations" / "openrazer" / "native-probe-catalog.json")
    ):
        raise ModelError("direct semantic reference differs from the live probe authority")
    _validate_raw_qualification(
        raw_qualification,
        profile_id=profile_id,
        source_revision=revision,
        package_sha256=package_sha256,
        receiver_generation=receiver_generation,
    )
    receiver_matches = _raw_receiver_matches(profile, raw_qualification)
    direct_by_method = {
        observation["method"]: observation
        for observation in direct_evidence["observations"]
        if observation["origin"] == "direct-semantic-reference"
    }
    maximum_attempts = catalog["busy_retry"]["maximum_attempts"]
    required = [probe["method"] for probe in profile["probes"]]
    probes = {probe["method"]: probe for probe in profile["probes"]}
    observations: list[dict[str, Any]] = []
    receiver_observations: dict[str, dict[str, Any]] = {}
    for method in required:
        direct = direct_by_method.get(method)
        if direct is not None:
            observations.append(direct)
        routed = receiver_matches.get(method, [])
        if not routed:
            continue
        receiver = _receiver_observation(routed, None, maximum_attempts)
        receiver["receiver_comparison"]["semantic_parity"] = (
            _semantic_parity_from_evidence(probes[method], direct, routed[-1])
        )
        receiver_observations[method] = receiver
        observations.append(receiver)

    passed: list[str] = []
    failed: list[str] = []
    for method in required:
        receiver = receiver_observations.get(method)
        if receiver is None:
            continue
        comparison = receiver["receiver_comparison"]
        states = tuple(
            comparison[field]
            for field in ("unchanged_request", "standard_response", "semantic_parity")
        )
        if all(state is True for state in states):
            passed.append(method)
        elif any(state is False for state in states):
            failed.append(method)
    missing = [method for method in required if method not in passed and method not in failed]
    backend_required = any(
        observation["receiver_comparison"][field] is False
        for observation in receiver_observations.values()
        for field in ("unchanged_request", "standard_response")
    )
    conclusion = (
        "backend-required"
        if backend_required
        else "passed"
        if not missing and not failed
        else "incomplete"
    )
    timestamp = created_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    evidence = {
        "$schema": "https://hyperflux.dev/schemas/native-transport-parity-evidence-v1.json",
        "schema": EVIDENCE_SCHEMA,
        "created_at": timestamp,
        "source_revision": revision,
        "profile_id": profile_id,
        "upstream_commit": catalog["upstream"]["commit"],
        "catalog_sha256": sha256_file(
            root / "integrations" / "openrazer" / "native-probe-catalog.json"
        ),
        "safety": {
            "read_only": True,
            "settings_changed": False,
            "serials_persisted": False,
            "raw_reports_persisted": False,
            "private_paths_persisted": False,
            "network_upload": False,
            "file_mode": "0600",
        },
        "observations": observations,
        "coverage": {
            "required": required,
            "passed": passed,
            "missing": missing,
            "failed": failed,
        },
        "conclusion": conclusion,
    }
    validate_parity_evidence(evidence)
    return evidence


def _request_evidence(item: MatchedExchange) -> dict[str, Any]:
    request = item.probe["request"]
    observed = item.exchange.request if item.request_complete else item.canonical_request
    return {
        "sha256": hashlib.sha256(observed).hexdigest(),
        "command_class": request["command_class"],
        "command_id": request["command_id"],
        "data_size": request["data_size"],
        "upstream_signature_matched": True,
        "complete_capture": item.request_complete,
        "reconstructed_from_upstream": not item.request_complete,
    }


def _direct_observation(item: MatchedExchange) -> dict[str, Any]:
    return {
        "method": item.method,
        "origin": "direct-semantic-reference",
        "comparison": item.probe["comparison"],
        "request": _request_evidence(item),
        "response": {
            "status": item.exchange.response[0],
            "structural_match": item.response_structural,
            "complete_capture": item.response_complete,
            "summary": item.summary,
        },
        "receiver_comparison": {
            "attempts": 0,
            "unchanged_request": None,
            "standard_response": None,
            "semantic_parity": None,
        },
    }


def _receiver_observation(
    items: list[MatchedExchange],
    direct: MatchedExchange | None,
    maximum_attempts: int,
) -> dict[str, Any]:
    if not items:
        raise ModelError("receiver parity observation requires at least one exchange")
    if len(items) > maximum_attempts:
        raise ModelError("receiver Busy retry count exceeds the read-only policy")
    final = items[-1]
    for earlier in items[:-1]:
        if earlier.exchange.response[0] != 1:
            raise ModelError("receiver retry sequence contains a non-Busy intermediate response")
    request_states = {item.request_exact for item in items}
    unchanged: bool | None
    if False in request_states:
        unchanged = False
    elif request_states == {True}:
        unchanged = True
    else:
        unchanged = None

    standard = _standard_terminal_response(final)

    semantic_parity: bool | None = None
    if direct is not None:
        comparison = final.probe["comparison"]
        direct_status = direct.exchange.response[0]
        receiver_status = final.exchange.response[0]
        if (
            direct.response_structural
            and final.response_structural
            and direct_status == SUCCESS_STATUS
            and receiver_status == SUCCESS_STATUS
            and comparison == "redacted-equality"
        ):
            if final.serial_value is not None and direct.serial_value is not None:
                semantic_parity = secrets.compare_digest(
                    final.serial_value, direct.serial_value
                )
        else:
            semantic_parity = _semantic_summary_parity(
                final.probe,
                direct_status=direct_status,
                direct_structural=direct.response_structural,
                direct_summary=direct.summary,
                receiver_status=receiver_status,
                receiver_structural=final.response_structural,
                receiver_summary=final.summary,
            )

    return {
        "method": final.method,
        "origin": "receiver-route",
        "comparison": final.probe["comparison"],
        "request": _request_evidence(final),
        "response": {
            "status": final.exchange.response[0],
            "structural_match": final.response_structural,
            "complete_capture": final.response_complete,
            "summary": final.summary,
        },
        "receiver_comparison": {
            "attempts": len(items),
            "unchanged_request": unchanged,
            "standard_response": standard,
            "semantic_parity": semantic_parity,
        },
    }


def _apply_legacy_firmware_result(
    observation: dict[str, Any],
    path: Path,
    kind: str,
    maximum_attempts: int,
) -> None:
    value = json.loads(_read_bounded_text(path))
    if value.get("schema") != "hyperflux-transport-parity-probe-v1":
        raise ModelError("unsupported legacy parity result schema")
    if value.get("operation") != "read-firmware-bounded-retry" or value.get("target") != kind:
        raise ModelError("legacy parity result does not describe this firmware probe")
    if value.get("settings_changed") is not False or value.get("network_upload") is not False:
        raise ModelError("legacy parity result crosses the read-only privacy boundary")
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= maximum_attempts:
        raise ModelError("legacy parity result has an invalid retry count")
    final = attempts[-1]
    firmware = value.get("final_firmware")
    if (
        not isinstance(final, dict)
        or not isinstance(firmware, dict)
        or not isinstance(firmware.get("major"), int)
        or not isinstance(firmware.get("minor"), int)
    ):
        raise ModelError("legacy parity result lacks a bounded firmware summary")
    observation["response"] = {
        "status": final.get("status", 0),
        "structural_match": final.get("structural_match") is True,
        "complete_capture": True,
        "summary": {
            "kind": "firmware-version",
            "major": firmware["major"],
            "minor": firmware["minor"],
            "available": True,
        },
    }
    comparison = observation["receiver_comparison"]
    comparison["attempts"] = len(attempts)
    comparison["standard_response"] = (
        value.get("passed") is True
        and final.get("status") == SUCCESS_STATUS
        and final.get("structural_match") is True
    )
    comparison["semantic_parity"] = value.get("matches_direct") is True


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot bind native parity evidence to source: {error}") from error
    revision = result.stdout.strip()
    if SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ModelError("native parity source revision is not an exact commit")
    return revision


def build_offline_parity_evidence(
    root: Path,
    *,
    profile_id: str,
    direct_capture: Path,
    receiver_capture: Path | None = None,
    receiver_result: Path | None = None,
    created_at: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    catalog = load_native_probe_catalog(root)
    profile = _profile(catalog, profile_id)
    direct_matches = _match_exchanges(profile, parse_usbmon_capture(direct_capture))
    receiver_matches = (
        _match_exchanges(profile, parse_usbmon_capture(receiver_capture))
        if receiver_capture is not None
        else {}
    )
    maximum_attempts = catalog["busy_retry"]["maximum_attempts"]
    observations: list[dict[str, Any]] = []
    receiver_observations: dict[str, dict[str, Any]] = {}
    required = [probe["method"] for probe in profile["probes"]]
    for method in required:
        direct_items = direct_matches.get(method, [])
        direct = direct_items[-1] if direct_items else None
        if direct is not None:
            observations.append(_direct_observation(direct))
        routed = receiver_matches.get(method, [])
        if routed:
            receiver = _receiver_observation(routed, direct, maximum_attempts)
            receiver_observations[method] = receiver
            observations.append(receiver)

    if receiver_result is not None:
        firmware = receiver_observations.get("get_firmware")
        if firmware is None:
            raise ModelError("legacy receiver result has no matching captured firmware request")
        _apply_legacy_firmware_result(
            firmware,
            receiver_result,
            profile["wireless_identity"]["device_kind"],
            maximum_attempts,
        )

    passed: list[str] = []
    failed: list[str] = []
    for method in required:
        receiver = receiver_observations.get(method)
        if receiver is None:
            continue
        comparison = receiver["receiver_comparison"]
        values = (
            comparison["unchanged_request"],
            comparison["standard_response"],
            comparison["semantic_parity"],
        )
        if all(value is True for value in values):
            passed.append(method)
        elif any(value is False for value in values):
            failed.append(method)
    missing = [method for method in required if method not in passed and method not in failed]
    conclusion = "passed" if not missing and not failed else "incomplete"
    if any(
        observation["receiver_comparison"][field] is False
        for observation in receiver_observations.values()
        for field in ("unchanged_request", "standard_response")
    ):
        conclusion = "backend-required"

    timestamp = created_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    revision = source_revision or _git_revision(root)
    if SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
        raise ModelError("native parity evidence source revision is invalid")
    evidence = {
        "$schema": "https://hyperflux.dev/schemas/native-transport-parity-evidence-v1.json",
        "schema": EVIDENCE_SCHEMA,
        "created_at": timestamp,
        "source_revision": revision,
        "profile_id": profile_id,
        "upstream_commit": catalog["upstream"]["commit"],
        "catalog_sha256": sha256_file(
            root / "integrations" / "openrazer" / "native-probe-catalog.json"
        ),
        "safety": {
            "read_only": True,
            "settings_changed": False,
            "serials_persisted": False,
            "raw_reports_persisted": False,
            "private_paths_persisted": False,
            "network_upload": False,
            "file_mode": "0600",
        },
        "observations": observations,
        "coverage": {
            "required": required,
            "passed": passed,
            "missing": missing,
            "failed": failed,
        },
        "conclusion": conclusion,
    }
    validate_parity_evidence(evidence)
    return evidence


def validate_parity_evidence(value: dict[str, Any]) -> None:
    def exact_keys(item: dict[str, Any], expected: set[str], label: str) -> None:
        missing_keys = sorted(expected - set(item))
        extra_keys = sorted(set(item) - expected)
        if missing_keys:
            raise ModelError(f"{label}: missing keys: {', '.join(missing_keys)}")
        if extra_keys:
            raise ModelError(f"{label}: unsupported keys: {', '.join(extra_keys)}")

    if not isinstance(value, dict):
        raise ModelError("native parity evidence must be an object")
    exact_keys(
        value,
        {
            "$schema",
            "schema",
            "created_at",
            "source_revision",
            "profile_id",
            "upstream_commit",
            "catalog_sha256",
            "safety",
            "observations",
            "coverage",
            "conclusion",
        },
        "native parity evidence",
    )
    if value["$schema"] != "https://hyperflux.dev/schemas/native-transport-parity-evidence-v1.json":
        raise ModelError("native parity evidence schema reference is not canonical")
    if value.get("schema") != EVIDENCE_SCHEMA:
        raise ModelError("unsupported native transport parity evidence schema")
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise ModelError("native parity evidence timestamp is not canonical UTC")
    try:
        datetime.fromisoformat(created_at.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ModelError("native parity evidence timestamp is invalid") from error
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or PROFILE_PATTERN.fullmatch(profile_id) is None:
        raise ModelError("native parity evidence profile identifier is invalid")
    upstream_commit = value.get("upstream_commit")
    if (
        not isinstance(upstream_commit, str)
        or SOURCE_REVISION_PATTERN.fullmatch(upstream_commit) is None
    ):
        raise ModelError("native parity evidence upstream commit is invalid")
    if not isinstance(value.get("safety"), dict):
        raise ModelError("native parity evidence safety declaration is missing")
    if value.get("safety") != {
        "read_only": True,
        "settings_changed": False,
        "serials_persisted": False,
        "raw_reports_persisted": False,
        "private_paths_persisted": False,
        "network_upload": False,
        "file_mode": "0600",
    }:
        raise ModelError("native parity evidence safety declaration is invalid")
    source_revision = value.get("source_revision")
    if (
        not isinstance(source_revision, str)
        or SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None
    ):
        raise ModelError("native parity evidence source revision is invalid")
    catalog_sha256 = value.get("catalog_sha256")
    if (
        not isinstance(catalog_sha256, str)
        or SHA256_PATTERN.fullmatch(catalog_sha256) is None
    ):
        raise ModelError("native parity evidence catalog binding is invalid")
    coverage = value.get("coverage")
    if not isinstance(coverage, dict):
        raise ModelError("native parity evidence coverage is missing")
    exact_keys(
        coverage,
        {"required", "passed", "missing", "failed"},
        "native parity evidence coverage",
    )
    required = coverage.get("required")
    passed = coverage.get("passed")
    missing = coverage.get("missing")
    failed = coverage.get("failed")
    if not all(isinstance(item, list) for item in (required, passed, missing, failed)):
        raise ModelError("native parity evidence coverage is malformed")
    for label, methods in (
        ("required", required),
        ("passed", passed),
        ("missing", missing),
        ("failed", failed),
    ):
        if (
            len(methods) > 128
            or len(methods) != len(set(methods))
            or any(not isinstance(method, str) or METHOD_PATTERN.fullmatch(method) is None for method in methods)
        ):
            raise ModelError(f"native parity evidence {label} methods are invalid")
        if methods != [method for method in required if method in set(methods)]:
            raise ModelError(f"native parity evidence {label} methods are out of order")
    if set(required) != set(passed) | set(missing) | set(failed):
        raise ModelError("native parity evidence coverage is not exhaustive")
    if set(passed) & set(missing) or set(passed) & set(failed) or set(missing) & set(failed):
        raise ModelError("native parity evidence coverage overlaps")

    observations = value.get("observations")
    if not isinstance(observations, list) or len(observations) > 256:
        raise ModelError("native parity evidence observations are not bounded")
    routed: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for index, observation in enumerate(observations):
        label = f"native parity observation {index}"
        if not isinstance(observation, dict):
            raise ModelError(f"{label}: expected an object")
        exact_keys(
            observation,
            {"method", "origin", "comparison", "request", "response", "receiver_comparison"},
            label,
        )
        method = observation["method"]
        origin = observation["origin"]
        if method not in required or origin not in {
            "direct-semantic-reference",
            "receiver-route",
        }:
            raise ModelError(f"{label}: invalid method or origin")
        if observation["comparison"] not in COMPARISONS:
            raise ModelError(f"{label}: invalid comparison policy")
        identity = (method, origin)
        if identity in seen:
            raise ModelError(f"{label}: duplicate method origin")
        seen.add(identity)

        request = observation["request"]
        response = observation["response"]
        comparison = observation["receiver_comparison"]
        if not all(isinstance(item, dict) for item in (request, response, comparison)):
            raise ModelError(f"{label}: malformed request, response, or comparison")
        exact_keys(
            request,
            {
                "sha256",
                "command_class",
                "command_id",
                "data_size",
                "upstream_signature_matched",
                "complete_capture",
                "reconstructed_from_upstream",
            },
            f"{label} request",
        )
        if (
            not isinstance(request.get("sha256"), str)
            or SHA256_PATTERN.fullmatch(request["sha256"]) is None
            or not _bounded_int(request.get("command_class"), 0, 255)
            or not _bounded_int(request.get("command_id"), 128, 255)
            or not _bounded_int(request.get("data_size"), 0, 80)
            or request.get("upstream_signature_matched") is not True
            or not isinstance(request.get("complete_capture"), bool)
            or not isinstance(request.get("reconstructed_from_upstream"), bool)
            or request["complete_capture"] == request["reconstructed_from_upstream"]
        ):
            raise ModelError(f"{label}: invalid request evidence")
        exact_keys(
            response,
            {"status", "structural_match", "complete_capture", "summary"},
            f"{label} response",
        )
        if (
            not _bounded_int(response.get("status"), 0, 255)
            or not isinstance(response.get("structural_match"), bool)
            or not isinstance(response.get("complete_capture"), bool)
            or not isinstance(response.get("summary"), dict)
        ):
            raise ModelError(f"{label}: invalid response evidence")
        exact_keys(
            comparison,
            {"attempts", "unchanged_request", "standard_response", "semantic_parity"},
            f"{label} comparison",
        )
        attempts = comparison["attempts"]
        states = tuple(
            comparison[field]
            for field in ("unchanged_request", "standard_response", "semantic_parity")
        )
        if (
            not _bounded_int(attempts, 0, 8)
            or any(state is not None and not isinstance(state, bool) for state in states)
        ):
            raise ModelError(f"{label}: invalid receiver comparison")
        if origin == "direct-semantic-reference":
            if attempts != 0 or states != (None, None, None):
                raise ModelError(f"{label}: direct evidence claims a receiver outcome")
        else:
            if not 1 <= attempts <= 8:
                raise ModelError(f"{label}: receiver attempts exceed the evidence bound")
            routed[method] = observation

    derived_passed: list[str] = []
    derived_failed: list[str] = []
    backend_required = False
    for method in required:
        observation = routed.get(method)
        if observation is None:
            continue
        comparison = observation["receiver_comparison"]
        states = tuple(
            comparison[field]
            for field in ("unchanged_request", "standard_response", "semantic_parity")
        )
        if all(state is True for state in states):
            derived_passed.append(method)
        elif any(state is False for state in states):
            derived_failed.append(method)
        if comparison["unchanged_request"] is False or comparison["standard_response"] is False:
            backend_required = True
    derived_missing = [
        method
        for method in required
        if method not in derived_passed and method not in derived_failed
    ]
    if (passed, missing, failed) != (derived_passed, derived_missing, derived_failed):
        raise ModelError("native parity evidence coverage differs from its observations")
    expected_conclusion = (
        "backend-required"
        if backend_required
        else "passed"
        if not missing and not failed
        else "incomplete"
    )
    if value.get("conclusion") != expected_conclusion:
        raise ModelError("native parity evidence conclusion differs from its observations")
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=True)
    if "/home/" in serialized:
        raise ModelError("native parity evidence contains a private path")
    forbidden_keys = {"serial", "serial_value", "raw_report", "request_bytes", "response_bytes"}

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if forbidden_keys & set(item):
                raise ModelError("native parity evidence contains forbidden raw material")
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)


def _default_cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "hyperflux-next" / "native-transport-parity"


def write_parity_evidence(
    value: dict[str, Any],
    *,
    cache_root: Path | None = None,
) -> Path:
    validate_parity_evidence(value)
    stamp = re.sub(r"[^0-9TZ]", "", value["created_at"])
    profile = re.sub(r"[^a-z0-9.-]", "-", value["profile_id"])
    return write_new_private_json(
        cache_root if cache_root is not None else _default_cache_root(),
        f"{stamp}-{profile}.json",
        value,
    )
