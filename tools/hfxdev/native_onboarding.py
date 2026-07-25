# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from .integrations import load_integration_catalog
from .knowledge import load_knowledge_inputs
from .model import ModelError, sha256_file
from .native_parity import validate_parity_evidence
from .native_probe import load_native_probe_catalog
from .openrazer_native_route import (
    NATIVE_PROTOCOL_FAMILY,
    NATIVE_REPORT_BYTES,
)
from .private_artifact import write_new_private_json
from .profiles import load_profile_inputs


OBSERVATION_SCHEMA = "hyperflux-native-route-observation-v1"
DRAFT_SCHEMA = "hyperflux-native-device-onboarding-draft-v1"
PROMOTION_SCHEMA = "hyperflux-native-device-promotion-assessment-v1"
SOURCE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
PROFILE_ID_PATTERN = re.compile(r"child\.[a-z0-9.-]{1,120}\Z")
UPSTREAM_IDS = ("openrazer", "openrgb")
NATIVE_CAPABILITY_ID = "native.feature-report-exchange"
MAX_INPUT_BYTES = 1024 * 1024
DRAFT_OBSERVATION_KEYS = {
    "observed_at",
    "source",
    "receiver_generation",
    "vendor_id",
    "product_id",
    "device_kind",
    "route_kind",
    "route_state",
    "access",
    "protocol_family",
    "report_bytes",
}
ALLOWED_PROMOTION_DOMAINS = (
    "evidence/native-transport",
    "generated/profile-projections",
    "integrations/openrazer/source-bindings",
    "integrations/openrgb/source-bindings",
    "profiles/children",
)
IMMUTABLE_ENGINE_DOMAINS = (
    "crates/hfx-bridge",
    "crates/hfx-kernel-transport",
    "crates/hfx-sdk",
    "crates/hfx-uhid-relay",
    "driver/kernel",
    "integrations/openrazer/native-route/patches",
    "integrations/openrgb/native-route/patches",
    "protocol",
)
SAFETY = {
    "candidate_only": True,
    "write_authority_granted": False,
    "serials_persisted": False,
    "raw_reports_persisted": False,
    "private_paths_persisted": False,
    "network_upload": False,
    "hardware_queried_by_tool": False,
    "hardware_write_executed": False,
    "file_mode": "0600",
}


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{label}: expected canonical UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ModelError(f"{label}: invalid timestamp") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ModelError(f"{label}: expected canonical UTC")
    return value


def _revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or SOURCE_REVISION_PATTERN.fullmatch(value) is None:
        raise ModelError(f"{label}: expected a 40-character lowercase Git revision")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ModelError(f"{label}: expected an integer from {minimum} through {maximum}")
    return value


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_bounded_json(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ModelError(f"{label} is not a regular file")
        if metadata.st_size > MAX_INPUT_BYTES:
            raise ModelError(f"{label} exceeds {MAX_INPUT_BYTES} bytes")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            payload = source.read(MAX_INPUT_BYTES + 1)
    except ModelError:
        raise
    except OSError as error:
        raise ModelError(f"{label} is missing, symbolic, or unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > MAX_INPUT_BYTES:
        raise ModelError(f"{label} exceeds {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ModelError(f"{label} root must be an object")
    return value


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot identify onboarding source revision: {error}") from error
    return _revision(result.stdout.strip(), "onboarding source revision")


def validate_route_observation(value: dict[str, Any]) -> None:
    _exact(
        value,
        {
            "$schema",
            "schema",
            "observed_at",
            "source",
            "receiver_generation",
            "vendor_id",
            "product_id",
            "device_kind",
            "route_kind",
            "route_state",
            "access",
            "protocol_family",
            "report_bytes",
            "privacy",
        },
        "native route observation",
    )
    if value["$schema"] != "https://hyperflux.dev/schemas/native-route-observation-v1.json":
        raise ModelError("native route observation schema reference is not canonical")
    if value["schema"] != OBSERVATION_SCHEMA:
        raise ModelError("unsupported native route observation schema")
    _timestamp(value["observed_at"], "native route observation timestamp")
    if value["source"] != "bridge-native-route-v7":
        raise ModelError("native route observation does not come from protocol v7")
    _integer(value["receiver_generation"], "receiver generation", 1, 2**64 - 1)
    _integer(value["vendor_id"], "native route vendor id", 0, 65535)
    _integer(value["product_id"], "native route product id", 0, 65535)
    if value["device_kind"] not in {"mouse", "keyboard"}:
        raise ModelError("native route observation device kind is unsupported")
    if value["route_kind"] != "hyperflux-wireless":
        raise ModelError("native route observation is not receiver-backed")
    if value["route_state"] not in {
        "available",
        "temporarily-unavailable",
        "withdrawn",
    }:
        raise ModelError("native route observation state is invalid")
    if value["access"] not in {"candidate", "qualified", "backend-required"}:
        raise ModelError("native route observation access is invalid")
    if (
        not isinstance(value["protocol_family"], str)
        or not 1 <= len(value["protocol_family"].encode("utf-8")) <= 64
    ):
        raise ModelError("native route observation protocol family is invalid")
    _integer(value["report_bytes"], "native route report bytes", 1, 4096)
    if value["privacy"] != {
        "serials_included": False,
        "route_tokens_included": False,
        "host_paths_included": False,
        "raw_reports_included": False,
    }:
        raise ModelError("native route observation privacy declaration is invalid")


def load_route_observation(path: Path) -> dict[str, Any]:
    value = _load_bounded_json(path, "native route observation")
    validate_route_observation(value)
    return value


def _route_observation_from_draft(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError("native onboarding draft observation is invalid")
    _exact(value, DRAFT_OBSERVATION_KEYS, "native onboarding draft observation")
    observation = {
        "$schema": "https://hyperflux.dev/schemas/native-route-observation-v1.json",
        "schema": OBSERVATION_SCHEMA,
        **deepcopy(value),
        "privacy": {
            "serials_included": False,
            "route_tokens_included": False,
            "host_paths_included": False,
            "raw_reports_included": False,
        },
    }
    validate_route_observation(observation)
    return observation


def _matching_records(
    records: list[dict[str, Any]], observation: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        deepcopy(record)
        for record in records
        if record["usb_identity"]
        == {
            "vendor_id": observation["vendor_id"],
            "product_id": observation["product_id"],
        }
        and record["device_kind"] == observation["device_kind"]
    ]


def _source_binding(
    root: Path,
    upstream_id: str,
    catalog: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    matches = _matching_records(catalog["records"], observation)
    return {
        "upstream_id": upstream_id,
        "commit": catalog["source"]["commit"],
        "catalog_sha256": sha256_file(
            root / "knowledge" / "upstreams" / f"{upstream_id}.json"
        ),
        "match_state": (
            "absent" if not matches else "exact" if len(matches) == 1 else "ambiguous"
        ),
        "records": matches,
    }


def _topology(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None or not isinstance(record.get("lighting_topology"), dict):
        return None
    source = record["lighting_topology"]
    dimensions = source.get("matrix_dimensions")
    if isinstance(dimensions, list) and len(dimensions) == 2:
        rows, columns = dimensions
        slots = rows * columns
    else:
        rows = source.get("rows")
        columns = source.get("columns")
        slots = source.get("application_slot_count")
    return {
        "rows": rows if isinstance(rows, int) else None,
        "columns": columns if isinstance(columns, int) else None,
        "application_slot_count": slots if isinstance(slots, int) else None,
        "layout_key": source.get("layout_key"),
        "zones": deepcopy(source.get("zones", [])),
    }


def _topology_assessment(bindings: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    exact = {
        upstream_id: binding["records"][0]
        for upstream_id, binding in bindings.items()
        if binding["match_state"] == "exact"
    }
    if len(exact) < 2:
        return ("partial" if exact else "unavailable", [])
    values = {upstream_id: _topology(record) for upstream_id, record in exact.items()}
    comparable = {
        upstream_id: (
            value["rows"], value["columns"], value["application_slot_count"]
        )
        for upstream_id, value in values.items()
        if value is not None
    }
    if len(comparable) < 2:
        return "partial", []
    distinct = set(comparable.values())
    if len(distinct) == 1:
        return "agreement", []
    return (
        "conflict",
        [
            "topology:"
            + upstream_id
            + ":"
            + "x".join("unknown" if item is None else str(item) for item in value)
            for upstream_id, value in sorted(comparable.items())
        ],
    )


def _existing_profile(root: Path, observation: dict[str, Any]) -> str | None:
    matches = [
        profile["profile_id"]
        for profile in load_profile_inputs(root).profiles
        if profile["kind"] == "child"
        and profile["device_kind"] == observation["device_kind"]
        and profile["identity"].get("vendor_id") == observation["vendor_id"]
        and profile["identity"].get("product_id") == observation["product_id"]
    ]
    if len(matches) > 1:
        raise ModelError("multiple child profiles claim the observed native identity")
    return matches[0] if matches else None


def _slug(model_name: str) -> str:
    value = re.sub(r"\(wireless\)", "", model_name, flags=re.IGNORECASE)
    value = re.sub(r"^razer\s+", "", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not value:
        raise ModelError("cannot derive a candidate profile identifier")
    return value[:96].rstrip("-")


def _profile_proposal(
    root: Path,
    observation: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    existing_profile_id: str | None,
    topology_state: str,
) -> dict[str, Any]:
    exact_records = [
        binding["records"][0]
        for binding in bindings.values()
        if binding["match_state"] == "exact"
    ]
    preferred = next(
        (record for record in exact_records if record["record_id"].startswith("openrgb:")),
        exact_records[0] if exact_records else None,
    )
    model_name = (
        preferred["model_name"]
        if preferred is not None
        else f"Razer device {observation['product_id']:04X}"
    )
    profile_id = existing_profile_id or (
        f"child.razer.{_slug(model_name)}.{observation['product_id']:04x}"
    )
    if PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
        raise ModelError("generated candidate profile identifier is invalid")
    openrgb = bindings["openrgb"]
    presentation = None
    if openrgb["match_state"] == "exact":
        record = openrgb["records"][0]
        upstreams = {
            value["id"]: value for value in load_integration_catalog(root)["upstreams"]
        }
        authority = upstreams["openrgb"]
        topology = record.get("lighting_topology") or {}
        presentation = {
            "upstream_id": "openrgb",
            "owner": authority["name"],
            "project_version": authority["version"],
            "source_commit": authority["commit"],
            "model_key": record["source_device_key"],
            "layout_key": topology.get("layout_key"),
            "transport_variant": "wireless",
        }
    return {
        "profile_id": profile_id,
        "revision": 1 if existing_profile_id is None else None,
        "kind": "child",
        "device_kind": observation["device_kind"],
        "identity": {
            "manufacturer": "Razer",
            "model_name": model_name,
            "authority": "paired-product-id",
            "vendor_id": observation["vendor_id"],
            "product_id": observation["product_id"],
        },
        "compatibility": {
            "receiver_protocols": ["razer-hyperflux-v2"],
            "routes": ["hyperflux-wireless"],
            "required_sibling_kinds": [],
        },
        "presentation": presentation,
        "upstream_record_ids": sorted(
            record["record_id"] for record in exact_records
        ),
        "native_control": {
            "protocol_family": observation["protocol_family"],
            "report_bytes": observation["report_bytes"],
            "capability_id": NATIVE_CAPABILITY_ID,
            "support_level": "candidate",
            "write_authority": False,
        },
        "topology": {
            "authority": "upstream",
            "state": topology_state,
            "hyperflux_carrier_map": None,
        },
        "required_evidence": [
            "exact-upstream-identity",
            "native-read-only-parity",
            "native-reversible-write-parity",
            "direct-usb-precedence",
            "lifecycle-and-sibling-isolation",
        ],
        "restrictions": [
            "This draft grants no native write authority.",
            "Another PID, route kind, or report format inherits nothing.",
            "Upstream topology remains authoritative; HyperFlux generates no LED carrier map.",
        ],
    }


def build_onboarding_draft(
    root: Path,
    observation: dict[str, Any],
    *,
    created_at: str,
    source_revision: str,
) -> dict[str, Any]:
    validate_route_observation(observation)
    _timestamp(created_at, "onboarding draft timestamp")
    _revision(source_revision, "onboarding draft source revision")
    inputs = load_knowledge_inputs(root)
    bindings = {
        upstream_id: _source_binding(
            root,
            upstream_id,
            inputs.source_catalogs[upstream_id],
            observation,
        )
        for upstream_id in UPSTREAM_IDS
    }
    same_protocol = (
        observation["protocol_family"] == NATIVE_PROTOCOL_FAMILY
        and observation["report_bytes"] == NATIVE_REPORT_BYTES
    )
    topology_state, topology_reasons = _topology_assessment(bindings)
    match_states = {binding["match_state"] for binding in bindings.values()}
    reasons: list[str] = list(topology_reasons)
    if not same_protocol or observation["access"] == "backend-required":
        state = "backend-required"
        reasons.append(
            "different-native-report-format"
            if not same_protocol
            else "bridge-classified-backend-required"
        )
    elif "ambiguous" in match_states:
        state = "ambiguous"
        reasons.append("multiple-exact-upstream-records")
    elif all(binding["match_state"] == "absent" for binding in bindings.values()):
        state = "unknown"
        reasons.append("no-exact-upstream-record")
    elif any(binding["match_state"] == "absent" for binding in bindings.values()):
        state = "partial"
        reasons.append("one-upstream-catalog-has-no-exact-record")
    elif any(
        binding["records"][0]["source_route"] != "vendor-wireless-receiver"
        for binding in bindings.values()
    ):
        state = "conflicted"
        reasons.append("upstream-route-is-not-wireless-receiver")
    elif topology_state == "conflict":
        state = "conflicted"
        reasons.append("upstream-topology-disagreement")
    else:
        state = "candidate"
    existing_profile_id = _existing_profile(root, observation)
    profile = _profile_proposal(
        root, observation, bindings, existing_profile_id, topology_state
    )
    draft = {
        "$schema": "https://hyperflux.dev/schemas/native-device-onboarding-draft-v1.json",
        "schema": DRAFT_SCHEMA,
        "created_at": created_at,
        "source_revision": source_revision,
        "observation": {
            key: deepcopy(observation[key])
            for key in (
                "observed_at",
                "source",
                "receiver_generation",
                "vendor_id",
                "product_id",
                "device_kind",
                "route_kind",
                "route_state",
                "access",
                "protocol_family",
                "report_bytes",
            )
        },
        "source_bindings": bindings,
        "assessment": {
            "state": state,
            "reason_codes": sorted(set(reasons)),
            "same_protocol": same_protocol,
            "identity_state": (
                "agreement"
                if all(binding["match_state"] == "exact" for binding in bindings.values())
                else "partial"
                if any(binding["match_state"] == "exact" for binding in bindings.values())
                else "unresolved"
            ),
            "topology_state": topology_state,
            "existing_profile_id": existing_profile_id,
            "writable": False,
        },
        "candidate_profile": profile,
        "promotion_contract": {
            "decision": (
                "profile-only-candidate"
                if state == "candidate"
                else "backend-required"
                if state == "backend-required"
                else "blocked"
            ),
            "engine_changes_allowed": False,
            "allowed_domains": list(ALLOWED_PROMOTION_DOMAINS),
            "immutable_engine_domains": list(IMMUTABLE_ENGINE_DOMAINS),
        },
        "safety": deepcopy(SAFETY),
    }
    validate_onboarding_draft(draft)
    return draft


def validate_onboarding_draft(value: dict[str, Any]) -> None:
    _exact(
        value,
        {
            "$schema",
            "schema",
            "created_at",
            "source_revision",
            "observation",
            "source_bindings",
            "assessment",
            "candidate_profile",
            "promotion_contract",
            "safety",
        },
        "native onboarding draft",
    )
    if value["$schema"] != "https://hyperflux.dev/schemas/native-device-onboarding-draft-v1.json":
        raise ModelError("native onboarding draft schema reference is not canonical")
    if value["schema"] != DRAFT_SCHEMA:
        raise ModelError("unsupported native onboarding draft schema")
    _timestamp(value["created_at"], "native onboarding draft timestamp")
    _revision(value["source_revision"], "native onboarding draft source revision")
    _route_observation_from_draft(value["observation"])
    if not isinstance(value["source_bindings"], dict) or set(value["source_bindings"]) != set(UPSTREAM_IDS):
        raise ModelError("native onboarding draft source bindings are incomplete")
    assessment = value["assessment"]
    if not isinstance(assessment, dict) or assessment.get("state") not in {
        "candidate",
        "partial",
        "conflicted",
        "ambiguous",
        "unknown",
        "backend-required",
    }:
        raise ModelError("native onboarding draft assessment is invalid")
    if assessment.get("writable") is not False:
        raise ModelError("native onboarding draft cannot grant write authority")
    profile = value["candidate_profile"]
    if not isinstance(profile, dict) or PROFILE_ID_PATTERN.fullmatch(
        str(profile.get("profile_id", ""))
    ) is None:
        raise ModelError("native onboarding draft profile identifier is invalid")
    native = profile.get("native_control")
    if not isinstance(native, dict) or native.get("write_authority") is not False:
        raise ModelError("native onboarding profile proposal cannot be writable")
    contract = value["promotion_contract"]
    if (
        not isinstance(contract, dict)
        or contract.get("engine_changes_allowed") is not False
        or contract.get("allowed_domains") != list(ALLOWED_PROMOTION_DOMAINS)
        or contract.get("immutable_engine_domains") != list(IMMUTABLE_ENGINE_DOMAINS)
    ):
        raise ModelError("native onboarding promotion boundary is invalid")
    if value["safety"] != SAFETY:
        raise ModelError("native onboarding safety declaration is invalid")
    forbidden = {
        "serial",
        "serial_value",
        "route_token",
        "raw_report",
        "request_bytes",
        "response_bytes",
        "host_path",
    }

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if forbidden & set(item):
                raise ModelError("native onboarding draft contains forbidden private material")
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str) and item.startswith("/home/"):
            raise ModelError("native onboarding draft contains a private path")

    walk(value)


def load_onboarding_draft(path: Path) -> dict[str, Any]:
    value = _load_bounded_json(path, "native onboarding draft")
    validate_onboarding_draft(value)
    return value


def _cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    return base / "hyperflux-next" / "native-onboarding"


def write_onboarding_draft(
    value: dict[str, Any], *, cache_root: Path | None = None
) -> Path:
    validate_onboarding_draft(value)
    stamp = re.sub(r"[^0-9TZ]", "", value["created_at"])
    profile = value["candidate_profile"]["profile_id"]
    return write_new_private_json(
        cache_root or _cache_root(), f"{stamp}-{profile}.json", value
    )


def build_promotion_assessment(
    root: Path,
    draft: dict[str, Any],
    parity_evidence: dict[str, Any],
    *,
    created_at: str,
    source_revision: str,
) -> dict[str, Any]:
    validate_onboarding_draft(draft)
    canonical_draft = build_onboarding_draft(
        root,
        _route_observation_from_draft(draft["observation"]),
        created_at=draft["created_at"],
        source_revision=draft["source_revision"],
    )
    if canonical_draft != draft:
        raise ModelError("native onboarding draft differs from canonical sources")
    validate_parity_evidence(parity_evidence)
    _timestamp(created_at, "native promotion timestamp")
    _revision(source_revision, "native promotion source revision")
    reasons: list[str] = []
    draft_state = draft["assessment"]["state"]
    profile_id = draft["candidate_profile"]["profile_id"]
    if draft_state != "candidate":
        reasons.append(f"onboarding-{draft_state}")
    if source_revision != draft["source_revision"]:
        reasons.append("assessment-source-revision-mismatch")
    if parity_evidence["profile_id"] != profile_id:
        reasons.append("parity-profile-mismatch")
    if parity_evidence["source_revision"] != draft["source_revision"]:
        reasons.append("source-revision-mismatch")
    if parity_evidence["upstream_commit"] != draft["source_bindings"]["openrazer"]["commit"]:
        reasons.append("openrazer-commit-mismatch")
    probe_catalog = load_native_probe_catalog(root)
    probe_digest = sha256_file(
        root / "integrations" / "openrazer" / "native-probe-catalog.json"
    )
    if parity_evidence["catalog_sha256"] != probe_digest:
        reasons.append("native-probe-catalog-mismatch")
    if parity_evidence["conclusion"] != "passed":
        reasons.append("native-parity-not-passed")
    probe_profiles = [
        device for device in probe_catalog["devices"] if device["profile_id"] == profile_id
    ]
    if len(probe_profiles) != 1:
        reasons.append("native-probe-profile-missing")
    else:
        expected_methods = [probe["method"] for probe in probe_profiles[0]["probes"]]
        coverage = parity_evidence["coverage"]
        if (
            coverage["required"] != expected_methods
            or coverage["passed"] != expected_methods
            or coverage["missing"]
            or coverage["failed"]
        ):
            reasons.append("native-parity-coverage-incomplete")
    if draft_state == "backend-required" or parity_evidence["conclusion"] == "backend-required":
        state = "backend-required"
    elif draft_state != "candidate" or reasons:
        state = "blocked"
    else:
        state = "ready-for-reviewed-profile-promotion"
    assessment = {
        "$schema": "https://hyperflux.dev/schemas/native-device-promotion-assessment-v1.json",
        "schema": PROMOTION_SCHEMA,
        "created_at": created_at,
        "source_revision": source_revision,
        "draft_sha256": _canonical_digest(draft),
        "profile_id": profile_id,
        "state": state,
        "reason_codes": sorted(set(reasons)),
        "evidence_bindings": {
            "onboarding_source_revision": draft["source_revision"],
            "parity_source_revision": parity_evidence["source_revision"],
            "openrazer_commit": parity_evidence["upstream_commit"],
            "native_probe_catalog_sha256": parity_evidence["catalog_sha256"],
            "parity_conclusion": parity_evidence["conclusion"],
        },
        "change_contract": {
            "engine_changes_allowed": False,
            "allowed_domains": list(ALLOWED_PROMOTION_DOMAINS),
            "immutable_engine_domains": list(IMMUTABLE_ENGINE_DOMAINS),
            "automatic_write_authority_granted": False,
        },
        "safety": deepcopy(SAFETY),
    }
    validate_promotion_assessment(assessment)
    return assessment


def validate_promotion_assessment(value: dict[str, Any]) -> None:
    _exact(
        value,
        {
            "$schema",
            "schema",
            "created_at",
            "source_revision",
            "draft_sha256",
            "profile_id",
            "state",
            "reason_codes",
            "evidence_bindings",
            "change_contract",
            "safety",
        },
        "native promotion assessment",
    )
    if value["$schema"] != "https://hyperflux.dev/schemas/native-device-promotion-assessment-v1.json":
        raise ModelError("native promotion assessment schema reference is not canonical")
    if value["schema"] != PROMOTION_SCHEMA:
        raise ModelError("unsupported native promotion assessment schema")
    _timestamp(value["created_at"], "native promotion timestamp")
    _revision(value["source_revision"], "native promotion source revision")
    if PROFILE_ID_PATTERN.fullmatch(str(value["profile_id"])) is None:
        raise ModelError("native promotion profile identifier is invalid")
    if value["state"] not in {
        "ready-for-reviewed-profile-promotion",
        "blocked",
        "backend-required",
    }:
        raise ModelError("native promotion state is invalid")
    contract = value["change_contract"]
    if (
        not isinstance(contract, dict)
        or contract.get("engine_changes_allowed") is not False
        or contract.get("automatic_write_authority_granted") is not False
        or contract.get("allowed_domains") != list(ALLOWED_PROMOTION_DOMAINS)
        or contract.get("immutable_engine_domains") != list(IMMUTABLE_ENGINE_DOMAINS)
    ):
        raise ModelError("native promotion change boundary is invalid")
    if value["safety"] != SAFETY:
        raise ModelError("native promotion safety declaration is invalid")


def write_promotion_assessment(
    value: dict[str, Any], *, cache_root: Path | None = None
) -> Path:
    validate_promotion_assessment(value)
    stamp = re.sub(r"[^0-9TZ]", "", value["created_at"])
    return write_new_private_json(
        cache_root or _cache_root(),
        f"{stamp}-{value['profile_id']}-promotion.json",
        value,
    )


def build_draft_from_file(
    root: Path,
    observation_path: Path,
    *,
    created_at: str,
    source_revision: str | None = None,
) -> dict[str, Any]:
    return build_onboarding_draft(
        root,
        load_route_observation(observation_path),
        created_at=created_at,
        source_revision=source_revision or _git_revision(root),
    )


def build_promotion_from_files(
    root: Path,
    draft_path: Path,
    parity_path: Path,
    *,
    created_at: str,
    source_revision: str | None = None,
) -> dict[str, Any]:
    parity = _load_bounded_json(parity_path, "native parity evidence")
    return build_promotion_assessment(
        root,
        load_onboarding_draft(draft_path),
        parity,
        created_at=created_at,
        source_revision=source_revision or _git_revision(root),
    )
