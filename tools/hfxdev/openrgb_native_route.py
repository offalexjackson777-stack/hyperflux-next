# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

from .integrations import upstream_index
from .model import ModelError, load_json, require_unique, sha256_file


TRANSFORM_SCHEMA = "hyperflux-openrgb-native-transform-v1"
TRANSFORM_KEYS = {"$schema", "schema", "upstream", "patch", "files"}
UPSTREAM_KEYS = {"id", "repository", "commit"}
PATCH_KEYS = {"path", "sha256"}
FILE_KEYS = {"path", "source_sha256", "transformed_sha256"}


@dataclass(frozen=True)
class PreparedOpenRgbNativeSource:
    root: Path
    result: Path
    source_commit: str
    patch_sha256: str


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelError(f"{label}: expected a lowercase SHA-256 digest")
    return value


def _safe_path(value: Any, label: str, *, patch: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ModelError(f"{label}: expected a bounded non-empty path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ModelError(f"{label}: path must be canonical and repository-relative")
    if patch:
        if path.suffix != ".patch" or path.parts[0] != "patches":
            raise ModelError(f"{label}: patch must remain below patches/")
    elif (
        path.as_posix()
        not in {"RGBController/RGBController.cpp", "RGBController/RGBController.h"}
        and path.parts[:3] != ("Controllers", "RazerController", "RazerController")
    ):
        raise ModelError(f"{label}: path leaves the pinned Razer controller boundary")
    return path


def load_native_transform(root: Path) -> dict[str, Any]:
    value = load_json(
        root / "integrations" / "openrgb" / "native-route" / "transform.json"
    )
    _exact_keys(value, TRANSFORM_KEYS, "OpenRGB native transform")
    if value["$schema"] != "../../../schemas/openrgb-native-transform.schema.json":
        raise ModelError("OpenRGB native transform schema reference is not canonical")
    if value["schema"] != TRANSFORM_SCHEMA:
        raise ModelError("unsupported OpenRGB native transform schema")

    upstream = value["upstream"]
    if not isinstance(upstream, dict):
        raise ModelError("OpenRGB native transform upstream must be an object")
    _exact_keys(upstream, UPSTREAM_KEYS, "OpenRGB native transform upstream")
    authority = upstream_index(root).get("openrgb")
    if authority is None or any(
        upstream[field] != authority[field] for field in ("id", "repository", "commit")
    ):
        raise ModelError("OpenRGB native transform differs from the integration pin")

    patch = value["patch"]
    if not isinstance(patch, dict):
        raise ModelError("OpenRGB native transform patch must be an object")
    _exact_keys(patch, PATCH_KEYS, "OpenRGB native transform patch")
    _safe_path(patch["path"], "OpenRGB native patch", patch=True)
    _digest(patch["sha256"], "OpenRGB native patch digest")

    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ModelError("OpenRGB native transform files must be a bounded non-empty list")
    paths: list[str] = []
    for index, record in enumerate(files):
        if not isinstance(record, dict):
            raise ModelError(f"OpenRGB native transform file {index}: expected an object")
        _exact_keys(record, FILE_KEYS, f"OpenRGB native transform file {index}")
        path = _safe_path(record["path"], f"OpenRGB native transform file {index}")
        _digest(record["source_sha256"], f"OpenRGB native file {index} source")
        _digest(record["transformed_sha256"], f"OpenRGB native file {index} transformed")
        paths.append(path.as_posix())
    require_unique(paths, "OpenRGB native transform path")
    if paths != sorted(paths):
        raise ModelError("OpenRGB native transform files must be sorted by path")
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
        raise ModelError(f"cannot inspect OpenRGB source: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ModelError(f"git {' '.join(arguments)} failed{suffix}")
    return result.stdout.strip()


def _validate_source(root: Path, source: Path, transform: dict[str, Any]) -> None:
    if source.is_symlink() or not source.is_dir() or (source / ".git").is_symlink():
        raise ModelError("OpenRGB native source is missing or uses a symbolic link")
    if _git(["rev-parse", "--is-inside-work-tree"], cwd=source) != "true":
        raise ModelError("OpenRGB native source is not a Git worktree")
    if _git(["rev-parse", "HEAD"], cwd=source) != transform["upstream"]["commit"]:
        raise ModelError("OpenRGB native source does not match the pinned commit")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=source):
        raise ModelError("OpenRGB native source has local modifications")
    if _git(["remote", "get-url", "origin"], cwd=source) != transform["upstream"]["repository"]:
        raise ModelError("OpenRGB native source has an unexpected origin")

    native_root = root / "integrations" / "openrgb" / "native-route"
    patch = native_root / transform["patch"]["path"]
    if patch.is_symlink() or not patch.is_file():
        raise ModelError("OpenRGB native patch is missing or symbolic")
    if sha256_file(patch) != transform["patch"]["sha256"]:
        raise ModelError("OpenRGB native patch differs from its source lock")
    for record in transform["files"]:
        path = source / record["path"]
        if path.is_symlink() or not path.is_file():
            raise ModelError(f"OpenRGB native source file is missing: {record['path']}")
        if sha256_file(path) != record["source_sha256"]:
            raise ModelError(f"OpenRGB native source drift: {record['path']}")


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
            raise ModelError(f"cannot apply OpenRGB native patch: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise ModelError(f"OpenRGB native patch did not apply exactly{suffix}")


def prepare_openrgb_native_source(
    root: Path, source: Path, output: Path
) -> PreparedOpenRgbNativeSource:
    root = root.resolve()
    source = source.expanduser().resolve()
    output = output.expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise ModelError("OpenRGB native output must not already exist")
    if output == source or source in output.parents or output in source.parents:
        raise ModelError("OpenRGB native output must be independent from the source checkout")

    transform = load_native_transform(root)
    _validate_source(root, source, transform)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.parent / f".{output.name}.prepare-{uuid.uuid4().hex}"
    native_root = root / "integrations" / "openrgb" / "native-route"
    patch = native_root / transform["patch"]["path"]
    try:
        shutil.copytree(source, temporary, symlinks=False, ignore=shutil.ignore_patterns(".git"))
        _apply_patch(temporary, patch)
        for record in transform["files"]:
            if sha256_file(temporary / record["path"]) != record["transformed_sha256"]:
                raise ModelError(f"OpenRGB native transformed output drift: {record['path']}")
        result_path = temporary / "hyperflux-native-transform-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema": "hyperflux-openrgb-native-transform-result-v1",
                    "source_commit": transform["upstream"]["commit"],
                    "patch_sha256": transform["patch"]["sha256"],
                    "transformed_files": [
                        {"path": record["path"], "sha256": record["transformed_sha256"]}
                        for record in transform["files"]
                    ],
                    "network_access_executed": False,
                    "hardware_queried": False,
                    "hardware_write_executed": False,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        result_path.chmod(0o600)
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PreparedOpenRgbNativeSource(
        root=output,
        result=output / "hyperflux-native-transform-result.json",
        source_commit=transform["upstream"]["commit"],
        patch_sha256=transform["patch"]["sha256"],
    )
