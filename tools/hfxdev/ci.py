# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from .development import (
    CARGO_CACHE_PATH,
    load_development_environment,
    networked_software_prepare_command,
)
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


def _linked_worktree_common_dir(root: Path) -> Path | None:
    marker = root / ".git"
    if marker.is_dir():
        return None
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise ModelError("linked worktree Git marker is invalid")
    lines = marker.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise ModelError("linked worktree Git marker is not canonical")
    git_dir = Path(lines[0].removeprefix("gitdir: "))
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    git_dir = Path(os.path.abspath(git_dir))
    common_marker = git_dir / "commondir"
    if (
        not git_dir.is_dir()
        or common_marker.is_symlink()
        or not common_marker.is_file()
        or common_marker.stat().st_size > 4096
    ):
        raise ModelError("linked worktree common Git directory is unavailable")
    common_lines = common_marker.read_text(encoding="utf-8").splitlines()
    if len(common_lines) != 1 or not common_lines[0]:
        raise ModelError("linked worktree common Git marker is not canonical")
    common_dir = Path(common_lines[0])
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    common_dir = Path(os.path.abspath(common_dir))
    if not common_dir.is_dir() or not git_dir.is_relative_to(common_dir):
        raise ModelError("linked worktree common Git directory is invalid")
    if ":" in str(common_dir):
        raise ModelError("linked worktree common Git path is not mount-safe")
    return common_dir


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
        hfx_command = [
            "/bin/bash",
            "-lc",
            'install -d -m 0700 "$HOME" "$CARGO_HOME" && '
            + networked_software_prepare_command(),
        ]
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
    linked_git = _linked_worktree_common_dir(root)
    command = [engine_path, "run"]
    if Path(engine_path).name == "podman":
        command.append("--userns=keep-id")
    command.extend(
        [
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
            f"USER={environment.workspace_user}",
            "--env",
            f"LOGNAME={environment.workspace_user}",
            "--env",
            f"CARGO_HOME={environment.workspace_path}/{CARGO_CACHE_PATH}",
            "--env",
            "RUSTUP_HOME=/opt/rustup",
            "--env",
            f"CARGO_NET_OFFLINE={'false' if operation == 'prepare' else 'true'}",
            "--env",
            "PIP_NO_INDEX=1",
            "--volume",
            f"{root}:{environment.workspace_path}:rw",
        ]
    )
    if linked_git is not None:
        command.extend(("--volume", f"{linked_git}:{linked_git}:ro"))
    command.extend(("--workdir", environment.workspace_path, image))
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
