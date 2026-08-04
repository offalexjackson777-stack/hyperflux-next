# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import stat
import subprocess
import time
from typing import Any

from .model import ModelError, sha256_file
from .native_parity import (
    build_live_parity_evidence,
    validate_parity_evidence,
    write_parity_evidence,
)
from .native_probe import load_native_probe_catalog
from .openrazer_native_route import load_native_route_catalog
from .private_artifact import write_new_private_json


QUALIFIER_PATH = Path("/usr/lib/hyperflux-next/hyperflux-next-native-qualifier")
QUALIFIER_PACKAGE_MEMBER = "usr/lib/hyperflux-next/hyperflux-next-native-qualifier"
READ_ONLY_APPROVAL = (
    "approve HyperFlux Next restoration-isolated bounded read-only native parity probes"
)
LIVE_RESULT_SCHEMA = "hyperflux-native-live-parity-result-v1"
RAW_PREFIX = "hyperflux-next-native-parity-"
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
MAX_BINARY_BYTES = 32 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
VIRTUAL_HID_PATTERN = re.compile(r"0006:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\.[0-9A-Fa-f]{4}\Z")
INACTIVE_SERVICES = (
    "hyperflux-next-bridge.service",
    "hyperflux-next-openrazer-relay.service",
)


@dataclass(frozen=True)
class LiveParityResult:
    evidence_path: Path
    run_path: Path
    conclusion: str
    passed: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class LiveParityPreflight:
    revision: str
    probes: dict[str, Any]
    profile: dict[str, Any]
    route: dict[str, Any]


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
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot bind live parity to source: {error}") from error
    revision = result.stdout.strip()
    if SOURCE_REVISION_PATTERN.fullmatch(revision) is None or status.stdout:
        raise ModelError("live parity requires one exact clean source revision")
    return revision


def _read_private_json(path: Path, *, maximum_bytes: int = MAX_PRIVATE_JSON_BYTES) -> dict[str, Any]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ModelError("private qualification input may not be a symbolic link")
    try:
        metadata = candidate.stat()
    except OSError as error:
        raise ModelError(f"cannot inspect private qualification input: {error}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > maximum_bytes
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ModelError("private qualification input is not a bounded 0600 regular file")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelError(f"cannot read private qualification input: {error}") from error
    if not isinstance(value, dict):
        raise ModelError("private qualification input must be a JSON object")
    return value


def _bounded_process_output(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > MAX_PROCESS_OUTPUT_BYTES:
        raise ModelError("native qualifier output exceeded its bound")
    return value.strip()


def _package_member_digest(package: Path) -> str:
    bsdtar = shutil.which("bsdtar")
    if bsdtar is None:
        raise ModelError("bsdtar is required to verify the installed qualifier")
    for member in (QUALIFIER_PACKAGE_MEMBER, f"./{QUALIFIER_PACKAGE_MEMBER}"):
        try:
            result = subprocess.run(
                [bsdtar, "-xOf", str(package), member],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot inspect candidate package: {error}") from error
        if result.returncode == 0:
            if not 0 < len(result.stdout) <= MAX_BINARY_BYTES:
                raise ModelError("packaged native qualifier has an invalid size")
            return hashlib.sha256(result.stdout).hexdigest()
    raise ModelError("candidate package does not contain the native qualifier")


def _verify_candidate_package(package: Path, expected_sha256: str) -> None:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise ModelError("candidate package digest is invalid")
    candidate = package.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ModelError("candidate package must be a regular file")
    if sha256_file(candidate) != expected_sha256:
        raise ModelError("candidate package digest differs from the reviewed value")
    if QUALIFIER_PATH.is_symlink() or not QUALIFIER_PATH.is_file():
        raise ModelError("the installed native qualifier is unavailable")
    packaged = _package_member_digest(candidate)
    if sha256_file(QUALIFIER_PATH) != packaged:
        raise ModelError("the installed native qualifier differs from the candidate package")


def _require_isolated_services() -> None:
    for service in INACTIVE_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", service],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot inspect qualification service isolation: {error}") from error
        if result.returncode == 0:
            raise ModelError(
                "production bridge and native relay must be stopped before read-only qualification"
            )


def _profile_authority(
    root: Path, profile_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    probes = load_native_probe_catalog(root)
    routes = load_native_route_catalog(root)
    probe_matches = [item for item in probes["devices"] if item["profile_id"] == profile_id]
    route_matches = [item for item in routes["routes"] if item["profile_id"] == profile_id]
    if len(probe_matches) != 1 or len(route_matches) != 1:
        raise ModelError("live parity profile is not an exact native-route candidate")
    return probes, probe_matches[0], route_matches[0]


def _wait_for_ready(process: subprocess.Popen[str], timeout_seconds: float) -> str:
    if process.stdout is None:
        raise ModelError("native qualifier output channel is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    lines: list[str] = []
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                remainder = process.stdout.read()
                if remainder:
                    lines.append(remainder)
                break
            events = selector.select(timeout=0.1)
            for key, _mask in events:
                line = key.fileobj.readline()
                if not line:
                    continue
                lines.append(line)
                if line.startswith("HFX_NATIVE_QUALIFIER_READY "):
                    return _bounded_process_output("".join(lines))
    finally:
        selector.close()
    raise ModelError("native qualifier did not reach its bounded ready state")


def _read_bounded_uevent(path: Path) -> str:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024:
            raise ModelError("virtual HID identity metadata is invalid")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ModelError(f"cannot inspect virtual HID identity: {error}") from error


def _find_virtual_device(route: dict[str, Any], timeout_seconds: float) -> Path:
    driver_root = Path("/sys/bus/hid/drivers") / route["driver_family"]
    deadline = time.monotonic() + timeout_seconds
    expected_vendor = route["vendor_id"]
    expected_product = route["wireless_product_id"]
    while time.monotonic() < deadline:
        matches: list[Path] = []
        try:
            entries = tuple(driver_root.iterdir())
        except OSError:
            entries = ()
        for entry in entries:
            match = VIRTUAL_HID_PATTERN.fullmatch(entry.name)
            if match is None:
                continue
            vendor = int(match.group(1), 16)
            product = int(match.group(2), 16)
            if (vendor, product) != (expected_vendor, expected_product):
                continue
            uevent = _read_bounded_uevent(entry / "uevent")
            if any(
                line.startswith("HID_PHYS=hyperflux-next/native/v1/qualify-")
                for line in uevent.splitlines()
            ):
                matches.append(entry)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ModelError("more than one exact qualification HID endpoint appeared")
        time.sleep(0.05)
    raise ModelError("the exact qualification HID endpoint did not appear")


def _feature_only_has_no_input_nodes(entry: Path) -> bool:
    try:
        target = entry.resolve(strict=True)
        return not any(
            path.is_dir() and (path.name == "input" or path.name.startswith("input"))
            for path in target.rglob("*")
        )
    except OSError as error:
        raise ModelError(f"cannot verify feature-only HID topology: {error}") from error


def _trigger_probe_attributes(
    entry: Path,
    profile: dict[str, Any],
    maximum_attempts: int,
    process: subprocess.Popen[str],
) -> dict[str, bool]:
    cat = shutil.which("cat")
    if cat is None:
        raise ModelError("cat is required to trigger OpenRazer read attributes")
    results: dict[str, bool] = {}
    for probe in profile["probes"]:
        method = probe["method"]
        attribute = entry / probe["driver"]["attribute"]
        success = False
        for _attempt in range(maximum_attempts):
            if process.poll() is not None:
                break
            try:
                read = subprocess.run(
                    [cat, str(attribute)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=8,
                )
            except subprocess.TimeoutExpired:
                continue
            except OSError as error:
                raise ModelError(f"cannot trigger source-bound OpenRazer attribute: {error}") from error
            if len(read.stderr) > MAX_PROCESS_OUTPUT_BYTES:
                raise ModelError("OpenRazer attribute error output exceeded its bound")
            if read.returncode == 0:
                success = True
                break
            time.sleep(0.02)
        results[method] = success
    return results


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _wait_for_completion(process: subprocess.Popen[str], timeout_seconds: float) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        stdout, stderr = process.communicate(timeout=1)
    return _bounded_process_output(stdout), _bounded_process_output(stderr)


def _safe_raw_path() -> Path:
    nonce = os.urandom(12).hex()
    return Path("/dev/shm") / f"{RAW_PREFIX}{nonce}.json"


@contextmanager
def _ephemeral_raw_path() -> Iterator[Path]:
    path = _safe_raw_path()
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _user_cache(user: str, leaf: str) -> tuple[Path, int, int]:
    try:
        account = pwd.getpwnam(user)
    except KeyError as error:
        raise ModelError("live parity output user does not exist") from error
    if account.pw_uid == 0:
        raise ModelError("live parity output must belong to a non-root operator")
    return Path(account.pw_dir) / ".cache" / "hyperflux-next" / leaf, account.pw_uid, account.pw_gid


def _drop_to_operator(user: str, uid: int, gid: int) -> None:
    try:
        os.initgroups(user, gid)
        os.setgid(gid)
        os.setuid(uid)
    except OSError as error:
        raise ModelError(f"cannot drop native parity output to the operator: {error}") from error
    if os.geteuid() != uid or os.getegid() != gid:
        raise ModelError("native parity output privilege drop did not become exact")


def _qualifier_command(
    *,
    profile_id: str,
    expected_generation: int,
    revision: str,
    package_sha256: str,
    raw_path: Path,
    timeout_seconds: int,
    approval_text: str,
) -> list[str]:
    return [
        str(QUALIFIER_PATH),
        "--profile",
        profile_id,
        "--expected-generation",
        str(expected_generation),
        "--source-revision",
        revision,
        "--package-sha256",
        package_sha256,
        "--raw-output",
        str(raw_path),
        "--timeout-seconds",
        str(timeout_seconds),
        "--approval-text",
        approval_text,
    ]


def prepare_live_parity(
    root: Path,
    *,
    profile_id: str,
    package: Path,
    package_sha256: str,
    expected_generation: int,
    timeout_seconds: int,
) -> LiveParityPreflight:
    """Checks the immutable software and isolation boundary for a live read."""

    if os.geteuid() != 0:
        raise ModelError("live native parity must run as root")
    if not 10 <= timeout_seconds <= 120 or not 1 <= expected_generation < 2**63:
        raise ModelError("live native parity generation or timeout is invalid")
    revision = _git_revision(root)
    _verify_candidate_package(package, package_sha256)
    _require_isolated_services()
    probes, profile, route = _profile_authority(root, profile_id)
    return LiveParityPreflight(
        revision=revision,
        probes=probes,
        profile=profile,
        route=route,
    )


def run_live_parity(
    root: Path,
    *,
    profile_id: str,
    direct_evidence_path: Path,
    package: Path,
    package_sha256: str,
    expected_generation: int,
    approval_text: str,
    user: str,
    timeout_seconds: int = 90,
    direct_serial_values: dict[str, bytes] | None = None,
) -> LiveParityResult:
    """Run one exact, restoration-isolated, read-only receiver parity session."""

    if os.geteuid() != 0:
        raise ModelError("live native parity must run as root")
    if approval_text != READ_ONLY_APPROVAL:
        raise ModelError("the exact read-only native parity approval is missing")
    preflight = prepare_live_parity(
        root,
        profile_id=profile_id,
        package=package,
        package_sha256=package_sha256,
        expected_generation=expected_generation,
        timeout_seconds=timeout_seconds,
    )
    revision = preflight.revision
    probes = preflight.probes
    profile = preflight.profile
    route = preflight.route
    direct_evidence = _read_private_json(direct_evidence_path)
    validate_parity_evidence(direct_evidence)
    parity_cache, uid, gid = _user_cache(user, "native-transport-parity")
    run_cache, _uid, _gid = _user_cache(user, "native-live-parity")
    process: subprocess.Popen[str] | None = None
    raw_qualification: dict[str, Any] | None = None
    attribute_results: dict[str, bool] = {}
    no_input_nodes = False
    qualifier_stdout = ""
    qualifier_stderr = ""
    with _ephemeral_raw_path() as raw_path:
        command = _qualifier_command(
            profile_id=profile_id,
            expected_generation=expected_generation,
            revision=revision,
            package_sha256=package_sha256,
            raw_path=raw_path,
            timeout_seconds=timeout_seconds,
            approval_text=approval_text,
        )
        try:
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
            qualifier_stdout = _wait_for_ready(process, 10)
            entry = _find_virtual_device(route, 10)
            no_input_nodes = _feature_only_has_no_input_nodes(entry)
            if not no_input_nodes:
                raise ModelError("qualification UHID endpoint created an input collection")
            attribute_results = _trigger_probe_attributes(
                entry,
                profile,
                probes["busy_retry"]["maximum_attempts"],
                process,
            )
            final_stdout, qualifier_stderr = _wait_for_completion(process, 10)
            qualifier_stdout = "\n".join(
                value for value in (qualifier_stdout, final_stdout) if value
            )
            raw_qualification = _read_private_json(raw_path)
        finally:
            if process is not None:
                _terminate_process(process)
    if raw_path.exists():
        raise ModelError("ephemeral native qualification capture was not deleted")
    if raw_qualification is None:
        raise ModelError("native qualifier produced no sanitizable capture")

    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    evidence = build_live_parity_evidence(
        root,
        profile_id=profile_id,
        direct_evidence=direct_evidence,
        raw_qualification=raw_qualification,
        package_sha256=package_sha256,
        receiver_generation=expected_generation,
        created_at=timestamp,
        source_revision=revision,
        direct_serial_values=direct_serial_values,
    )
    _drop_to_operator(user, uid, gid)
    evidence_path = write_parity_evidence(evidence, cache_root=parity_cache)
    coverage = evidence["coverage"]
    run_result = {
        "schema": LIVE_RESULT_SCHEMA,
        "created_at": timestamp,
        "source_revision": revision,
        "package_sha256": package_sha256,
        "profile_id": profile_id,
        "receiver_generation": expected_generation,
        "read_only": True,
        "production_services_isolated": True,
        "feature_only_no_input_nodes": no_input_nodes,
        "methods_triggered": [
            {"method": method, "attribute_read_succeeded": attribute_results.get(method, False)}
            for method in coverage["required"]
        ],
        "qualifier_completed": raw_qualification["completed"],
        "qualifier_exit_code": process.returncode if process is not None else None,
        "qualifier_ready_recorded": "HFX_NATIVE_QUALIFIER_READY " in qualifier_stdout,
        "qualifier_error_recorded": bool(qualifier_stderr),
        "ephemeral_raw_deleted": True,
        "parity_sha256": sha256_file(evidence_path),
        "conclusion": evidence["conclusion"],
        "coverage": coverage,
        "network_upload": False,
        "settings_changed": False,
        "rgb_changed": False,
    }
    stamp = re.sub(r"[^0-9TZ]", "", timestamp)
    safe_profile = re.sub(r"[^a-z0-9.-]", "-", profile_id)
    run_path = write_new_private_json(
        run_cache,
        f"{stamp}-{safe_profile}.json",
        run_result,
    )
    return LiveParityResult(
        evidence_path=evidence_path,
        run_path=run_path,
        conclusion=evidence["conclusion"],
        passed=tuple(coverage["passed"]),
        missing=tuple(coverage["missing"]),
        failed=tuple(coverage["failed"]),
    )
