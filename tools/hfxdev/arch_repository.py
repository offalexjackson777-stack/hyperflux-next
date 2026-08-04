# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Callable

from .arch_update_policy import _package_metadata
from .model import ModelError, load_json, sha256_file


_FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RepositoryPackage:
    name: str
    version: str
    filename: str
    sha256: str


@dataclass(frozen=True)
class ArchRepositoryPolicy:
    name: str
    architecture: str
    official_arch_repository: bool
    server_url: str
    release_url: str
    release_tag: str
    product_release_url: str
    product_release_tag: str
    channel_purpose: str
    github_release_prerelease: bool
    human_facing_product_release: bool
    signature_level: str
    primary_fingerprint: str
    signing_subkey_fingerprint: str
    public_key_path: str
    public_key_asset: str
    fingerprint_asset: str
    signature_timestamp: int
    expires_at: str
    packages: tuple[RepositoryPackage, ...]
    database: str
    database_archive: str
    files: str
    files_archive: str
    manifest: str
    checksums: str


def _run(
    command: list[str],
    *,
    label: str,
    timeout_seconds: int = 120,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"{label} could not run: {error}") from error
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise ModelError(
            f"{label} failed with exit status {result.returncode}: {details}"
        )
    return result


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ModelError(f"{label} has missing or unknown fields")
    return value


def load_arch_repository_policy(root: Path) -> ArchRepositoryPolicy:
    value = load_json(root / "assurance" / "arch-repository.json")
    _exact_keys(
        value,
        {"$schema", "schema", "repository", "signing", "packages", "assets"},
        "Arch repository policy",
    )
    if value["$schema"] != "../schemas/arch-repository.schema.json":
        raise ModelError("Arch repository policy has a non-canonical schema")
    if value["schema"] != "hyperflux-arch-repository-v1":
        raise ModelError("unsupported Arch repository policy")

    repository = _exact_keys(
        value["repository"],
        {
            "name",
            "architecture",
            "official_arch_repository",
            "server_url",
            "release_url",
            "release_tag",
            "product_release_url",
            "product_release_tag",
            "channel_purpose",
            "github_release_prerelease",
            "human_facing_product_release",
            "signature_level",
        },
        "Arch repository identity",
    )
    expected_repository = {
        "name": "hyperflux-next",
        "architecture": "x86_64",
        "official_arch_repository": False,
        "server_url": (
            "https://github.com/offalexjackson777-stack/hyperflux-next/"
            "releases/download/arch-repository"
        ),
        "release_url": (
            "https://github.com/offalexjackson777-stack/hyperflux-next/"
            "releases/tag/arch-repository"
        ),
        "release_tag": "arch-repository",
        "product_release_url": (
            "https://github.com/offalexjackson777-stack/hyperflux-next/"
            "releases/tag/v2.0.0"
        ),
        "product_release_tag": "v2.0.0",
        "channel_purpose": "machine-managed-arch-repository",
        "github_release_prerelease": True,
        "human_facing_product_release": False,
        "signature_level": "Required DatabaseRequired TrustedOnly",
    }
    if repository != expected_repository:
        raise ModelError("Arch repository identity differs from the public release")

    signing = _exact_keys(
        value["signing"],
        {
            "primary_fingerprint",
            "signing_subkey_fingerprint",
            "public_key_path",
            "public_key_asset",
            "fingerprint_asset",
            "signature_timestamp",
            "expires_at",
        },
        "Arch repository signing policy",
    )
    for field in ("primary_fingerprint", "signing_subkey_fingerprint"):
        if not isinstance(signing[field], str) or not _FINGERPRINT.fullmatch(
            signing[field]
        ):
            raise ModelError(f"Arch repository {field} is not a full fingerprint")
    if signing["primary_fingerprint"] == signing["signing_subkey_fingerprint"]:
        raise ModelError(
            "Arch repository signing key must use a distinct signing subkey"
        )
    if not isinstance(signing["signature_timestamp"], int) or signing[
        "signature_timestamp"
    ] <= 0:
        raise ModelError("Arch repository signature timestamp is invalid")
    if not isinstance(signing["expires_at"], str) or not signing["expires_at"].endswith(
        "Z"
    ):
        raise ModelError("Arch repository signing expiration is invalid")
    expected_signing_assets = {
        "public_key_path": "packaging/keys/hyperflux-next-arch-repository-key.asc",
        "public_key_asset": "hyperflux-next-arch-repository-key.asc",
        "fingerprint_asset": "hyperflux-next-arch-repository-fingerprint.txt",
    }
    for field, expected in expected_signing_assets.items():
        if signing[field] != expected:
            raise ModelError(f"Arch repository {field} is not canonical")
        if field.endswith("_asset") and Path(signing[field]).name != signing[field]:
            raise ModelError(f"Arch repository {field} is not a plain asset name")

    raw_packages = value["packages"]
    if not isinstance(raw_packages, list) or len(raw_packages) != 2:
        raise ModelError("Arch repository must contain exactly Core and Full")
    packages: list[RepositoryPackage] = []
    for index, raw in enumerate(raw_packages):
        package = _exact_keys(
            raw,
            {"name", "version", "filename", "sha256"},
            f"Arch repository package {index}",
        )
        if not isinstance(package["sha256"], str) or not _SHA256.fullmatch(
            package["sha256"]
        ):
            raise ModelError(f"Arch repository package {index} has an invalid digest")
        packages.append(RepositoryPackage(**package))
    if {package.name for package in packages} != {
        "hyperflux-next-linux",
        "hyperflux-next-full",
    }:
        raise ModelError("Arch repository package set is not exact Core and Full")
    if len({package.filename for package in packages}) != 2:
        raise ModelError("Arch repository package filenames must be unique")
    if {package.version for package in packages} != {"2.0.0-1"}:
        raise ModelError("Arch repository package versions must match the release")

    assets = _exact_keys(
        value["assets"],
        {
            "database",
            "database_archive",
            "files",
            "files_archive",
            "manifest",
            "checksums",
        },
        "Arch repository assets",
    )
    expected_assets = {
        "database": "hyperflux-next.db",
        "database_archive": "hyperflux-next.db.tar.zst",
        "files": "hyperflux-next.files",
        "files_archive": "hyperflux-next.files.tar.zst",
        "manifest": "hyperflux-next-arch-repository-manifest.json",
        "checksums": "SHA256SUMS-repository",
    }
    if assets != expected_assets:
        raise ModelError("Arch repository asset names are not stable")

    public_key_path = signing["public_key_path"]
    root_resolved = root.resolve(strict=True)
    public_key = root / public_key_path
    try:
        public_key.resolve(strict=True).relative_to(root_resolved)
    except (OSError, ValueError) as error:
        raise ModelError("Arch repository public key path escapes the source tree") from error
    if not public_key.is_file() or public_key.is_symlink():
        raise ModelError("Arch repository public key is missing or unsafe")
    key_text = public_key.read_text(encoding="ascii")
    if "BEGIN PGP PUBLIC KEY BLOCK" not in key_text or "PRIVATE KEY" in key_text:
        raise ModelError("Arch repository key file is not a public OpenPGP key")

    return ArchRepositoryPolicy(
        **repository,
        **signing,
        packages=tuple(packages),
        **assets,
    )


def repository_configuration(policy: ArchRepositoryPolicy) -> str:
    return "\n".join(
        (
            f"[{policy.name}]",
            f"SigLevel = {policy.signature_level}",
            f"Server = {policy.server_url}",
            "",
        )
    )


def fingerprint_receipt(policy: ArchRepositoryPolicy) -> str:
    grouped = " ".join(
        policy.primary_fingerprint[index : index + 4]
        for index in range(0, len(policy.primary_fingerprint), 4)
    )
    return "\n".join(
        (
            "HyperFlux Next Arch Repository package-signing key",
            f"Primary fingerprint: {grouped}",
            f"Signing subkey fingerprint: {policy.signing_subkey_fingerprint}",
            f"Expires: {policy.expires_at}",
            f"Repository: {policy.server_url}",
            "This is a project-operated third-party repository, not an official "
            "Arch Linux repository.",
            "",
        )
    )


def _payload_asset_names(policy: ArchRepositoryPolicy) -> tuple[str, ...]:
    names = [
        policy.database,
        f"{policy.database}.sig",
        policy.database_archive,
        f"{policy.database_archive}.sig",
        policy.files,
        f"{policy.files}.sig",
        policy.files_archive,
        f"{policy.files_archive}.sig",
        policy.public_key_asset,
        policy.fingerprint_asset,
    ]
    for package in policy.packages:
        names.extend((package.filename, f"{package.filename}.sig"))
    return tuple(sorted(names))


def _sign(
    path: Path,
    *,
    gnupg_home: Path,
    policy: ArchRepositoryPolicy,
) -> Path:
    signature = path.with_name(path.name + ".sig")
    _run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(gnupg_home),
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--faked-system-time",
            f"{policy.signature_timestamp}!",
            "--local-user",
            f"{policy.signing_subkey_fingerprint}!",
            "--detach-sign",
            "--output",
            str(signature),
            str(path),
        ],
        label=f"sign {path.name}",
    )
    return signature


def _materialize_alias(alias: Path, archive: Path) -> None:
    if not alias.is_symlink() or alias.resolve(strict=True) != archive.resolve(
        strict=True
    ):
        raise ModelError(f"repo-add produced an unexpected alias: {alias.name}")
    alias.unlink()
    shutil.copyfile(archive, alias)
    alias.chmod(0o644)


def _normalize_repository_archive(path: Path, *, timestamp: int) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".hfx-repository-archive-", dir=path.parent
    ) as temporary:
        root = Path(temporary) / "root"
        root.mkdir(mode=0o700)
        _run(
            ["bsdtar", "-xf", str(path), "-C", str(root)],
            label=f"extract {path.name} for deterministic metadata",
        )
        entries = sorted(
            root.rglob("*"),
            key=lambda item: (item.relative_to(root).as_posix(), not item.is_dir()),
        )
        if not entries or any(
            entry.is_symlink() or not (entry.is_dir() or entry.is_file())
            for entry in entries
        ):
            raise ModelError(f"{path.name} contains an unsafe repository entry")
        uncompressed = Path(temporary) / "repository.tar"
        with tarfile.open(uncompressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for entry in entries:
                relative = entry.relative_to(root).as_posix()
                information = tarfile.TarInfo(
                    relative + "/" if entry.is_dir() else relative
                )
                information.uid = 0
                information.gid = 0
                information.uname = "root"
                information.gname = "root"
                information.mtime = timestamp
                information.mode = 0o755 if entry.is_dir() else 0o644
                if entry.is_dir():
                    information.type = tarfile.DIRTYPE
                    archive.addfile(information)
                else:
                    information.size = entry.stat().st_size
                    with entry.open("rb") as source:
                        archive.addfile(information, source)
        compressed = Path(temporary) / path.name
        _run(
            [
                "zstd",
                "--quiet",
                "--force",
                "--threads=1",
                "-19",
                "-o",
                str(compressed),
                str(uncompressed),
            ],
            label=f"compress deterministic {path.name}",
        )
        os.replace(compressed, path)
        path.chmod(0o644)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o644)


def _validate_private_gnupg_home(gnupg_home: Path) -> None:
    try:
        home_stat = gnupg_home.lstat()
    except OSError as error:
        raise ModelError("repository signing home is missing or unsafe") from error
    if (
        not stat.S_ISDIR(home_stat.st_mode)
        or gnupg_home.is_symlink()
        or home_stat.st_uid != os.geteuid()
        or stat.S_IMODE(home_stat.st_mode) != 0o700
    ):
        raise ModelError(
            "repository signing home must be owned by the current user and mode 0700"
        )
    for entry in gnupg_home.rglob("*"):
        entry_stat = entry.lstat()
        if entry_stat.st_uid != os.geteuid() or stat.S_ISLNK(entry_stat.st_mode):
            raise ModelError("repository signing home contains an unsafe owner or link")
        mode = stat.S_IMODE(entry_stat.st_mode)
        if stat.S_ISDIR(entry_stat.st_mode):
            if mode != 0o700:
                raise ModelError("repository signing directories must be mode 0700")
        elif stat.S_ISREG(entry_stat.st_mode):
            if mode != 0o600:
                raise ModelError("repository signing files must be mode 0600")
        else:
            raise ModelError("repository signing home contains an unsupported file type")


def _manifest_policy(root: Path) -> dict[str, Any]:
    return load_json(root / "assurance" / "arch-repository.json")


def _repository_manifest(
    root: Path,
    repository: Path,
    policy: ArchRepositoryPolicy,
) -> dict[str, Any]:
    payload_assets = [
        {
            "name": name,
            "sha256": sha256_file(repository / name),
            "size": (repository / name).stat().st_size,
        }
        for name in _payload_asset_names(policy)
    ]
    authority = root / "assurance" / "arch-repository.json"
    return {
        "schema": "hyperflux-arch-repository-manifest-v2",
        "policy_sha256": sha256_file(authority),
        "policy": _manifest_policy(root),
        "published_assets": payload_assets,
    }


def _build_arch_repository_directory(
    root: Path,
    *,
    supplied: dict[str, Path],
    gnupg_home: Path,
    policy: ArchRepositoryPolicy,
    output: Path,
) -> None:
    output.mkdir(mode=0o755)
    for package in policy.packages:
        destination = output / package.filename
        shutil.copyfile(supplied[package.filename], destination)
        destination.chmod(0o644)
        if sha256_file(destination) != package.sha256:
            raise ModelError(f"copied repository package changed: {package.filename}")
        _sign(destination, gnupg_home=gnupg_home, policy=policy)

    environment = dict(os.environ)
    environment["GNUPGHOME"] = str(gnupg_home)
    environment["SOURCE_DATE_EPOCH"] = str(policy.signature_timestamp)
    _run(
        [
            "repo-add",
            "--include-sigs",
            str(output / policy.database_archive),
            *(str(output / package.filename) for package in policy.packages),
        ],
        label="create signed-package Arch repository metadata",
        environment=environment,
    )
    _normalize_repository_archive(
        output / policy.database_archive, timestamp=policy.signature_timestamp
    )
    _normalize_repository_archive(
        output / policy.files_archive, timestamp=policy.signature_timestamp
    )
    _materialize_alias(output / policy.database, output / policy.database_archive)
    _materialize_alias(output / policy.files, output / policy.files_archive)
    for archive, alias in (
        (output / policy.database_archive, output / policy.database),
        (output / policy.files_archive, output / policy.files),
    ):
        signature = _sign(archive, gnupg_home=gnupg_home, policy=policy)
        shutil.copyfile(signature, alias.with_name(alias.name + ".sig"))
        alias.with_name(alias.name + ".sig").chmod(0o644)

    shutil.copyfile(root / policy.public_key_path, output / policy.public_key_asset)
    (output / policy.public_key_asset).chmod(0o644)
    (output / policy.fingerprint_asset).write_text(
        fingerprint_receipt(policy), encoding="utf-8"
    )
    (output / policy.fingerprint_asset).chmod(0o644)

    _write_json(output / policy.manifest, _repository_manifest(root, output, policy))
    checksum_names = sorted((*_payload_asset_names(policy), policy.manifest))
    (output / policy.checksums).write_text(
        "".join(f"{sha256_file(output / name)}  {name}\n" for name in checksum_names),
        encoding="ascii",
    )
    (output / policy.checksums).chmod(0o644)
    verify_arch_repository(root, output)


def _publish_atomic_directory(
    output: Path,
    build: Callable[[Path], None],
) -> None:
    if output.exists() or output.is_symlink():
        raise ModelError("repository output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ModelError("repository output parent is unsafe")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    temporary.rmdir()
    try:
        build(temporary)
        os.replace(temporary, output)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def build_arch_repository(
    root: Path,
    *,
    core_package: Path,
    full_package: Path,
    gnupg_home: Path,
    key_fingerprint: str,
    output: Path,
) -> Path:
    policy = load_arch_repository_policy(root)
    if key_fingerprint != policy.primary_fingerprint:
        raise ModelError(
            "repository signing key differs from the canonical fingerprint"
        )
    _validate_private_gnupg_home(gnupg_home)

    supplied = {core_package.name: core_package, full_package.name: full_package}
    if set(supplied) != {package.filename for package in policy.packages}:
        raise ModelError("repository package filenames differ from the frozen release")
    for package in policy.packages:
        source = supplied[package.filename]
        if not source.is_file() or source.is_symlink():
            raise ModelError(
                f"repository package is missing or unsafe: {package.filename}"
            )
        if sha256_file(source) != package.sha256:
            raise ModelError(f"repository package bytes differ: {package.filename}")
        name, version, _ = _package_metadata(
            source, label=package.name, timeout_seconds=30
        )
        if (name, version) != (package.name, package.version):
            raise ModelError(f"repository package metadata differs: {package.filename}")

    key_listing = _run(
        [
            "gpg",
            "--batch",
            "--homedir",
            str(gnupg_home),
            "--with-colons",
            "--list-secret-keys",
            policy.primary_fingerprint,
        ],
        label="inspect repository signing key",
    ).stdout
    fingerprints = tuple(
        line.split(":")[9]
        for line in key_listing.splitlines()
        if line.startswith("fpr:")
    )
    if (
        policy.primary_fingerprint not in fingerprints
        or policy.signing_subkey_fingerprint not in fingerprints
    ):
        raise ModelError("repository signing home lacks the canonical key and subkey")

    def build(temporary: Path) -> None:
        _build_arch_repository_directory(
            root,
            supplied=supplied,
            gnupg_home=gnupg_home,
            policy=policy,
            output=temporary,
        )
        _validate_private_gnupg_home(gnupg_home)

    _publish_atomic_directory(output, build)
    verify_arch_repository(root, output)
    return output / policy.manifest


def _verify_signature(
    signature: Path,
    payload: Path,
    *,
    gnupg_home: Path,
    policy: ArchRepositoryPolicy,
) -> None:
    result = _run(
        [
            "gpg",
            "--batch",
            "--homedir",
            str(gnupg_home),
            "--status-fd",
            "1",
            "--verify",
            str(signature),
            str(payload),
        ],
        label=f"verify {signature.name}",
    )
    valid = tuple(
        line.split()
        for line in result.stdout.splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
    )
    if (
        len(valid) != 1
        or policy.signing_subkey_fingerprint not in valid[0]
        or policy.primary_fingerprint not in valid[0]
    ):
        raise ModelError(f"{signature.name} is not signed by the canonical key")


def verify_arch_repository(root: Path, repository: Path) -> dict[str, Any]:
    policy = load_arch_repository_policy(root)
    if not repository.is_dir() or repository.is_symlink():
        raise ModelError("Arch repository must be one regular directory")
    expected = set(_payload_asset_names(policy)) | {policy.manifest, policy.checksums}
    actual = {path.name for path in repository.iterdir()}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ModelError(
            f"Arch repository inventory differs (missing={missing}, extra={extra})"
        )
    for path in repository.iterdir():
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_mode & 0o777 != 0o644
        ):
            raise ModelError(
                f"Arch repository asset is not regular mode 0644: {path.name}"
            )

    for package in policy.packages:
        if sha256_file(repository / package.filename) != package.sha256:
            raise ModelError(f"Arch repository package changed: {package.filename}")
        name, version, dependencies = _package_metadata(
            repository / package.filename,
            label=package.name,
            timeout_seconds=30,
        )
        if (name, version) != (package.name, package.version):
            raise ModelError(
                f"Arch repository package metadata changed: {package.filename}"
            )
        if (
            package.name == "hyperflux-next-full"
            and "hyperflux-next-linux=2.0.0-1" not in dependencies
        ):
            raise ModelError("Full no longer requires the exact Core version")

    if (repository / policy.database).read_bytes() != (
        repository / policy.database_archive
    ).read_bytes():
        raise ModelError("stable database alias differs from its canonical archive")
    if (repository / policy.files).read_bytes() != (
        repository / policy.files_archive
    ).read_bytes():
        raise ModelError("stable files alias differs from its canonical archive")
    if (repository / f"{policy.database}.sig").read_bytes() != (
        repository / f"{policy.database_archive}.sig"
    ).read_bytes():
        raise ModelError("stable database signature alias differs")
    if (repository / f"{policy.files}.sig").read_bytes() != (
        repository / f"{policy.files_archive}.sig"
    ).read_bytes():
        raise ModelError("stable files signature alias differs")

    if (repository / policy.fingerprint_asset).read_text(
        encoding="utf-8"
    ) != fingerprint_receipt(policy):
        raise ModelError("published fingerprint receipt differs from policy")
    if (repository / policy.public_key_asset).read_bytes() != (
        root / policy.public_key_path
    ).read_bytes():
        raise ModelError("published public key differs from the tracked key")

    with tempfile.TemporaryDirectory(prefix="hfx-arch-repository-key-") as temporary:
        key_home = Path(temporary)
        key_home.chmod(0o700)
        imported = _run(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(key_home),
                "--with-colons",
                "--import-options",
                "show-only",
                "--import",
                str(repository / policy.public_key_asset),
            ],
            label="inspect published repository key",
        ).stdout
        imported_fingerprints = tuple(
            line.split(":")[9]
            for line in imported.splitlines()
            if line.startswith("fpr:")
        )
        if (
            policy.primary_fingerprint not in imported_fingerprints
            or policy.signing_subkey_fingerprint not in imported_fingerprints
        ):
            raise ModelError("published repository key fingerprints differ")
        _run(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(key_home),
                "--import",
                str(repository / policy.public_key_asset),
            ],
            label="import published repository key",
        )
        signed_payloads = [
            *(repository / package.filename for package in policy.packages),
            repository / policy.database,
            repository / policy.database_archive,
            repository / policy.files,
            repository / policy.files_archive,
        ]
        for payload in signed_payloads:
            _verify_signature(
                payload.with_name(payload.name + ".sig"),
                payload,
                gnupg_home=key_home,
                policy=policy,
            )

    listing = _run(
        ["bsdtar", "-tf", str(repository / policy.database_archive)],
        label="inspect Arch repository database",
    ).stdout.splitlines()
    descriptions = sorted(item for item in listing if item.endswith("/desc"))
    if len(descriptions) != 2:
        raise ModelError(
            "Arch repository database does not contain exactly two packages"
        )
    database_packages: set[tuple[str, str, str, str, str]] = set()
    for description in descriptions:
        text = _run(
            [
                "bsdtar",
                "-xOf",
                str(repository / policy.database_archive),
                description,
            ],
            label=f"read {description}",
        ).stdout
        fields: dict[str, str] = {}
        lines = text.splitlines()
        for index, line in enumerate(lines[:-1]):
            if line.startswith("%") and line.endswith("%"):
                if line in fields or not lines[index + 1]:
                    raise ModelError(
                        f"Arch repository description repeats or empties {line}: "
                        f"{description}"
                    )
                fields[line] = lines[index + 1]
        required = {"%NAME%", "%VERSION%", "%FILENAME%", "%SHA256SUM%", "%PGPSIG%"}
        if not required <= fields.keys():
            raise ModelError(
                f"Arch repository description is incomplete: {description}"
            )
        database_packages.add(
            (
                fields["%NAME%"],
                fields["%VERSION%"],
                fields["%FILENAME%"],
                fields["%SHA256SUM%"],
                fields["%PGPSIG%"],
            )
        )
    expected_packages = {
        (
            package.name,
            package.version,
            package.filename,
            package.sha256,
            base64.b64encode(
                (repository / f"{package.filename}.sig").read_bytes()
            ).decode("ascii"),
        )
        for package in policy.packages
    }
    if database_packages != expected_packages:
        raise ModelError("Arch repository database package identities differ")

    manifest = load_json(repository / policy.manifest)
    expected_manifest_keys = {"schema", "policy_sha256", "policy", "published_assets"}
    _exact_keys(manifest, expected_manifest_keys, "Arch repository manifest")
    if manifest["schema"] != "hyperflux-arch-repository-manifest-v2":
        raise ModelError("unsupported Arch repository manifest")
    expected_manifest = _repository_manifest(root, repository, policy)
    if manifest != expected_manifest:
        raise ModelError("Arch repository manifest differs from the complete policy")
    manifest_assets = manifest["published_assets"]
    if not isinstance(manifest_assets, list) or [
        item["name"] for item in manifest_assets
    ] != list(_payload_asset_names(policy)):
        raise ModelError("Arch repository manifest inventory differs")
    for item in manifest_assets:
        _exact_keys(item, {"name", "sha256", "size"}, "Arch repository manifest asset")
        path = repository / item["name"]
        if item["sha256"] != sha256_file(path) or item["size"] != path.stat().st_size:
            raise ModelError(f"Arch repository manifest asset changed: {item['name']}")

    checksum_lines = (repository / policy.checksums).read_text(
        encoding="ascii"
    ).splitlines()
    expected_checksum_names = sorted((*_payload_asset_names(policy), policy.manifest))
    parsed_checksums: dict[str, str] = {}
    for line in checksum_lines:
        digest, separator, name = line.partition("  ")
        if not separator or not _SHA256.fullmatch(digest) or name in parsed_checksums:
            raise ModelError("Arch repository checksum receipt is malformed")
        parsed_checksums[name] = digest
    if sorted(parsed_checksums) != expected_checksum_names:
        raise ModelError("Arch repository checksum inventory differs")
    for name, digest in parsed_checksums.items():
        if sha256_file(repository / name) != digest:
            raise ModelError(f"Arch repository checksum differs: {name}")

    return {
        "schema": "hyperflux-arch-repository-verification-v1",
        "result": "passed",
        "repository": policy.name,
        "server_url": policy.server_url,
        "primary_fingerprint": policy.primary_fingerprint,
        "packages": [
            {"name": package.name, "version": package.version, "sha256": package.sha256}
            for package in policy.packages
        ],
        "manifest_sha256": sha256_file(repository / policy.manifest),
        "checksums_sha256": sha256_file(repository / policy.checksums),
        "asset_count": len(expected),
    }
