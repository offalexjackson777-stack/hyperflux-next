# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .integrations import load_integration_catalog
from .model import ModelError, sha256_file
from .openrazer_version_matrix import load_openrazer_version_matrix


IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class PreparedUpstreams:
    root: Path
    manifest: Path
    reused: tuple[str, ...]
    fetched: tuple[str, ...]


def _git(arguments: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot run git {' '.join(arguments)}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ModelError(f"git {' '.join(arguments)} failed{suffix}")
    return result.stdout.strip()


def _validate_checkout(path: Path, upstream: dict[str, object]) -> None:
    identifier = str(upstream["id"])
    if path.is_symlink() or not path.is_dir() or (path / ".git").is_symlink():
        raise ModelError(f"prepared {identifier} checkout is missing or uses a symbolic link")
    if _git(["rev-parse", "--is-inside-work-tree"], cwd=path) != "true":
        raise ModelError(f"prepared {identifier} path is not a Git worktree")
    if _git(["rev-parse", "HEAD"], cwd=path) != upstream["commit"]:
        raise ModelError(f"prepared {identifier} checkout does not match its catalog commit")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=path):
        raise ModelError(f"prepared {identifier} checkout has local modifications")
    if _git(["remote", "get-url", "origin"], cwd=path) != upstream["repository"]:
        raise ModelError(f"prepared {identifier} checkout has an unexpected origin")
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=path,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if symbolic.returncode == 0:
        raise ModelError(f"prepared {identifier} checkout must use detached HEAD")


def _fetch_checkout(destination: Path, upstream: dict[str, object]) -> None:
    destination.mkdir(mode=0o700)
    try:
        _git(["init", "--quiet"], cwd=destination)
        _git(["remote", "add", "origin", str(upstream["repository"])], cwd=destination)
        _git(
            [
                "-c",
                "advice.detachedHead=false",
                "fetch",
                "--depth=1",
                "--no-tags",
                "origin",
                str(upstream["commit"]),
            ],
            cwd=destination,
        )
        _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=destination)
        _validate_checkout(destination, upstream)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _ensure_commits(path: Path, commits: list[str]) -> list[str]:
    fetched: list[str] = []
    for commit in commits:
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=path,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if present.returncode != 0:
            _git(
                ["fetch", "--depth=1", "--no-tags", "origin", commit],
                cwd=path,
            )
            fetched.append(commit)
        if _git(["rev-parse", f"{commit}^{{commit}}"], cwd=path) != commit:
            raise ModelError(f"prepared OpenRazer matrix commit is unavailable: {commit}")
    return fetched


def prepare_upstreams(root: Path, output: Path | None = None) -> PreparedUpstreams:
    root = root.resolve()
    destination_root = (output or root / ".hfx" / "upstreams").expanduser()
    if not destination_root.is_absolute():
        destination_root = root / destination_root
    if destination_root.is_symlink():
        raise ModelError("upstream destination may not be a symbolic link")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not destination_root.is_dir():
        raise ModelError("upstream destination is not a directory")

    catalog_path = root / "integrations" / "catalog.json"
    catalog = load_integration_catalog(root)
    has_openrazer = any(
        upstream["id"] == "openrazer" for upstream in catalog["upstreams"]
    )
    version_matrix = load_openrazer_version_matrix(root) if has_openrazer else None
    reused: list[str] = []
    fetched: list[str] = []
    entries: list[dict[str, str]] = []
    for upstream in catalog["upstreams"]:
        identifier = upstream["id"]
        if IDENTIFIER.fullmatch(identifier) is None:
            raise ModelError(f"invalid upstream checkout identifier: {identifier}")
        destination = destination_root / identifier
        if destination.exists() or destination.is_symlink():
            _validate_checkout(destination, upstream)
            reused.append(identifier)
        else:
            temporary = destination_root / f".{identifier}.prepare-{uuid.uuid4().hex}"
            _fetch_checkout(temporary, upstream)
            try:
                os.replace(temporary, destination)
            except OSError as error:
                shutil.rmtree(temporary, ignore_errors=True)
                raise ModelError(f"cannot commit prepared {identifier} checkout: {error}") from error
            _validate_checkout(destination, upstream)
            fetched.append(identifier)
        entry = {
            "id": identifier,
            "commit": upstream["commit"],
            "path": identifier,
        }
        if identifier == "openrazer":
            if version_matrix is None:
                raise ModelError("OpenRazer version matrix is unavailable")
            verification_commits = [case["commit"] for case in version_matrix["cases"]]
            additional_fetches = _ensure_commits(destination, verification_commits)
            if additional_fetches:
                if identifier in reused:
                    reused.remove(identifier)
                if identifier not in fetched:
                    fetched.append(identifier)
            entry["verification_commits"] = verification_commits
        entries.append(entry)

    manifest = destination_root / "upstreams.lock.json"
    value = {
        "schema": "hyperflux-upstream-checkout-lock-v1",
        "catalog_sha256": sha256_file(catalog_path),
        "network_access_executed": bool(fetched),
        "upstreams": entries,
    }
    if has_openrazer:
        value["openrazer_version_matrix_sha256"] = sha256_file(
            root / "integrations" / "openrazer" / "userspace-version-matrix.json"
        )
    temporary_manifest = destination_root / f".upstreams.lock-{uuid.uuid4().hex}.json"
    temporary_manifest.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.chmod(0o600)
    os.replace(temporary_manifest, manifest)
    return PreparedUpstreams(
        root=destination_root,
        manifest=manifest,
        reused=tuple(reused),
        fetched=tuple(fetched),
    )
