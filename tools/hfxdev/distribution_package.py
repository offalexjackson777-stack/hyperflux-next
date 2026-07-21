# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

from .distributions import DistributionCatalog, DistributionTarget, load_distribution_catalog
from .linux_runtime import LinuxRuntime, load_linux_runtime
from .model import ModelError, sha256_file
from .package_pipeline import ArtifactSet, StageResult, load_artifact_set, stage_rootfs


BUILD_MANIFEST_NAME = "distribution-package-build-manifest.json"
CANONICAL_ARCH_BUILD_ROOT = "/build/hyperflux-next"
CANONICAL_ARCH_PACKAGER = "HyperFlux Next Build System <build@hyperflux.invalid>"
INSTALLED_MANIFEST_PATH = PurePosixPath(
    "/usr/share/hyperflux-next/distribution-package.json"
)
ARCH_POST_TRANSACTION_HOOK = PurePosixPath(
    "/usr/share/libalpm/hooks/95-hyperflux-next-post-transaction.hook"
)


@dataclass(frozen=True)
class DistributionPackageResult:
    package: Path
    manifest: Path
    distribution: str
    architecture: str
    common_payload_sha256: str
    distribution_payload_sha256: str


def _new_output_directory(path: Path) -> Path:
    path = path.resolve()
    if path in {Path("/"), Path.home()}:
        raise ModelError("distribution package: refusing unsafe output directory")
    if path.exists():
        if not path.is_dir() or path.is_symlink() or any(path.iterdir()):
            raise ModelError("distribution package: output directory must be absent or empty")
    else:
        path.mkdir(parents=True)
    return path


def _stage_path(root: Path, destination: str | PurePosixPath) -> Path:
    path = PurePosixPath(destination)
    if not path.is_absolute() or len(path.parts) < 2 or path.parts[1] not in {"etc", "usr"}:
        raise ModelError(f"unsafe distribution payload destination: {destination}")
    return root.joinpath(*path.parts[1:])


def _write_overlay(root: Path, destination: str | PurePosixPath, content: str) -> Path:
    path = _stage_path(root, destination)
    if path.exists() or path.is_symlink():
        raise ModelError(f"distribution payload collision: {destination}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    return path


def _tree_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ModelError(f"distribution payload contains a symbolic link: {path}")
        if path.is_file():
            files.append(path)
    return files


def _tree_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(path.stat().st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _normalize(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    root.chmod(0o755)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def _installed_distribution_manifest(
    distribution: str,
    runtime: LinuxRuntime,
    artifacts: ArtifactSet,
    common: StageResult,
    target: DistributionTarget,
    architecture: str,
    overlay_paths: list[Path],
    root: Path,
) -> str:
    value = {
        "schema": "hyperflux-installed-distribution-package-v1",
        "distribution": distribution,
        "package": runtime.product.package_name,
        "version": runtime.product.version,
        "release": runtime.product.package_release,
        "architecture": architecture,
        "source_revision": artifacts.revision,
        "common_payload_sha256": common.payload_sha256,
        "python_discovery_path": target.python_discovery_for(artifacts.python),
        "overlay_files": [
            {
                "path": "/" + path.relative_to(root).as_posix(),
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
                "sha256": sha256_file(path),
            }
            for path in sorted(overlay_paths)
        ],
    }
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def _arch_hook(runtime: LinuxRuntime) -> str:
    return "\n".join(
        [
            "[Trigger]",
            "Operation = Install",
            "Operation = Upgrade",
            "Type = Package",
            f"Target = {runtime.product.package_name}",
            "",
            "[Action]",
            "Description = Complete HyperFlux Next compatibility activation",
            "When = PostTransaction",
            f"Exec = {runtime.operations.activation_path} post-update",
            "",
        ]
    )


def _arch_install(runtime: LinuxRuntime) -> str:
    activation = runtime.operations.activation_path
    return "\n".join(
        [
            "# Generated by ./hfx package distro. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            "",
            "_hyperflux_refresh_system() {",
            "  systemd-sysusers hyperflux-next.conf",
            "  systemd-tmpfiles --create hyperflux-next.conf",
            "  systemctl daemon-reload",
            "  udevadm control --reload",
            "  udevadm trigger --subsystem-match=misc --action=change",
            "}",
            "",
            "post_install() {",
            "  _hyperflux_refresh_system",
            f"  {activation} fresh-install",
            "}",
            "",
            "pre_upgrade() {",
            f"  {activation} pre-update",
            "}",
            "",
            "post_upgrade() {",
            "  _hyperflux_refresh_system",
            "}",
            "",
            "pre_remove() {",
            f"  {activation} pre-remove",
            "}",
            "",
            "post_remove() {",
            "  systemctl daemon-reload",
            "  udevadm control --reload",
            "}",
            "",
        ]
    )


def _shell_word(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _shell_array(values: tuple[str, ...] | list[str]) -> str:
    return "(" + " ".join(_shell_word(item) for item in values) + ")"


def _arch_version(version: str) -> str:
    value = version.replace("-", "_")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_]*", value):
        raise ModelError("Arch package version cannot be represented safely")
    return value


def _arch_pkgbuild(
    runtime: LinuxRuntime,
    catalog: DistributionCatalog,
    target: DistributionTarget,
    architecture: str,
    python_version: str,
    payload_sha256: str,
) -> str:
    optional = [
        f"{item.package}: {item.purpose}" for item in target.optional_dependencies
    ]
    return "\n".join(
        [
            "# Generated by ./hfx package distro. Do not edit manually.",
            "# SPDX-License-Identifier: GPL-2.0-only",
            f"pkgname={runtime.product.package_name}",
            f"pkgver={_arch_version(runtime.product.version)}",
            f"pkgrel={runtime.product.package_release}",
            f"pkgdesc={_shell_word(catalog.description)}",
            f"arch={_shell_array([architecture])}",
            "url=''",
            f"license={_shell_array(list(catalog.licenses))}",
            f"depends={_shell_array(list(target.dependencies_for(python_version)))}",
            f"optdepends={_shell_array(optional)}",
            f"conflicts={_shell_array(list(target.conflicts))}",
            "backup=('etc/hyperflux-next/bridge.json')",
            "options=('!strip' '!debug')",
            "install=hyperflux-next.install",
            "source=('payload.tar')",
            "noextract=('payload.tar')",
            f"sha256sums=('{payload_sha256}')",
            "",
            "package() {",
            "  bsdtar -xf \"$srcdir/payload.tar\" -C \"$pkgdir\"",
            "}",
            "",
        ]
    )


def _tar_payload(root: Path, destination: Path, epoch: int) -> None:
    with tarfile.open(destination, "w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = epoch
            info.mode = 0o755 if path.is_dir() else stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif path.is_file() and not path.is_symlink():
                info.size = path.stat().st_size
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                raise ModelError(f"unsupported distribution payload entry: {relative}")
    os.utime(destination, (epoch, epoch))


def _canonical_arch_buildinfo(content: bytes) -> bytes:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ModelError("Arch package BUILDINFO is not UTF-8") from error
    replacements = {"builddir": 0, "startdir": 0}
    result = []
    for line in lines:
        key, separator, _value = line.partition(" = ")
        if separator and key in replacements:
            replacements[key] += 1
            result.append(f"{key} = {CANONICAL_ARCH_BUILD_ROOT}")
        else:
            result.append(line)
    if replacements != {"builddir": 1, "startdir": 1}:
        raise ModelError("Arch package BUILDINFO path fields are incomplete")
    return ("\n".join(result) + "\n").encode("utf-8")


def _canonicalize_arch_package(package: Path, epoch: int) -> None:
    tar_descriptor, tar_name = tempfile.mkstemp(
        prefix="hyperflux-arch-canonical-", suffix=".tar", dir=package.parent
    )
    compressed_descriptor, compressed_name = tempfile.mkstemp(
        prefix="hyperflux-arch-canonical-", suffix=".pkg.tar.zst", dir=package.parent
    )
    os.close(tar_descriptor)
    os.close(compressed_descriptor)
    tar_path = Path(tar_name)
    compressed_path = Path(compressed_name)
    try:
        names: set[str] = set()
        with tarfile.open(package, "r:*") as source, tarfile.open(
            tar_path, "w", format=tarfile.GNU_FORMAT
        ) as destination:
            members = source.getmembers()
            if not members or len(members) > 10000:
                raise ModelError("Arch package member count is invalid")
            for original in members:
                path = PurePosixPath(original.name)
                if path.is_absolute() or ".." in path.parts or original.name in names:
                    raise ModelError("Arch package contains an unsafe or duplicate member")
                names.add(original.name)
                if not (original.isdir() or original.isfile()):
                    raise ModelError("Arch package contains an unsupported member type")
                member = copy.copy(original)
                member.uid = 0
                member.gid = 0
                member.uname = "root"
                member.gname = "root"
                member.mtime = epoch
                if member.isdir():
                    destination.addfile(member)
                    continue
                stream = source.extractfile(original)
                if stream is None:
                    raise ModelError("Arch package regular file has no content")
                content = stream.read()
                if original.name == ".BUILDINFO":
                    content = _canonical_arch_buildinfo(content)
                member.size = len(content)
                destination.addfile(member, io.BytesIO(content))
        result = subprocess.run(
            [
                "zstd",
                "--quiet",
                "--force",
                "--threads=1",
                "-19",
                "-o",
                str(compressed_path),
                str(tar_path),
            ],
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise ModelError(
                "Arch package canonical compression failed "
                f"with exit status {result.returncode}"
            )
        compressed_path.chmod(0o644)
        os.utime(compressed_path, (epoch, epoch))
        os.replace(compressed_path, package)
    except (OSError, subprocess.TimeoutExpired, tarfile.TarError) as error:
        raise ModelError(f"Arch package canonicalization failed: {error}") from error
    finally:
        tar_path.unlink(missing_ok=True)
        compressed_path.unlink(missing_ok=True)


def _arch_metadata(content: bytes, label: str) -> dict[str, tuple[str, ...]]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ModelError(f"Arch package {label} is not UTF-8") from error
    values: dict[str, list[str]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" = ")
        if not separator or not key:
            raise ModelError(f"Arch package {label} contains a malformed field")
        values.setdefault(key, []).append(value)
    return {key: tuple(items) for key, items in values.items()}


def _one_arch_value(
    metadata: dict[str, tuple[str, ...]], key: str, label: str
) -> str:
    values = metadata.get(key, ())
    if len(values) != 1:
        raise ModelError(f"Arch package {label} must contain exactly one {key}")
    return values[0]


def _verify_arch_package(
    package: Path,
    root: Path,
    runtime: LinuxRuntime,
    catalog: DistributionCatalog,
    target: DistributionTarget,
    artifacts: ArtifactSet,
    architecture: str,
    payload_sha256: str,
    payload_file_count: int,
) -> None:
    metadata_files: dict[str, bytes] = {}
    digest = hashlib.sha256()
    payload_files = 0
    forbidden = tuple(
        item.encode("utf-8")
        for item in {str(root), str(Path.home()), "/tmp/hyperflux-"}
        if len(item) > 1
    )
    try:
        with tarfile.open(package, "r:*") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise ModelError("Arch package contains duplicate members")
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ModelError("Arch package contains an unsafe member path")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ModelError("Arch package contains an unsupported member type")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ModelError("Arch package regular file has no content")
                content = stream.read()
                if any(value in content for value in forbidden):
                    raise ModelError("Arch package contains a private or temporary build path")
                if member.name.startswith("."):
                    metadata_files[member.name] = content
                    continue
                payload_files += 1
                digest.update(member.name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(f"{member.mode:04o}".encode("ascii"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(content).digest())
    except (OSError, tarfile.TarError) as error:
        raise ModelError(f"cannot verify Arch package: {error}") from error
    required_metadata = {".BUILDINFO", ".INSTALL", ".MTREE", ".PKGINFO"}
    if set(metadata_files) != required_metadata:
        raise ModelError("Arch package metadata members are incomplete or unexpected")
    if payload_files != payload_file_count or digest.hexdigest() != payload_sha256:
        raise ModelError("Arch package payload differs from the staged distribution root")
    if metadata_files[".INSTALL"] != _arch_install(runtime).encode("utf-8"):
        raise ModelError("Arch package install script differs from the lifecycle contract")

    package_info = _arch_metadata(metadata_files[".PKGINFO"], "PKGINFO")
    expected_version = f"{_arch_version(runtime.product.version)}-{runtime.product.package_release}"
    singles = {
        "pkgname": runtime.product.package_name,
        "pkgver": expected_version,
        "pkgdesc": catalog.description,
        "arch": architecture,
        "packager": CANONICAL_ARCH_PACKAGER,
        "builddate": str(artifacts.source_date_epoch),
    }
    for key, expected in singles.items():
        if _one_arch_value(package_info, key, "PKGINFO") != expected:
            raise ModelError(f"Arch package PKGINFO {key} does not match its authority")
    repeated = {
        "license": catalog.licenses,
        "depend": target.dependencies_for(artifacts.python),
        "optdepend": tuple(
            f"{item.package}: {item.purpose}" for item in target.optional_dependencies
        ),
        "conflict": target.conflicts,
        "backup": ("etc/hyperflux-next/bridge.json",),
    }
    for key, expected in repeated.items():
        if package_info.get(key, ()) != expected:
            raise ModelError(f"Arch package PKGINFO {key} does not match its authority")

    build_info = _arch_metadata(metadata_files[".BUILDINFO"], "BUILDINFO")
    for key, expected in {
        "pkgname": runtime.product.package_name,
        "pkgver": expected_version,
        "pkgarch": architecture,
        "packager": CANONICAL_ARCH_PACKAGER,
        "builddate": str(artifacts.source_date_epoch),
        "builddir": CANONICAL_ARCH_BUILD_ROOT,
        "startdir": CANONICAL_ARCH_BUILD_ROOT,
    }.items():
        if _one_arch_value(build_info, key, "BUILDINFO") != expected:
            raise ModelError(f"Arch package BUILDINFO {key} does not match its authority")


def _run_makepkg(workspace: Path, packages: Path, epoch: int) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(epoch),
            "PKGDEST": str(packages),
            "SRCDEST": str(workspace / "source-cache"),
            "BUILDDIR": str(workspace / "makepkg-build"),
            "PACKAGER": CANONICAL_ARCH_PACKAGER,
        }
    )
    try:
        result = subprocess.run(
            ["makepkg", "--cleanbuild", "--force", "--nodeps", "--noconfirm"],
            cwd=workspace,
            env=environment,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"Arch package build failed: {error}") from error
    if result.returncode != 0:
        raise ModelError(f"Arch package build failed with exit status {result.returncode}")


def build_distribution_package(
    root: Path,
    build_manifest: Path,
    distribution: str,
    output: Path,
) -> DistributionPackageResult:
    root = root.resolve()
    if distribution != "arch":
        raise ModelError(f"distribution package target {distribution} is not implemented yet")
    output = _new_output_directory(output)
    runtime = load_linux_runtime(root)
    catalog = load_distribution_catalog(root)
    target = catalog.targets[distribution]
    artifacts = load_artifact_set(root, build_manifest)
    architecture = target.architecture_for(artifacts.target)
    packages = output / "packages"
    packages.mkdir()

    with tempfile.TemporaryDirectory(prefix="hyperflux-arch-package-") as temporary:
        workspace_root = Path(temporary)
        common_root = workspace_root / "common-root"
        common = stage_rootfs(root, build_manifest, common_root)
        package_root = workspace_root / "package-root"
        shutil.copytree(common.root, package_root, symlinks=False)
        overlays = [
            _write_overlay(
                package_root,
                target.python_discovery_for(artifacts.python),
                runtime.operations.python_module_directory + "\n",
            ),
            _write_overlay(package_root, ARCH_POST_TRANSACTION_HOOK, _arch_hook(runtime)),
        ]
        installed_manifest = _write_overlay(
            package_root,
            INSTALLED_MANIFEST_PATH,
            _installed_distribution_manifest(
                distribution,
                runtime,
                artifacts,
                common,
                target,
                architecture,
                overlays,
                package_root,
            ),
        )
        overlays.append(installed_manifest)
        _normalize(package_root, artifacts.source_date_epoch)
        distribution_files = _tree_files(package_root)
        distribution_digest = _tree_digest(package_root, distribution_files)

        workspace = workspace_root / "makepkg"
        workspace.mkdir()
        payload = workspace / "payload.tar"
        _tar_payload(package_root, payload, artifacts.source_date_epoch)
        (workspace / "hyperflux-next.install").write_text(
            _arch_install(runtime), encoding="utf-8"
        )
        (workspace / "PKGBUILD").write_text(
            _arch_pkgbuild(
                runtime,
                catalog,
                target,
                architecture,
                artifacts.python,
                sha256_file(payload),
            ),
            encoding="utf-8",
        )
        for path in (workspace / "PKGBUILD", workspace / "hyperflux-next.install"):
            path.chmod(0o644)
            os.utime(path, (artifacts.source_date_epoch, artifacts.source_date_epoch))
        _run_makepkg(workspace, packages, artifacts.source_date_epoch)

    built = sorted(packages.glob("*.pkg.tar.*"))
    if len(built) != 1 or not built[0].is_file() or built[0].is_symlink():
        raise ModelError("Arch package build did not produce exactly one package")
    package = built[0]
    _canonicalize_arch_package(package, artifacts.source_date_epoch)
    _verify_arch_package(
        package,
        root,
        runtime,
        catalog,
        target,
        artifacts,
        architecture,
        distribution_digest,
        len(distribution_files),
    )
    manifest_value = {
        "$schema": "https://hyperflux.dev/schemas/distribution-package-build-v1.json",
        "schema": "hyperflux-distribution-package-build-v1",
        "source": {
            "revision": artifacts.revision,
            "source_date_epoch": artifacts.source_date_epoch,
            "distribution_catalog_sha256": catalog.source_sha256,
        },
        "package": {
            "distribution": distribution,
            "name": runtime.product.package_name,
            "version": runtime.product.version,
            "release": runtime.product.package_release,
            "architecture": architecture,
            "file": package.name,
            "sha256": sha256_file(package),
            "size": package.stat().st_size,
        },
        "payload": {
            "common_sha256": common.payload_sha256,
            "distribution_sha256": distribution_digest,
            "common_files": common.file_count,
            "distribution_files": len(distribution_files),
        },
    }
    manifest = output / BUILD_MANIFEST_NAME
    manifest.write_text(
        json.dumps(manifest_value, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o644)
    os.utime(manifest, (artifacts.source_date_epoch, artifacts.source_date_epoch))
    return DistributionPackageResult(
        package=package,
        manifest=manifest,
        distribution=distribution,
        architecture=architecture,
        common_payload_sha256=common.payload_sha256,
        distribution_payload_sha256=distribution_digest,
    )
