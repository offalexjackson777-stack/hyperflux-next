# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from .distribution_native import (
    NativePackageContext,
    tree_digest,
    tree_files,
    write_payload_tar,
)
from .model import ModelError


RPM_BUILDHOST = "hyperflux.invalid"
RPM_CONFIGURATION = "/etc/hyperflux-next/bridge.json"
RPM_LICENSE_PATH = "/usr/share/licenses/hyperflux-next/LICENSE"
RPM_SCRIPT_TAGS = {
    "pre": ("PREINPROG", "PREIN"),
    "post": ("POSTINPROG", "POSTIN"),
    "preun": ("PREUNPROG", "PREUN"),
    "postun": ("POSTUNPROG", "POSTUN"),
    "posttrans": ("POSTTRANSPROG", "POSTTRANS"),
}


def _rpm_version(version: str) -> str:
    value = version.replace("-", "~")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", value):
        raise ModelError("RPM package version cannot be represented safely")
    return value


def _rpm_dependency(value: str, label: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"([A-Za-z0-9][A-Za-z0-9+_.-]{0,95})"
        r"(?:(>=|<=|=|>|<)([A-Za-z0-9][A-Za-z0-9.+~_-]{0,63}))?",
        value,
    )
    if match is None:
        raise ModelError(f"RPM {label} is not representable safely")
    return match.group(1), match.group(2) or "", match.group(3) or ""


def _plain_rpm_dependency(value: str, label: str) -> str:
    name, operator, _version = _rpm_dependency(value, label)
    if operator:
        raise ModelError(f"RPM {label} must be a plain package name")
    return name


def _format_rpm_dependency(value: str, label: str) -> str:
    name, operator, version = _rpm_dependency(value, label)
    return f"{name} {operator} {version}" if operator else name


def _rpm_license(context: NativePackageContext) -> str:
    return " AND ".join(context.catalog.licenses)


def _refresh_system() -> list[str]:
    return [
        "systemd-sysusers hyperflux-next.conf",
        "systemd-tmpfiles --create hyperflux-next.conf",
        "systemctl daemon-reload",
        "udevadm control --reload",
        "udevadm trigger --subsystem-match=misc --action=change",
    ]


def _rpm_scripts(context: NativePackageContext) -> dict[str, str]:
    activation = context.runtime.operations.activation_path
    module = context.runtime.kernel.dkms_name
    version = context.runtime.product.version
    update_state = context.runtime.update_state_path
    post = [
        "set -eu",
        f"dkms add -m {module} -v {version} --rpm_safe_upgrade",
        f"dkms autoinstall -m {module} -v {version}",
        *_refresh_system(),
        'if [ "$1" -eq 1 ]; then',
        f"  {activation} fresh-install",
        "fi",
    ]
    return {
        "pre": "\n".join(
            [
                "set -eu",
                'if [ "$1" -gt 1 ]; then',
                f"  {activation} pre-update",
                "fi",
            ]
        ),
        "post": "\n".join(post),
        "preun": "\n".join(
            [
                "set -eu",
                'if [ "$1" -eq 0 ]; then',
                f"  {activation} pre-remove",
                "fi",
                f"dkms remove -m {module} -v {version} --all --rpm_safe_upgrade",
            ]
        ),
        "postun": "\n".join(
            [
                "set -eu",
                'if [ "$1" -eq 0 ]; then',
                "  systemctl daemon-reload",
                "  udevadm control --reload",
                "fi",
            ]
        ),
        "posttrans": "\n".join(
            [
                "set -eu",
                f"if [ -f {update_state} ]; then",
                f"  {activation} post-update",
                "fi",
            ]
        ),
    }


def _rpm_files(context: NativePackageContext) -> list[str]:
    result = []
    for path in tree_files(context.package_root):
        destination = "/" + path.relative_to(context.package_root).as_posix()
        if not re.fullmatch(r"/[A-Za-z0-9._+@%/=-]+", destination):
            raise ModelError("RPM package contains a path that cannot be represented safely")
        mode = f"{stat.S_IMODE(path.stat().st_mode):04o}"
        prefix = f"%attr({mode},root,root)"
        if destination == RPM_CONFIGURATION:
            prefix = f"%config(noreplace) {prefix}"
        elif destination == RPM_LICENSE_PATH:
            prefix = f"%license {prefix}"
        result.append(f"{prefix} {destination}")
    return result


def _rpm_spec(context: NativePackageContext) -> str:
    dependencies = [
        _format_rpm_dependency(value, "dependency")
        for value in context.target.dependencies_for(context.artifacts.python)
    ]
    optional = [
        _plain_rpm_dependency(item.package, "suggestion")
        for item in context.target.optional_dependencies
    ]
    conflicts = [
        _plain_rpm_dependency(value, "conflict")
        for value in context.target.conflicts
    ]
    scripts = _rpm_scripts(context)
    lines = [
        "%global debug_package %{nil}",
        "%global __os_install_post %{nil}",
        f"Name: {context.runtime.product.package_name}",
        f"Version: {_rpm_version(context.runtime.product.version)}",
        f"Release: {context.runtime.product.package_release}",
        f"Summary: {context.catalog.description}",
        f"License: {_rpm_license(context)}",
        f"BuildArch: {context.architecture}",
        "Source0: payload.tar",
        "AutoReqProv: no",
    ]
    lines.extend(f"Requires: {value}" for value in dependencies)
    lines.extend(f"Suggests: {value}" for value in optional)
    lines.extend(f"Conflicts: {value}" for value in conflicts)
    lines.extend(
        [
            "",
            "%description",
            context.catalog.description,
            "",
            "%prep",
            "",
            "%build",
            "",
            "%install",
            'rm -rf "%{buildroot}"',
            'mkdir -p "%{buildroot}"',
            'tar -xf "%{SOURCE0}" -C "%{buildroot}"',
            "",
        ]
    )
    for name in ("pre", "post", "preun", "postun", "posttrans"):
        lines.extend((f"%{name}", scripts[name], ""))
    lines.append("%files")
    lines.extend(_rpm_files(context))
    lines.append("")
    return "\n".join(lines)


def _run_rpmbuild(context: NativePackageContext, topdir: Path, spec: Path) -> None:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(context.artifacts.source_date_epoch)
    command = [
        "rpmbuild",
        "-bb",
        "--nodeps",
        "--target",
        context.architecture,
        "--define",
        f"_topdir {topdir}",
        "--define",
        f"_buildhost {RPM_BUILDHOST}",
        "--define",
        "use_source_date_epoch_as_buildtime 1",
        "--define",
        "build_mtime_policy clamp_to_source_date_epoch",
        "--define",
        "_binary_payload w19.zstdio",
        "--define",
        "_build_id_links none",
        str(spec),
    ]
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"RPM package build failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ModelError(
            f"RPM package build failed with exit status {result.returncode}: {detail}"
        )


def _rpm_query(package: Path, query_format: str) -> str:
    try:
        result = subprocess.run(
            ["rpm", "-qp", "--qf", query_format, str(package)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"cannot inspect RPM package: {error}") from error
    if result.returncode != 0:
        raise ModelError(
            f"cannot inspect RPM package: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout


def _rpm_names(package: Path, tag: str) -> tuple[str, ...]:
    output = _rpm_query(package, f"[%{{{tag}}}\\n]")
    return tuple(line for line in output.splitlines() if line and line != "(none)")


def _rpm_relations(package: Path, family: str) -> set[tuple[str, str, str]]:
    output = _rpm_query(
        package,
        f"[%{{{family}NAME}}\\t%{{{family}FLAGS:depflags}}\\t"
        f"%{{{family}VERSION}}\\n]",
    )
    result = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise ModelError(f"RPM {family.lower()} metadata is malformed")
        name, operator, version = fields
        result.add(
            (
                name,
                "" if operator in {"", "(none)"} else operator,
                "" if version in {"", "(none)"} else version,
            )
        )
    return result


def _verify_rpm_scripts(
    context: NativePackageContext, package: Path, expected: dict[str, str]
) -> None:
    for name, (program_tag, content_tag) in RPM_SCRIPT_TAGS.items():
        program = _rpm_query(package, f"%{{{program_tag}}}").strip()
        content = _rpm_query(package, f"%{{{content_tag}}}").strip()
        if program != "/bin/sh" or content != expected[name].strip():
            raise ModelError(f"RPM {name} script differs from its authority")


def _extract_rpm(package: Path, destination: Path) -> None:
    destination.mkdir()
    try:
        converter = subprocess.run(
            ["rpm2cpio", str(package)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        extractor = subprocess.run(
            ["cpio", "-idm", "--quiet", "--no-absolute-filenames"],
            cwd=destination,
            input=converter.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"cannot extract RPM payload: {error}") from error
    if converter.returncode != 0 or extractor.returncode != 0:
        detail = (converter.stderr + extractor.stderr).decode(
            "utf-8", errors="replace"
        )
        raise ModelError(f"cannot extract RPM payload: {detail.strip()}")


def _verify_rpm_package(context: NativePackageContext, package: Path) -> None:
    database = context.workspace_root / "rpm-verification-db"
    database.mkdir()
    try:
        initialization = subprocess.run(
            ["rpm", "--dbpath", str(database), "--initdb"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if initialization.returncode != 0:
            detail = initialization.stderr.strip() or initialization.stdout.strip()
            raise ModelError(f"cannot initialize private RPM verification: {detail}")
        integrity = subprocess.run(
            [
                "rpm",
                "--dbpath",
                str(database),
                "--nosignature",
                "-K",
                str(package),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"cannot verify RPM package integrity: {error}") from error
    if integrity.returncode != 0:
        detail = integrity.stderr.strip() or integrity.stdout.strip()
        raise ModelError(f"RPM package integrity verification failed: {detail}")
    version = _rpm_version(context.runtime.product.version)
    fields = _rpm_query(
        package,
        "%{NAME}\\n%{VERSION}\\n%{RELEASE}\\n%{ARCH}\\n%{BUILDTIME}\\n"
        "%{BUILDHOST}\\n%{LICENSE}\\n%{PAYLOADCOMPRESSOR}\\n",
    ).splitlines()
    expected = [
        context.runtime.product.package_name,
        version,
        str(context.runtime.product.package_release),
        context.architecture,
        str(context.artifacts.source_date_epoch),
        RPM_BUILDHOST,
        _rpm_license(context),
        "zstd",
    ]
    if fields != expected:
        raise ModelError("RPM package metadata differs from its authority")

    requirements = _rpm_relations(package, "REQUIRE")
    declared_requirements = {
        _rpm_dependency(value, "dependency")
        for value in context.target.dependencies_for(context.artifacts.python)
    }
    unexpected = {
        relation
        for relation in requirements - declared_requirements
        if not relation[0].startswith("rpmlib(") and relation[0] != "/bin/sh"
    }
    if not declared_requirements <= requirements or unexpected:
        raise ModelError("RPM package dependencies differ from their authority")
    if set(_rpm_names(package, "SUGGESTNAME")) != {
        item.package for item in context.target.optional_dependencies
    }:
        raise ModelError("RPM package suggestions differ from their authority")
    if set(_rpm_names(package, "CONFLICTNAME")) != set(context.target.conflicts):
        raise ModelError("RPM package conflicts differ from their authority")
    _verify_rpm_scripts(context, package, _rpm_scripts(context))

    file_lines = _rpm_query(
        package,
        "[%{FILENAMES}\\t%{FILEMODES:octal}\\t%{FILEUSERNAME}\\t"
        "%{FILEGROUPNAME}\\t%{FILEMTIMES}\\t%{FILEFLAGS}\\n]",
    ).splitlines()
    expected_files = {}
    for path in tree_files(context.package_root):
        destination = "/" + path.relative_to(context.package_root).as_posix()
        flags = "17" if destination == RPM_CONFIGURATION else "0"
        if destination == RPM_LICENSE_PATH:
            flags = "128"
        expected_files[destination] = (
            f"{stat.S_IFREG | stat.S_IMODE(path.stat().st_mode):o}",
            "root",
            "root",
            str(context.artifacts.source_date_epoch),
            flags,
        )
    actual_files = {}
    for line in file_lines:
        fields = line.split("\t")
        if len(fields) != 6 or fields[0] in actual_files:
            raise ModelError("RPM package file metadata is malformed")
        actual_files[fields[0]] = tuple(fields[1:])
    if actual_files != expected_files:
        raise ModelError("RPM package file metadata differs from the staged root")

    extracted = context.workspace_root / "rpm-extracted"
    _extract_rpm(package, extracted)
    extracted_files = tree_files(extracted)
    if (
        len(extracted_files) != context.payload_file_count
        or tree_digest(extracted, extracted_files) != context.payload_sha256
    ):
        raise ModelError("RPM package payload differs from the staged distribution root")
    forbidden = {
        str(context.repository_root),
        str(Path.home()),
        "/tmp/hyperflux-",
    }
    for path in extracted_files:
        content = path.read_bytes()
        if any(value.encode("utf-8") in content for value in forbidden if len(value) > 1):
            raise ModelError("RPM package contains a private or temporary build path")


def build_rpm_package(context: NativePackageContext) -> Path:
    topdir = context.workspace_root / "rpmbuild"
    for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (topdir / name).mkdir(parents=True)
    payload = topdir / "SOURCES/payload.tar"
    write_payload_tar(
        context.package_root, payload, context.artifacts.source_date_epoch
    )
    spec = topdir / "SPECS/hyperflux-next.spec"
    spec.write_text(_rpm_spec(context), encoding="utf-8")
    spec.chmod(0o644)
    os.utime(
        spec,
        (context.artifacts.source_date_epoch, context.artifacts.source_date_epoch),
    )
    _run_rpmbuild(context, topdir, spec)
    built = sorted((topdir / "RPMS").rglob("*.rpm"))
    if len(built) != 1 or not built[0].is_file() or built[0].is_symlink():
        raise ModelError("RPM package build did not produce exactly one package")
    package = context.packages / built[0].name
    shutil.copyfile(built[0], package)
    package.chmod(0o644)
    os.utime(
        package,
        (context.artifacts.source_date_epoch, context.artifacts.source_date_epoch),
    )
    _verify_rpm_package(context, package)
    return package
