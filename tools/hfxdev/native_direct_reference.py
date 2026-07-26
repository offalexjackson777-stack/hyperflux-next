# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

from .model import ModelError
from .native_parity import (
    DirectSemanticReference,
    UsbmonExchange,
    build_direct_semantic_reference,
)
from .native_live_parity import (
    READ_ONLY_APPROVAL,
    LiveParityResult,
    prepare_live_parity,
    run_live_parity,
)
from .native_probe import REPORT_SIZE
from .private_artifact import write_new_private_json


HID_IDENTITY_PATTERN = re.compile(
    r"0003:(?P<vendor>[0-9A-Fa-f]{4}):(?P<product>[0-9A-Fa-f]{4})\.[0-9A-Fa-f]{4}\Z"
)
MAX_ATTRIBUTE_BYTES = 4096
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
CAPTURE_PREFIX = "hyperflux-next-direct-reference-"
STAGED_PARITY_APPROVAL = (
    "approve HyperFlux Next supervised direct-to-receiver native parity session"
)
TSHARK_FIELDS = (
    "frame.time_epoch",
    "usb.urb_id",
    "usb.urb_type",
    "usb.transfer_type",
    "usb.bus_id",
    "usb.device_address",
    "usb.endpoint_address",
    "usb.bmRequestType",
    "usb.setup.bRequest",
    "usb.setup.wValue",
    "usb.setup.wIndex",
    "usb.setup.wLength",
    "usb.data_len",
    "usb.capdata",
    "usb.data_fragment",
    "usbhid.data",
    "usbhid.data.vendor",
)


def _numeric_field(value: str, label: str) -> int:
    candidate = value.strip().lower()
    if not candidate:
        raise ModelError(f"tshark USB row lacks {label}")
    try:
        return int(candidate, 16 if candidate.startswith("0x") else 10)
    except ValueError as error:
        raise ModelError(f"tshark USB row has invalid {label}") from error


def _tshark_payload(values: list[str]) -> bytes:
    payloads: set[bytes] = set()
    for value in values:
        compact = value.replace(":", "").strip()
        if not compact:
            continue
        if re.fullmatch(r"[0-9a-fA-F]+", compact) is None or len(compact) % 2:
            raise ModelError("tshark USB row contains malformed report bytes")
        decoded = bytes.fromhex(compact)
        if len(decoded) == REPORT_SIZE:
            payloads.add(decoded)
    if len(payloads) > 1:
        raise ModelError("tshark USB row exposes conflicting complete reports")
    return next(iter(payloads), b"")


def parse_tshark_usbmon_fields(
    text: str,
    *,
    target_bus: int,
    target_device: int,
) -> tuple[UsbmonExchange, ...]:
    """Parses bounded full-capture tshark fields for one direct USB device."""

    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) > MAX_CAPTURE_BYTES:
        raise ModelError("tshark USB export exceeds the bounded size limit")
    lines = text.splitlines()
    if len(lines) > 65536:
        raise ModelError("tshark USB export exceeds the bounded line limit")
    if not 1 <= target_bus <= 255 or not 1 <= target_device <= 255:
        raise ModelError("tshark USB target identity is invalid")

    pending_sets: list[tuple[int, int, bytes]] = []
    pending_gets: dict[str, tuple[int, int]] = {}
    exchanges: list[UsbmonExchange] = []
    for line_number, line in enumerate(lines, start=1):
        fields = line.split("\t")
        if len(fields) != len(TSHARK_FIELDS):
            raise ModelError(f"tshark USB row {line_number} has an invalid field count")
        (
            timestamp,
            urb_id,
            urb_type,
            transfer_type,
            bus,
            device,
            _endpoint,
            request_type,
            request_id,
            value,
            interface,
            setup_length,
            _data_length,
            capdata,
            fragment,
            hid_data,
            hid_vendor_data,
        ) = fields
        if not bus or not device:
            continue
        if (
            _numeric_field(bus, "bus") != target_bus
            or _numeric_field(device, "device") != target_device
        ):
            continue
        if not transfer_type or _numeric_field(transfer_type, "transfer type") != 2:
            continue
        try:
            timestamp_us = int(Decimal(timestamp) * 1_000_000)
        except (InvalidOperation, ValueError) as error:
            raise ModelError(f"tshark USB row {line_number} has an invalid timestamp") from error
        event = urb_type.strip()
        if event in {"0x53", "83"}:
            event = "S"
        elif event in {"0x43", "67"}:
            event = "C"
        payload = _tshark_payload([capdata, fragment, hid_data, hid_vendor_data])

        if event == "S" and request_type and request_id and value and setup_length:
            setup = (
                _numeric_field(request_type, "request type"),
                _numeric_field(request_id, "request identifier"),
                _numeric_field(value, "report value"),
                _numeric_field(interface or "0", "interface"),
                _numeric_field(setup_length, "setup length"),
            )
            if setup[:3] == (0x21, 0x09, 0x0300) and setup[4] == REPORT_SIZE:
                if len(payload) != REPORT_SIZE:
                    raise ModelError("captured direct Razer request is not complete")
                pending_sets.append((timestamp_us, setup[3], payload))
            elif setup[:3] == (0xA1, 0x01, 0x0300) and setup[4] == REPORT_SIZE:
                if not urb_id:
                    raise ModelError("captured direct Razer GET lacks its URB identity")
                pending_gets[urb_id] = (timestamp_us, setup[3])
            continue

        if event != "C" or not urb_id or urb_id not in pending_gets or not payload:
            continue
        _get_timestamp, get_interface = pending_gets.pop(urb_id)
        if len(payload) != REPORT_SIZE:
            raise ModelError("captured direct Razer response is not complete")
        if not pending_sets:
            raise ModelError("captured direct Razer response has no preceding SET")
        request_timestamp, set_interface, request = pending_sets.pop(0)
        if set_interface != get_interface:
            raise ModelError("captured direct Razer SET and GET use different interfaces")
        exchanges.append(
            UsbmonExchange(
                timestamp_us=request_timestamp,
                bus=target_bus,
                device=target_device,
                interface=set_interface,
                request=request,
                request_length=REPORT_SIZE,
                response=payload,
                response_length=REPORT_SIZE,
            )
        )

    if pending_sets or pending_gets:
        raise ModelError("tshark USB export ends with an unmatched Razer transfer")
    return tuple(exchanges)


@dataclass(frozen=True)
class DirectUsbEndpoint:
    driver_entry: Path
    bus: int
    device: int


@dataclass(frozen=True)
class CapturedDirectReference:
    reference: DirectSemanticReference
    attributes_read: tuple[str, ...]
    bus: int
    device: int


def _bounded_sysfs_number(path: Path, label: str) -> int:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ModelError(f"direct USB {label} is not a sysfs value")
        with path.open("rb") as handle:
            value = handle.read(17)
    except OSError as error:
        raise ModelError(f"cannot read direct USB {label}: {error}") from error
    if not value or len(value) > 16:
        raise ModelError(f"direct USB {label} is outside its bound")
    try:
        number = int(value.strip(), 10)
    except ValueError as error:
        raise ModelError(f"direct USB {label} is invalid") from error
    if not 1 <= number <= 255:
        raise ModelError(f"direct USB {label} is outside one byte")
    return number


def _usb_location(entry: Path, devices_root: Path) -> tuple[int, int]:
    try:
        resolved = entry.resolve(strict=True)
        authority = devices_root.resolve(strict=True)
        resolved.relative_to(authority)
    except (OSError, ValueError) as error:
        raise ModelError("direct USB driver entry escapes the sysfs device tree") from error
    for parent in (resolved, *resolved.parents):
        if parent == authority.parent:
            break
        busnum = parent / "busnum"
        devnum = parent / "devnum"
        if busnum.exists() and devnum.exists():
            return (
                _bounded_sysfs_number(busnum, "bus number"),
                _bounded_sysfs_number(devnum, "device number"),
            )
        if parent == authority:
            break
    raise ModelError("direct USB driver entry has no physical bus/device identity")


def find_exact_direct_endpoint(
    route: dict[str, Any],
    profile: dict[str, Any],
    *,
    driver_root: Path = Path("/sys/bus/hid/drivers"),
    devices_root: Path = Path("/sys/devices"),
) -> DirectUsbEndpoint:
    """Finds one physical direct-data route from exact source-bound identities."""

    family = route["driver_family"]
    vendor = route["vendor_id"]
    products = set(route["direct_data_product_ids"])
    attributes = tuple(probe["driver"]["attribute"] for probe in profile["probes"])
    directory = driver_root / family
    try:
        entries = tuple(directory.iterdir())
    except FileNotFoundError as error:
        raise ModelError("the exact direct USB data route is absent") from error
    except OSError as error:
        raise ModelError(f"cannot inspect direct USB driver entries: {error}") from error

    candidates: dict[tuple[int, int], list[Path]] = {}
    for entry in entries:
        match = HID_IDENTITY_PATTERN.fullmatch(entry.name)
        if match is None:
            continue
        if int(match.group("vendor"), 16) != vendor:
            continue
        if int(match.group("product"), 16) not in products:
            continue
        if not all((entry / attribute).is_file() for attribute in attributes):
            continue
        location = _usb_location(entry, devices_root)
        candidates.setdefault(location, []).append(entry)
    if not candidates:
        raise ModelError(
            "the exact direct USB data route with every source-bound read attribute is absent"
        )
    if len(candidates) != 1:
        raise ModelError("more than one exact direct USB device is connected")
    (bus, device), carriers = next(iter(candidates.items()))
    carrier = sorted(carriers, key=lambda value: value.name)[0]
    return DirectUsbEndpoint(driver_entry=carrier, bus=bus, device=device)


def _read_attribute(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelError(f"cannot open source-bound OpenRazer attribute: {error}") from error
    try:
        value = os.read(descriptor, MAX_ATTRIBUTE_BYTES + 1)
    except OSError as error:
        raise ModelError(f"cannot read source-bound OpenRazer attribute: {error}") from error
    finally:
        os.close(descriptor)
    if len(value) > MAX_ATTRIBUTE_BYTES:
        raise ModelError("source-bound OpenRazer attribute exceeded its read bound")


def trigger_direct_attributes(entry: Path, profile: dict[str, Any]) -> tuple[str, ...]:
    attributes: list[str] = []
    for probe in profile["probes"]:
        attribute = probe["driver"]["attribute"]
        _read_attribute(entry / attribute)
        attributes.append(attribute)
    return tuple(attributes)


@contextmanager
def _private_capture_directory(uid: int, gid: int) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix=CAPTURE_PREFIX, dir="/dev/shm"))
    try:
        os.chmod(directory, 0o700)
        os.chown(directory, uid, gid)
        yield directory
    finally:
        try:
            shutil.rmtree(directory)
        except OSError as error:
            raise ModelError("private direct USB material could not be deleted") from error


def _capture_process_identity() -> tuple[int, int]:
    uid = os.geteuid()
    gid = os.getegid()
    if uid != 0:
        raise ModelError("private direct USB capture must run as root")
    return uid, gid


def _bounded_output(value: bytes, label: str) -> str:
    if len(value) > MAX_PROCESS_OUTPUT_BYTES:
        raise ModelError(f"{label} output exceeded its bound")
    return value.decode("utf-8", errors="replace").strip()


def _bounded_log(path: Path, label: str) -> str:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ModelError(f"{label} log is not a regular file")
        with path.open("rb") as handle:
            value = handle.read(MAX_PROCESS_OUTPUT_BYTES + 1)
    except OSError as error:
        raise ModelError(f"cannot read {label} log: {error}") from error
    return _bounded_output(value, label)


@dataclass(frozen=True)
class CaptureProcess:
    process: subprocess.Popen[bytes]
    diagnostic_path: Path


def _start_dumpcap(interface: str, capture: Path) -> CaptureProcess:
    executable = shutil.which("dumpcap")
    if executable is None:
        raise ModelError("dumpcap is required for complete direct USB references")
    diagnostic_path = capture.with_name("dumpcap.log")
    previous_umask = os.umask(0o077)
    try:
        with diagnostic_path.open("xb") as diagnostic:
            process = subprocess.Popen(
                [
                    executable,
                    "-i",
                    interface,
                    "-s",
                    "0",
                    "-q",
                    "-a",
                    "duration:5",
                    "-a",
                    f"filesize:{MAX_CAPTURE_BYTES // 1024}",
                    "-w",
                    str(capture),
                ],
                stdout=subprocess.DEVNULL,
                stderr=diagnostic,
            )
    except OSError as error:
        raise ModelError(f"cannot start private direct USB capture: {error}") from error
    finally:
        os.umask(previous_umask)
    time.sleep(0.25)
    if process.poll() is not None:
        process.wait(timeout=1)
        detail = _bounded_log(diagnostic_path, "dumpcap")
        raise ModelError(f"private direct USB capture did not start: {detail}")
    return CaptureProcess(process=process, diagnostic_path=diagnostic_path)


def _stop_dumpcap(capture: CaptureProcess) -> None:
    process = capture.process
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    detail = _bounded_log(capture.diagnostic_path, "dumpcap")
    if process.returncode not in {0, 130}:
        raise ModelError(f"private direct USB capture failed: {detail}")


def _export_capture(capture: Path, *, bus: int, device: int) -> str:
    executable = shutil.which("tshark")
    if executable is None:
        raise ModelError("tshark is required for complete direct USB references")
    command = [
        executable,
        "-n",
        "-r",
        str(capture),
        "-Y",
        f"usb.bus_id == {bus} && usb.device_address == {device}",
        "-T",
        "fields",
        "-E",
        "separator=/t",
        "-E",
        "quote=n",
        "-E",
        "occurrence=f",
    ]
    for field_name in TSHARK_FIELDS:
        command.extend(("-e", field_name))
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot sanitize direct USB capture: {error}") from error
    if result.returncode != 0:
        detail = _bounded_output(result.stderr, "tshark")
        raise ModelError(f"cannot sanitize direct USB capture: {detail}")
    if len(result.stdout) > MAX_CAPTURE_BYTES:
        raise ModelError("sanitized direct USB capture exceeded its bound")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError as error:
        raise ModelError("sanitized direct USB capture is not UTF-8") from error


def capture_direct_reference(
    root: Path,
    *,
    profile_id: str,
    route: dict[str, Any],
    profile: dict[str, Any],
    source_revision: str,
    driver_root: Path = Path("/sys/bus/hid/drivers"),
    devices_root: Path = Path("/sys/devices"),
) -> CapturedDirectReference:
    endpoint = find_exact_direct_endpoint(
        route,
        profile,
        driver_root=driver_root,
        devices_root=devices_root,
    )
    process: CaptureProcess | None = None
    with _private_capture_directory(*_capture_process_identity()) as directory:
        capture = directory / "direct.pcapng"
        try:
            process = _start_dumpcap(f"usbmon{endpoint.bus}", capture)
            attributes = trigger_direct_attributes(endpoint.driver_entry, profile)
            time.sleep(0.25)
        finally:
            if process is not None:
                _stop_dumpcap(process)
        try:
            metadata = capture.lstat()
        except OSError as error:
            raise ModelError(f"private direct USB capture is absent: {error}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_CAPTURE_BYTES
        ):
            raise ModelError("private direct USB capture has an invalid type or size")
        os.chmod(capture, 0o600)
        exported = _export_capture(
            capture,
            bus=endpoint.bus,
            device=endpoint.device,
        )
        exchanges = parse_tshark_usbmon_fields(
            exported,
            target_bus=endpoint.bus,
            target_device=endpoint.device,
        )
        reference = build_direct_semantic_reference(
            root,
            profile_id=profile_id,
            exchanges=exchanges,
            source_revision=source_revision,
        )
    if directory.exists():
        raise ModelError("private direct USB capture directory was not deleted")
    return CapturedDirectReference(
        reference=reference,
        attributes_read=attributes,
        bus=endpoint.bus,
        device=endpoint.device,
    )


def _operator_identity(user: str) -> tuple[int, int]:
    try:
        account = pwd.getpwnam(user)
    except KeyError as error:
        raise ModelError("staged parity operator does not exist") from error
    if account.pw_uid == 0:
        raise ModelError("staged parity operator must not be root")
    return account.pw_uid, account.pw_gid


def _wait_for_operator(timeout_seconds: int) -> None:
    if not 30 <= timeout_seconds <= 600:
        raise ModelError("staged parity transition timeout is invalid")
    selector = selectors.DefaultSelector()
    try:
        selector.register(sys.stdin, selectors.EVENT_READ)
        events = selector.select(timeout_seconds)
        if not events:
            raise ModelError("staged direct-to-receiver transition timed out")
        if sys.stdin.readline() == "":
            raise ModelError("staged direct-to-receiver transition input closed")
    finally:
        selector.close()


def _require_direct_route_absent(
    route: dict[str, Any],
    profile: dict[str, Any],
) -> None:
    try:
        find_exact_direct_endpoint(route, profile)
    except ModelError as error:
        if str(error) in {
            "the exact direct USB data route is absent",
            "the exact direct USB data route with every source-bound read attribute is absent",
        }:
            return
        raise
    raise ModelError("the direct USB data route is still active after the transition")


def run_staged_live_parity(
    root: Path,
    *,
    profile_id: str,
    package: Path,
    package_sha256: str,
    expected_generation: int,
    approval_text: str,
    user: str,
    timeout_seconds: int = 90,
    transition_timeout_seconds: int = 300,
) -> LiveParityResult:
    """Captures upstream direct reads, then compares the same device via the receiver."""

    if approval_text != STAGED_PARITY_APPROVAL:
        raise ModelError("the exact staged native parity approval is missing")
    preflight = prepare_live_parity(
        root,
        profile_id=profile_id,
        package=package,
        package_sha256=package_sha256,
        expected_generation=expected_generation,
        timeout_seconds=timeout_seconds,
    )
    uid, gid = _operator_identity(user)
    captured = capture_direct_reference(
        root,
        profile_id=profile_id,
        route=preflight.route,
        profile=preflight.profile,
        source_revision=preflight.revision,
    )
    observed = {
        item["method"]
        for item in captured.reference.evidence["observations"]
    }
    required = {
        probe["method"]
        for probe in preflight.profile["probes"]
    }
    missing = sorted(required - observed)
    if missing:
        raise ModelError(
            "direct OpenRazer capture missed source-bound methods: " + ", ".join(missing)
        )
    print(
        "Direct OpenRazer reference captured completely.\n"
        "Switch this same device to 2.4 GHz, remove its direct USB data route, "
        "place or wake it on the HyperFlux receiver, and exercise it.\n"
        "Press Enter only after it is ready. The receiver itself stays connected.",
        flush=True,
    )
    _wait_for_operator(transition_timeout_seconds)
    _require_direct_route_absent(preflight.route, preflight.profile)

    with _private_capture_directory(uid, gid) as directory:
        direct_evidence = write_new_private_json(
            directory,
            "direct-evidence.json",
            captured.reference.evidence,
        )
        result = run_live_parity(
            root,
            profile_id=profile_id,
            direct_evidence_path=direct_evidence,
            package=package,
            package_sha256=package_sha256,
            expected_generation=expected_generation,
            approval_text=READ_ONLY_APPROVAL,
            user=user,
            timeout_seconds=timeout_seconds,
            direct_serial_values=captured.reference.serial_values,
        )
    if directory.exists():
        raise ModelError("ephemeral staged parity material was not deleted")
    return result
