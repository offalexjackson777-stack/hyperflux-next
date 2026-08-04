# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any

from .model import ModelError, sha256_file
from .native_probe import load_native_probe_catalog
from .native_parity_execution import (
    _raw_receiver_matches,
    _semantic_parity_from_evidence,
    _semantic_summary_parity,
    _standard_terminal_response,
    _validate_raw_qualification,
)
from .native_parity_parsing import (
    COMPARISONS,
    EVIDENCE_SCHEMA,
    METHOD_PATTERN,
    PROFILE_PATTERN,
    SHA256_PATTERN,
    SOURCE_REVISION_PATTERN,
    SUCCESS_STATUS,
    DirectSemanticReference,
    MatchedExchange,
    UsbmonExchange,
    _bounded_int,
    _declared_receiver_selector,
    _match_exchanges,
    _profile,
    _read_bounded_text,
    _route_translation,
    _serial_comparison_value,
    parse_usbmon_capture,
)


from .native_parity_evidence_validation import validate_parity_evidence


def build_direct_semantic_reference(
    root: Path,
    *,
    profile_id: str,
    exchanges: tuple[UsbmonExchange, ...],
    created_at: str | None = None,
    source_revision: str | None = None,
) -> DirectSemanticReference:
    """Sanitizes complete direct USB exchanges without persisting identity material."""

    if len(exchanges) > 256:
        raise ModelError("direct semantic reference exceeds its exchange bound")
    catalog = load_native_probe_catalog(root)
    profile = _profile(catalog, profile_id)
    matches = _match_exchanges(profile, exchanges)
    required = [probe["method"] for probe in profile["probes"]]
    observations: list[dict[str, Any]] = []
    serial_values: dict[str, bytes] = {}
    for method in required:
        items = matches.get(method, [])
        if not items:
            continue
        item = items[-1]
        observations.append(_direct_observation(item))
        if item.serial_value is not None:
            serial_values[method] = item.serial_value
    timestamp = (
        created_at
        or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    revision = source_revision or _git_revision(root)
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
            "passed": [],
            "missing": required,
            "failed": [],
        },
        "conclusion": "incomplete",
    }
    validate_parity_evidence(evidence)
    return DirectSemanticReference(evidence=evidence, serial_values=serial_values)


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
    direct_serial_values: dict[str, bytes] | None = None,
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
    required = [probe["method"] for probe in profile["probes"]]
    probes = {probe["method"]: probe for probe in profile["probes"]}
    direct_by_method = {
        observation["method"]: observation
        for observation in direct_evidence["observations"]
        if observation["origin"] == "direct-semantic-reference"
    }
    private_serials = direct_serial_values or {}
    if any(
        method not in direct_by_method
        or probes[method]["comparison"] != "redacted-equality"
        or not isinstance(value, bytes)
        or _serial_comparison_value(value) != value
        for method, value in private_serials.items()
    ):
        raise ModelError("direct serial comparison material is invalid")
    maximum_attempts = catalog["busy_retry"]["maximum_attempts"]
    receiver_selector = _declared_receiver_selector(
        root, profile["wireless_identity"]["device_kind"]
    )
    observations: list[dict[str, Any]] = []
    receiver_observations: dict[str, dict[str, Any]] = {}
    for method in required:
        direct = direct_by_method.get(method)
        if direct is not None:
            observations.append(direct)
        routed = receiver_matches.get(method, [])
        if not routed:
            continue
        final = routed[-1]
        application_selector = final.exchange.request[1]
        response_selector = (
            final.exchange.response[1] if final.response_complete else None
        )
        receiver = _receiver_observation(
            routed,
            None,
            maximum_attempts,
            route_translation=_route_translation(
                application_selector=application_selector,
                receiver_selector=receiver_selector,
                response_selector=response_selector,
                response_restored_for_application=(
                    response_selector == application_selector
                    if response_selector is not None
                    else None
                ),
                authority="receiver-profile",
            ),
        )
        receiver["receiver_comparison"]["semantic_parity"] = (
            _semantic_parity_from_evidence(
                probes[method],
                direct,
                routed[-1],
                private_serials.get(method),
            )
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
        "route_translation": None,
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
    *,
    route_translation: dict[str, Any] | None = None,
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
        "route_translation": route_translation,
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
            final = routed[-1]
            translation = None
            if direct is not None:
                translation = _route_translation(
                    application_selector=direct.exchange.request[1],
                    receiver_selector=final.exchange.request[1],
                    response_selector=(
                        final.exchange.response[1] if final.response_complete else None
                    ),
                    response_restored_for_application=None,
                    authority="captured-usbmon",
                )
            receiver = _receiver_observation(
                routed,
                direct,
                maximum_attempts,
                route_translation=translation,
            )
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
