# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique
from .toolchains import load_toolchain_pins


ENVIRONMENT_KEYS = {
    "$schema",
    "schema",
    "platform",
    "base_image",
    "archive",
    "system_packages",
    "rust",
    "workspace",
    "network_policy",
}
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+_.-]{0,63}$")
REQUIRED_PACKAGES = {
    "bash",
    "binutils",
    "ca-certificates",
    "clang",
    "cmake",
    "curl",
    "dbus",
    "dkms",
    "dpkg",
    "fakeroot",
    "gcc",
    "git",
    "hidapi",
    "jq",
    "kmod",
    "linux-headers",
    "make",
    "ninja",
    "patchelf",
    "pkgconf",
    "python",
    "python-dbus",
    "python-gobject",
    "python-markdown",
    "python-pip",
    "python-setuptools",
    "python-wheel",
    "python-yaml",
    "qt5-base",
    "rpm-tools",
    "rustup",
    "sudo",
    "zstd",
}


@dataclass(frozen=True)
class SystemPackage:
    name: str
    version: str


@dataclass(frozen=True)
class DevelopmentEnvironment:
    platform: str
    image_repository: str
    image_tag: str
    image_digest: str
    archive_date: str
    archive_mirror: str
    packages: tuple[SystemPackage, ...]
    rust_toolchain: str
    rust_profile: str
    rust_components: tuple[str, ...]
    workspace_user: str
    workspace_uid: int
    workspace_path: str
    container_network_uses: tuple[str, ...]
    post_create_network_uses: tuple[str, ...]

    @property
    def image(self) -> str:
        return f"{self.image_repository}:{self.image_tag}@{self.image_digest}"


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{label}: expected a non-empty string")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ModelError(f"{label}: expected a non-empty string list")
    require_unique(value, label)
    return tuple(value)


def _arch_upstream_version(value: str, label: str) -> str:
    without_epoch = value.split(":", 1)[-1]
    try:
        upstream, release = without_epoch.rsplit("-", 1)
    except ValueError as error:
        raise ModelError(f"{label}: invalid Arch package version") from error
    if not upstream or not release:
        raise ModelError(f"{label}: invalid Arch package version")
    return upstream


def load_development_environment(root: Path) -> DevelopmentEnvironment:
    value = load_json(root / "toolchains" / "development-environment.json")
    _exact_keys(value, ENVIRONMENT_KEYS, "development environment")
    if value["schema"] != "hyperflux-development-environment-v1":
        raise ModelError("unsupported development environment schema")
    if value["$schema"] != "../schemas/development-environment.schema.json":
        raise ModelError("development environment has a non-canonical schema reference")
    if value["platform"] != "linux/amd64":
        raise ModelError("development environment must use the reviewed linux/amd64 platform")

    image = value["base_image"]
    if not isinstance(image, dict):
        raise ModelError("development environment base image must be an object")
    _exact_keys(image, {"repository", "tag", "digest"}, "development base image")
    if image["repository"] != "docker.io/library/archlinux" or image["tag"] != "base-devel":
        raise ModelError("development environment must use the reviewed Arch base-devel image")
    if not isinstance(image["digest"], str) or DIGEST.fullmatch(image["digest"]) is None:
        raise ModelError("development base image must use an immutable SHA-256 digest")

    archive = value["archive"]
    if not isinstance(archive, dict):
        raise ModelError("development archive must be an object")
    _exact_keys(archive, {"date", "mirror"}, "development archive")
    try:
        parsed_date = date.fromisoformat(_nonempty(archive["date"], "archive date"))
    except ValueError as error:
        raise ModelError("development archive date is invalid") from error
    expected_mirror = (
        f"https://archive.archlinux.org/repos/{parsed_date:%Y/%m/%d}/$repo/os/$arch"
    )
    if archive["mirror"] != expected_mirror:
        raise ModelError("development archive mirror does not match its snapshot date")

    raw_packages = value["system_packages"]
    if not isinstance(raw_packages, list) or not raw_packages:
        raise ModelError("development environment has no system packages")
    packages: list[SystemPackage] = []
    for index, package in enumerate(raw_packages):
        if not isinstance(package, dict):
            raise ModelError(f"development package {index}: expected an object")
        _exact_keys(package, {"name", "version"}, f"development package {index}")
        name = _nonempty(package["name"], f"development package {index} name")
        version = _nonempty(package["version"], f"development package {name} version")
        if PACKAGE_NAME.fullmatch(name) is None or any(character.isspace() for character in version):
            raise ModelError(f"development package {index}: invalid package pin")
        packages.append(SystemPackage(name, version))
    names = [package.name for package in packages]
    require_unique(names, "development package name")
    if names != sorted(names):
        raise ModelError("development packages must be sorted by name")
    missing_packages = sorted(REQUIRED_PACKAGES - set(names))
    if missing_packages:
        raise ModelError(f"development environment is missing required packages: {', '.join(missing_packages)}")

    rust = value["rust"]
    if not isinstance(rust, dict):
        raise ModelError("development Rust configuration must be an object")
    _exact_keys(rust, {"installer", "toolchain", "profile", "components"}, "development Rust")
    components = _string_list(rust["components"], "development Rust components")
    if rust["installer"] != "arch-rustup-package" or rust["profile"] != "minimal":
        raise ModelError("development Rust installation policy is unsupported")
    if set(components) < {"clippy", "rustfmt"}:
        raise ModelError("development Rust environment requires clippy and rustfmt")
    pins = load_toolchain_pins(root)
    if rust["toolchain"] != pins.rustup_toolchain:
        raise ModelError("development Rust toolchain differs from toolchains/pins.json")
    package_versions = {package.name: package.version for package in packages}
    expected_versions = {
        "clang": pins.clang.removeprefix("clang version "),
        "cmake": pins.cmake.removeprefix("cmake version "),
        "ninja": pins.ninja,
        "python-pip": pins.pip,
        "python-setuptools": pins.setuptools,
        "python-wheel": pins.wheel,
    }
    for package_name, expected in expected_versions.items():
        if _arch_upstream_version(package_versions[package_name], package_name) != expected:
            raise ModelError(f"{package_name} pin differs from toolchains/pins.json")
    if not package_versions["python"].startswith(pins.python + "."):
        raise ModelError("Python package pin differs from toolchains/pins.json")

    workspace = value["workspace"]
    if not isinstance(workspace, dict):
        raise ModelError("development workspace must be an object")
    _exact_keys(workspace, {"user", "uid", "path"}, "development workspace")
    path = PurePosixPath(workspace["path"])
    if (
        workspace["user"] != "hyperflux"
        or workspace["uid"] != 1000
        or not path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != "/workspaces/hyperflux-next"
    ):
        raise ModelError("development workspace identity is unsupported")

    network = value["network_policy"]
    if not isinstance(network, dict):
        raise ModelError("development network policy must be an object")
    _exact_keys(network, {"container_build", "post_create", "verification"}, "development network policy")
    container_uses = _string_list(network["container_build"], "container build network uses")
    post_create_uses = _string_list(network["post_create"], "post-create network uses")
    if set(container_uses) != {"arch-snapshot-packages", "pinned-rust-toolchain"}:
        raise ModelError("container build network policy is incomplete")
    if post_create_uses != ("pinned-upstream-checkouts",):
        raise ModelError("post-create network policy is incomplete")
    if network["verification"] != "forbidden":
        raise ModelError("verification must remain network-free")

    return DevelopmentEnvironment(
        platform=value["platform"],
        image_repository=image["repository"],
        image_tag=image["tag"],
        image_digest=image["digest"],
        archive_date=archive["date"],
        archive_mirror=archive["mirror"],
        packages=tuple(packages),
        rust_toolchain=rust["toolchain"],
        rust_profile=rust["profile"],
        rust_components=components,
        workspace_user=workspace["user"],
        workspace_uid=workspace["uid"],
        workspace_path=workspace["path"],
        container_network_uses=container_uses,
        post_create_network_uses=post_create_uses,
    )
