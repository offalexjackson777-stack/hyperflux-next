# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "integrations" / "openrazer" / "userspace-version-matrix.json"
CAPABILITY_PATH = (
    ROOT / "integrations" / "openrazer" / "userspace-capability-catalog.json"
)
EXPECTED_PATH = (
    ROOT / "integrations" / "openrazer" / "userspace-version-compatibility.json"
)
MAXIMUM_ARCHIVE_BYTES = 64 * 1024 * 1024
DAEMON_START_TIMEOUT_SECONDS = 15
CASE_TIMEOUT_SECONDS = 45


def _stage(value: str) -> None:
    print(f"HFX_OPENRAZER_MATRIX_STAGE {value}", file=sys.stderr, flush=True)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_package_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg == "version" and isinstance(keyword.value, ast.Constant):
                value = keyword.value.value
                if isinstance(value, str) and value:
                    return value
    raise RuntimeError(f"OpenRazer package version is not declaratively available: {path}")


def _source_archive(repository: Path, commit: str, destination: Path) -> None:
    result = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-2048:]
        raise RuntimeError(f"OpenRazer source commit {commit} is unavailable: {detail}")
    if not result.stdout or len(result.stdout) > MAXIMUM_ARCHIVE_BYTES:
        raise RuntimeError(f"OpenRazer source archive is empty or unbounded: {commit}")
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def _bounded_log(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-4096:]


def _wait_for_owner(bus: Any, process: subprocess.Popen[Any]) -> str:
    deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        owner = bus.owner("org.razer")
        if owner is not None:
            return owner
        if process.poll() is not None:
            raise RuntimeError(f"OpenRazer daemon exited with status {process.returncode}")
        time.sleep(0.05)
    raise RuntimeError("OpenRazer daemon did not own org.razer within the bound")


def _wait_for_line(process: subprocess.Popen[str], expected: str) -> None:
    if process.stdout is None:
        raise RuntimeError("bounded helper readiness pipe is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"bounded helper exited with status {process.returncode}")
            if selector.select(max(0.0, deadline - time.monotonic())):
                if process.stdout.readline().strip() == expected:
                    return
    finally:
        selector.close()
    raise RuntimeError("bounded helper did not become ready")


def _notification_stub() -> int:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(
        "org.freedesktop.Notifications",
        bus=bus,
        allow_replacement=False,
        replace_existing=False,
        do_not_queue=True,
    )

    class Notifications(dbus.service.Object):
        @dbus.service.method(
            "org.freedesktop.Notifications",
            in_signature="susssasa{sv}i",
            out_signature="u",
        )
        def Notify(
            self,
            _application: str,
            _replaces: int,
            _icon: str,
            _summary: str,
            _body: str,
            _actions: list[str],
            _hints: dict[str, Any],
            _timeout: int,
        ) -> int:
            return 1

        @dbus.service.method(
            "org.freedesktop.Notifications", in_signature="u", out_signature=""
        )
        def CloseNotification(self, _notification: int) -> None:
            return None

        @dbus.service.method(
            "org.freedesktop.Notifications", in_signature="", out_signature="as"
        )
        def GetCapabilities(self) -> list[str]:
            return []

        @dbus.service.method(
            "org.freedesktop.Notifications", in_signature="", out_signature="ssss"
        )
        def GetServerInformation(self) -> tuple[str, str, str, str]:
            return ("HyperFlux test stub", "HyperFlux", "1", "1.2")

    service = Notifications(bus, "/org/freedesktop/Notifications")
    print("HFX_NOTIFICATION_STUB_READY", flush=True)
    try:
        GLib.MainLoop().run()
    finally:
        service.remove_from_connection()
        del name
    return 0


def _case_result(case: dict[str, Any], source: Path) -> dict[str, Any]:
    _stage(f"{case['id']} source-import")
    sys.path.insert(0, str(source / "pylib"))
    sys.path.insert(1, str(source / "daemon"))
    sys.path.insert(2, str(ROOT / "tests"))
    from openrazer_public_api import DBusPythonBus, probe_capabilities
    from openrazer._fake_driver import FakeDevice

    client_declared_version = _declared_package_version(source / "pylib" / "setup.py")
    daemon_declared_version = _declared_package_version(source / "daemon" / "setup.py")
    if client_declared_version != daemon_declared_version:
        raise RuntimeError(
            f"{case['id']} daemon/client source versions are not a matched pair"
        )

    source_presence: dict[str, bool] = {}
    for device in case["devices"]:
        config = source / "pylib" / "openrazer" / "_fake_driver" / (
            device["fake_driver"] + ".cfg"
        )
        present = config.is_file()
        source_presence[device["profile_id"]] = present
        if present != (device["definition"] == "present"):
            raise RuntimeError(
                f"{case['id']} {device['profile_id']} source-definition boundary drifted"
            )

    with TemporaryDirectory(prefix=f"hfx-{case['id']}-") as raw_directory:
        temporary = Path(raw_directory)
        fake_root = temporary / "fake"
        run_root = temporary / "run"
        log_root = temporary / "log"
        home_root = temporary / "home"
        for path in (fake_root, run_root, log_root, home_root):
            path.mkdir(mode=0o700)
        fake_devices = [
            FakeDevice(device["fake_driver"], tmp_dir=str(fake_root))
            for device in case["devices"]
            if source_presence[device["profile_id"]]
        ]
        _stage(f"{case['id']} fake-devices-ready")
        stdout_path = temporary / "daemon.stdout"
        stderr_path = temporary / "daemon.stderr"
        config_path = temporary / "razer.conf"
        config = configparser.ConfigParser()
        if not config.read(source / "daemon" / "resources" / "razer.conf"):
            raise RuntimeError(f"{case['id']} default daemon configuration is unavailable")
        config.set("Startup", "battery_notifier", "False")
        config.set("Startup", "devices_off_on_screensaver", "False")
        with config_path.open("w", encoding="utf-8") as output:
            config.write(output)
        config_path.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home_root),
                # The official fake-driver lane has no system group. This bypasses
                # only OpenRazer's initial plugdev membership guard; endpoint files
                # remain owned by the unprivileged test process.
                "LOGNAME": "root",
                "USER": "root",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join(
                    (str(source / "pylib"), str(source / "daemon"))
                ),
            }
        )
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            notification = subprocess.Popen(
                [sys.executable, __file__, "--notification-stub"],
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                bufsize=1,
            )
            _wait_for_line(notification, "HFX_NOTIFICATION_STUB_READY")
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    str(source / "daemon" / "run_openrazer_daemon.py"),
                    "-F",
                    "--run-dir",
                    str(run_root),
                    "--log-dir",
                    str(log_root),
                    "--test-dir",
                    str(fake_root),
                    "--config",
                    str(config_path),
                ],
                env=environment,
                stdout=stdout,
                stderr=stderr,
            )
            try:
                bus = DBusPythonBus()
                owner = _wait_for_owner(bus, daemon)
                _stage(f"{case['id']} daemon-owner-ready")
                import openrazer
                from openrazer.client import DeviceManager

                client_source = Path(openrazer.__file__).resolve()
                if not client_source.is_relative_to((source / "pylib").resolve()):
                    raise RuntimeError("OpenRazer client was not imported from the matched source")
                manager = DeviceManager()
                _stage(f"{case['id']} matched-client-enumerated")
                client_devices = {
                    (int(device._vid), int(device._pid)): device
                    for device in manager.devices
                }
                capability = probe_capabilities(bus)
                _stage(f"{case['id']} capability-probe-complete")
                capability_devices = {
                    (device["vendor_id"], device["product_id"]): device
                    for device in capability["devices"]
                    if device["profile_id"] is not None
                }
                catalog = _load(CAPABILITY_PATH)
                identity_by_profile = {
                    device["profile_id"]: device["identity"]
                    for device in catalog["devices"]
                }
                results: list[dict[str, Any]] = []
                for selected in case["devices"]:
                    identity = identity_by_profile[selected["profile_id"]]
                    key = (identity["vendor_id"], identity["product_id"])
                    client_device = client_devices.get(key)
                    device_capability = capability_devices.get(key)
                    if selected["definition"] == "absent":
                        state = "not-recognized-by-release"
                    elif client_device is None or device_capability is None:
                        state = "not-enumerated"
                    else:
                        state = device_capability["state"]
                    results.append(
                        {
                            "profile_id": selected["profile_id"],
                            "device_kind": identity["device_kind"],
                            "definition": selected["definition"],
                            "client_enumerated": client_device is not None,
                            "daemon_enumerated": device_capability is not None,
                            "state": state,
                            "required_problems": (
                                device_capability["required_problems"]
                                if device_capability is not None
                                else []
                            ),
                            "features": (
                                [
                                    {
                                        "id": feature["id"],
                                        "requirement": feature["requirement"],
                                        "state": feature["state"],
                                    }
                                    for feature in device_capability["features"]
                                ]
                                if device_capability is not None
                                else []
                            ),
                            "semantic_probes": (
                                device_capability["semantic_probes"]
                                if device_capability is not None
                                else []
                            ),
                        }
                    )
                return {
                    "id": case["id"],
                    "display_version": case["display_version"],
                    "channel": case["channel"],
                    "commit": case["commit"],
                    "matched_source_pair": True,
                    "client_declared_version": client_declared_version,
                    "daemon_declared_version": daemon_declared_version,
                    "daemon_owner_acquired": owner.startswith(":"),
                    "reported_version": capability["observed_version"],
                    "version_probe_state": capability["version_probe_state"],
                    "overall_state": capability["state"],
                    "devices": results,
                }
            except Exception as error:
                raise RuntimeError(
                    f"{case['id']} failed: {error}\n"
                    f"stdout:\n{_bounded_log(stdout_path)}\n"
                    f"stderr:\n{_bounded_log(stderr_path)}"
                ) from error
            finally:
                _stage(f"{case['id']} cleanup-start")
                if daemon.poll() is None:
                    daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                for device in fake_devices:
                    device.close()
                if notification.poll() is None:
                    notification.terminate()
                try:
                    notification.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    notification.kill()
                    notification.wait(timeout=5)
                if notification.stdout is not None:
                    notification.stdout.close()
                _stage(f"{case['id']} cleanup-complete")


def _run_inner(arguments: argparse.Namespace) -> int:
    matrix = _load(MATRIX_PATH)
    case = next((value for value in matrix["cases"] if value["id"] == arguments.case), None)
    if case is None:
        raise RuntimeError(f"unknown OpenRazer matrix case: {arguments.case}")
    print(json.dumps(_case_result(case, arguments.source), sort_keys=True))
    return 0


def build_matrix(source_repository: Path) -> dict[str, Any]:
    matrix = _load(MATRIX_PATH)
    results: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="hfx-openrazer-matrix-") as raw_directory:
        temporary = Path(raw_directory)
        for case in matrix["cases"]:
            source = temporary / case["id"]
            _source_archive(source_repository, case["commit"], source)
            environment = os.environ.copy()
            python_paths = [
                str(ROOT / "integrations" / "openrazer" / "compatibility"),
                str(ROOT / "sdk" / "python"),
            ]
            environment["PYTHONPATH"] = os.pathsep.join(python_paths)
            environment["PYTHONNOUSERSITE"] = "1"
            command = [
                "dbus-run-session",
                "--",
                sys.executable,
                __file__,
                "--case",
                case["id"],
                "--source",
                str(source),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=CASE_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                detail = error.stderr or ""
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"{case['id']} exceeded {CASE_TIMEOUT_SECONDS}s:\n{detail[-4096:]}"
                ) from error
            if completed.returncode != 0:
                raise RuntimeError(
                    f"{case['id']} matrix subprocess failed ({completed.returncode}): "
                    + completed.stderr[-4096:]
                )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if len(lines) != 1:
                raise RuntimeError(f"{case['id']} emitted an invalid bounded result")
            result = json.loads(lines[0])
            if not isinstance(result, dict):
                raise RuntimeError(f"{case['id']} did not emit an object")
            results.append(result)
    return {
        "$schema": "../../schemas/openrazer-userspace-version-compatibility.schema.json",
        "schema": "hyperflux-openrazer-userspace-version-compatibility-v1",
        "matrix_sha256": _sha256(MATRIX_PATH),
        "capability_catalog_sha256": _sha256(CAPABILITY_PATH),
        "network_access_executed": False,
        "hardware_queried": False,
        "hardware_write_executed": False,
        "cases": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repository", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--notification-stub", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.notification_stub:
        return _notification_stub()
    if arguments.case is not None:
        if arguments.source is None:
            raise RuntimeError("matrix case source is required")
        return _run_inner(arguments)
    if arguments.source_repository is None:
        raise RuntimeError("--source-repository is required")
    source_repository = arguments.source_repository.resolve()
    result = build_matrix(source_repository)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if arguments.verify:
        expected = _load(EXPECTED_PATH)
        if result != expected:
            raise RuntimeError(
                "committed OpenRazer userspace version compatibility is stale"
            )
    print(
        "OpenRazer matched userspace matrix: "
        + ", ".join(case["display_version"] for case in result["cases"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
