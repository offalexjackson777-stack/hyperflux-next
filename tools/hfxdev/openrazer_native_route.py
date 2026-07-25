# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import uuid
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique, sha256_file


TRANSFORM_SCHEMA = "hyperflux-openrazer-native-transform-v1"
DEPLOYMENT_SCHEMA = "hyperflux-openrazer-native-deployment-v1"
TRANSFORM_KEYS = {"$schema", "schema", "upstream", "patch", "files"}
DEPLOYMENT_KEYS = {"$schema", "schema", "relay", "driver"}
RELAY_KEYS = {"executable_path", "service_unit", "uhid_device", "restart_seconds"}
DRIVER_KEYS = {"dkms_name", "dkms_version", "source_directory", "modules"}
UPSTREAM_KEYS = {"id", "repository", "commit"}
PATCH_KEYS = {"path", "sha256"}
FILE_KEYS = {"path", "source_sha256", "transformed_sha256"}


@dataclass(frozen=True)
class PreparedOpenRazerNativeSource:
    root: Path
    result: Path
    source_commit: str
    patch_sha256: str


@dataclass(frozen=True)
class OpenRazerRelayDeployment:
    executable_path: str
    service_unit: str
    uhid_device: str
    restart_seconds: int


@dataclass(frozen=True)
class OpenRazerDriverDeployment:
    dkms_name: str
    dkms_version: str
    source_directory: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class OpenRazerNativeDeployment:
    source_sha256: str
    relay: OpenRazerRelayDeployment
    driver: OpenRazerDriverDeployment


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _safe_relative_path(value: Any, label: str, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ModelError(f"{label}: expected a bounded non-empty path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ModelError(f"{label}: path must be canonical and repository-relative")
    if suffix is not None and path.suffix != suffix:
        raise ModelError(f"{label}: expected a {suffix} file")
    return path


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelError(f"{label}: expected a lowercase SHA-256 digest")
    return value


def load_native_transform(root: Path) -> dict[str, Any]:
    transform = load_json(
        root / "integrations" / "openrazer" / "native-route" / "transform.json"
    )
    _exact_keys(transform, TRANSFORM_KEYS, "OpenRazer native transform")
    if transform["$schema"] != "../../../schemas/openrazer-native-transform.schema.json":
        raise ModelError("OpenRazer native transform schema reference is not canonical")
    if transform["schema"] != TRANSFORM_SCHEMA:
        raise ModelError("unsupported OpenRazer native transform schema")

    upstream = transform["upstream"]
    if not isinstance(upstream, dict):
        raise ModelError("OpenRazer native transform upstream must be an object")
    _exact_keys(upstream, UPSTREAM_KEYS, "OpenRazer native transform upstream")
    authority = upstream_index(root).get("openrazer")
    if authority is None or any(
        upstream[field] != authority[field] for field in ("id", "repository", "commit")
    ):
        raise ModelError("OpenRazer native transform differs from the integration pin")

    patch = transform["patch"]
    if not isinstance(patch, dict):
        raise ModelError("OpenRazer native transform patch must be an object")
    _exact_keys(patch, PATCH_KEYS, "OpenRazer native transform patch")
    patch_path = _safe_relative_path(patch["path"], "OpenRazer native patch", suffix=".patch")
    if not patch_path.parts or patch_path.parts[0] != "patches":
        raise ModelError("OpenRazer native patch must remain below patches/")
    _digest(patch["sha256"], "OpenRazer native patch digest")

    files = transform["files"]
    if not isinstance(files, list) or not files or len(files) > 32:
        raise ModelError("OpenRazer native transform files must be a bounded non-empty list")
    paths: list[str] = []
    for index, record in enumerate(files):
        if not isinstance(record, dict):
            raise ModelError(f"OpenRazer native transform file {index}: expected an object")
        _exact_keys(record, FILE_KEYS, f"OpenRazer native transform file {index}")
        path = _safe_relative_path(record["path"], f"OpenRazer native transform file {index}")
        if not path.parts or path.parts[0] != "driver":
            raise ModelError(f"OpenRazer native transform file {index}: path must remain below driver/")
        _digest(record["source_sha256"], f"OpenRazer native transform file {index} source")
        _digest(
            record["transformed_sha256"],
            f"OpenRazer native transform file {index} transformed",
        )
        paths.append(path.as_posix())
    require_unique(paths, "OpenRazer native transform path")
    if paths != sorted(paths):
        raise ModelError("OpenRazer native transform files must be sorted by path")
    return transform


def load_native_deployment(root: Path) -> OpenRazerNativeDeployment:
    path = root / "integrations" / "openrazer" / "native-route" / "deployment.json"
    value = load_json(path)
    _exact_keys(value, DEPLOYMENT_KEYS, "OpenRazer native deployment")
    if value["$schema"] != "../../../schemas/openrazer-native-deployment.schema.json":
        raise ModelError("OpenRazer native deployment schema reference is not canonical")
    if value["schema"] != DEPLOYMENT_SCHEMA:
        raise ModelError("unsupported OpenRazer native deployment schema")

    relay = value["relay"]
    if not isinstance(relay, dict):
        raise ModelError("OpenRazer native relay deployment must be an object")
    _exact_keys(relay, RELAY_KEYS, "OpenRazer native relay deployment")
    executable = _deployment_path(
        relay["executable_path"],
        "OpenRazer native relay executable",
        ("/", "usr", "lib", "hyperflux-next"),
    )
    service = relay["service_unit"]
    if not isinstance(service, str) or re.fullmatch(
        r"[a-z][a-z0-9@_.-]{0,95}\.service", service
    ) is None:
        raise ModelError("OpenRazer native relay service unit is invalid")
    if relay["uhid_device"] != "/dev/uhid":
        raise ModelError("OpenRazer native relay must use the kernel UHID device")
    restart_seconds = relay["restart_seconds"]
    if (
        isinstance(restart_seconds, bool)
        or not isinstance(restart_seconds, int)
        or not 1 <= restart_seconds <= 60
    ):
        raise ModelError("OpenRazer native relay restart delay is invalid")

    driver = value["driver"]
    if not isinstance(driver, dict):
        raise ModelError("OpenRazer native driver deployment must be an object")
    _exact_keys(driver, DRIVER_KEYS, "OpenRazer native driver deployment")
    if driver["dkms_name"] != "openrazer-driver":
        raise ModelError("OpenRazer native DKMS name must preserve the upstream identity")
    version = driver["dkms_version"]
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ModelError("OpenRazer native DKMS version is invalid")
    upstream = upstream_index(root).get("openrazer")
    if upstream is None or not str(upstream["version"]).startswith(version):
        raise ModelError("OpenRazer native DKMS version differs from the pinned upstream")
    source_directory = _deployment_path(
        driver["source_directory"],
        "OpenRazer native DKMS source directory",
        ("/", "usr", "src"),
    )
    if source_directory != f"/usr/src/openrazer-driver-{version}":
        raise ModelError("OpenRazer native DKMS source directory does not derive from its identity")
    modules = driver["modules"]
    if (
        not isinstance(modules, list)
        or not 1 <= len(modules) <= 16
        or modules != sorted(modules)
        or len(set(modules)) != len(modules)
        or any(
            not isinstance(module, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", module) is None
            for module in modules
        )
    ):
        raise ModelError("OpenRazer native DKMS module inventory is invalid")
    return OpenRazerNativeDeployment(
        source_sha256=sha256_file(path),
        relay=OpenRazerRelayDeployment(
            executable_path=executable,
            service_unit=service,
            uhid_device=relay["uhid_device"],
            restart_seconds=restart_seconds,
        ),
        driver=OpenRazerDriverDeployment(
            dkms_name=driver["dkms_name"],
            dkms_version=version,
            source_directory=source_directory,
            modules=tuple(modules),
        ),
    )


def _deployment_path(value: Any, label: str, prefix: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value != value.strip():
        raise ModelError(f"{label}: must be an absolute path")
    path = PurePosixPath(value)
    if path.parts[: len(prefix)] != prefix or ".." in path.parts or str(path) != value:
        raise ModelError(f"{label}: path is outside its deployment boundary")
    if len(value.encode()) > 240 or any(
        ord(character) < 32 or ord(character) > 126 for character in value
    ):
        raise ModelError(f"{label}: path is not bounded printable ASCII")
    return value


def _git(arguments: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect OpenRazer source: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ModelError(f"git {' '.join(arguments)} failed{suffix}")
    return result.stdout.strip()


def _validate_source(root: Path, source: Path, transform: dict[str, Any]) -> None:
    if source.is_symlink() or not source.is_dir() or (source / ".git").is_symlink():
        raise ModelError("OpenRazer native source is missing or uses a symbolic link")
    if _git(["rev-parse", "--is-inside-work-tree"], cwd=source) != "true":
        raise ModelError("OpenRazer native source is not a Git worktree")
    if _git(["rev-parse", "HEAD"], cwd=source) != transform["upstream"]["commit"]:
        raise ModelError("OpenRazer native source does not match the pinned commit")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=source):
        raise ModelError("OpenRazer native source has local modifications")
    if _git(["remote", "get-url", "origin"], cwd=source) != transform["upstream"]["repository"]:
        raise ModelError("OpenRazer native source has an unexpected origin")

    native_root = root / "integrations" / "openrazer" / "native-route"
    patch = native_root / transform["patch"]["path"]
    if patch.is_symlink() or not patch.is_file():
        raise ModelError("OpenRazer native patch is missing or symbolic")
    if sha256_file(patch) != transform["patch"]["sha256"]:
        raise ModelError("OpenRazer native patch differs from its source lock")

    for record in transform["files"]:
        path = source / record["path"]
        if path.is_symlink() or not path.is_file():
            raise ModelError(f"OpenRazer native source file is missing or symbolic: {record['path']}")
        if sha256_file(path) != record["source_sha256"]:
            raise ModelError(f"OpenRazer native source drift: {record['path']}")


def _apply_patch(destination: Path, patch: Path) -> None:
    for check_only in (True, False):
        arguments = [
            "patch",
            "--batch",
            "--forward",
            "--fuzz=0",
            "--no-backup-if-mismatch",
            "-p1",
            "-i",
            str(patch),
        ]
        if check_only:
            arguments.insert(1, "--dry-run")
        try:
            result = subprocess.run(
                arguments,
                cwd=destination,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot apply OpenRazer native patch: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise ModelError(f"OpenRazer native patch did not apply exactly{suffix}")


def prepare_openrazer_native_source(
    root: Path, source: Path, output: Path
) -> PreparedOpenRazerNativeSource:
    root = root.resolve()
    source = source.expanduser().resolve()
    output = output.expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise ModelError("OpenRazer native output must not already exist")
    if output == source or source in output.parents or output in source.parents:
        raise ModelError("OpenRazer native output must be independent from the source checkout")

    transform = load_native_transform(root)
    _validate_source(root, source, transform)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.parent / f".{output.name}.prepare-{uuid.uuid4().hex}"
    native_root = root / "integrations" / "openrazer" / "native-route"
    patch = native_root / transform["patch"]["path"]
    try:
        shutil.copytree(
            source,
            temporary,
            symlinks=False,
            ignore=shutil.ignore_patterns(".git", "*.o", "*.ko", "*.mod", "*.mod.c"),
        )
        _apply_patch(temporary, patch)
        for record in transform["files"]:
            path = temporary / record["path"]
            if sha256_file(path) != record["transformed_sha256"]:
                raise ModelError(f"OpenRazer native transformed output drift: {record['path']}")

        result_path = temporary / "hyperflux-native-transform-result.json"
        result = {
            "schema": "hyperflux-openrazer-native-transform-result-v1",
            "source_commit": transform["upstream"]["commit"],
            "patch_sha256": transform["patch"]["sha256"],
            "transformed_files": [
                {
                    "path": record["path"],
                    "sha256": record["transformed_sha256"],
                }
                for record in transform["files"]
            ],
            "network_access_executed": False,
            "hardware_queried": False,
            "hardware_write_executed": False,
        }
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result_path.chmod(0o600)
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PreparedOpenRazerNativeSource(
        root=output,
        result=output / "hyperflux-native-transform-result.json",
        source_commit=transform["upstream"]["commit"],
        patch_sha256=transform["patch"]["sha256"],
    )
