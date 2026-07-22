# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

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
    engine: str
    image: str
    user_id: int
    group_id: int
    workspace_user: str


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
    else:
        raise ModelError(f"unsupported CI operation: {operation}")

    user_id = os.getuid() if uid is None else uid
    group_id = os.getgid() if gid is None else gid
    if user_id <= 0 or group_id <= 0:
        raise ModelError("CI container identity must be unprivileged")
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
    return ContainerInvocation(
        operation,
        network,
        tuple(command),
        engine_path,
        image,
        user_id,
        group_id,
        environment.workspace_user,
    )


def _read_image_identity_file(invocation: ContainerInvocation, path: str) -> str:
    command = [
        invocation.engine,
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "32",
        "--entrypoint",
        "/bin/cat",
        invocation.image,
        path,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ModelError(f"cannot inspect CI image identity: {error}") from error
    if result.returncode != 0:
        raise ModelError(
            f"cannot read {path} from the pinned CI image "
            f"(exit status {result.returncode})"
        )
    if not result.stdout or len(result.stdout.encode("utf-8")) > 262_144:
        raise ModelError(f"CI image {path} is empty or unbounded")
    return result.stdout


def _project_user_database(
    source: str,
    *,
    username: str,
    user_id: int,
    group_id: int,
) -> str:
    records: list[list[str]] = []
    target: list[str] | None = None
    for line in source.splitlines():
        fields = line.split(":")
        if (
            len(fields) != 7
            or not fields[0]
            or not fields[2].isdigit()
            or not fields[3].isdigit()
        ):
            raise ModelError("CI image user account database is malformed")
        if fields[1] not in {"x", "*", "!", "!!"}:
            raise ModelError(
                "CI image user account database contains inline credential material"
            )
        fields[1] = "x"
        if fields[0] == username:
            if target is not None:
                raise ModelError("CI image contains duplicate workspace users")
            target = fields
        elif fields[2] == str(user_id):
            raise ModelError("host UID collides with a pinned CI image account")
        records.append(fields)
    if target is None:
        raise ModelError("CI image workspace user is missing from its account database")
    target[2] = str(user_id)
    target[3] = str(group_id)
    target[5] = "/tmp/hfx-home"
    return "\n".join(":".join(fields) for fields in records) + "\n"


def _project_group_database(
    source: str,
    *,
    username: str,
    group_id: int,
) -> str:
    records: list[list[str]] = []
    target: list[str] | None = None
    for line in source.splitlines():
        fields = line.split(":")
        if len(fields) != 4 or not fields[0] or not fields[2].isdigit():
            raise ModelError("CI image group database is malformed")
        if fields[0] == username:
            if target is not None:
                raise ModelError("CI image contains duplicate workspace groups")
            target = fields
        elif fields[2] == str(group_id):
            raise ModelError("host GID collides with a pinned CI image group")
        records.append(fields)
    if target is None:
        raise ModelError("CI image workspace group is missing")
    target[2] = str(group_id)
    return "\n".join(":".join(fields) for fields in records) + "\n"


def _runtime_command(
    invocation: ContainerInvocation,
    *,
    user_database: Path,
    group_database: Path,
) -> tuple[str, ...]:
    try:
        image_index = invocation.command.index(invocation.image)
    except ValueError as error:
        raise ModelError("CI invocation lost its pinned image binding") from error
    identity_mounts = (
        "--volume",
        f"{user_database}:/etc/passwd:ro",
        "--volume",
        f"{group_database}:/etc/group:ro",
    )
    return (
        *invocation.command[:image_index],
        *identity_mounts,
        *invocation.command[image_index:],
    )


def run_container(invocation: ContainerInvocation) -> int:
    user_database_source = _read_image_identity_file(invocation, "/etc/passwd")
    group_database_source = _read_image_identity_file(invocation, "/etc/group")
    user_database_projection = _project_user_database(
        user_database_source,
        username=invocation.workspace_user,
        user_id=invocation.user_id,
        group_id=invocation.group_id,
    )
    group_database_projection = _project_group_database(
        group_database_source,
        username=invocation.workspace_user,
        group_id=invocation.group_id,
    )
    with tempfile.TemporaryDirectory(prefix="hyperflux-ci-identity-") as temporary:
        identity_root = Path(temporary)
        user_database = identity_root / "user-database"
        group_database = identity_root / "group-database"
        user_database.write_text(user_database_projection, encoding="utf-8")
        group_database.write_text(group_database_projection, encoding="utf-8")
        user_database.chmod(0o444)
        group_database.chmod(0o444)
        command = _runtime_command(
            invocation,
            user_database=user_database,
            group_database=group_database,
        )
        try:
            return subprocess.run(command, check=False).returncode
        except OSError as error:
            raise ModelError(f"cannot execute OCI container: {error}") from error
