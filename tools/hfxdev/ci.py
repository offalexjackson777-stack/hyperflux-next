# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import shutil
import subprocess
import tempfile
import time

from .development import (
    CARGO_CACHE_PATH,
    load_development_environment,
    networked_software_prepare_command,
)
from .model import ModelError


IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?$"
)
IMAGE_ID = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
PINNED_VERIFICATION_ENVIRONMENT = "HFX_PINNED_VERIFICATION_ENVIRONMENT"
MAX_IMAGE_PACKAGE_OUTPUT_BYTES = 65_536
MAX_IMAGE_ID_OUTPUT_BYTES = 1_024
MAX_IMAGE_IDENTITY_FILE_BYTES = 262_144
IMAGE_INSPECTION_TIMEOUT_SECONDS = 20
IMAGE_CLEANUP_TIMEOUT_SECONDS = 10


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
    system_packages: tuple[tuple[str, str], ...]


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
    openrgb_qualification: Path | None = None,
) -> ContainerInvocation:
    root = root.resolve()
    environment = load_development_environment(root)
    engine_path = _engine(engine)
    image = _image(image)
    revision = _changed_revision(changed_from)
    if operation == "prepare":
        if (
            output is not None
            or lane is not None
            or revision is not None
            or openrgb_qualification is not None
        ):
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
        if openrgb_qualification is not None:
            if lane != "full":
                raise ModelError(
                    "external OpenRGB qualification is valid only for the full lane"
                )
            qualification = _relative_output(
                root,
                openrgb_qualification,
                "external OpenRGB qualification binding",
            )
        else:
            qualification = None
    elif operation == "openrgb-qualification-prepare":
        if output is None or lane is not None or revision is not None:
            raise ModelError(
                "OpenRGB qualification preparation requires only an output"
            )
        if openrgb_qualification is not None:
            raise ModelError(
                "OpenRGB qualification preparation accepts no prior binding"
            )
        network = "none"
        qualification = None
        hfx_command = [
            "./hfx",
            "ci",
            "openrgb-prepare",
            "--output",
            _relative_output(root, output, "OpenRGB qualification output"),
        ]
    else:
        raise ModelError(f"unsupported CI operation: {operation}")

    user_id = os.getuid() if uid is None else uid
    group_id = os.getgid() if gid is None else gid
    if user_id <= 0 or group_id <= 0:
        raise ModelError("CI container identity must be unprivileged")
    linked_git = _linked_worktree_common_dir(root)
    command = [engine_path, "run", "--pull=never"]
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
    if operation in {"verify", "openrgb-qualification-prepare"}:
        command.extend(("--env", f"{PINNED_VERIFICATION_ENVIRONMENT}=1"))
    if operation == "verify" and qualification is not None:
        command.extend(
            (
                "--env",
                f"HFX_EXTERNAL_OPENRGB_QUALIFICATION_BINDING={qualification}",
            )
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
        tuple((package.name, package.version) for package in environment.packages),
    )


@dataclass(frozen=True)
class BoundedCommandResult:
    returncode: int
    stdout: str
    stderr: str


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as error:
        raise ModelError("bounded image inspection process could not be stopped") from error


def _bounded_command(
    command: list[str],
    *,
    label: str,
    max_output_bytes: int,
    timeout_seconds: float = IMAGE_INSPECTION_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> BoundedCommandResult:
    if max_output_bytes <= 0 or timeout_seconds <= 0:
        raise ModelError("bounded image inspection limits must be positive")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=None if environment is None else {**os.environ, **environment},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ModelError(f"cannot inspect {label}: {error}") from error
    if process.stdout is None or process.stderr is None:
        _stop_process(process)
        raise ModelError(f"cannot inspect {label}: output pipes are unavailable")

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    streams = (process.stdout, process.stderr)
    try:
        for name, stream in zip(outputs, streams, strict=True):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, name)
    except (OSError, ValueError) as error:
        selector.close()
        for stream in streams:
            stream.close()
        _stop_process(process)
        raise ModelError(f"cannot inspect {label}: output setup failed") from error
    deadline = time.monotonic() + timeout_seconds
    total = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise ModelError(f"cannot inspect {label}: command timed out")
            events = selector.select(remaining)
            if not events:
                _stop_process(process)
                raise ModelError(f"cannot inspect {label}: command timed out")
            for key, _mask in events:
                chunk = os.read(
                    key.fd,
                    min(8192, max_output_bytes + 1 - total),
                )
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                outputs[key.data].extend(chunk)
                total += len(chunk)
                if total > max_output_bytes:
                    _stop_process(process)
                    raise ModelError(f"{label} output exceeded its bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            raise ModelError(f"cannot inspect {label}: command timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _stop_process(process)
            raise ModelError(f"cannot inspect {label}: command timed out") from error
    finally:
        selector.close()
        for stream in streams:
            stream.close()

    try:
        stdout = outputs["stdout"].decode("utf-8")
        stderr = outputs["stderr"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ModelError(f"{label} output is not UTF-8") from error
    return BoundedCommandResult(returncode, stdout, stderr)


def _quiet_engine_command(command: list[str], label: str) -> int:
    try:
        return subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=IMAGE_CLEANUP_TIMEOUT_SECONDS,
        ).returncode
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot {label}: {error}") from error


def _remove_inspection_container(engine: str, name: str) -> None:
    engine_name = Path(engine).name
    if engine_name == "podman":
        remove = [engine, "rm", "--force", "--ignore", name]
        absent_status = 125
    elif engine_name == "docker":
        remove = [engine, "rm", "--force", name]
        absent_status = 1
    else:
        raise ModelError("inspection container cleanup engine is unsupported")
    _quiet_engine_command(remove, "remove bounded inspection container")
    status = _quiet_engine_command(
        [engine, "container", "inspect", name],
        "verify bounded inspection container cleanup",
    )
    if status == 0:
        raise ModelError("bounded inspection container remained after cleanup")
    if status != absent_status:
        raise ModelError("bounded inspection container cleanup could not be verified")


def _bounded_container_command(
    command: list[str],
    *,
    label: str,
    max_output_bytes: int,
    timeout_seconds: float = IMAGE_INSPECTION_TIMEOUT_SECONDS,
) -> BoundedCommandResult:
    if len(command) < 2 or command[1] != "run":
        raise ModelError("bounded inspection command must be an OCI run")
    name = f"hyperflux-ci-inspect-{os.getpid()}-{secrets.token_hex(12)}"
    named_command = [*command[:2], "--name", name, *command[2:]]
    try:
        return _bounded_command(
            named_command,
            label=label,
            max_output_bytes=max_output_bytes,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _remove_inspection_container(command[0], name)


def _resolve_image_identity(invocation: ContainerInvocation) -> str:
    result = _bounded_command(
        [
            invocation.engine,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            invocation.image,
        ],
        label="CI image identity",
        max_output_bytes=MAX_IMAGE_ID_OUTPUT_BYTES,
    )
    identity = result.stdout.strip()
    if result.returncode != 0 or IMAGE_ID.fullmatch(identity) is None:
        raise ModelError("CI image identity is unavailable or malformed")
    return identity


def _bind_image_identity(
    invocation: ContainerInvocation, image_identity: str
) -> ContainerInvocation:
    if IMAGE_ID.fullmatch(image_identity) is None:
        raise ModelError("CI image identity is not immutable")
    try:
        image_index = invocation.command.index(invocation.image)
    except ValueError as error:
        raise ModelError("CI invocation lost its requested image binding") from error
    command = list(invocation.command)
    command[image_index] = image_identity
    return replace(invocation, command=tuple(command), image=image_identity)


def _verify_image_package_inventory(invocation: ContainerInvocation) -> None:
    expected = dict(invocation.system_packages)
    if not expected or len(expected) != len(invocation.system_packages):
        raise ModelError("CI invocation has an invalid system package contract")
    command = [
        invocation.engine,
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "32",
        "--entrypoint",
        "/usr/bin/pacman",
        invocation.image,
        "-Q",
        *expected,
    ]
    result = _bounded_container_command(
        command,
        label="CI image system package inventory",
        max_output_bytes=MAX_IMAGE_PACKAGE_OUTPUT_BYTES,
    )
    if not result.stdout:
        raise ModelError("CI image system package inventory is empty")

    actual: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split(" ", 1)
        if len(fields) != 2 or not fields[0] or not fields[1]:
            raise ModelError("CI image system package inventory is malformed")
        name, version = fields
        if name in actual:
            raise ModelError("CI image system package inventory contains duplicates")
        actual[name] = version

    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    mismatched = sorted(
        name
        for name in set(expected) & set(actual)
        if expected[name] != actual[name]
    )
    if missing or unexpected or mismatched:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        if mismatched:
            details.append(f"version drift {', '.join(mismatched)}")
        raise ModelError(
            "CI image system packages do not match the source-bound environment: "
            + "; ".join(details)
        )
    if result.returncode != 0:
        raise ModelError(
            "CI image system package inventory is unavailable "
            f"(exit status {result.returncode})"
        )


def _read_image_identity_file(invocation: ContainerInvocation, path: str) -> str:
    command = [
        invocation.engine,
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--read-only",
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
    result = _bounded_container_command(
        command,
        label=f"CI image {path}",
        max_output_bytes=MAX_IMAGE_IDENTITY_FILE_BYTES,
    )
    if result.returncode != 0:
        raise ModelError(
            f"cannot read {path} from the pinned CI image "
            f"(exit status {result.returncode})"
        )
    if not result.stdout:
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


def _prepare_bound_invocation(invocation: ContainerInvocation) -> ContainerInvocation:
    image_identity = _resolve_image_identity(invocation)
    bound_invocation = _bind_image_identity(invocation, image_identity)
    _verify_image_package_inventory(bound_invocation)
    return bound_invocation


def _run_bound_container(bound_invocation: ContainerInvocation) -> int:
    user_database_source = _read_image_identity_file(
        bound_invocation, "/etc/passwd"
    )
    group_database_source = _read_image_identity_file(
        bound_invocation, "/etc/group"
    )
    user_database_projection = _project_user_database(
        user_database_source,
        username=bound_invocation.workspace_user,
        user_id=bound_invocation.user_id,
        group_id=bound_invocation.group_id,
    )
    group_database_projection = _project_group_database(
        group_database_source,
        username=bound_invocation.workspace_user,
        group_id=bound_invocation.group_id,
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
            bound_invocation,
            user_database=user_database,
            group_database=group_database,
        )
        try:
            return subprocess.run(command, check=False).returncode
        except OSError as error:
            raise ModelError(f"cannot execute OCI container: {error}") from error


def run_container(invocation: ContainerInvocation) -> int:
    return _run_bound_container(_prepare_bound_invocation(invocation))
