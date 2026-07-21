# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from .development import load_development_environment
from .model import ModelError


IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?$"
)
REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ContainerInvocation:
    operation: str
    network: str
    command: tuple[str, ...]


def _engine(requested: str | None = None) -> str:
    value = requested or os.environ.get("HFX_OCI_ENGINE")
    if value is not None:
        if value not in {"docker", "podman"}:
            raise ModelError("HFX_OCI_ENGINE must be docker or podman")
        path = shutil.which(value)
        if path is None:
            raise ModelError(f"OCI engine is unavailable: {value}")
        return path
    for candidate in ("docker", "podman"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    raise ModelError("OCI engine is unavailable; install Docker or Podman")


def _image(value: str) -> str:
    if IMAGE.fullmatch(value) is None or ".." in value:
        raise ModelError("CI image name is not canonical")
    return value


def _relative_output(root: Path, value: Path, label: str) -> str:
    if value.is_absolute():
        raise ModelError(f"{label} must be relative to the repository")
    pure = PurePosixPath(value.as_posix())
    if ".." in pure.parts or not pure.parts or pure.parts[0] != "build":
        raise ModelError(f"{label} must stay below build/")
    resolved = root / pure
    if resolved.is_symlink():
        raise ModelError(f"{label} may not be a symlink")
    return pure.as_posix()


def _changed_revision(value: str | None) -> str | None:
    if value is None or value == "" or value == "0" * 40:
        return None
    if REVISION.fullmatch(value) is None:
        raise ModelError("changed-from must be an exact 40-character Git revision")
    return value


def container_invocation(
    root: Path,
    *,
    image: str,
    operation: str,
    output: Path | None = None,
    lane: str | None = None,
    changed_from: str | None = None,
    engine: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> ContainerInvocation:
    root = root.resolve()
    environment = load_development_environment(root)
    engine_path = _engine(engine)
    image = _image(image)
    revision = _changed_revision(changed_from)
    if operation == "prepare":
        if output is not None or lane is not None or revision is not None:
            raise ModelError("CI prepare accepts no lane, output, or changed revision")
        network = "bridge"
        hfx_command = ["./hfx", "upstream", "prepare", "--output", ".hfx/upstreams"]
    elif operation == "verify":
        if lane not in {"fast", "full"} or output is None:
            raise ModelError("CI verify requires a fast or full lane and an output")
        network = "none"
        hfx_command = [
            "./hfx",
            "verify",
            "--fast" if lane == "fast" else "--full",
            "--output",
            _relative_output(root, output, "CI evidence output"),
        ]
        if revision is not None:
            hfx_command.extend(["--changed-from", revision])
    elif operation == "docs":
        if output is None or lane is not None or revision is not None:
            raise ModelError("CI docs requires only an output")
        network = "none"
        relative = _relative_output(root, output, "CI documentation output")
        hfx_command = [
            "/bin/bash",
            "-lc",
            'install -d -m 0700 "$HOME" && ./hfx docs build --output "$1" && exec ./hfx docs verify --site "$1"',
            "hfx-docs",
            relative,
        ]
    else:
        raise ModelError(f"unsupported CI operation: {operation}")

    user_id = os.getuid() if uid is None else uid
    group_id = os.getgid() if gid is None else gid
    if user_id < 0 or group_id < 0:
        raise ModelError("CI container identity is invalid")
    command = [
        engine_path,
        "run",
        "--rm",
        "--network",
        network,
        "--user",
        f"{user_id}:{group_id}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "2048",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev",
        "--env",
        "HOME=/tmp/hfx-home",
        "--env",
        "CARGO_HOME=/opt/cargo",
        "--env",
        "RUSTUP_HOME=/opt/rustup",
        "--env",
        "CARGO_NET_OFFLINE=true",
        "--env",
        "PIP_NO_INDEX=1",
        "--volume",
        f"{root}:{environment.workspace_path}:rw",
        "--workdir",
        environment.workspace_path,
        image,
    ]
    if hfx_command and hfx_command[0] != "/bin/bash":
        command.extend(
            [
                "/bin/bash",
                "-lc",
                'install -d -m 0700 "$HOME" && exec "$@"',
                "hfx-ci",
                *hfx_command,
            ]
        )
    else:
        command.extend(hfx_command)
    return ContainerInvocation(operation, network, tuple(command))


def run_container(invocation: ContainerInvocation) -> int:
    try:
        return subprocess.run(invocation.command, check=False).returncode
    except OSError as error:
        raise ModelError(f"cannot execute OCI container: {error}") from error
