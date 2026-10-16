# SPDX-License-Identifier: GPL-2.0-only

"""Owner hardware reports: from `hyperfluxctl qualification --json` to a public record.

A device owner runs `hyperfluxctl qualification --json` and pastes the output in the
device support form, ticking what they checked by hand. A maintainer runs
`./hfx qualification ingest` on the issue body (or the raw report). The ingest
step checks the report against its schema and the rules in
assurance/owner-reports.json, refuses anything that looks private, and writes a
small record to evidence/owner-reports/ that holds only allowlisted values.
`./hfx generate` then marks the device as confirmed by owners.

Owner records are documentation evidence. They never change what the runtime
does with a device and never make a device "tested on real hardware": that
still needs a child profile and the maintainer's physical checks.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .model import ModelError, load_json
from .schema_subset import SchemaViolation, validate
from .source_validated import load_source_validated_catalog
from .source_validated_catalog import (
    PHYSICALLY_PROVEN,
    SOURCE_QUALIFIED_OPERATIONAL,
    public_support_tier,
)

POLICY_PATH = Path("assurance") / "owner-reports.json"
POLICY_SCHEMA_PATH = Path("schemas") / "owner-report-policy.schema.json"
RECORD_SCHEMA_PATH = Path("schemas") / "owner-report.schema.json"
RECORDS_DIRECTORY = Path("evidence") / "owner-reports"
RECORD_SCHEMA = "hyperflux-owner-report-v1"
RECORD_SCHEMA_REFERENCE = "../../schemas/owner-report.schema.json"
PROFILE_CATALOG_PATH = Path("generated") / "profiles" / "catalog.json"
ERROR_CATALOG_PATH = Path("errors") / "catalog.json"

# The issue form shows these exact words; the body parser matches them exactly.
REPORT_HEADING = "Output of `hyperfluxctl qualification --json`"
CONFIRMATIONS_HEADING = "If your device is listed as expected to work, what did you check?"
CONFIRMATION_LABELS = {
    "input": (
        "While it was connected through the receiver, it worked normally: "
        "the pointer moved and the buttons clicked, or the keys typed."
    ),
    "openrazer-lighting": (
        "I changed its lighting in Polychromatic or another OpenRazer app, "
        "and the device changed."
    ),
    "openrgb-lighting": "I changed its lighting in OpenRGB, and the device changed.",
}

_ORDINAL_RECEIVER = re.compile(r"^receiver-[1-9][0-9]{0,2}$")
_ORDINAL_DEVICE = re.compile(r"^device-[1-9][0-9]{0,2}$")
_VERSION = re.compile(r"^([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RECORD_NAME = re.compile(
    r"^(?P<route>razer\.1532\.[0-9a-f]{4}\.(?:mouse|keyboard))--issue-(?P<issue>[1-9][0-9]{0,8})\.json$"
)

# Private text a report must never carry. Each entry is a category name and a
# pattern; refusal messages name the category and the JSON path, never the text.
_PRIVATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a private file path",
        re.compile(
            r"(?:^|[^A-Za-z0-9_.-])/(?:home|root|run/user|media|mnt|var/home|Users|tmp|dev|sys|proc|etc)(?:/|\b)"
        ),
    ),
    ("a home folder path", re.compile(r"(?:^|[\s\"'=(])~[A-Za-z0-9_.-]*/")),
    ("a Windows file path", re.compile(r"\b[A-Za-z]:\\")),
    ("an email address or user@host name", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")),
    ("a MAC address", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("a UUID", re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")),
    ("a network address", re.compile(r"\b(?!127\.0\.0\.1\b)(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")),
    ("a USB bus path", re.compile(r"\b[0-9]{1,3}-[0-9]{1,3}(?:\.[0-9]{1,3})*:[0-9]{1,3}\.[0-9]{1,3}\b")),
    ("a local network host name", re.compile(r"\b[A-Za-z0-9-]+\.(?:local|lan|home|internal|localdomain)\b")),
    (
        "a serial number, host name, or user name label",
        re.compile(r"(?i)\b(?:serial|s/n|hostname|host name|username|user name|machine name|login)\b"),
    ),
    ("an environment variable", re.compile(r"\b(?:USER|LOGNAME|HOME|HOSTNAME|XDG_[A-Z_]+)=")),
)
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


class OwnerReportRefused(ModelError):
    """The report cannot become an owner record. The message never quotes report text."""


def _refuse(message: str) -> OwnerReportRefused:
    return OwnerReportRefused(f"report refused: {message}")


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def load_owner_report_policy(root: Path) -> dict[str, Any]:
    policy = load_json(root / POLICY_PATH)
    try:
        validate(load_json(root / POLICY_SCHEMA_PATH), policy)
    except SchemaViolation as error:
        raise ModelError(f"{POLICY_PATH}: {error}") from error
    if policy["$schema"] != "../schemas/owner-report-policy.schema.json":
        raise ModelError(f"{POLICY_PATH}: wrong schema reference")
    if _parse_version(policy["report"]["minimum_version"]) is None:
        raise ModelError(f"{POLICY_PATH}: minimum_version must be a release version")
    lighting = policy["confirmations"]["lighting"]
    if sorted(item["id"] for item in lighting) != sorted({item["id"] for item in lighting}):
        raise ModelError(f"{POLICY_PATH}: lighting confirmations repeat an id")
    for item in lighting:
        if item["id"] != f"{item['integration']}-lighting":
            raise ModelError(f"{POLICY_PATH}: {item['id']} must name its own app")
    return policy


# ---------------------------------------------------------------------------
# Reading the owner's submission


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _refuse("the report repeats a field name")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise _refuse("the report holds a number JSON does not allow")


def parse_report_text(text: str, maximum_bytes: int) -> dict[str, Any]:
    if len(text.encode("utf-8")) > maximum_bytes:
        raise _refuse(f"the report is larger than {maximum_bytes} bytes")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise _refuse(f"the report is not valid JSON (line {error.lineno}, column {error.colno})") from None
    if not isinstance(value, dict):
        raise _refuse("the report must be a JSON object")
    return value


def _sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            heading = line[4:].strip()
            if heading in sections:
                raise _refuse("the issue repeats a form heading")
            current = sections.setdefault(heading, [])
            continue
        if current is not None:
            current.append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def _fenced_block(text: str) -> str:
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line.strip().startswith("```")]
    if not starts:
        return text
    if len(starts) != 2 or lines[starts[0]].strip() not in {"```", "```json"} or lines[starts[1]].strip() != "```":
        raise _refuse("the report section must hold exactly one JSON code block")
    return "\n".join(lines[starts[0] + 1 : starts[1]])


def parse_issue_body(body: str) -> tuple[str, frozenset[str]]:
    """Return the report text and the confirmation ids ticked in a device support issue."""
    sections = _sections(body)
    if REPORT_HEADING not in sections:
        raise _refuse("the issue has no hardware report section")
    report = _fenced_block(sections[REPORT_HEADING]).strip()
    if not report or report == "_No response_":
        raise _refuse("the hardware report section is empty")
    confirmations: set[str] = set()
    labels = {label: identifier for identifier, label in CONFIRMATION_LABELS.items()}
    for line in sections.get(CONFIRMATIONS_HEADING, "").split("\n"):
        match = re.match(r"^- \[([ xX])\] (.+)$", line.strip())
        if match is None:
            continue
        identifier = labels.get(match.group(2).strip())
        if identifier is None:
            raise _refuse("the confirmation list holds an option the form does not offer")
        if match.group(1) in {"x", "X"}:
            confirmations.add(identifier)
    return report, frozenset(confirmations)


def read_submission(text: str) -> tuple[str, frozenset[str] | None]:
    """Accept either a raw JSON report or a device support issue body."""
    if text.lstrip().startswith("{"):
        return text, None
    return parse_issue_body(text)


# ---------------------------------------------------------------------------
# Privacy


def _walk_strings(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}]")


def private_text_findings(report: dict[str, Any]) -> list[str]:
    """Return 'path: category' lines for every string that looks private.

    The report has already passed the schema, so every key is a known field
    name and safe to print. String values are never printed.
    """
    findings: list[str] = []
    for path, text in _walk_strings(report, "$"):
        for category, pattern in _PRIVATE_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path}: looks like {category}")
                break
        else:
            # Percent escapes such as %3A in action links are separators, not code.
            for token in _TOKEN.findall(_PERCENT_ESCAPE.sub(" ", text)):
                if _SHA256.match(token):
                    continue
                mixed = (
                    len(token) >= 10
                    and re.search(r"[0-9]", token)
                    and re.search(r"[A-Za-z]", token)
                )
                if mixed or (token.isdigit() and len(token) >= 10):
                    findings.append(f"{path}: looks like a serial number or other unique code")
                    break
    return findings


def _check_ordinals(report: dict[str, Any]) -> None:
    for r_index, receiver in enumerate(report["receivers"]):
        if not _ORDINAL_RECEIVER.match(receiver["receiver_id"]):
            raise _refuse(f"$.receivers[{r_index}].receiver_id is not a numbered receiver-N name")
        for d_index, device in enumerate(receiver["devices"]):
            if not _ORDINAL_DEVICE.match(device["device_id"]):
                raise _refuse(
                    f"$.receivers[{r_index}].devices[{d_index}].device_id is not a numbered device-N name"
                )
    for p_index, plan in enumerate(report["plans"]):
        if not _ORDINAL_RECEIVER.match(plan["receiver_id"]) or not _ORDINAL_DEVICE.match(plan["device_id"]):
            raise _refuse(f"$.plans[{p_index}] does not use numbered receiver-N and device-N names")


# ---------------------------------------------------------------------------
# Acceptance rules


def _check_report_envelope(root: Path, policy: dict[str, Any], report: dict[str, Any]) -> None:
    rules = policy["report"]
    companion = report["companion"]
    if "collection" not in report:
        raise _refuse("the report was not made by hyperfluxctl qualification")
    collection = report["collection"]
    if collection["producer"] != rules["producer"]:
        raise _refuse("the report was not made by hyperfluxctl qualification")
    if "snapshot" not in collection["bridge_requests"]:
        raise _refuse("the report did not read the device list from the HyperFlux service")
    if report["actions"]:
        raise _refuse("a hardware report must not offer actions")
    if companion["state"] not in rules["companion_states"]:
        raise _refuse(f"HyperFlux was not ready (state {companion['state']})")
    if companion["simulation"]:
        raise _refuse("the report came from the simulator, not real hardware")
    if companion["hardware_write_executed"]:
        raise _refuse("the report says it wrote to hardware")
    if companion["endpoint"] != rules["companion_endpoint"]:
        raise _refuse("$.companion.endpoint is not the fixed local address")
    if companion["bridge_protocol"] is None:
        raise _refuse("$.companion.bridge_protocol is missing")
    minimum = _parse_version(rules["minimum_version"])
    assert minimum is not None
    for path, version in (
        ("$.companion.version", companion["version"]),
        ("$.system.driver_version", report["system"]["driver_version"]),
        ("$.system.bridge_version", report["system"]["bridge_version"]),
    ):
        parsed = _parse_version(version) if isinstance(version, str) else None
        if parsed is None:
            raise _refuse(f"{path} is not a release version")
        if parsed < minimum:
            raise _refuse(f"{path} is older than {rules['minimum_version']}")
    if report["system"]["profile_catalog_digest"] is None:
        raise _refuse("$.system.profile_catalog_digest is missing")
    known_codes = {entry["code"] for entry in load_json(root / ERROR_CATALOG_PATH)["errors"]}
    for index, finding in enumerate(collection["diagnostic_findings"]):
        path = f"$.collection.diagnostic_findings[{index}]"
        if finding["finding_id"] not in known_codes:
            raise _refuse(f"{path} is not a known HyperFlux problem code")
        if finding["severity"] in rules["refused_finding_severities"]:
            raise _refuse(
                f"{path} is a problem of severity {finding['severity']} "
                f"({finding['finding_id']}); fix it and run the report again"
            )


def _receiver_profiles(root: Path, policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    catalog = load_json(root / PROFILE_CATALOG_PATH)
    return {
        profile["profile_id"]: profile
        for profile in catalog["profiles"]
        if profile["kind"] == policy["receiver"]["profile_kind"]
    }


def _check_receiver(
    receiver: dict[str, Any], index: int, policy: dict[str, Any], receivers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    path = f"$.receivers[{index}]"
    if receiver["profile"] is None:
        raise _refuse(f"{path} is not a receiver HyperFlux supports")
    profile = receivers.get(receiver["profile"]["id"])
    if profile is None:
        raise _refuse(f"{path}.profile.id is not a receiver profile in this repository")
    identity = profile["identity"]
    if (receiver["vendor_id"], receiver["product_id"]) != (identity["vendor_id"], identity["product_id"]):
        raise _refuse(f"{path} USB ID does not match its receiver profile")
    if policy["receiver"]["require_current_profile_digest"] and receiver["profile"]["digest"] != profile["runtime_sha256"]:
        raise _refuse(
            f"{path}.profile.digest is from a different receiver profile; "
            "ask the owner to update HyperFlux and run the report again"
        )
    if receiver["lifecycle"] not in policy["receiver"]["lifecycles"]:
        raise _refuse(f"{path}.lifecycle is {receiver['lifecycle']}, not ready")
    return profile


def _route_for_device(
    device: dict[str, Any], routes: dict[tuple[int, str], dict[str, Any]]
) -> dict[str, Any] | None:
    for kind in ("mouse", "keyboard"):
        route = routes.get((device["product_id"], kind))
        if route is not None and device["kind"] in {kind, "other"}:
            return route
    return None


def _select_device(
    report: dict[str, Any],
    devices: list[dict[str, Any]],
    device_id: str | None,
) -> tuple[int, dict[str, Any], int, dict[str, Any], dict[str, Any]]:
    routes = {
        (route["identity"]["product_id"], route["identity"]["device_kind"]): route
        for route in devices
    }
    candidates: list[tuple[int, dict[str, Any], int, dict[str, Any], dict[str, Any]]] = []
    for r_index, receiver in enumerate(report["receivers"]):
        for d_index, device in enumerate(receiver["devices"]):
            if device_id is not None and device["device_id"] != device_id:
                continue
            route = _route_for_device(device, routes)
            if route is None:
                if device_id is not None:
                    raise _refuse(
                        f"$.receivers[{r_index}].devices[{d_index}] is not on the receiver's compatibility list"
                    )
                continue
            if device_id is None and public_support_tier(route) != SOURCE_QUALIFIED_OPERATIONAL:
                continue
            candidates.append((r_index, receiver, d_index, device, route))
    if device_id is not None and not candidates:
        raise _refuse("the report has no device with that number")
    if not candidates:
        raise _refuse("the report shows no device that is listed as expected to work")
    if len(candidates) > 1:
        raise _refuse(
            "the report shows more than one device that is listed as expected to work; "
            "choose one with --device"
        )
    return candidates[0]


def _check_device(
    device: dict[str, Any], path: str, route: dict[str, Any], policy: dict[str, Any]
) -> None:
    tier = public_support_tier(route)
    if tier != policy["device"]["eligible_tier"]:
        if tier == PHYSICALLY_PROVEN:
            raise _refuse(f"{route['model_name']} is already tested on real hardware")
        raise _refuse(f"{route['model_name']} is not supported yet, so a report cannot confirm it")
    if device["profile"] is not None or device["capabilities"]:
        raise _refuse(f"{path} has a device profile, which this device does not have in the repository")
    if device["vendor_id"] not in {None, route["identity"]["vendor_id"]}:
        raise _refuse(f"{path}.vendor_id does not match the listed device")
    if device["presence"] not in policy["device"]["presence"]:
        raise _refuse(f"{path}.presence is {device['presence']}; wake the device and run the report again")
    if device["support"] not in policy["device"]["support"]:
        raise _refuse(f"{path}.support does not match a device without a profile")


def _check_confirmations(
    confirmations: frozenset[str], route: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    rules = policy["confirmations"]
    missing = [item for item in rules["required"] if item not in confirmations]
    if missing:
        raise _refuse("the owner did not confirm that the device works normally through the receiver")
    available = {
        item["id"]
        for item in rules["lighting"]
        if route["integrations"][item["integration"]]["state"] == "source-validated"
    }
    claimed = {item["id"] for item in rules["lighting"]} & confirmations
    unexpected = sorted(claimed - available)
    if unexpected:
        raise _refuse(
            f"the owner ticked {', '.join(unexpected)}, but that app has no known way to "
            f"change lighting on {route['model_name']}; check the issue with the owner"
        )
    if not claimed:
        raise _refuse("the owner did not confirm that lighting changed in an app that supports the device")
    known = set(rules["required"]) | {item["id"] for item in rules["lighting"]}
    if confirmations - known:
        raise _refuse("unknown confirmation")
    return sorted(confirmations)


def _canonical_sha256(report: dict[str, Any]) -> str:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_owner_record(
    root: Path,
    submission: str,
    *,
    issue: int,
    device_id: str | None = None,
    confirmations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Check one submission and return the record it would become."""
    if not 1 <= issue <= 999_999_999:
        raise _refuse("the issue number must be a positive number")
    if device_id is not None and not _ORDINAL_DEVICE.match(device_id):
        raise _refuse("--device must be a numbered device-N name from the report")
    policy = load_owner_report_policy(root)
    text, ticked = read_submission(submission)
    if ticked is not None and confirmations is not None:
        raise _refuse("confirmations come from the issue form; do not also pass --confirmed")
    chosen = ticked if ticked is not None else frozenset(confirmations or ())
    report = parse_report_text(text, policy["report"]["maximum_bytes"])
    try:
        validate(load_json(root / policy["report"]["schema_path"]), report)
    except SchemaViolation as error:
        raise _refuse(str(error)) from None
    private = private_text_findings(report)
    if private:
        raise _refuse(
            "it holds text that looks private; ask the owner to make a fresh report with "
            "`hyperfluxctl qualification --json` and paste it unchanged:\n  "
            + "\n  ".join(private)
        )
    _check_ordinals(report)
    _check_report_envelope(root, policy, report)
    devices = load_source_validated_catalog(root)["devices"]
    r_index, receiver, d_index, device, route = _select_device(report, devices, device_id)
    receiver_profile = _check_receiver(receiver, r_index, policy, _receiver_profiles(root, policy))
    _check_device(device, f"$.receivers[{r_index}].devices[{d_index}]", route, policy)
    confirmed = _check_confirmations(chosen, route, policy)
    route_id = route["route_id"]
    record = {
        "$schema": RECORD_SCHEMA_REFERENCE,
        "schema": RECORD_SCHEMA,
        "id": f"claim.owner-report.{route_id}.issue-{issue}",
        "summary": (
            f"An owner reported that {route['model_name']} works through the "
            f"{receiver_profile['identity']['model_name']}."
        ),
        "evidence_level": "hardware-observed",
        "privacy": "public-summary",
        "route_id": route_id,
        "model_name": route["model_name"],
        "source_refs": [{"kind": "github-issue", "issue": issue}],
        "report": {
            "schema": report["schema"],
            "sha256": _canonical_sha256(report),
            "generated_on": report["generated_at"][:10],
            "hyperflux_version": report["companion"]["version"],
            "driver_version": report["system"]["driver_version"],
            "bridge_version": report["system"]["bridge_version"],
            "bridge_protocol": report["companion"]["bridge_protocol"],
            "receiver_profile": {
                "id": receiver_profile["profile_id"],
                "digest": receiver_profile["runtime_sha256"],
            },
            "profile_catalog_digest": report["system"]["profile_catalog_digest"],
            "finding_ids": sorted(
                {finding["finding_id"] for finding in report["collection"]["diagnostic_findings"]}
            ),
        },
        "observed": {
            "presence": device["presence"],
            "support": device["support"],
            "reported_kind": device["kind"],
            "battery": device["battery"]["availability"],
        },
        "confirmations": confirmed,
        "limitations": list(policy["limitations"]),
    }
    _validate_record(root, record, f"{RECORDS_DIRECTORY}/<new record>")
    return record


def record_file_name(record: dict[str, Any]) -> str:
    return f"{record['route_id']}--issue-{record['source_refs'][0]['issue']}.json"


def write_owner_record(root: Path, record: dict[str, Any], output_dir: Path | None = None) -> Path:
    directory = output_dir if output_dir is not None else root / RECORDS_DIRECTORY
    destination = directory / record_file_name(record)
    if destination.exists():
        raise ModelError(f"{destination.name} already exists; one record per issue")
    for existing in load_owner_reports(root) if output_dir is None else []:
        if existing["source_refs"][0]["issue"] == record["source_refs"][0]["issue"]:
            raise ModelError("issue already has an owner record for another device")
    directory.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return destination


# ---------------------------------------------------------------------------
# Committed records


def _validate_record(root: Path, record: dict[str, Any], label: str) -> None:
    try:
        validate(load_json(root / RECORD_SCHEMA_PATH), record)
    except SchemaViolation as error:
        raise ModelError(f"{label}: {error}") from error
    issue = record["source_refs"][0]["issue"]
    if record["id"] != f"claim.owner-report.{record['route_id']}.issue-{issue}":
        raise ModelError(f"{label}: id does not match its route and issue")
    if "input" not in record["confirmations"] or len(record["confirmations"]) < 2:
        raise ModelError(f"{label}: confirmations must include input and one lighting app")
    leaks = private_text_findings(record)
    if leaks:
        raise ModelError(f"{label}: record holds text that looks private: " + "; ".join(leaks))


def load_owner_reports(root: Path) -> list[dict[str, Any]]:
    directory = root / RECORDS_DIRECTORY
    if not directory.is_dir():
        return []
    routes = {route["route_id"]: route for route in load_source_validated_catalog(root)["devices"]}
    records: list[dict[str, Any]] = []
    issues: set[int] = set()
    for path in sorted(directory.iterdir()):
        label = str(path.relative_to(root))
        match = _RECORD_NAME.match(path.name)
        if match is None:
            raise ModelError(f"{label}: not an owner record file name")
        record = load_json(path)
        _validate_record(root, record, label)
        issue = record["source_refs"][0]["issue"]
        if (match.group("route"), int(match.group("issue"))) != (record["route_id"], issue):
            raise ModelError(f"{label}: file name does not match the record")
        route = routes.get(record["route_id"])
        if route is None:
            raise ModelError(f"{label}: route is not on the receiver's compatibility list")
        if record["model_name"] != route["model_name"]:
            raise ModelError(f"{label}: model name does not match the catalog")
        if issue in issues:
            raise ModelError(f"{label}: issue already has an owner record")
        issues.add(issue)
        records.append(record)
    return records


def owner_confirmed_routes(
    records: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, tuple[int, ...]]:
    """Map route id to the issue numbers that confirm it, when there are enough."""
    issues: dict[str, set[int]] = {}
    for record in records:
        issues.setdefault(record["route_id"], set()).add(record["source_refs"][0]["issue"])
    return {
        route_id: tuple(sorted(numbers))
        for route_id, numbers in sorted(issues.items())
        if len(numbers) >= policy["minimum_reports"]
    }


def load_owner_confirmations(root: Path) -> dict[str, tuple[int, ...]]:
    return owner_confirmed_routes(load_owner_reports(root), load_owner_report_policy(root))
