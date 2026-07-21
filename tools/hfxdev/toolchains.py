# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import os
from pathlib import Path
import platform
import subprocess

from .model import ModelError, load_json


PIN_KEYS = {
    "$schema",
    "schema",
    "rustup_toolchain",
    "rustc",
    "cargo",
    "python",
    "pip",
    "setuptools",
    "wheel",
    "clang",
    "cmake",
    "ninja",
}


@dataclass(frozen=True)
class ToolchainPins:
    rustup_toolchain: str
    rustc: str
    cargo: str
    python: str
    pip: str
    setuptools: str
    wheel: str
    clang: str
    cmake: str
    ninja: str


def load_toolchain_pins(root: Path) -> ToolchainPins:
    value = load_json(root / "toolchains" / "pins.json")
    if set(value) != PIN_KEYS or value["schema"] != "hyperflux-toolchain-pins-v1":
        raise ModelError("toolchain pins have missing, unknown, or unsupported fields")
    for key in PIN_KEYS - {"$schema", "schema"}:
        if not isinstance(value[key], str) or not value[key].strip():
            raise ModelError(f"toolchain pin {key} must be a non-empty string")
    return ToolchainPins(
        rustup_toolchain=value["rustup_toolchain"],
        rustc=value["rustc"],
        cargo=value["cargo"],
        python=value["python"],
        pip=value["pip"],
        setuptools=value["setuptools"],
        wheel=value["wheel"],
        clang=value["clang"],
        cmake=value["cmake"],
        ninja=value["ninja"],
    )


def toolchain_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["RUSTUP_TOOLCHAIN"] = load_toolchain_pins(root).rustup_toolchain
    return environment


def _output(command: list[str], environment: dict[str, str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect {label}: {error}") from error
    return result.stdout.strip()


def current_toolchain_identity(root: Path) -> dict[str, str]:
    environment = toolchain_environment(root)
    rust_verbose = _output(["rustc", "-vV"], environment, "Rust target")
    target = next(
        (line.removeprefix("host: ") for line in rust_verbose.splitlines() if line.startswith("host: ")),
        "",
    )
    if not target:
        raise ModelError("Rust target identity is unavailable")
    return {
        "rustc": _output(["rustc", "--version"], environment, "rustc"),
        "cargo": _output(["cargo", "--version"], environment, "cargo"),
        "python": platform.python_version(),
        "pip": importlib.metadata.version("pip"),
        "setuptools": importlib.metadata.version("setuptools"),
        "wheel": importlib.metadata.version("wheel"),
        "clang": _output(["clang++", "--version"], environment, "clang++").splitlines()[0],
        "cmake": _output(["cmake", "--version"], environment, "CMake").splitlines()[0],
        "ninja": _output(["ninja", "--version"], environment, "Ninja"),
        "target": target,
    }


def verify_current_toolchain(root: Path) -> dict[str, str]:
    pins = load_toolchain_pins(root)
    identity = current_toolchain_identity(root)
    expected = {
        "rustc": pins.rustc,
        "cargo": pins.cargo,
        "pip": pins.pip,
        "setuptools": pins.setuptools,
        "wheel": pins.wheel,
        "clang": pins.clang,
        "cmake": pins.cmake,
        "ninja": pins.ninja,
    }
    for key, value in expected.items():
        if identity[key] != value:
            raise ModelError(f"{key} does not match toolchains/pins.json")
    if ".".join(identity["python"].split(".")[:2]) != pins.python:
        raise ModelError("Python does not match toolchains/pins.json")
    return identity
