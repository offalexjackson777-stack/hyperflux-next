# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
from pathlib import Path

from .model import ModelError
from .openrazer_native_route import prepare_openrazer_native_source
from .verify_compiled import _build_kernel_module_copy
from .verify_integration import _openrazer_source


SCHEMA = "hyperflux-kernel-canary-result-v1"
BUILD_TIMEOUT_SECONDS = 900
MODULES = ("hyperflux-next", "openrazer")
MODULE_TITLES = {
    "hyperflux-next": "HyperFlux driver",
    "openrazer": "Patched OpenRazer driver",
}


@dataclass(frozen=True)
class CanaryBuild:
    kernel: str
    module: str
    passed: bool
    detail: str


def kernel_release(build_directory: Path) -> str:
    release_file = build_directory / "include" / "config" / "kernel.release"
    if release_file.is_file():
        release = release_file.read_text(encoding="utf-8").strip()
        if release and "/" not in release and release not in {".", ".."}:
            return release
    raise ModelError(f"kernel header directory has no release identity: {build_directory}")


def _header_directories(values: list[Path]) -> list[tuple[str, Path]]:
    if not values:
        raise ModelError("kernel canary needs at least one --header-dir")
    seen: set[str] = set()
    directories: list[tuple[str, Path]] = []
    for value in values:
        if not value.is_absolute() or not (value / "Makefile").is_file():
            raise ModelError(f"kernel header directory is unavailable: {value}")
        release = kernel_release(value)
        if release in seen:
            continue
        seen.add(release)
        directories.append((release, value))
    return directories


def _build_environment(build_directory: Path) -> tuple[dict[str, str] | None, bool]:
    # A source tree prepared with modules_prepare has no Module.symvers, so modpost
    # cannot resolve symbols exported by the kernel. Everything still compiles with
    # W=1; only the link-time symbol check becomes a warning.
    if (build_directory / "Module.symvers").is_file():
        return None, True
    return {"KBUILD_MODPOST_WARN": "1"}, False


def run_kernel_canary(root: Path, output: Path, header_directories: list[Path]) -> int:
    root = root.resolve()
    output = output if output.is_absolute() else root / output
    kernels = _header_directories(header_directories)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    prepared = prepare_openrazer_native_source(
        root, _openrazer_source(root), output / "openrazer-source"
    )
    sources = {
        "hyperflux-next": root / "driver" / "kernel",
        "openrazer": prepared.root / "driver",
    }

    builds: list[CanaryBuild] = []
    symbols_checked: dict[str, bool] = {}
    for release, build_directory in kernels:
        environment, checked = _build_environment(build_directory)
        symbols_checked[release] = checked
        for module in MODULES:
            try:
                _build_kernel_module_copy(
                    root,
                    build_directory,
                    sources[module],
                    output / "builds" / release / module,
                    label=f"{MODULE_TITLES[module]} build against {release}",
                    timeout_seconds=BUILD_TIMEOUT_SECONDS,
                    environment=environment,
                )
            except ModelError as error:
                builds.append(CanaryBuild(release, module, False, str(error)))
            else:
                builds.append(CanaryBuild(release, module, True, "built with W=1"))

    passed = all(build.passed for build in builds)
    result = {
        "schema": SCHEMA,
        "status": "passed" if passed else "failed",
        "kernels": [
            {
                "release": release,
                "symbols_checked": symbols_checked[release],
                "builds": [
                    {"module": build.module, "passed": build.passed, "detail": build.detail}
                    for build in builds
                    if build.kernel == release
                ],
            }
            for release, _ in kernels
        ],
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "summary.md").write_text(kernel_canary_summary(result), encoding="utf-8")
    return 0 if passed else 1


def kernel_canary_summary(result: dict) -> str:
    lines = [
        "## Kernel canary",
        "",
        "| Kernel | " + " | ".join(MODULE_TITLES[module] for module in MODULES) + " |",
        "| --- | " + " | ".join("---" for _ in MODULES) + " |",
    ]
    unchecked: list[str] = []
    for kernel in result["kernels"]:
        cells = {
            build["module"]: "Builds" if build["passed"] else "**Fails**"
            for build in kernel["builds"]
        }
        lines.append(
            f"| `{kernel['release']}` | "
            + " | ".join(cells[module] for module in MODULES)
            + " |"
        )
        if not kernel["symbols_checked"]:
            unchecked.append(f"`{kernel['release']}`")
    lines.append("")
    if unchecked:
        lines.append(
            "Prepared source trees without `Module.symvers` ("
            + ", ".join(unchecked)
            + ") cannot check kernel symbols at link time; their builds still compile with `W=1`."
        )
        lines.append("")
    lines.append(
        "This build never gates merges. A failure is an early warning that a coming kernel"
        " changes an interface the drivers use."
    )
    return "\n".join(lines) + "\n"
