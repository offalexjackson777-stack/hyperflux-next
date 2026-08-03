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
        observation_keys = {
            "method",
            "origin",
            "comparison",
            "request",
            "response",
            "receiver_comparison",
        }
        if "route_translation" in observation:
            observation_keys.add("route_translation")
        exact_keys(observation, observation_keys, label)
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
            if observation.get("route_translation") is not None:
                raise ModelError(f"{label}: direct evidence claims receiver translation")
        else:
            if not 1 <= attempts <= 8:
                raise ModelError(f"{label}: receiver attempts exceed the evidence bound")
            routed[method] = observation
        translation = observation.get("route_translation")
        if translation is not None:
            if not isinstance(translation, dict):
                raise ModelError(f"{label}: malformed route translation")
            exact_keys(
                translation,
                {
                    "field",
                    "application_selector",
                    "receiver_selector",
                    "applied",
                    "response_selector",
                    "response_restored_for_application",
                    "authority",
                },
                f"{label} route translation",
            )
            application_selector = translation["application_selector"]
            receiver_selector = translation["receiver_selector"]
            response_selector = translation["response_selector"]
            restored = translation["response_restored_for_application"]
            if (
                translation["field"] != "transaction-id"
                or not _bounded_int(application_selector, 0, 255)
                or not _bounded_int(receiver_selector, 0, 255)
                or translation["applied"]
                != (application_selector != receiver_selector)
                or (
                    response_selector is not None
                    and not _bounded_int(response_selector, 0, 255)
                )
                or (restored is not None and not isinstance(restored, bool))
                or translation["authority"]
                not in {"captured-usbmon", "receiver-profile"}
                or (restored is True and response_selector != application_selector)
            ):
                raise ModelError(f"{label}: invalid route translation")

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
