# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import re
import secrets
from typing import Any

from .model import ModelError
from .native_probe import REPORT_SIZE
from .native_parity_parsing import (
    BUSY_STATUS,
    METHOD_PATTERN,
    QUALIFICATION_CERTAINTIES,
    RAW_QUALIFICATION_SCHEMA,
    SUCCESS_STATUS,
    MatchedExchange,
    UsbmonExchange,
    _bounded_int,
    _request_matches_signature,
    _response_is_structural,
    _semantic_summary,
    canonical_request,
)


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
    direct_serial_value: bytes | None = None,
) -> bool | None:
    if direct is None or not receiver.response_complete:
        return None
    response = direct["response"]
    if probe["comparison"] == "redacted-equality":
        if not response["structural_match"] or not receiver.response_structural:
            return False
        direct_status = response["status"]
        receiver_status = receiver.exchange.response[0]
        if BUSY_STATUS in {direct_status, receiver_status}:
            return None
        if direct_status != receiver_status:
            return False
        if direct_status != SUCCESS_STATUS:
            return None
        if (
            response["summary"].get("available") is not True
            or receiver.summary.get("available") is not True
            or direct_serial_value is None
            or receiver.serial_value is None
        ):
            return None
        return secrets.compare_digest(direct_serial_value, receiver.serial_value)
    return _semantic_summary_parity(
        probe,
        direct_status=response["status"],
        direct_structural=response["structural_match"],
        direct_summary=response["summary"],
        receiver_status=receiver.exchange.response[0],
        receiver_structural=receiver.response_structural,
        receiver_summary=receiver.summary,
    )
