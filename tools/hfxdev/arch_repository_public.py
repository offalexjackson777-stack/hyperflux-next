# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .arch_repository import (
    _payload_asset_names,
    load_arch_repository_policy,
    published_arch_release,
    repository_configuration,
    verify_arch_repository,
)
from .arch_update_policy import _package_metadata
from .model import ModelError, sha256_file


_DEPENDENCY = re.compile(
    r"^([a-z0-9][a-z0-9+_.-]{0,63})(?:([<>=]+)([^ ]+))?$"
)
_RELEASE_API_BASE = (
    "https://api.github.com/repos/offalexjackson777-stack/hyperflux-next/releases"
)


def _run(
    command: list[str],
    *,
    label: str,
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"{label} could not run: {error}") from error
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise ModelError(
            f"{label} failed with exit status {result.returncode}: {details}"
        )
    return result


def _require_tools() -> None:
    missing = [
        command
        for command in ("curl", "fakeroot", "gpg", "pacman", "pacman-key")
        if shutil.which(command) is None
    ]
    if missing:
        raise ModelError(f"public Arch repository proof lacks tools: {missing}")


def _fetch(url: str, destination: Path) -> str:
    result = _run(
        [
            "curl",
            "--disable",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--output",
            str(destination),
            "--write-out",
            "%{http_code}\n%{url_effective}\n",
            "--",
            url,
        ],
        label=f"fetch public repository asset {destination.name}",
    )
    fields = result.stdout.splitlines()
    if len(fields) != 2 or fields[0] != "200" or not fields[1].startswith("https://"):
        raise ModelError(f"public repository asset did not resolve over HTTPS: {url}")
    return fields[1]


def _dependency_assumptions(repository: Path, package_names: set[str]) -> tuple[str, ...]:
    assumptions: dict[str, str] = {}
    for package in sorted(repository.glob("*.pkg.tar.zst")):
        _, _, dependencies = _package_metadata(
            package, label=package.name, timeout_seconds=30
        )
        for dependency in dependencies:
            match = _DEPENDENCY.fullmatch(dependency)
            if match is None:
                raise ModelError(f"public repository dependency is malformed: {dependency}")
            name, operator, version = match.groups()
            if name in package_names:
                continue
            if operator not in (None, "="):
                raise ModelError(
                    f"public repository proof cannot normalize dependency: {dependency}"
                )
            assumptions[name] = version if operator == "=" else "999.0-1"
    return tuple(f"{name}={version}" for name, version in sorted(assumptions.items()))


def _pacman_base(workspace: Path) -> list[str]:
    return [
        "fakeroot",
        "pacman",
        "--config",
        str(workspace / "pacman.conf"),
        "--root",
        str(workspace / "root"),
        "--dbpath",
        str(workspace / "database"),
        "--cachedir",
        str(workspace / "cache"),
        "--hookdir",
        str(workspace / "hooks"),
        "--gpgdir",
        str(workspace / "gnupg"),
        "--logfile",
        str(workspace / "pacman.log"),
        "--noconfirm",
    ]


def _write_receipt_atomic(output: Path, receipt: dict[str, object]) -> None:
    if output.exists() or output.is_symlink():
        raise ModelError("public repository receipt output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.tmp-", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, output)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def verify_public_arch_repository(root: Path, output: Path) -> Path:
    _require_tools()
    policy = load_arch_repository_policy(root)
    if not policy.github_release_prerelease or policy.human_facing_product_release:
        raise ModelError("public Arch repository is not a separate machine channel")
    source_status = _run(
        ["git", "-C", str(root), "status", "--short", "--untracked-files=no"],
        label="check public repository proof source state",
    ).stdout
    if source_status.strip():
        raise ModelError("public Arch repository proof requires a clean tracked tree")

    with tempfile.TemporaryDirectory(prefix="hfx-public-arch-repository-") as temporary:
        workspace = Path(temporary)
        repository = workspace / "repository"
        repository.mkdir(mode=0o755)
        names = sorted(
            (*_payload_asset_names(policy), policy.manifest, policy.checksums)
        )
        release_api_path = workspace / "machine-release.json"
        release_api_url = f"{_RELEASE_API_BASE}/tags/{policy.release_tag}"
        _fetch(release_api_url, release_api_path)
        try:
            release_api = json.loads(release_api_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelError("machine-channel release API response is malformed") from error
        if (
            release_api.get("tag_name") != policy.release_tag
            or release_api.get("html_url") != policy.release_url
            or release_api.get("draft") is not False
            or release_api.get("prerelease") is not True
            or not isinstance(release_api.get("id"), int)
        ):
            raise ModelError("machine-channel release metadata differs from policy")
        release_assets = release_api.get("assets")
        if not isinstance(release_assets, list):
            raise ModelError("machine-channel release has no asset inventory")
        remote_assets = {
            item.get("name"): item
            for item in release_assets
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if set(remote_assets) != set(names):
            raise ModelError("machine-channel release asset inventory differs")
        for name in names:
            if remote_assets[name].get("browser_download_url") != (
                f"{policy.server_url}/{name}"
            ):
                raise ModelError(f"machine-channel asset URL differs: {name}")

        latest_api_path = workspace / "latest-release.json"
        _fetch(f"{_RELEASE_API_BASE}/latest", latest_api_path)
        try:
            latest_api = json.loads(latest_api_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelError("latest product release API response is malformed") from error
        if latest_api.get("tag_name") != policy.product_release_tag:
            raise ModelError("machine channel displaced the latest product release")

        fetches = []
        for name in names:
            request_url = f"{policy.server_url}/{name}"
            effective_url = _fetch(request_url, repository / name)
            digest = sha256_file(repository / name)
            size = (repository / name).stat().st_size
            if remote_assets[name].get("digest") != f"sha256:{digest}" or (
                remote_assets[name].get("size") != size
            ):
                raise ModelError(f"machine-channel asset digest differs: {name}")
            fetches.append(
                {
                    "name": name,
                    "request_url": request_url,
                    "effective_url": effective_url,
                    "sha256": digest,
                    "size": size,
                }
            )
        local_verification = verify_arch_repository(root, repository)

        pacman = workspace / "pacman"
        for name in ("root", "database", "cache", "hooks", "gnupg"):
            (pacman / name).mkdir(parents=True, mode=0o700 if name == "gnupg" else 0o755)
        (pacman / "pacman.conf").write_text(
            "\n".join(
                (
                    "[options]",
                    "Architecture = x86_64",
                    "SigLevel = Required DatabaseRequired TrustedOnly",
                    "LocalFileSigLevel = Required TrustedOnly",
                    "ParallelDownloads = 1",
                    "",
                    *repository_configuration(policy).splitlines(),
                )
            ),
            encoding="utf-8",
        )
        _run(
            ["pacman-key", "--gpgdir", str(pacman / "gnupg"), "--init"],
            label="initialize isolated Pacman keyring",
        )
        _run(
            [
                "pacman-key",
                "--gpgdir",
                str(pacman / "gnupg"),
                "--add",
                str(repository / policy.public_key_asset),
            ],
            label="import exact repository public key",
        )
        _run(
            [
                "pacman-key",
                "--gpgdir",
                str(pacman / "gnupg"),
                "--lsign-key",
                policy.primary_fingerprint,
            ],
            label="locally trust exact repository public key",
        )
        key_listing = _run(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(pacman / "gnupg"),
                "--with-colons",
                "--list-keys",
                policy.primary_fingerprint,
            ],
            label="inspect isolated Pacman keyring",
        ).stdout
        fingerprints = {
            line.split(":")[9]
            for line in key_listing.splitlines()
            if line.startswith("fpr:")
        }
        if fingerprints != {
            policy.primary_fingerprint,
            policy.signing_subkey_fingerprint,
        }:
            raise ModelError("isolated Pacman keyring fingerprints differ")

        base = _pacman_base(pacman)
        sync = _run([*base, "-Sy"], label="authenticate public repository database")
        query = _run(
            [*base, "-Si", "hyperflux-next-full"],
            label="query public Full package",
        )
        if "Name            : hyperflux-next-full" not in query.stdout or (
            f"Version         : {published_arch_release(policy)}" not in query.stdout
        ):
            raise ModelError("public Pacman query returned the wrong Full package")

        package_names = {package.name for package in policy.packages}
        assumptions = _dependency_assumptions(repository, package_names)
        assumption_arguments = [
            argument
            for assumption in assumptions
            for argument in ("--assume-installed", assumption)
        ]
        plan = _run(
            [
                *base,
                "-Sp",
                "--print-format",
                "__HFX_PACKAGE__%n %v %l",
                *assumption_arguments,
                "hyperflux-next-full",
            ],
            label="resolve public Full and Core transaction",
        )
        planned = sorted(
            line.removeprefix("__HFX_PACKAGE__").split(maxsplit=2)
            for line in plan.stdout.splitlines()
            if line.startswith("__HFX_PACKAGE__")
        )
        expected_plan = sorted(
            [package.name, package.version, f"{policy.server_url}/{package.filename}"]
            for package in policy.packages
        )
        if planned != expected_plan:
            raise ModelError(f"public Pacman transaction differs: {planned}")

        _run(
            [
                *base,
                "-Sw",
                *assumption_arguments,
                "hyperflux-next-full",
            ],
            label="download and authenticate public Full and Core packages",
        )
        cached = {
            path.name: sha256_file(path)
            for path in sorted((pacman / "cache").glob("*.pkg.tar.zst"))
        }
        expected_cached = {
            package.filename: package.sha256 for package in policy.packages
        }
        if cached != expected_cached:
            raise ModelError(f"public Pacman package downloads differ: {cached}")

        source_revision = _run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            label="bind public repository proof source",
        ).stdout.strip()
        receipt: dict[str, object] = {
            "$schema": "../schemas/arch-repository-publication.schema.json",
            "schema": "hyperflux-arch-repository-publication-v1",
            "source_revision": source_revision,
            "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "release": {
                "tag": policy.release_tag,
                "url": policy.release_url,
                "prerelease": policy.github_release_prerelease,
                "human_facing_product_release": policy.human_facing_product_release,
                "github_release_id": release_api["id"],
                "anonymous_api_url": release_api_url,
                "latest_product_release_tag": policy.product_release_tag,
            },
            "repository": {
                "name": policy.name,
                "server_url": policy.server_url,
                "primary_fingerprint": policy.primary_fingerprint,
                "signing_subkey_fingerprint": policy.signing_subkey_fingerprint,
                "manifest_sha256": local_verification["manifest_sha256"],
                "checksums_sha256": local_verification["checksums_sha256"],
                "anonymous_https_assets": fetches,
            },
            "pacman": {
                "database_authenticated": True,
                "database_sync_output": sync.stdout.strip(),
                "full_query_authenticated": True,
                "planned_packages": [
                    {"name": name, "version": version, "url": url}
                    for name, version, url in planned
                ],
                "dependency_assumptions": list(assumptions),
                "package_signatures_authenticated": True,
                "downloaded_package_sha256": cached,
            },
        }
        _write_receipt_atomic(output, receipt)
    return output
