# SPDX-License-Identifier: GPL-2.0-only

"""Build the OpenRGB plugin inside a release that builds OpenRGB its own way.

OpenRGB loads a plugin only when both use the same Qt. The shared package
build makes the Arch plugin against Qt 6. A release whose OpenRGB uses another
Qt, such as Debian 14 with Qt 5.15, builds the plugin from the same pinned
sources inside the release, with the release's own compiler and libraries.
"""

from __future__ import annotations

import os
from pathlib import Path

from .distributions import OPENRGB_PLUGIN_BUILD, DistributionTarget
from .install import load_install_manifest
from .model import ModelError
from .package_pipeline import (
    ArtifactSet,
    _pinned_source,
    _run,
    prepare_openrgb_native_source,
)


OPENRGB_CAPABILITY = "openrgb-source"


def openrgb_source(root: Path) -> Path:
    """The pinned OpenRGB checkout: `HFX_OPENRGB_SOURCE_DIR` or the prepared one."""

    value = os.environ.get("HFX_OPENRGB_SOURCE_DIR")
    source = Path(value) if value is not None else root / ".hfx" / "upstreams" / "openrgb"
    if not source.is_absolute():
        raise ModelError("HFX_OPENRGB_SOURCE_DIR must name an absolute path")
    if not (source / "OpenRGBPluginInterface.h").is_file():
        raise ModelError("pinned OpenRGB checkout is unavailable; run ./hfx upstream prepare")
    return _pinned_source(root, OPENRGB_CAPABILITY, source.resolve())


def build_release_openrgb_plugin(
    root: Path,
    artifacts: ArtifactSet,
    target: DistributionTarget,
    work: Path,
) -> Path:
    """Build the plugin for `target` in `work` and return the built module."""

    if not target.openrgb_plugin_release_build:
        raise ModelError(f"distribution {target.id} does not build the OpenRGB plugin itself")
    build = load_install_manifest(root).build(OPENRGB_PLUGIN_BUILD)
    if build.capability != OPENRGB_CAPABILITY or not build.source or not build.output:
        raise ModelError(f"build {build.id}: not an OpenRGB plugin build")
    root = root.resolve()
    work.mkdir(parents=True)
    upstream = openrgb_source(root)
    native = prepare_openrgb_native_source(root, upstream, work / "native-source").root
    build_directory = work / "build"
    # Keep local paths out of the module, as the shared build does.
    remaps = (
        (build_directory, ".hfx-build"),
        (native, f".upstream/{OPENRGB_CAPABILITY}-native"),
        (upstream, f".upstream/{OPENRGB_CAPABILITY}"),
        (root, "."),
    )
    flags = " ".join(
        f"-ffile-prefix-map={source}={destination} "
        f"-fdebug-prefix-map={source}={destination}"
        for source, destination in remaps
    )
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(artifacts.source_date_epoch),
            "CXXFLAGS": f"{environment.get('CXXFLAGS', '')} {flags}".strip(),
            "CFLAGS": f"{environment.get('CFLAGS', '')} {flags}".strip(),
        }
    )
    environment.pop("CMAKE_GENERATOR", None)
    _run(
        [
            "cmake",
            "-S",
            str(root / build.source),
            "-B",
            str(build_directory),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_TESTING=OFF",
            f"-DHFX_SOURCE_REVISION={artifacts.revision}",
            f"-DHFX_OPENRGB_QT_MAJOR={target.openrgb_qt_major}",
            f"-DHFX_OPENRGB_SOURCE_DIR={upstream}",
            f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native}",
        ],
        cwd=root,
        environment=environment,
        label=f"configure the {target.id} OpenRGB plugin",
    )
    _run(
        [
            "cmake",
            "--build",
            str(build_directory),
            "--target",
            build.target or "",
            "--parallel",
            str(os.cpu_count() or 1),
        ],
        cwd=root,
        environment=environment,
        label=f"build the {target.id} OpenRGB plugin",
        timeout_seconds=1800,
    )
    module = build_directory / build.output
    if not module.is_file() or module.is_symlink():
        raise ModelError(f"build {build.id}: the {target.id} plugin was not built")
    # Strip as dh_strip does for a shared object; Debian refuses unstripped ones.
    _run(
        [
            "strip",
            "--strip-unneeded",
            "--remove-section=.comment",
            "--remove-section=.note",
            str(module),
        ],
        cwd=root,
        environment=environment,
        label=f"strip the {target.id} OpenRGB plugin",
    )
    return module
