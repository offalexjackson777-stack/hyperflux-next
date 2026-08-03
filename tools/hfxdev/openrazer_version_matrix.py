# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer_capability import load_userspace_capability_catalog


MATRIX_SCHEMA = "hyperflux-openrazer-userspace-version-matrix-v1"
EVIDENCE_SCHEMA = "hyperflux-openrazer-userspace-version-compatibility-v1"
REQUIRED_VERSIONS = ("3.6.0", "3.10.0", "3.12.2", "3.12.4", "master")
TOP_KEYS = {"$schema", "schema", "upstream_id", "cases"}
CASE_KEYS = {
    "id",
    "display_version",
    "channel",
    "source_ref",
    "commit",
    "devices",
}
DEVICE_KEYS = {"profile_id", "fake_driver", "definition"}
EVIDENCE_TOP_KEYS = {
    "$schema",
    "schema",
    "matrix_sha256",
    "capability_catalog_sha256",
    "network_access_executed",
    "hardware_queried",
    "hardware_write_executed",
    "cases",
}
EVIDENCE_CASE_KEYS = {
    "id",
    "display_version",
    "channel",
    "commit",
    "matched_source_pair",
    "client_declared_version",
    "daemon_declared_version",
    "daemon_owner_acquired",
    "reported_version",
    "version_probe_state",
    "overall_state",
    "devices",
}
EVIDENCE_DEVICE_KEYS = {
    "profile_id",
    "device_kind",
    "definition",
    "client_enumerated",
    "daemon_enumerated",
    "state",
    "required_problems",
    "features",
    "semantic_probes",
}
IDENTIFIER = re.compile(r"openrazer-[a-z0-9.-]{1,63}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
FAKE_DRIVER = re.compile(r"razer[a-z0-9]{1,127}\Z")


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extra)}")


def load_openrazer_version_matrix(root: Path) -> dict[str, Any]:
    matrix = load_json(
        root / "integrations" / "openrazer" / "userspace-version-matrix.json"
    )
    _exact(matrix, TOP_KEYS, "OpenRazer userspace version matrix")
    if (
        matrix["$schema"]
        != "../../schemas/openrazer-userspace-version-matrix.schema.json"
        or matrix["schema"] != MATRIX_SCHEMA
        or matrix["upstream_id"] != "openrazer"
    ):
        raise ModelError("unsupported OpenRazer userspace version matrix schema")

    cases = matrix["cases"]
    if not isinstance(cases, list) or len(cases) != len(REQUIRED_VERSIONS):
        raise ModelError("OpenRazer userspace version matrix must cover five exact lanes")
    capability_catalog = load_userspace_capability_catalog(root)
    expected_profiles = sorted(
        device["profile_id"] for device in capability_catalog["devices"]
    )
    case_ids: list[str] = []
    commits: list[str] = []
    refs: list[str] = []
    observed_versions: list[str] = []
    for index, case in enumerate(cases):
        label = f"OpenRazer userspace version case {index}"
        if not isinstance(case, dict):
            raise ModelError(f"{label}: expected an object")
        _exact(case, CASE_KEYS, label)
        identifier = case["id"]
        version = case["display_version"]
        commit = case["commit"]
        source_ref = case["source_ref"]
        expected_channel = "upstream-head" if version == "master" else "release"
        if (
            not isinstance(identifier, str)
            or IDENTIFIER.fullmatch(identifier) is None
            or version != REQUIRED_VERSIONS[index]
            or case["channel"] != expected_channel
            or not isinstance(source_ref, str)
            or not source_ref.startswith("refs/")
            or not isinstance(commit, str)
            or COMMIT.fullmatch(commit) is None
        ):
            raise ModelError(f"{label}: invalid source binding")
        devices = case["devices"]
        if not isinstance(devices, list) or len(devices) != len(expected_profiles):
            raise ModelError(f"{label}: every selected profile must be classified")
        profile_ids: list[str] = []
        for device_index, device in enumerate(devices):
            device_label = f"{label} device {device_index}"
            if not isinstance(device, dict):
                raise ModelError(f"{device_label}: expected an object")
            _exact(device, DEVICE_KEYS, device_label)
            if (
                device["profile_id"] not in expected_profiles
                or not isinstance(device["fake_driver"], str)
                or FAKE_DRIVER.fullmatch(device["fake_driver"]) is None
                or device["definition"] not in {"present", "absent"}
            ):
                raise ModelError(f"{device_label}: invalid device boundary")
            profile_ids.append(device["profile_id"])
        require_unique(profile_ids, f"{label} profile")
        if profile_ids != expected_profiles:
            raise ModelError(f"{label}: device profiles must be sorted and complete")
        case_ids.append(identifier)
        commits.append(commit)
        refs.append(source_ref)
        observed_versions.append(version)
    require_unique(case_ids, "OpenRazer userspace version case id")
    require_unique(commits, "OpenRazer userspace version commit")
    require_unique(refs, "OpenRazer userspace version source ref")
    if tuple(observed_versions) != REQUIRED_VERSIONS:
        raise ModelError("OpenRazer userspace version lanes are not canonical")
    return matrix


def load_openrazer_version_compatibility(root: Path) -> dict[str, Any]:
    path = (
        root
        / "integrations"
        / "openrazer"
        / "userspace-version-compatibility.json"
    )
    evidence = load_json(path)
    _exact(evidence, EVIDENCE_TOP_KEYS, "OpenRazer userspace compatibility evidence")
    if (
        evidence["$schema"]
        != "../../schemas/openrazer-userspace-version-compatibility.schema.json"
        or evidence["schema"] != EVIDENCE_SCHEMA
        or evidence["matrix_sha256"]
        != sha256_file(
            root / "integrations" / "openrazer" / "userspace-version-matrix.json"
        )
        or evidence["capability_catalog_sha256"]
        != sha256_file(
            root / "integrations" / "openrazer" / "userspace-capability-catalog.json"
        )
        or evidence["network_access_executed"] is not False
        or evidence["hardware_queried"] is not False
        or evidence["hardware_write_executed"] is not False
    ):
        raise ModelError("OpenRazer userspace compatibility evidence binding drifted")

    matrix = load_openrazer_version_matrix(root)
    capability_catalog = load_userspace_capability_catalog(root)
    capabilities = {
        device["profile_id"]: device for device in capability_catalog["devices"]
    }
    cases = evidence["cases"]
    if not isinstance(cases, list) or len(cases) != len(matrix["cases"]):
        raise ModelError("OpenRazer userspace compatibility evidence is incomplete")
    for index, (case, selected) in enumerate(zip(cases, matrix["cases"], strict=True)):
        label = f"OpenRazer userspace compatibility case {index}"
        if not isinstance(case, dict):
            raise ModelError(f"{label}: expected an object")
        _exact(case, EVIDENCE_CASE_KEYS, label)
        if any(
            case[key] != selected[key]
            for key in ("id", "display_version", "channel", "commit")
        ):
            raise ModelError(f"{label}: source identity drifted")
        if (
            case["matched_source_pair"] is not True
            or case["daemon_owner_acquired"] is not True
            or not isinstance(case["client_declared_version"], str)
            or not case["client_declared_version"]
            or case["client_declared_version"] != case["daemon_declared_version"]
            or case["version_probe_state"] != "reported"
            or case["overall_state"] not in {"compatible", "limited"}
        ):
            raise ModelError(f"{label}: matched userspace pair did not qualify")
        devices = case["devices"]
        if not isinstance(devices, list) or len(devices) != len(selected["devices"]):
            raise ModelError(f"{label}: per-device evidence is incomplete")
        for device_index, (device, boundary) in enumerate(
            zip(devices, selected["devices"], strict=True)
        ):
            device_label = f"{label} device {device_index}"
            if not isinstance(device, dict):
                raise ModelError(f"{device_label}: expected an object")
            _exact(device, EVIDENCE_DEVICE_KEYS, device_label)
            profile_id = boundary["profile_id"]
            capability = capabilities[profile_id]
            if (
                device["profile_id"] != profile_id
                or device["device_kind"] != capability["identity"]["device_kind"]
                or device["definition"] != boundary["definition"]
            ):
                raise ModelError(f"{device_label}: profile identity drifted")
            if boundary["definition"] == "absent":
                if (
                    device["client_enumerated"] is not False
                    or device["daemon_enumerated"] is not False
                    or device["state"] != "not-recognized-by-release"
                    or any(
                        device[key]
                        for key in ("required_problems", "features", "semantic_probes")
                    )
                ):
                    raise ModelError(f"{device_label}: absent device boundary drifted")
                continue
            if (
                device["client_enumerated"] is not True
                or device["daemon_enumerated"] is not True
                or device["state"] not in {"compatible", "limited"}
                or device["required_problems"]
            ):
                raise ModelError(f"{device_label}: recognized device did not qualify")
            expected_features = {
                feature["id"]: feature["requirement"]
                for feature in capability["features"]
            }
            feature_results = device["features"]
            if not isinstance(feature_results, list) or {
                feature.get("id") for feature in feature_results if isinstance(feature, dict)
            } != set(expected_features):
                raise ModelError(f"{device_label}: feature evidence is incomplete")
            for feature in feature_results:
                if (
                    set(feature) != {"id", "requirement", "state"}
                    or feature["requirement"] != expected_features[feature["id"]]
                    or feature["state"] not in {"available", "unavailable"}
                    or (
                        feature["requirement"] == "required"
                        and feature["state"] != "available"
                    )
                ):
                    raise ModelError(f"{device_label}: feature evidence is invalid")
            expected_probes = {probe["id"] for probe in capability["semantic_probes"]}
            probe_results = device["semantic_probes"]
            if (
                not isinstance(probe_results, list)
                or {probe.get("id") for probe in probe_results if isinstance(probe, dict)}
                != expected_probes
                or any(
                    set(probe) != {"id", "state"} or probe["state"] != "passed"
                    for probe in probe_results
                )
            ):
                raise ModelError(f"{device_label}: semantic probes did not qualify")
    return evidence


__all__ = [
    "REQUIRED_VERSIONS",
    "load_openrazer_version_compatibility",
    "load_openrazer_version_matrix",
]
