# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import time

from .contract import ISOLATED_SESSION_MARKER


READY_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 5.0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        prog="hyperflux-openrazer-session",
        description="Run one legacy OpenRazer client in an isolated HyperFlux D-Bus session.",
    )
    value.add_argument("--socket", type=Path, help="override the local HyperFlux SDK socket")
    value.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("command", nargs=argparse.REMAINDER)
    return value


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    command = list(options.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser().error("a legacy client command is required after --")
    if options.inside:
        return _inside_session(command, options.socket)
    executable = shutil.which("dbus-run-session")
    if executable is None:
        print("dbus-run-session is required for isolated org.razer compatibility", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment[ISOLATED_SESSION_MARKER] = "1"
    child = [
        executable,
        "--",
        sys.executable,
        "-m",
        "hyperflux_openrazer_compat.session",
        "--inside",
    ]
    if options.socket is not None:
        child.extend(("--socket", str(options.socket)))
    child.extend(("--", *command))
    return subprocess.run(child, env=environment, check=False).returncode


def _inside_session(command: list[str], socket_path: Path | None) -> int:
    if os.environ.get(ISOLATED_SESSION_MARKER) != "1" or not os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS"
    ):
        print("isolated OpenRazer supervisor was started outside dbus-run-session", file=sys.stderr)
        return 1
    service_command = [
        sys.executable,
        "-m",
        "hyperflux_openrazer_compat.cli",
        "--identity",
        "org-razer-private-session",
    ]
    if socket_path is not None:
        service_command.extend(("--socket", str(socket_path)))
    service = subprocess.Popen(
        service_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        ready = _read_ready(service)
        if (
            ready.get("schema") != "hyperflux-openrazer-ready-v1"
            or ready.get("identity_mode") != "org-razer-private-session"
            or ready.get("bus_name") != "org.razer"
        ):
            raise RuntimeError("compatibility provider returned an invalid readiness record")
        return subprocess.run(command, check=False).returncode
    except BaseException as error:
        print(f"HyperFlux private OpenRazer session failed: {error}", file=sys.stderr)
        return 1
    finally:
        _terminate(service)


def _read_ready(process: subprocess.Popen[str]) -> dict[str, object]:
    if process.stdout is None:
        raise RuntimeError("compatibility provider readiness pipe is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"compatibility provider exited before readiness ({process.returncode})"
                )
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(remaining):
                continue
            line = process.stdout.readline()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RuntimeError("compatibility provider readiness is not an object")
            return value
    finally:
        selector.close()
    raise RuntimeError("compatibility provider did not become ready in time")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    if process.stdout is not None:
        process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
