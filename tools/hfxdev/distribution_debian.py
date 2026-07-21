# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

from .distribution_native import NativePackageContext, normalize_tree, tree_files
from .model import ModelError


DEBIAN_MAINTAINER = "HyperFlux Next Build System <build@hyperflux.invalid>"
DEBIAN_CONFIGURATION = "/etc/hyperflux-next/bridge.json"


def _debian_version(version: str, release: int) -> str:
    upstream = version.replace("-", "~")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", upstream):
        raise ModelError("Debian package version cannot be represented safely")
    return f"{upstream}-{release}"


def _installed_size(root: Path) -> int:
    size = sum(path.stat().st_size for path in tree_files(root))
    return max(1, (size + 1023) // 1024)


def _debian_control(context: NativePackageContext) -> str:
    dependencies = ", ".join(context.target.dependencies_for(context.artifacts.python))
    optional = ", ".join(item.package for item in context.target.optional_dependencies)
    conflicts = ", ".join(context.target.conflicts)
    lines = [
        f"Package: {context.runtime.product.package_name}",
        f"Version: {_debian_version(context.runtime.product.version, context.runtime.product.package_release)}",
        "Section: kernel",
        "Priority: optional",
        f"Architecture: {context.architecture}",
        f"Maintainer: {DEBIAN_MAINTAINER}",
        f"Installed-Size: {_installed_size(context.package_root)}",
        f"Depends: {dependencies}",
    ]
    if optional:
        lines.append(f"Suggests: {optional}")
    if conflicts:
        lines.append(f"Conflicts: {conflicts}")
    lines.extend(
        [
            f"Description: {context.catalog.description}",
            " HyperFlux Next provides the receiver transport, local bridge, SDK,",
            " diagnostics, and optional application adapters as one coherent package.",
            "",
        ]
    )
    return "\n".join(lines)


def _refresh_system_lines() -> list[str]:
    return [
        "systemd-sysusers hyperflux-next.conf",
        "systemd-tmpfiles --create hyperflux-next.conf",
        "systemctl daemon-reload",
        "udevadm control --reload",
        "udevadm trigger --subsystem-match=misc --action=change",
    ]


def _debian_scripts(context: NativePackageContext) -> dict[str, str]:
    activation = context.runtime.operations.activation_path
    module = context.runtime.kernel.dkms_name
    version = context.runtime.product.version
    postinst = [
        "#!/bin/sh",
        "set -eu",
        "",
        'case "${1:-}" in',
        "  configure)",
        "    if [ ! -x /usr/lib/dkms/common.postinst ]; then",
        '      echo "HyperFlux Next requires DKMS common.postinst support." >&2',
        "      exit 1",
        "    fi",
        f"    /usr/lib/dkms/common.postinst {module} {version} /usr/share/hyperflux-next \"\" \"${{2:-}}\"",
    ]
    postinst.extend(f"    {line}" for line in _refresh_system_lines())
    postinst.extend(
        [
            '    if [ -n "${2:-}" ]; then',
            f"      {activation} post-update",
            "    else",
            f"      {activation} fresh-install",
            "    fi",
            "    ;;",
            "esac",
            "exit 0",
            "",
        ]
    )
    return {
        "preinst": "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                "",
                'case "${1:-}" in',
                "  upgrade)",
                f"    {activation} pre-update",
                "    ;;",
                "esac",
                "exit 0",
                "",
            ]
        ),
        "postinst": "\n".join(postinst),
        "prerm": "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                "",
                'case "${1:-}" in',
                "  remove)",
                f"    {activation} pre-remove",
                "    ;;",
                "esac",
                "",
                'case "${1:-}" in',
                "  remove|upgrade|deconfigure|failed-upgrade)",
                f"    if dkms status -m {module} -v {version} 2>/dev/null | grep -q .; then",
                f"      dkms remove -m {module} -v {version} --all || true",
                "    fi",
                "    ;;",
                "esac",
                "exit 0",
                "",
            ]
        ),
        "postrm": "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                "",
                'case "${1:-}" in',
                "  remove|purge)",
                "    systemctl daemon-reload",
                "    udevadm control --reload",
                "    ;;",
                "esac",
                "exit 0",
                "",
            ]
        ),
    }


def _debian_md5sums(root: Path) -> str:
    lines = []
    for path in tree_files(root):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()}  {relative}")
    return "\n".join(lines) + "\n"


def _write_control_files(context: NativePackageContext, root: Path) -> dict[str, bytes]:
    control = root / "DEBIAN"
    control.mkdir(mode=0o755)
    content = {
        "control": _debian_control(context),
        "conffiles": DEBIAN_CONFIGURATION + "\n",
        "md5sums": _debian_md5sums(context.package_root),
        **_debian_scripts(context),
    }
    result = {}
    for name, value in content.items():
        path = control / name
        payload = value.encode("utf-8")
        path.write_bytes(payload)
        path.chmod(0o755 if name in {"preinst", "postinst", "prerm", "postrm"} else 0o644)
        result[name] = payload
    return result


def _run_dpkg_deb(root: Path, package: Path, epoch: int) -> None:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    try:
        result = subprocess.run(
            [
                "dpkg-deb",
                "--build",
                "--root-owner-group",
                "--uniform-compression",
                "--threads-max=1",
                "-Zxz",
                "-z9",
                str(root),
                str(package),
            ],
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"Debian package build failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ModelError(
            f"Debian package build failed with exit status {result.returncode}: {detail}"
        )


def _dpkg_tar(package: Path, option: str) -> bytes:
    try:
        result = subprocess.run(
            ["dpkg-deb", option, str(package)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"cannot inspect Debian package: {error}") from error
    if result.returncode != 0:
        raise ModelError(
            f"cannot inspect Debian package with {option}: exit status {result.returncode}"
        )
    return result.stdout


def _normalized_member_name(member: tarfile.TarInfo) -> str:
    name = member.name.removeprefix("./")
    return PurePosixPath(name).as_posix()


def _verify_debian_package(
    context: NativePackageContext, package: Path, controls: dict[str, bytes]
) -> None:
    control_members: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    try:
        with tarfile.open(
            fileobj=io.BytesIO(_dpkg_tar(package, "--ctrl-tarfile")), mode="r:*"
        ) as archive:
            for member in archive.getmembers():
                name = _normalized_member_name(member)
                if name in {"", "."} or member.isdir():
                    continue
                if not member.isfile() or name in control_members:
                    raise ModelError("Debian control archive contains an unsupported member")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ModelError("Debian control archive file has no content")
                control_members[name] = (member, stream.read())
    except tarfile.TarError as error:
        raise ModelError(f"cannot parse Debian control archive: {error}") from error
    if set(control_members) != set(controls):
        raise ModelError("Debian control archive members are incomplete or unexpected")
    for name, expected in controls.items():
        member, actual = control_members[name]
        expected_mode = (
            0o755 if name in {"preinst", "postinst", "prerm", "postrm"} else 0o644
        )
        if (
            actual != expected
            or member.mode != expected_mode
            or member.uid != 0
            or member.gid != 0
            or member.mtime != context.artifacts.source_date_epoch
        ):
            raise ModelError(f"Debian control member {name} differs from its authority")

    payload_entries: list[tuple[str, int, bytes]] = []
    forbidden = tuple(
        value.encode("utf-8")
        for value in {
            str(context.repository_root),
            str(Path.home()),
            "/tmp/hyperflux-",
        }
        if len(value) > 1
    )
    try:
        with tarfile.open(
            fileobj=io.BytesIO(_dpkg_tar(package, "--fsys-tarfile")), mode="r:*"
        ) as archive:
            names: set[str] = set()
            for member in archive.getmembers():
                name = _normalized_member_name(member)
                if name in {"", "."}:
                    continue
                if (
                    name in names
                    or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts
                ):
                    raise ModelError("Debian payload contains an unsafe or duplicate member")
                names.add(name)
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.mtime != context.artifacts.source_date_epoch
                ):
                    raise ModelError("Debian payload metadata is not canonical")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ModelError("Debian payload contains an unsupported member type")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ModelError("Debian payload file has no content")
                content = stream.read()
                if any(value in content for value in forbidden):
                    raise ModelError("Debian package contains a private or temporary build path")
                payload_entries.append((name, member.mode, content))
    except tarfile.TarError as error:
        raise ModelError(f"cannot parse Debian payload archive: {error}") from error
    digest = hashlib.sha256()
    for name, mode, content in sorted(payload_entries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    if (
        len(payload_entries) != context.payload_file_count
        or digest.hexdigest() != context.payload_sha256
    ):
        raise ModelError("Debian package payload differs from the staged distribution root")


def build_debian_package(context: NativePackageContext) -> Path:
    root = context.workspace_root / "debian-root"
    shutil.copytree(context.package_root, root, symlinks=False)
    controls = _write_control_files(context, root)
    normalize_tree(root, context.artifacts.source_date_epoch)
    version = _debian_version(
        context.runtime.product.version, context.runtime.product.package_release
    )
    package = context.packages / (
        f"{context.runtime.product.package_name}_{version}_{context.architecture}.deb"
    )
    _run_dpkg_deb(root, package, context.artifacts.source_date_epoch)
    package.chmod(0o644)
    os.utime(
        package,
        (context.artifacts.source_date_epoch, context.artifacts.source_date_epoch),
    )
    _verify_debian_package(context, package, controls)
    return package
