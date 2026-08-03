#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
from typing import Any


SCHEMA = "hyperflux-stock-application-compatibility-v1"
MAX_ARCHIVE_MEMBERS = 4096
MAX_COMMAND_OUTPUT = 1_048_576
DAEMON_START_TIMEOUT = 15.0
DAEMON_STOP_TIMEOUT = 5.0
CASE_TIMEOUT = 90
MOUSE_PID = 0x00CD
KEYBOARD_PID = 0x0296

MOUSE_NAME = "Razer Basilisk V3 Pro 35K (Wireless)"
KEYBOARD_NAME = "Razer DeathStalker V2 Pro TKL (Wireless)"

MOUSE_ENDPOINTS: dict[str, bytes] = {
    "charge_level": b"255",
    "charge_low_threshold": b"38",
    "charge_status": b"1",
    "device_idle_time": b"600",
    "device_mode": bytes.fromhex("0000"),
    "device_serial": b"HFXTESTMOUSE00CD",
    "device_type": MOUSE_NAME.encode(),
    "dpi": b"800:800",
    "dpi_stages": bytes.fromhex("010320032005dc05dc"),
    "firmware_version": b"v1.0",
    "logo_led_brightness": b"0",
    "logo_matrix_effect_none": b"",
    "logo_matrix_effect_spectrum": b"",
    "logo_matrix_effect_static": b"",
    "logo_matrix_effect_wave": b"",
    "matrix_brightness": b"0",
    "matrix_custom_frame": b"",
    "matrix_effect_custom": b"",
    "matrix_effect_none": b"",
    "matrix_effect_spectrum": b"",
    "matrix_effect_static": b"",
    "matrix_effect_wave": b"",
    "poll_rate": b"500",
    "scroll_acceleration": b"0",
    "scroll_led_brightness": b"0",
    "scroll_matrix_effect_none": b"",
    "scroll_matrix_effect_spectrum": b"",
    "scroll_matrix_effect_static": b"",
    "scroll_matrix_effect_wave": b"",
    "scroll_mode": b"0",
    "scroll_smart_reel": b"0",
    "tilt_hwheel": b"0",
    "tilt_repeat": b"0",
    "tilt_repeat_delay": b"0",
    "version": b"1.0.0",
}

KEYBOARD_ENDPOINTS: dict[str, bytes] = {
    "charge_level": b"255",
    "charge_status": b"1",
    "device_mode": bytes.fromhex("0000"),
    "device_serial": b"HFXTESTKEYBOARD0296",
    "device_type": KEYBOARD_NAME.encode(),
    "firmware_version": b"v1.0",
    "game_led_state": b"0",
    "kbd_layout": b"01",
    "key_alt_f4": bytes.fromhex("00"),
    "key_alt_tab": bytes.fromhex("00"),
    "key_super": bytes.fromhex("00"),
    "macro_led_effect": b"0",
    "macro_led_state": b"0",
    "matrix_brightness": b"0",
    "matrix_custom_frame": b"",
    "matrix_effect_breath": b"",
    "matrix_effect_custom": b"",
    "matrix_effect_none": b"",
    "matrix_effect_reactive": b"",
    "matrix_effect_spectrum": b"",
    "matrix_effect_starlight": b"",
    "matrix_effect_static": b"",
    "matrix_effect_wave": b"",
    "version": b"1.0.0",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def package_info(path: Path) -> dict[str, tuple[str, ...]]:
    completed = subprocess.run(
        ["bsdtar", "-xOf", str(path), ".PKGINFO"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_COMMAND_OUTPUT:
        raise RuntimeError(f"cannot read package metadata: {path.name}")
    result: dict[str, list[str]] = {}
    for raw_line in completed.stdout.decode("utf-8", errors="strict").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        key, separator, value = raw_line.partition(" = ")
        if not separator or not key or not value:
            raise RuntimeError(f"malformed package metadata: {path.name}")
        result.setdefault(key, []).append(value)
    return {key: tuple(values) for key, values in result.items()}


def archive_members(path: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["bsdtar", "-tf", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_COMMAND_OUTPUT:
        raise RuntimeError(f"cannot inventory package archive: {path.name}")
    members = tuple(
        line.rstrip("/")
        for line in completed.stdout.decode("utf-8", errors="strict").splitlines()
        if line
    )
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise RuntimeError(f"package member count is invalid: {path.name}")
    for member in members:
        parts = Path(member).parts
        if member.startswith("/") or ".." in parts:
            raise RuntimeError(f"unsafe package member: {member}")
    return members


def extract_package(path: Path, destination: Path) -> None:
    archive_members(path)
    completed = subprocess.run(
        ["bsdtar", "-xf", str(path), "-C", str(destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if completed.returncode != 0 or len(completed.stderr) > MAX_COMMAND_OUTPUT:
        raise RuntimeError(f"package extraction failed: {path.name}")


def validate_driver_source(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("driver source archive is not a regular file")
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        if not members or len(members) > 128:
            raise RuntimeError("driver source member count is invalid")
        names = {member.name for member in members}
        required = {
            "dkms.conf",
            "driver/razeraccessory_driver.c",
            "driver/razerkbd_driver.c",
            "driver/razerkbd_driver.h",
            "driver/razerkraken_driver.c",
            "driver/razermouse_driver.c",
            "driver/razermouse_driver.h",
        }
        if not required.issubset(names):
            raise RuntimeError("driver source archive is incomplete")
        for member in members:
            if member.issym() or member.islnk() or member.name.startswith("/"):
                raise RuntimeError("driver source archive has unsafe members")
            if ".." in Path(member.name).parts:
                raise RuntimeError("driver source archive has path traversal")

        def text(name: str) -> str:
            member = archive.getmember(name)
            if member.size > MAX_COMMAND_OUTPUT:
                raise RuntimeError(f"driver source member is oversized: {name}")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"driver source member is unreadable: {name}")
            return source.read().decode("utf-8", errors="strict")

        kbd_c = text("driver/razerkbd_driver.c")
        kbd_h = text("driver/razerkbd_driver.h")
        mouse_c = text("driver/razermouse_driver.c")
        mouse_h = text("driver/razermouse_driver.h")
        capability = 'MODULE_INFO(hyperflux_native_transport, "razer-feature-report-v1");'
        if capability not in kbd_c or capability not in mouse_c:
            raise RuntimeError("driver source lacks native transport capability metadata")
        if (
            "USB_DEVICE_ID_RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS 0x0296"
            not in kbd_h
            or "USB_DEVICE_ID_RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS" not in kbd_c
            or "USB_DEVICE_ID_RAZER_BASILISK_V3_PRO_35K_WIRELESS 0x00CD"
            not in mouse_h
            or "USB_DEVICE_ID_RAZER_BASILISK_V3_PRO_35K_WIRELESS" not in mouse_c
        ):
            raise RuntimeError("driver source lacks the exact assurance fixture identities")
    return {
        "sha256": sha256_file(path),
        "native_transport_capability": "razer-feature-report-v1",
        "fixture_product_ids": ["00cd", "0296"],
    }


def create_fake_device(root: Path, directory: str, endpoints: dict[str, bytes]) -> None:
    device = root / directory
    device.mkdir(mode=0o755)
    for name, value in endpoints.items():
        path = device / name
        with path.open("xb") as output:
            output.write(value)
        path.chmod(0o660)
    device.chmod(0o555)


def create_fake_devices(root: Path) -> None:
    if any(root.iterdir()):
        raise RuntimeError("fake-device root is not empty")
    create_fake_device(root, "0003:1532:00CD.0001", MOUSE_ENDPOINTS)
    create_fake_device(root, "0003:1532:0296.0001", KEYBOARD_ENDPOINTS)


def remove_fake_devices(root: Path) -> None:
    for device in root.iterdir():
        if device.is_symlink() or not device.is_dir():
            raise RuntimeError("fake-device root contains an unexpected entry")
        device.chmod(0o755)
        for endpoint in device.iterdir():
            if endpoint.is_symlink() or not endpoint.is_file():
                raise RuntimeError("fake device contains an unexpected endpoint")
            endpoint.chmod(0o600)
        shutil.rmtree(device)


def file_text(path: Path) -> str:
    value = path.read_bytes()
    if len(value) > 4096:
        raise RuntimeError(f"fake endpoint grew beyond its bound: {path.name}")
    return value.decode("utf-8", errors="strict")


def wait_for_bus_owner(process: subprocess.Popen[bytes], expected: bool) -> None:
    import dbus

    bus = dbus.SessionBus()
    daemon = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
    interface = dbus.Interface(daemon, "org.freedesktop.DBus")
    deadline = time.monotonic() + DAEMON_START_TIMEOUT
    while time.monotonic() < deadline:
        if expected and process.poll() is not None:
            raise RuntimeError(f"OpenRazer daemon exited with status {process.returncode}")
        if bool(interface.NameHasOwner("org.razer")) is expected:
            return
        time.sleep(0.05)
    raise RuntimeError("OpenRazer D-Bus ownership did not converge")


def start_daemon(
    root: Path,
    environment: dict[str, str],
    fake_root: Path,
    temporary: Path,
    phase: str,
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    run_root = temporary / f"run-{phase}"
    log_root = temporary / f"log-{phase}"
    run_root.mkdir(mode=0o700)
    log_root.mkdir(mode=0o700)
    stdout_path = temporary / f"daemon-{phase}.stdout"
    stderr_path = temporary / f"daemon-{phase}.stderr"
    stdout = stdout_path.open("xb")
    stderr = stderr_path.open("xb")
    command = [
        sys.executable,
        str(root / "usr/bin/openrazer-daemon"),
        "-F",
        "--run-dir",
        str(run_root),
        "--log-dir",
        str(log_root),
        "--test-dir",
        str(fake_root),
        "--config",
        str(temporary / "razer.conf"),
        "--persistence",
        str(temporary / f"persistence-{phase}.json"),
    ]
    process = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr)
    stdout.close()
    stderr.close()
    wait_for_bus_owner(process, True)
    return process, stdout_path, stderr_path


def stop_daemon(
    process: subprocess.Popen[bytes], stdout_path: Path, stderr_path: Path
) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=DAEMON_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=DAEMON_STOP_TIMEOUT)
    wait_for_bus_owner(process, False)
    for path in (stdout_path, stderr_path):
        if path.stat().st_size > MAX_COMMAND_OUTPUT:
            raise RuntimeError(f"OpenRazer daemon output exceeded its bound: {path.name}")
    if process.returncode not in (0, -15):
        detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4096:]
        raise RuntimeError(f"OpenRazer daemon failed: {detail}")


def device_map() -> dict[int, Any]:
    from openrazer.client import DeviceManager

    manager = DeviceManager()
    devices = {int(device._pid): device for device in manager.devices}
    if set(devices) not in ({MOUSE_PID, KEYBOARD_PID}, set()):
        raise RuntimeError("stock OpenRazer enumerated an unexpected device set")
    return devices


def assert_public_openrazer_controls(fake_root: Path) -> dict[str, Any]:
    devices = device_map()
    if set(devices) != {MOUSE_PID, KEYBOARD_PID}:
        raise RuntimeError("stock OpenRazer did not enumerate both assurance fixtures")
    mouse = devices[MOUSE_PID]
    keyboard = devices[KEYBOARD_PID]
    if mouse.name != MOUSE_NAME or keyboard.name != KEYBOARD_NAME:
        raise RuntimeError("stock OpenRazer presentation identity drifted")
    for capability in (
        "battery",
        "brightness",
        "dpi",
        "poll_rate",
        "scroll_mode",
        "scroll_acceleration",
        "scroll_smart_reel",
        "lighting_static",
    ):
        if not mouse.has(capability):
            raise RuntimeError(f"stock OpenRazer mouse lacks {capability}")
    for capability in (
        "battery",
        "brightness",
        "game_mode_led",
        "lighting_led_matrix",
        "lighting_static",
    ):
        if not keyboard.has(capability):
            raise RuntimeError(f"stock OpenRazer keyboard lacks {capability}")
    if mouse.battery_level != 100 or not mouse.is_charging:
        raise RuntimeError("stock OpenRazer mouse battery state is not source-derived")
    if keyboard.battery_level != 100 or not keyboard.is_charging:
        raise RuntimeError("stock OpenRazer keyboard battery state is not source-derived")

    mouse.dpi = (1200, 1200)
    mouse.poll_rate = 1000
    mouse.scroll_mode = 1
    mouse.scroll_acceleration = True
    mouse.scroll_smart_reel = True
    mouse.set_idle_time(900)
    mouse.set_low_battery_threshold(20)
    mouse.brightness = 25.0
    mouse.fx.static(17, 34, 51)
    keyboard.game_mode_led = True
    keyboard.brightness = 35.0
    keyboard.fx.static(68, 85, 102)

    mouse_root = fake_root / "0003:1532:00CD.0001"
    keyboard_root = fake_root / "0003:1532:0296.0001"
    if file_text(mouse_root / "dpi") != "1200:1200":
        raise RuntimeError("stock OpenRazer DPI write did not reach the driver ABI")
    if file_text(mouse_root / "poll_rate") != "1000":
        raise RuntimeError("stock OpenRazer poll-rate write did not reach the driver ABI")
    if file_text(mouse_root / "scroll_mode") != "1":
        raise RuntimeError("stock OpenRazer scroll-mode write did not reach the driver ABI")
    if file_text(mouse_root / "scroll_acceleration") != "1":
        raise RuntimeError("stock OpenRazer acceleration write did not reach the driver ABI")
    if file_text(mouse_root / "scroll_smart_reel") != "1":
        raise RuntimeError("stock OpenRazer smart-reel write did not reach the driver ABI")
    if mouse.get_idle_time() != 900 or mouse.get_low_battery_threshold() != 20:
        raise RuntimeError("stock OpenRazer battery policy did not round-trip")
    if not (mouse_root / "matrix_effect_static").read_bytes():
        raise RuntimeError("stock OpenRazer mouse lighting write was empty")
    if file_text(keyboard_root / "game_led_state") != "1":
        raise RuntimeError("stock OpenRazer keyboard game-mode write did not reach the driver ABI")
    if not (keyboard_root / "matrix_effect_static").read_bytes():
        raise RuntimeError("stock OpenRazer keyboard lighting write was empty")
    return {
        "device_count": 2,
        "product_ids": ["00cd", "0296"],
        "battery_percent": {"mouse": 100, "keyboard": 100},
        "writes": [
            "dpi",
            "poll_rate",
            "scroll_mode",
            "scroll_acceleration",
            "scroll_smart_reel",
            "idle_time",
            "low_battery_threshold",
            "brightness",
            "static_lighting",
            "game_mode",
        ],
    }


def assert_stock_polychromatic(root: Path, configuration: Path) -> dict[str, Any]:
    from polychromatic import common
    from polychromatic.backends.openrazer import OpenRazerBackend

    common.paths = SimpleNamespace(data_dir=str(root / "usr/share/polychromatic"))

    class Debug:
        debug = False

        @staticmethod
        def stdout(*_arguments: object) -> None:
            return None

    base = SimpleNamespace(
        _=lambda value: value,
        common=common,
        dbg=Debug(),
        paths=SimpleNamespace(config=str(configuration)),
    )
    backend = OpenRazerBackend(base)
    if backend.init() is not True:
        raise RuntimeError("stock Polychromatic could not initialize its OpenRazer backend")
    devices = backend.get_devices()
    by_pid = {int(device.pid, 16): device for device in devices}
    if set(by_pid) != {MOUSE_PID, KEYBOARD_PID}:
        raise RuntimeError("stock Polychromatic did not expose exactly both assurance fixtures")
    mouse = by_pid[MOUSE_PID]
    keyboard = by_pid[KEYBOARD_PID]
    if mouse.name != MOUSE_NAME or keyboard.name != KEYBOARD_NAME:
        raise RuntimeError("stock Polychromatic presentation identity drifted")
    mouse_options = {option.uid for zone in mouse.zones for option in zone.options}
    keyboard_options = {option.uid for zone in keyboard.zones for option in zone.options}
    required_mouse = {
        "brightness",
        "poll_rate",
        "scroll_mode",
        "scroll_accel",
        "scroll_smart_reel",
        "idle_time",
        "low_battery_threshold",
        "static",
    }
    required_keyboard = {"brightness", "game_mode", "static"}
    if not required_mouse.issubset(mouse_options) or mouse.dpi is None or mouse.battery is None:
        raise RuntimeError("stock Polychromatic omitted mouse controls")
    if not required_keyboard.issubset(keyboard_options) or keyboard.battery is None:
        raise RuntimeError("stock Polychromatic omitted keyboard controls")
    return {
        "device_count": 2,
        "backend": "polychromatic.backends.openrazer.OpenRazerBackend",
        "mouse_options": sorted(required_mouse),
        "keyboard_options": sorted(required_keyboard),
    }


def inside_case(arguments: argparse.Namespace) -> int:
    root = arguments.root.resolve()
    temporary = arguments.temporary.resolve()
    fake_root = temporary / "fake"
    fake_root.mkdir(mode=0o700)
    home = temporary / "home"
    poly_config = temporary / "polychromatic-config"
    for path in (home, poly_config):
        path.mkdir(mode=0o700)

    config_source = root / "usr/share/openrazer/razer.conf.example"
    config = configparser.ConfigParser()
    if not config.read(config_source):
        raise RuntimeError("stock OpenRazer daemon configuration is unavailable")
    config.set("Startup", "battery_notifier", "False")
    config.set("Startup", "devices_off_on_screensaver", "False")
    config_path = temporary / "razer.conf"
    with config_path.open("x", encoding="utf-8") as output:
        config.write(output)
    config_path.chmod(0o600)

    site_roots = sorted((root / "usr/lib").glob("python*/site-packages"))
    if len(site_roots) != 1:
        raise RuntimeError("stock Python package root is ambiguous")
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "LOGNAME": "root",
            "USER": "root",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(site_roots[0]),
            "XDG_CACHE_HOME": str(temporary / "cache"),
            "XDG_CONFIG_HOME": str(temporary / "config"),
        }
    )
    sys.path.insert(0, str(site_roots[0]))

    create_fake_devices(fake_root)
    daemon, stdout_path, stderr_path = start_daemon(
        root, environment, fake_root, temporary, "initial"
    )
    try:
        import openrazer
        import polychromatic
        from openrazer.client import DeviceManager

        if not Path(openrazer.__file__).resolve().is_relative_to(root):
            raise RuntimeError("OpenRazer client was not imported from the stock package")
        if not Path(polychromatic.__file__).resolve().is_relative_to(root):
            raise RuntimeError("Polychromatic was not imported from the stock package")
        manager = DeviceManager()
        if manager.version != arguments.expected_version:
            raise RuntimeError("stock OpenRazer client version drifted")
        if manager.daemon_version != arguments.expected_version:
            raise RuntimeError("stock OpenRazer daemon version drifted")
        public_api = assert_public_openrazer_controls(fake_root)
        polychromatic = assert_stock_polychromatic(root, poly_config)
    finally:
        stop_daemon(daemon, stdout_path, stderr_path)

    remove_fake_devices(fake_root)
    daemon, stdout_path, stderr_path = start_daemon(
        root, environment, fake_root, temporary, "disconnected"
    )
    try:
        if device_map():
            raise RuntimeError("stock OpenRazer retained devices after disconnect")
    finally:
        stop_daemon(daemon, stdout_path, stderr_path)

    create_fake_devices(fake_root)
    daemon, stdout_path, stderr_path = start_daemon(
        root, environment, fake_root, temporary, "reconnected"
    )
    try:
        if set(device_map()) != {MOUSE_PID, KEYBOARD_PID}:
            raise RuntimeError("stock OpenRazer did not re-enumerate both devices")
    finally:
        stop_daemon(daemon, stdout_path, stderr_path)

    write_json(
        arguments.inside_output,
        {
            "schema": SCHEMA,
            "case_id": arguments.case_id,
            "expected_version": arguments.expected_version,
            "public_openrazer": public_api,
            "stock_polychromatic": polychromatic,
            "reconnect_device_counts": [2, 0, 2],
            "private_hyperflux_client_required": False,
        },
    )
    return 0


def run_case(arguments: argparse.Namespace) -> int:
    packages = {
        "daemon": arguments.daemon_package.resolve(),
        "client": arguments.client_package.resolve(),
        "polychromatic": arguments.polychromatic_package.resolve(),
    }
    expected_names = {
        "daemon": "openrazer-daemon",
        "client": "python-openrazer",
        "polychromatic": "polychromatic",
    }
    metadata: dict[str, Any] = {}
    for role, path in packages.items():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"{role} package is not a regular file")
        info = package_info(path)
        if info.get("pkgname") != (expected_names[role],):
            raise RuntimeError(f"unexpected {role} package identity")
        metadata[role] = {
            "pkgname": info["pkgname"][0],
            "pkgver": info["pkgver"][0],
            "sha256": sha256_file(path),
        }
    if not metadata["daemon"]["pkgver"].startswith(arguments.expected_version + "-"):
        raise RuntimeError("daemon package version does not match the case")
    if not metadata["client"]["pkgver"].startswith(arguments.expected_version + "-"):
        raise RuntimeError("client package version does not match the case")
    if not metadata["polychromatic"]["pkgver"].startswith("0.9.7-"):
        raise RuntimeError("Polychromatic package is not stock 0.9.7")
    driver = validate_driver_source(arguments.driver_source_tar.resolve())

    with tempfile.TemporaryDirectory(prefix=f"hfx-stock-{arguments.case_id}-") as raw:
        temporary = Path(raw)
        root = temporary / "root"
        root.mkdir(mode=0o700)
        for path in packages.values():
            extract_package(path, root)
        inside_output = temporary / "inside-result.json"
        command = [
            "dbus-run-session",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--inside",
            "--case-id",
            arguments.case_id,
            "--expected-version",
            arguments.expected_version,
            "--root",
            str(root),
            "--temporary",
            str(temporary),
            "--inside-output",
            str(inside_output),
        ]
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CASE_TIMEOUT,
        )
        if (
            completed.returncode != 0
            or len(completed.stdout) > MAX_COMMAND_OUTPUT
            or len(completed.stderr) > MAX_COMMAND_OUTPUT
        ):
            detail = completed.stderr.decode("utf-8", errors="replace")[-4096:]
            raise RuntimeError(f"stock compatibility case failed: {detail}")
        result = json.loads(inside_output.read_text(encoding="utf-8"))
        if result.get("schema") != SCHEMA or result.get("case_id") != arguments.case_id:
            raise RuntimeError("inside compatibility result is invalid")
    result["packages"] = metadata
    result["patched_driver"] = driver
    write_json(arguments.output.resolve(), result)
    print(f"stock-application-compatibility={arguments.case_id}=PASS")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--inside", action="store_true")
    result.add_argument("--case-id", required=True)
    result.add_argument("--expected-version", required=True)
    result.add_argument("--root", type=Path)
    result.add_argument("--temporary", type=Path)
    result.add_argument("--inside-output", type=Path)
    result.add_argument("--daemon-package", type=Path)
    result.add_argument("--client-package", type=Path)
    result.add_argument("--polychromatic-package", type=Path)
    result.add_argument("--driver-source-tar", type=Path)
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    arguments = parser().parse_args()
    if arguments.inside:
        required = (arguments.root, arguments.temporary, arguments.inside_output)
        if any(value is None for value in required):
            raise RuntimeError("inside compatibility invocation is incomplete")
        return inside_case(arguments)
    required = (
        arguments.daemon_package,
        arguments.client_package,
        arguments.polychromatic_package,
        arguments.driver_source_tar,
        arguments.output,
    )
    if any(value is None for value in required):
        raise RuntimeError("compatibility package invocation is incomplete")
    return run_case(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
