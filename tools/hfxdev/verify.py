# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time

from .model import ModelError, load_foundation, load_json, require_unique, sha256_file
from .assurance import load_design_coverage
from .errors import load_error_catalog
from .formal_model import load_formal_model, run_formal_model
from .integrations import load_integration_catalog, load_openrazer_compatibility_contract
from .install import load_install_manifest
from .linux_runtime import load_linux_runtime
from .openrazer import load_imported_metadata, transformed_metadata
from .package_pipeline import build_artifacts, load_artifact_set, stage_rootfs
from .performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
    verify_static_performance_budgets,
)
from .render import rendered_files
from .profiles import load_profile_inputs
from .protocol import load_protocol_catalog
from .release import load_release_gates
from .supply_chain import load_dependency_inventory
from .testgraph import TestNode, load_test_catalog
from .toolchains import toolchain_environment, verify_current_toolchain


ALLOWED_DISPOSITIONS = {
    "REIMPLEMENT",
    "REIMPLEMENT_AND_GENERATE",
    "REIMPLEMENT_WITH_PINNED_IMPORT",
    "MIGRATE_AFTER_REVIEW",
    "SPECIFICATION_ONLY",
    "TEST_REFERENCE",
    "RESEARCH_LINK",
    "REJECT",
}
ALLOWED_STATUSES = {"PENDING_REVIEW", "IN_PROGRESS", "ACCEPTED", "REJECTED"}
IGNORED_PATH_PARTS = {
    ".git",
    ".hfx",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "target",
}
FUTURE_MTIME_TOLERANCE_SECONDS = 5.0
INSTALLED_SCHEMA_ROOT = Path("/usr/share/hyperflux-next/schemas")


def _check_constitution(constitution: dict) -> None:
    if constitution.get("schema") != "hyperflux-architecture-constitution-v1":
        raise ModelError("unsupported architecture constitution schema")
    invariant_ids = [entry["id"] for entry in constitution["invariants"]]
    require_unique(invariant_ids, "architecture invariant id")
    component_ids = [entry["id"] for entry in constitution["components"]]
    require_unique(component_ids, "architecture component id")
    if constitution["publication_interlock"].get("remote_repository_created") is not False:
        raise ModelError("publication interlock must keep remote repository creation false")
    if constitution["publication_interlock"].get("publication_authorized") is not False:
        raise ModelError("publication interlock must keep publication authorization false")


def _check_sources(root: Path, sources: dict) -> None:
    source_ids = [source["id"] for source in sources["sources"]]
    require_unique(source_ids, "migration source id")
    for source in sources["sources"]:
        if source["kind"] == "imported-document":
            path = root / source["imported_path"]
            if not path.is_file():
                raise ModelError(f"missing imported document: {source['imported_path']}")
            if source["sha256"] == "TO_BE_CAPTURED":
                raise ModelError(f"{source['id']}: imported document digest is not captured")
            actual = sha256_file(path)
            if actual != source["sha256"]:
                raise ModelError(f"{source['id']}: imported document digest mismatch")
            continue
        inventory_path = root / source["inventory"]
        if not inventory_path.is_file():
            raise ModelError(f"{source['id']}: missing source inventory")
        inventory = load_json(inventory_path)
        if inventory.get("source") != source["id"]:
            raise ModelError(f"{source['id']}: inventory source mismatch")
        if not inventory.get("commit", "").startswith(source["commit"]):
            raise ModelError(f"{source['id']}: inventory commit mismatch")
        entries = inventory.get("entries", [])
        if inventory.get("entry_count") != len(entries):
            raise ModelError(f"{source['id']}: inventory count mismatch")
        paths = [entry["path"] for entry in entries]
        require_unique(paths, f"{source['id']} inventory path")
        if paths != sorted(paths):
            raise ModelError(f"{source['id']}: inventory paths are not sorted")


def _check_ledger(sources: dict, ledger: dict) -> None:
    source_ids = {source["id"] for source in sources["sources"]}
    entry_ids = [entry["id"] for entry in ledger["entries"]]
    require_unique(entry_ids, "migration ledger id")
    for entry in ledger["entries"]:
        if entry["disposition"] not in ALLOWED_DISPOSITIONS:
            raise ModelError(f"{entry['id']}: unknown disposition {entry['disposition']}")
        if entry["status"] not in ALLOWED_STATUSES:
            raise ModelError(f"{entry['id']}: unknown status {entry['status']}")
        unknown_sources = sorted(set(entry["sources"]) - source_ids)
        if unknown_sources:
            raise ModelError(f"{entry['id']}: unknown sources {', '.join(unknown_sources)}")
        if not entry["rationale"].strip():
            raise ModelError(f"{entry['id']}: rationale is empty")


def _check_generated(root: Path) -> None:
    for path, expected in rendered_files(root).items():
        if not path.is_file():
            raise ModelError(f"missing generated file: {path.relative_to(root)}")
        if path.read_text(encoding="utf-8") != expected:
            raise ModelError(f"stale generated file: {path.relative_to(root)}; run ./hfx generate")


def _check_repository_paths(root: Path) -> None:
    absolute_home = re.compile(r"/home/[A-Za-z0-9_.-]+/")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PATH_PARTS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".png", ".jpg", ".zst"} or path.name == "LICENSE":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if absolute_home.search(text):
            raise ModelError(f"private absolute path found in {path.relative_to(root)}")


def _check_profile_contract(root: Path) -> None:
    load_profile_inputs(root)


def _check_integration_contract(root: Path) -> None:
    load_integration_catalog(root)
    load_openrazer_compatibility_contract(root)
    load_imported_metadata(root)


def _tool_environment(root: Path) -> dict[str, str]:
    return toolchain_environment(root)


def _run_command(
    root: Path,
    command: list[str],
    label: str,
    timeout_seconds: int,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    process_environment = _tool_environment(root)
    if environment is not None:
        process_environment.update(environment)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            env=process_environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelError(f"{label} exceeded its {timeout_seconds}s timeout") from error
    if result.returncode != 0:
        raise ModelError(f"{label} failed")


def _run_python_tests(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "repository unit tests",
        node.timeout_seconds,
    )


def _run_cpp_sdk_contracts(root: Path, node: TestNode) -> None:
    binary = root / "build" / "domain-types-smoke"
    binary.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "tests/cpp/domain_types_smoke.cpp",
            "-o",
            str(binary),
        ],
        "C++ SDK compile",
        node.timeout_seconds,
    )
    _run_command(root, [str(binary)], "C++ SDK smoke test", node.timeout_seconds)
    integration_binary = root / "build" / "integration-catalog-smoke"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "tests/cpp/integration_catalog_smoke.cpp",
            "-o",
            str(integration_binary),
        ],
        "C++ integration catalog compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(integration_binary)],
        "C++ integration catalog smoke test",
        node.timeout_seconds,
    )
    protocol_json_binary = root / "build" / "protocol-json-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/protocol_json_contract.cpp",
            "-o",
            str(protocol_json_binary),
        ],
        "C++ protocol JSON contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(protocol_json_binary)],
        "C++ protocol JSON contract",
        node.timeout_seconds,
    )
    sdk_client_binary = root / "build" / "sdk-client-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_client_contract.cpp",
            "sdk/cpp/src/client.cpp",
            "sdk/cpp/src/channel.cpp",
            "sdk/cpp/src/identity.cpp",
            "-pthread",
            "-o",
            str(sdk_client_binary),
        ],
        "C++ SDK client contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_client_binary)],
        "C++ SDK client contract",
        node.timeout_seconds,
    )
    sdk_channel_binary = root / "build" / "sdk-channel-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_channel_contract.cpp",
            "sdk/cpp/src/channel.cpp",
            "-pthread",
            "-o",
            str(sdk_channel_binary),
        ],
        "C++ SDK channel contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_channel_binary)],
        "C++ SDK channel contract",
        node.timeout_seconds,
    )
    sdk_lighting_binary = root / "build" / "sdk-lighting-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_lighting_contract.cpp",
            "sdk/cpp/src/lighting.cpp",
            "-o",
            str(sdk_lighting_binary),
        ],
        "C++ SDK lighting contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_lighting_binary)],
        "C++ SDK lighting contract",
        node.timeout_seconds,
    )
    sdk_recovery_binary = root / "build" / "sdk-recovery-contract"
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Isdk/cpp/include",
            "-Isdk/cpp/vendor/include",
            "tests/cpp/sdk_recovery_contract.cpp",
            "sdk/cpp/src/recovery.cpp",
            "sdk/cpp/src/client.cpp",
            "sdk/cpp/src/channel.cpp",
            "sdk/cpp/src/identity.cpp",
            "-pthread",
            "-o",
            str(sdk_recovery_binary),
        ],
        "C++ SDK recovery contract compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(sdk_recovery_binary)],
        "C++ SDK recovery contract",
        node.timeout_seconds,
    )


def _run_kernel_profile_contracts(root: Path, node: TestNode) -> None:
    kernel_binary = root / "build" / "kernel-profile-table-smoke"
    kernel_binary.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        root,
        [
            "clang++",
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "tests/cpp/kernel_profile_table_smoke.cpp",
            "-o",
            str(kernel_binary),
        ],
        "kernel profile table compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(kernel_binary)],
        "kernel profile table smoke test",
        node.timeout_seconds,
    )
    uapi_binary = root / "build" / "kernel-uapi-smoke"
    _run_command(
        root,
        [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Idriver/kernel/uapi",
            "tests/c/kernel_uapi_smoke.c",
            "-o",
            str(uapi_binary),
        ],
        "kernel UAPI compile",
        node.timeout_seconds,
    )
    _run_command(root, [str(uapi_binary)], "kernel UAPI smoke test", node.timeout_seconds)

    protocol_binary = root / "build" / "kernel-protocol-smoke"
    _run_command(
        root,
        [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-Idriver/kernel",
            "-Idriver/kernel/uapi",
            "tests/c/kernel_protocol_smoke.c",
            "-o",
            str(protocol_binary),
        ],
        "kernel protocol compile",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [str(protocol_binary)],
        "kernel protocol smoke test",
        node.timeout_seconds,
    )

    configured = os.environ.get("HFX_KERNEL_BUILD_DIRS")
    if configured:
        build_directories = [Path(value) for value in configured.split(os.pathsep) if value]
    else:
        build_directories = [Path("/lib/modules") / os.uname().release / "build"]
    if not build_directories:
        raise ModelError("kernel module verification has no configured header directory")
    for build_directory in build_directories:
        if not build_directory.is_absolute() or not (build_directory / "Makefile").is_file():
            raise ModelError(f"kernel header directory is unavailable: {build_directory}")
        label = build_directory.resolve().parent.name
        output_directory = root / "build" / "kernel" / label
        _run_command(
            root,
            [
                "make",
                "-C",
                str(build_directory),
                f"M={root / 'driver' / 'kernel'}",
                "clean",
            ],
            f"legacy kernel source cleanup ({label})",
            node.timeout_seconds,
        )
        source_link = root / "driver" / "kernel" / "source"
        if source_link.is_symlink() and os.readlink(source_link) == ".":
            source_link.unlink()
        elif source_link.exists() or source_link.is_symlink():
            raise ModelError("kernel build left an unexpected source-tree artifact")
        if output_directory.exists():
            shutil.rmtree(output_directory)
        output_directory.mkdir(parents=True)
        _run_command(
            root,
            [
                "make",
                "-C",
                str(build_directory),
                f"M={root / 'driver' / 'kernel'}",
                f"MO={output_directory}",
                "W=1",
                "modules",
            ],
            f"kernel module build ({label})",
            node.timeout_seconds,
        )
        if source_link.is_symlink() and os.readlink(source_link) == ".":
            source_link.unlink()
        elif source_link.exists() or source_link.is_symlink():
            raise ModelError("kernel build left an unexpected source-tree artifact")


def _pinned_upstream_source(
    root: Path,
    *,
    environment_name: str,
    upstream_id: str,
    required_paths: tuple[str, ...],
    label: str,
) -> Path:
    value = os.environ.get(environment_name)
    if value is None:
        raise ModelError(f"{environment_name} is required for the pinned {label} contract")
    source = Path(value)
    if not source.is_absolute():
        raise ModelError(f"{environment_name} must name an absolute path")
    source = source.resolve()
    if not source.is_dir() or not all((source / path).is_file() for path in required_paths):
        raise ModelError(f"{environment_name} is not a {label} checkout")
    expected = {
        upstream["id"]: upstream["commit"]
        for upstream in load_integration_catalog(root)["upstreams"]
    }[upstream_id]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect pinned {label} source: {error}") from error
    if result.stdout.strip() != expected:
        raise ModelError(f"{label} source checkout does not match the integration catalog pin")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect pinned {label} worktree: {error}") from error
    if status.stdout:
        raise ModelError(f"pinned {label} source checkout has local modifications")
    return source


def _openrgb_source(root: Path) -> Path:
    return _pinned_upstream_source(
        root,
        environment_name="HFX_OPENRGB_SOURCE_DIR",
        upstream_id="openrgb",
        required_paths=("OpenRGBPluginInterface.h",),
        label="OpenRGB",
    )


def _openrazer_source(root: Path) -> Path:
    return _pinned_upstream_source(
        root,
        environment_name="HFX_OPENRAZER_SOURCE_DIR",
        upstream_id="openrazer",
        required_paths=("pylib/openrazer/client/__init__.py",),
        label="OpenRazer",
    )


def _run_openrazer_metadata_contracts(root: Path, _node: TestNode) -> None:
    source = _openrazer_source(root)
    expected = transformed_metadata(root, source)
    actual = load_imported_metadata(root)
    if actual != expected:
        raise ModelError("committed OpenRazer metadata is stale; rerun ./hfx import openrazer")


def _run_openrazer_compatibility_contracts(root: Path, node: TestNode) -> None:
    source = _openrazer_source(root)
    build_directory = root / "build" / "openrazer-compatibility"
    if build_directory.exists():
        shutil.rmtree(build_directory)
    package_source = build_directory / "package"
    shutil.copytree(
        root / "integrations" / "openrazer" / "compatibility",
        package_source,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
    )
    wheel_directory = build_directory / "wheel"
    wheel_directory.mkdir()
    _run_command(
        root,
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_directory),
            str(package_source),
        ],
        "OpenRazer compatibility wheel",
        node.timeout_seconds,
        environment={"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"},
    )
    python_path = os.pathsep.join(
        (
            str(source / "pylib"),
            str(root / "sdk" / "python"),
            str(root / "integrations" / "openrazer" / "compatibility"),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    _run_command(
        root,
        ["dbus-run-session", "--", sys.executable, "tests/openrazer_compat_contracts.py", "-v"],
        "OpenRazer compatibility contracts",
        node.timeout_seconds,
        environment={
            "HFX_OPENRAZER_SOURCE_DIR": str(source),
            "HFX_OPENRAZER_WHEEL_DIR": str(wheel_directory),
            "PYTHONPATH": python_path,
            "PYTHONWARNINGS": "error::ResourceWarning",
        },
    )


def _polychromatic_source(root: Path) -> Path:
    return _pinned_upstream_source(
        root,
        environment_name="HFX_POLYCHROMATIC_SOURCE_DIR",
        upstream_id="polychromatic",
        required_paths=(
            "polychromatic/backends/_backend.py",
            "polychromatic/middleman.py",
        ),
        label="Polychromatic",
    )


def _run_polychromatic_adapter_contracts(root: Path, node: TestNode) -> None:
    source = _polychromatic_source(root)
    build_directory = root / "build" / "polychromatic-adapter"
    if build_directory.exists():
        shutil.rmtree(build_directory)
    contract_source = build_directory / "source"
    shutil.copytree(
        source,
        contract_source,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    patch_path = root / "integrations" / "polychromatic" / "patches" / "0001-discover-native-backends.patch"
    patch_directory = contract_source.relative_to(root).as_posix()
    _run_command(
        root,
        ["git", "apply", "--check", f"--directory={patch_directory}", str(patch_path)],
        "Polychromatic native-backend seam check",
        node.timeout_seconds,
    )
    _run_command(
        root,
        ["git", "apply", f"--directory={patch_directory}", str(patch_path)],
        "Polychromatic native-backend seam apply",
        node.timeout_seconds,
    )
    package_source = build_directory / "package"
    shutil.copytree(
        root / "integrations" / "polychromatic",
        package_source,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
    )
    wheel_directory = build_directory / "wheel"
    wheel_directory.mkdir()
    _run_command(
        root,
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_directory),
            str(package_source),
        ],
        "Polychromatic native adapter wheel",
        node.timeout_seconds,
        environment={"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"},
    )
    python_path = os.pathsep.join(
        (
            str(contract_source),
            str(root / "sdk" / "python"),
            str(root / "integrations" / "polychromatic"),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    _run_command(
        root,
        [sys.executable, "tests/polychromatic_contracts.py", "-v"],
        "Polychromatic native adapter contracts",
        node.timeout_seconds,
        environment={
            "HFX_POLYCHROMATIC_WHEEL_DIR": str(wheel_directory),
            "PYTHONPATH": python_path,
        },
    )


def _run_openrgb_cmake_contracts(
    root: Path,
    node: TestNode,
    *,
    build_name: str,
    build_type: str,
    label: str,
    thread_sanitizer: bool,
) -> None:
    source = _openrgb_source(root)
    build_directory = root / "build" / build_name
    if build_directory.exists():
        shutil.rmtree(build_directory)
    configure = [
        "cmake",
        "-S",
        "integrations/openrgb",
        "-B",
        str(build_directory),
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DBUILD_TESTING=ON",
        f"-DHFX_OPENRGB_SOURCE_DIR={source}",
    ]
    if thread_sanitizer:
        configure.append("-DHFX_OPENRGB_THREAD_SANITIZER=ON")
    _run_command(
        root,
        configure,
        f"{label} configure",
        node.timeout_seconds,
    )
    _run_command(
        root,
        ["cmake", "--build", str(build_directory), "--parallel", "4"],
        f"{label} build",
        node.timeout_seconds,
    )
    ctest = ["ctest", "--test-dir", str(build_directory), "--output-on-failure"]
    environment = None
    if thread_sanitizer:
        ctest.extend(["--timeout", "60"])
        environment = {
            "TSAN_OPTIONS": "halt_on_error=1 history_size=7 second_deadlock_stack=1"
        }
    _run_command(
        root,
        ctest,
        f"{label} contracts",
        node.timeout_seconds,
        environment=environment,
    )


def _run_openrgb_adapter_contracts(root: Path, node: TestNode) -> None:
    _run_openrgb_cmake_contracts(
        root,
        node,
        build_name="openrgb-adapter",
        build_type="Release",
        label="OpenRGB adapter",
        thread_sanitizer=False,
    )


def _run_openrgb_thread_sanitizer(root: Path, node: TestNode) -> None:
    _run_openrgb_cmake_contracts(
        root,
        node,
        build_name="openrgb-tsan",
        build_type="RelWithDebInfo",
        label="OpenRGB ThreadSanitizer",
        thread_sanitizer=True,
    )


def _check_build_cache_clock(root: Path, *, now: float | None = None) -> None:
    target = root / "target"
    if not target.is_dir():
        return
    current = time.time() if now is None else now
    cutoff = current + FUTURE_MTIME_TOLERANCE_SECONDS
    future: list[Path] = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        try:
            modified = path.stat().st_mtime
        except OSError as error:
            raise ModelError(f"cannot inspect build cache timestamp: {path}") from error
        if modified > cutoff:
            future.append(path.relative_to(root))
            if len(future) == 3:
                break
    if future:
        sample = ", ".join(path.as_posix() for path in future)
        raise ModelError(
            "build cache contains future-dated artifacts and may produce false green results; "
            f"run cargo clean before verification (examples: {sample})"
        )


def _check_toolchain(root: Path, _node: TestNode) -> None:
    _check_build_cache_clock(root)
    verify_current_toolchain(root)


def _check_schema_contracts(root: Path) -> None:
    schema_ids: list[str] = []
    for path in sorted((root / "schemas").glob("*.schema.json")):
        schema = load_json(path)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ModelError(f"{path.relative_to(root)}: unsupported JSON Schema draft")
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("https://hyperflux.dev/schemas/"):
            raise ModelError(f"{path.relative_to(root)}: invalid canonical schema id")
        schema_ids.append(schema_id)
    require_unique(schema_ids, "JSON Schema id")

    for path in sorted(root.rglob("*.json")):
        if any(part in IGNORED_PATH_PARTS for part in path.parts):
            continue
        value = load_json(path)
        schema_reference = value.get("$schema")
        if not isinstance(schema_reference, str) or schema_reference.startswith("https://"):
            continue
        target = _resolve_schema_reference(root, path, schema_reference)
        if target is None or not target.is_file():
            raise ModelError(f"{path.relative_to(root)}: missing schema reference {schema_reference}")

    scenario_schema = load_json(root / "schemas" / "simulator-scenario.schema.json")
    event_limit = scenario_schema["properties"]["events"]["maxItems"]
    if event_limit != 4096:
        raise ModelError("simulator scenario event limit must remain 4096")
    replay = load_json(root / "tests" / "fixtures" / "replay" / "qualified-lifecycle-v1.json")
    provenance = replay.get("provenance", {})
    if provenance.get("source") != "sanitized-replay":
        raise ModelError("committed replay fixture must identify sanitized-replay provenance")
    if provenance.get("hardware_claim_authority") is not False:
        raise ModelError("replay fixture must not claim hardware authority")
    if provenance.get("private_identifiers_exported") is not False:
        raise ModelError("replay fixture must declare zero private identifiers")
    if len(replay.get("events", [])) > event_limit:
        raise ModelError("replay fixture exceeds the bounded event limit")
    load_test_catalog(root)
    load_linux_runtime(root)
    load_install_manifest(root)


def _resolve_schema_reference(
    root: Path,
    document: Path,
    schema_reference: str,
) -> Path | None:
    reference = Path(schema_reference)
    if not reference.is_absolute():
        return (document.parent / reference).resolve()
    if reference.parent != INSTALLED_SCHEMA_ROOT or reference.name in {"", ".", ".."}:
        return None
    return root / "schemas" / reference.name


def _check_protocol_contract(root: Path) -> None:
    load_protocol_catalog(root)


def _check_error_contract(root: Path) -> None:
    load_error_catalog(root)


def _run_foundation_contracts(root: Path, _node: TestNode) -> None:
    constitution, sources, ledger = load_foundation(root)
    _check_constitution(constitution)
    _check_sources(root, sources)
    _check_ledger(sources, ledger)
    load_design_coverage(root)


def _run_schema_contracts(root: Path, _node: TestNode) -> None:
    _check_schema_contracts(root)


def _run_profile_contracts(root: Path, _node: TestNode) -> None:
    _check_profile_contract(root)


def _run_integration_contracts(root: Path, _node: TestNode) -> None:
    _check_integration_contract(root)


def _run_protocol_contracts(root: Path, _node: TestNode) -> None:
    _check_protocol_contract(root)


def _run_error_contracts(root: Path, _node: TestNode) -> None:
    _check_error_contract(root)


def _run_generated_freshness(root: Path, _node: TestNode) -> None:
    _check_generated(root)


def _run_privacy_boundary(root: Path, _node: TestNode) -> None:
    _check_repository_paths(root)


def _run_toolchain_contract(root: Path, node: TestNode) -> None:
    _check_toolchain(root, node)


def _run_assurance_contracts(root: Path, _node: TestNode) -> None:
    load_dependency_inventory(root)
    load_release_gates(root)
    metrics = load_performance_budgets(root)
    verify_static_performance_budgets(root, metrics)


def _run_formal_model_contracts(root: Path, _node: TestNode) -> None:
    run_formal_model(load_formal_model(root))


def _run_rust_format(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "fmt", "--all", "--", "--check"],
        "Rust formatting",
        node.timeout_seconds,
    )


def _run_rust_clippy(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
        "Rust Clippy",
        node.timeout_seconds,
    )


def _run_rust_unit(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "test", "--workspace", "--exclude", "hfx-sim", "--all-targets", "--locked"],
        "Rust domain and profile tests",
        node.timeout_seconds,
    )


def _run_simulator_contracts(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        ["cargo", "test", "-p", "hfx-sim", "--all-targets", "--locked"],
        "virtual receiver and deterministic replay tests",
        node.timeout_seconds,
    )


def _staged_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    snapshot: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ModelError(f"package stage contains a symbolic link: {relative}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            snapshot.append((relative, "directory", mode, path.stat().st_mtime_ns))
        elif path.is_file():
            snapshot.append(
                (
                    relative,
                    "file",
                    mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    sha256_file(path),
                )
            )
        else:
            raise ModelError(f"package stage contains an unsupported file type: {relative}")
    return tuple(snapshot)


def _python_sdk_license_files(
    staged_root: Path, module_directory: str
) -> tuple[Path, ...]:
    installed = PurePosixPath(module_directory)
    if not installed.is_absolute() or ".." in installed.parts:
        raise ModelError("Python module directory is not a safe installed path")
    module_root = staged_root.joinpath(*installed.parts[1:])
    return tuple(
        sorted(module_root.glob("hyperflux_next_sdk-*.dist-info/licenses/LICENSE"))
    )


def _run_package_contracts(root: Path, node: TestNode) -> None:
    workspace = root / "build" / "package-contracts"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    manifest_path = build_artifacts(
        root,
        workspace / "artifacts",
        capabilities={"openrgb-source": _openrgb_source(root)},
    )
    artifacts = load_artifact_set(root, manifest_path)
    if artifacts.omitted:
        raise ModelError("complete package contract unexpectedly omitted a build product")

    first = stage_rootfs(root, manifest_path, workspace / "root-a")
    second = stage_rootfs(root, manifest_path, workspace / "root-b")
    if (
        first.payload_sha256 != second.payload_sha256
        or first.file_count != second.file_count
        or first.inventory.read_bytes() != second.inventory.read_bytes()
        or _staged_tree_snapshot(first.root) != _staged_tree_snapshot(second.root)
    ):
        raise ModelError("independent package stages are not byte-for-byte reproducible")

    runtime = load_linux_runtime(root)
    license_files = _python_sdk_license_files(
        first.root,
        runtime.operations.python_module_directory,
    )
    if len(license_files) != 1 or license_files[0].read_bytes() != (
        root / "LICENSE"
    ).read_bytes():
        raise ModelError("packaged Python SDK does not carry the canonical license")

    _run_command(
        root,
        [str(first.root / "usr/bin/hyperfluxctl"), "--help"],
        "staged operations CLI",
        node.timeout_seconds,
    )
    _run_command(
        root,
        [
            str(first.root / "usr/lib/hyperflux-next/hyperflux-next-bridge"),
            "--help",
        ],
        "staged bridge CLI",
        node.timeout_seconds,
    )
    plugin = first.root / "usr/lib/openrgb/plugins/hyperflux-next-openrgb.so"
    try:
        dynamic = subprocess.run(
            ["readelf", "-d", str(plugin)],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=node.timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect staged OpenRGB module: {error}") from error
    if dynamic.returncode != 0 or "(RPATH)" in dynamic.stdout or "(RUNPATH)" in dynamic.stdout:
        raise ModelError("staged OpenRGB module has an invalid dynamic-library contract")

    staged_payload_size = sum(
        path.stat().st_size for path in first.root.rglob("*") if path.is_file()
    )
    verify_package_performance_budgets(
        load_performance_budgets(root),
        {artifact.build_id: artifact.size for artifact in artifacts.artifacts},
        staged_payload_size,
    )


RUNNERS = {
    "foundation-contracts": _run_foundation_contracts,
    "schema-contracts": _run_schema_contracts,
    "profile-contracts": _run_profile_contracts,
    "integration-contracts": _run_integration_contracts,
    "protocol-contracts": _run_protocol_contracts,
    "error-contracts": _run_error_contracts,
    "generated-freshness": _run_generated_freshness,
    "privacy-boundary": _run_privacy_boundary,
    "python-unit": _run_python_tests,
    "toolchain-contract": _run_toolchain_contract,
    "assurance-contracts": _run_assurance_contracts,
    "formal-model-contracts": _run_formal_model_contracts,
    "rust-format": _run_rust_format,
    "rust-clippy": _run_rust_clippy,
    "rust-unit": _run_rust_unit,
    "simulator-contracts": _run_simulator_contracts,
    "cpp-sdk-contracts": _run_cpp_sdk_contracts,
    "openrgb-adapter-contracts": _run_openrgb_adapter_contracts,
    "openrgb-thread-sanitizer": _run_openrgb_thread_sanitizer,
    "openrazer-metadata-contracts": _run_openrazer_metadata_contracts,
    "openrazer-compatibility-contracts": _run_openrazer_compatibility_contracts,
    "polychromatic-adapter-contracts": _run_polychromatic_adapter_contracts,
    "kernel-profile-contracts": _run_kernel_profile_contracts,
    "package-contracts": _run_package_contracts,
}


def verify_all(root: Path) -> list[str]:
    catalog = load_test_catalog(root)
    passed: list[str] = []
    for node in catalog.ordered():
        print(f"[{node.id}] {node.title}", flush=True)
        RUNNERS[node.runner](root, node)
        passed.append(node.title)
    return passed
