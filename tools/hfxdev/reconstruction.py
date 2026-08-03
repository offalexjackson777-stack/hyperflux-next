# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from fnmatch import fnmatch
import hashlib
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any, Iterable

from .model import ModelError, load_json, require_unique


LEDGER_SCHEMA = "hyperflux-reconstruction-ledger-v1"
BASELINE_COMMIT = "c348305bb9c6790141932ddf79f707e5223bb51c"
SECTIONS = (
    "binaries",
    "units",
    "rules",
    "package_artifacts",
    "dependencies",
    "compatibility_subsystems",
    "generated_families",
    "handwritten_modules",
)
DECISION_KEYS = {"id", "action", "target_owner", "phase", "rationale"}
TARGET_OWNERS = {
    "core",
    "full",
    "source-only",
    "workspace-shared",
    "generated",
    "phase-3-workspace",
    "phase-4-modules",
    "deferred",
    "none",
}
PRODUCTION_ROOTS = ("crates", "driver/kernel", "integrations", "tools/hfxdev")
PRODUCTION_SUFFIXES = {".rs", ".c", ".cpp", ".py"}
LARGE_MODULE_LINES = 800
INTERFACE_BASELINE_SCHEMA = "hyperflux-reconstruction-interface-baseline-v1"
INTERFACE_CHARACTERIZATION_COMMIT = "0ee607601187b7b4c34bb7ad8148321056c1a330"
INTERFACE_GENERATED_OUTPUT_PROJECTION_SHA256 = (
    "267c3ef7e7ad1dd38289c8c88b6990e71f4f9828288cd52251191f4cc49dade2"
)
RELEASE_8_PACKAGES = {
    "linux": (
        "hyperflux-next-linux",
        "4f3c9ab25029f65e75b03e2cdf0a50aa00d2a043abb79b25d3082d468fe00697",
    ),
    "full": (
        "hyperflux-next-full",
        "1cd840343323ea761e89bae175993c4c1547f0c2f4ae7a825e26ce1b308fa1d3",
    ),
}
STOCK_APPLICATION_PACKAGES = {
    "openrgb",
    "openrazer-daemon",
    "python-openrazer",
    "polychromatic",
}


def _exact(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields {', '.join(missing)}")
        if extra:
            details.append(f"unknown fields {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelError(f"{label}: invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ModelError(f"{label}: path escapes the repository")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ModelError(f"{label}: invalid SHA-256")
    return value


def _generated_output_projection_digest(entries: list[dict[str, Any]]) -> str:
    manifest = "".join(
        f"{entry['sha256']}  {entry['path']}\n" for entry in entries
    ).encode("utf-8")
    return _sha256(manifest)


def _string_list(value: object, label: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ModelError(f"{label}: invalid string list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelError(f"{label}: invalid string list")
    return value


def _decision(entry: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ModelError(f"{label}: must be an object")
    _exact(entry, DECISION_KEYS | expected, label)
    if not isinstance(entry["id"], str) or not entry["id"]:
        raise ModelError(f"{label}: invalid id")
    action = entry["action"]
    owner = entry["target_owner"]
    if action not in {"KEEP", "MOVE", "RETIRE"} or owner not in TARGET_OWNERS:
        raise ModelError(f"{label}: invalid decision")
    if (action == "RETIRE") != (owner == "none"):
        raise ModelError(f"{label}: retired decisions must have no target owner")
    if not isinstance(entry["phase"], int) or not 1 <= entry["phase"] <= 9:
        raise ModelError(f"{label}: invalid phase")
    if not isinstance(entry["rationale"], str) or not entry["rationale"].strip():
        raise ModelError(f"{label}: missing rationale")
    return entry


def _section(
    ledger: dict[str, Any],
    name: str,
    extra_keys: set[str],
) -> list[dict[str, Any]]:
    values = ledger.get(name)
    if not isinstance(values, list) or not values:
        raise ModelError(f"reconstruction ledger {name}: must be a non-empty list")
    parsed = [
        _decision(value, extra_keys, f"reconstruction ledger {name} {index}")
        for index, value in enumerate(values)
    ]
    require_unique([entry["id"] for entry in parsed], f"reconstruction {name} id")
    return parsed


def _cargo_binary_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for manifest_path in sorted((root / "crates").glob("*/Cargo.toml")):
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        implicit = [manifest_path.parent / "src" / "main.rs"]
        implicit.extend(sorted((manifest_path.parent / "src" / "bin").glob("*.rs")))
        paths.update(
            path.relative_to(root).as_posix()
            for path in implicit
            if path.is_file() and not path.is_symlink()
        )
        for target in manifest.get("bin", []):
            path = target.get("path")
            if not isinstance(path, str):
                raise ModelError(f"{manifest_path}: binary target lacks a path")
            candidate = manifest_path.parent / path
            if not candidate.is_file() or candidate.is_symlink():
                raise ModelError(f"{candidate}: binary source is unavailable")
            paths.add(candidate.relative_to(root).as_posix())
    return paths


def _install_authorities(root: Path) -> tuple[set[str], set[str], set[str]]:
    install = load_json(root / "packaging" / "install.json")
    builds = install.get("builds")
    payloads = install.get("payloads")
    if not isinstance(builds, list) or not isinstance(payloads, list):
        raise ModelError("install authority lacks builds or payloads")
    build_keys = {f"install-build:{value['id']}" for value in builds}
    payload_keys = {f"install-payload:{value['id']}" for value in payloads}
    unit_keys = {
        f"install-payload:{value['id']}"
        for value in payloads
        if str(value.get("destination", "")).endswith((".service", ".socket"))
    }
    rule_keys = {
        f"install-payload:{value['id']}"
        for value in payloads
        if str(value.get("source", "")).endswith((".rules", ".sysusers", ".tmpfiles"))
    }
    rule_keys.update(
        f"install-build:{value['id']}"
        for value in builds
        if value.get("kind") == "source-transform-file"
    )
    return build_keys | payload_keys, unit_keys, rule_keys


def _dependency_authorities(root: Path) -> set[str]:
    dependencies = load_json(root / "assurance" / "dependencies.json")
    keys = {
        f"rust-registry:{value['name']}@{value['version']}"
        for value in dependencies.get("rust_registry_packages", [])
    }
    keys.update(
        f"python-external:{value['name']}{value['specifier']}"
        for value in dependencies.get("python_external_packages", [])
    )
    integrations = load_json(root / "integrations" / "catalog.json")
    keys.update(
        f"upstream:{value['id']}@{value['commit']}"
        for value in integrations.get("upstreams", [])
    )
    distributions = load_json(root / "packaging" / "distributions.json")
    components = distributions["targets"]["arch"]["components"]
    for component_id, component in components.items():
        keys.update(
            f"arch:{component_id}:role:{value}"
            for value in component["dependency_roles"]
        )
        keys.update(
            f"arch:{component_id}:dependency:{value}"
            for value in component["dependencies"]
        )
        keys.update(
            f"arch:{component_id}:optional:{value['package']}"
            for value in component["optional_dependencies"]
        )
    return keys


def _compatibility_authorities(root: Path) -> set[str]:
    integrations = load_json(root / "integrations" / "catalog.json")
    keys = {f"adapter:{value['id']}" for value in integrations.get("adapters", [])}
    distributions = load_json(root / "packaging" / "distributions.json")
    components = distributions["targets"]["arch"]["components"]
    for component_id, component in components.items():
        for relation in ("conflicts", "provides", "replaces"):
            keys.update(
                f"arch:{component_id}:{relation.removesuffix('s')}:{value}"
                for value in component[relation]
            )
    return keys


def _is_generated_source(path: Path) -> bool:
    return (
        "tests" in path.parts
        or path.name == "tests.rs"
        or "generated" in path.parts
        or path.name == "generated.rs"
        or path.name.startswith("generated_v")
        or path.name.startswith("generated_")
    )


def _large_handwritten_modules(root: Path) -> dict[str, int]:
    modules: dict[str, int] = {}
    for relative_root in PRODUCTION_ROOTS:
        directory = root / relative_root
        for path in sorted(directory.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix not in PRODUCTION_SUFFIXES
                or _is_generated_source(path.relative_to(root))
            ):
                continue
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines >= LARGE_MODULE_LINES:
                modules[path.relative_to(root).as_posix()] = lines
    return modules


def _validate_exact_authorities(
    entries: Iterable[dict[str, Any]], expected: set[str], label: str
) -> None:
    actual = [entry["authority_key"] for entry in entries]
    require_unique(actual, f"reconstruction {label} authority")
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    if missing or extra:
        raise ModelError(
            f"reconstruction {label} coverage differs: missing={missing}, extra={extra}"
        )


def _validate_generated_families(root: Path, entries: list[dict[str, Any]]) -> None:
    from .render import rendered_binary_files, rendered_files

    outputs = {
        path.relative_to(root).as_posix() for path in rendered_files(root)
    } | {
        path.relative_to(root).as_posix() for path in rendered_binary_files(root)
    }
    matched_families: set[str] = set()
    for output in sorted(outputs):
        matches = [
            entry["id"]
            for entry in entries
            if any(fnmatch(output, pattern) for pattern in entry["patterns"])
        ]
        if len(matches) != 1:
            raise ModelError(
                f"generated output {output} has {len(matches)} reconstruction families: {matches}"
            )
        matched_families.add(matches[0])
    unused = sorted({entry["id"] for entry in entries} - matched_families)
    if unused:
        raise ModelError(f"reconstruction generated families match no outputs: {unused}")


def load_reconstruction_ledger(root: Path) -> dict[str, Any]:
    root = root.resolve()
    ledger = load_json(root / "reconstruction" / "2.0" / "ledger.json")
    _exact(
        ledger,
        {"$schema", "schema", "baseline_commit", "target", *SECTIONS},
        "reconstruction ledger",
    )
    if ledger["schema"] != LEDGER_SCHEMA or ledger["baseline_commit"] != BASELINE_COMMIT:
        raise ModelError("reconstruction ledger baseline identity is invalid")
    if ledger["target"] != {"version": "2.0.0", "package_release": 1}:
        raise ModelError("reconstruction ledger target identity is invalid")

    binaries = _section(ledger, "binaries", {"path"})
    units = _section(ledger, "units", {"authority_key"})
    rules = _section(ledger, "rules", {"authority_key"})
    artifacts = _section(ledger, "package_artifacts", {"authority_key"})
    dependencies = _section(ledger, "dependencies", {"authority_key"})
    compatibility = _section(
        ledger, "compatibility_subsystems", {"authority_key"}
    )
    generated = _section(ledger, "generated_families", {"patterns"})
    modules = _section(ledger, "handwritten_modules", {"path", "baseline_lines"})

    all_ids = [entry["id"] for name in SECTIONS for entry in ledger[name]]
    require_unique(all_ids, "reconstruction decision id")

    binary_paths = [_safe_relative(entry["path"], entry["id"]) for entry in binaries]
    require_unique(binary_paths, "reconstruction binary path")
    expected_binaries = _cargo_binary_paths(root)
    if set(binary_paths) != expected_binaries:
        raise ModelError("reconstruction binary coverage differs from Cargo targets")

    install_keys, unit_keys, rule_keys = _install_authorities(root)
    _validate_exact_authorities(artifacts, install_keys, "package artifact")
    _validate_exact_authorities(units, unit_keys, "unit")
    _validate_exact_authorities(rules, rule_keys, "rule")
    _validate_exact_authorities(
        dependencies, _dependency_authorities(root), "dependency"
    )
    _validate_exact_authorities(
        compatibility, _compatibility_authorities(root), "compatibility"
    )

    for entry in generated:
        patterns = entry["patterns"]
        if not isinstance(patterns, list) or not patterns:
            raise ModelError(f"{entry['id']}: generated patterns are missing")
        for index, pattern in enumerate(patterns):
            _safe_relative(pattern, f"{entry['id']} pattern {index}")
    _validate_generated_families(root, generated)

    module_paths = [_safe_relative(entry["path"], entry["id"]) for entry in modules]
    require_unique(module_paths, "reconstruction handwritten module path")
    current_large = _large_handwritten_modules(root)
    missing_modules = sorted(set(current_large) - set(module_paths))
    if missing_modules:
        raise ModelError(
            f"large handwritten modules lack reconstruction decisions: {missing_modules}"
        )
    for entry in modules:
        baseline_lines = entry["baseline_lines"]
        if not isinstance(baseline_lines, int) or baseline_lines < LARGE_MODULE_LINES:
            raise ModelError(f"{entry['id']}: invalid baseline line count")
        current = current_large.get(entry["path"])
        if current is not None and current > baseline_lines:
            raise ModelError(
                f"{entry['path']}: grew beyond its reconstruction baseline without review"
            )
        if current is not None and entry["phase"] == 4:
            if entry["action"] != "KEEP":
                raise ModelError(
                    f"{entry['path']}: Phase 4 extraction remains above the review signal"
                )
            if re.search(r"\b(retain|defer|exempt)\b", entry["rationale"], re.I) is None:
                raise ModelError(
                    f"{entry['path']}: Phase 4 keep lacks a specific retain/defer rationale"
                )
    return ledger


def _validate_payload(package: dict[str, Any], label: str) -> set[str]:
    payload = package.get("payload")
    if not isinstance(payload, list) or not payload:
        raise ModelError(f"{label}: payload is empty")
    paths: list[str] = []
    files: set[str] = set()
    for index, entry in enumerate(payload):
        item_label = f"{label} payload {index}"
        if not isinstance(entry, dict):
            raise ModelError(f"{item_label}: must be an object")
        kind = entry.get("type")
        expected = {"path", "type", "mode", "uid", "gid"}
        if kind == "file":
            expected |= {"size", "sha256"}
        _exact(entry, expected, item_label)
        path = _safe_relative(entry["path"], item_label)
        if path.startswith(".") or "//" in path:
            raise ModelError(f"{item_label}: package metadata is not payload")
        if kind not in {"file", "dir"}:
            raise ModelError(f"{item_label}: unsupported payload type")
        if (
            not isinstance(entry["mode"], str)
            or not re.fullmatch(r"[0-7]{3,4}", entry["mode"])
            or entry["uid"] != 0
            or entry["gid"] != 0
        ):
            raise ModelError(f"{item_label}: ownership or mode differs")
        if kind == "file":
            if not isinstance(entry["size"], int) or entry["size"] < 0:
                raise ModelError(f"{item_label}: invalid size")
            _sha256_value(entry["sha256"], item_label)
            files.add(path)
        paths.append(path)
    if paths != sorted(paths):
        raise ModelError(f"{label}: payload is not sorted")
    require_unique(paths, f"{label} payload path")
    return files


def load_interface_baseline(root: Path) -> dict[str, Any]:
    root = root.resolve()
    baseline = load_json(root / "reconstruction" / "2.0" / "interface-baseline.json")
    _exact(
        baseline,
        {
            "$schema",
            "schema",
            "behavioral_reference_commit",
            "characterization_commit",
            "release",
            "packages",
            "rust_root_exports",
            "generated_outputs",
            "stock_application_boundary",
        },
        "reconstruction interface baseline",
    )
    if (
        baseline["schema"] != INTERFACE_BASELINE_SCHEMA
        or baseline["behavioral_reference_commit"] != BASELINE_COMMIT
        or baseline["characterization_commit"] != INTERFACE_CHARACTERIZATION_COMMIT
        or baseline["release"] != {"version": "0.0.0_dev.1", "package_release": 8}
    ):
        raise ModelError("reconstruction interface baseline identity differs")

    packages = baseline["packages"]
    if not isinstance(packages, list) or len(packages) != 2:
        raise ModelError("reconstruction interface baseline requires two packages")
    package_files: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ModelError("reconstruction package baseline must be an object")
        _exact(
            package,
            {
                "component",
                "archive_name",
                "archive_size",
                "archive_sha256",
                "pkginfo",
                "payload",
            },
            "reconstruction package baseline",
        )
        component = package["component"]
        if component not in RELEASE_8_PACKAGES:
            raise ModelError("reconstruction package component differs")
        expected_name, expected_sha256 = RELEASE_8_PACKAGES[component]
        if (
            package["archive_sha256"] != expected_sha256
            or not isinstance(package["archive_size"], int)
            or package["archive_size"] <= 0
        ):
            raise ModelError(f"reconstruction {component} archive identity differs")
        metadata = package["pkginfo"]
        if not isinstance(metadata, dict):
            raise ModelError(f"reconstruction {component} PKGINFO is invalid")
        for key, values in metadata.items():
            if not isinstance(key, str) or not key:
                raise ModelError(f"reconstruction {component} PKGINFO key is invalid")
            _string_list(values, f"reconstruction {component} PKGINFO {key}")
        if metadata.get("pkgname") != [expected_name] or metadata.get("pkgver") != [
            "0.0.0_dev.1-8"
        ]:
            raise ModelError(f"reconstruction {component} package metadata differs")
        package_files[component] = _validate_payload(package, f"reconstruction {component}")

    require_unique(
        [package["component"] for package in packages], "reconstruction package component"
    )
    core = next(package for package in packages if package["component"] == "linux")
    full = next(package for package in packages if package["component"] == "full")
    if core["pkginfo"].get("backup") != ["etc/hyperflux-next/bridge.json"]:
        raise ModelError("release 8 configuration preservation differs")
    if "backup" in full["pkginfo"]:
        raise ModelError("release 8 Full package unexpectedly preserves configuration")
    overlap = sorted(package_files["linux"] & package_files["full"])
    if overlap:
        raise ModelError(f"release 8 sibling file payloads overlap: {overlap}")

    boundary = baseline["stock_application_boundary"]
    if not isinstance(boundary, dict):
        raise ModelError("stock application boundary is invalid")
    _exact(
        boundary,
        {"optional_packages", "forbidden_payload_prefixes"},
        "stock application boundary",
    )
    optional = set(
        _string_list(boundary["optional_packages"], "stock optional packages")
    )
    if optional != STOCK_APPLICATION_PACKAGES:
        raise ModelError("stock optional application boundary differs")
    prefixes = _string_list(
        boundary["forbidden_payload_prefixes"], "stock payload prefixes"
    )
    all_files = package_files["linux"] | package_files["full"]
    conflicts = sorted(
        path for path in all_files if any(path.startswith(prefix) for prefix in prefixes)
    )
    if conflicts:
        raise ModelError(f"release 8 owns stock application payloads: {conflicts}")
    full_optional = full["pkginfo"].get("optdepend", [])
    if {
        value.split(":", 1)[0].strip() for value in full_optional
    } != STOCK_APPLICATION_PACKAGES:
        raise ModelError("release 8 Full optional application relations differ")

    exports = baseline["rust_root_exports"]
    if not isinstance(exports, list) or not exports:
        raise ModelError("Rust root export baseline is empty")
    packages_seen: list[str] = []
    for entry in exports:
        if not isinstance(entry, dict):
            raise ModelError("Rust root export entry must be an object")
        _exact(entry, {"package", "import", "path", "symbols"}, "Rust root export")
        package_name = entry["package"]
        if not isinstance(package_name, str) or entry["import"] != package_name.replace("-", "_"):
            raise ModelError("Rust root export package identity differs")
        _safe_relative(entry["path"], f"Rust root export {package_name}")
        symbols = _string_list(entry["symbols"], f"Rust root export {package_name}")
        if symbols != sorted(symbols, key=lambda value: value.replace("_", "\0")):
            raise ModelError(f"Rust root export {package_name} is not sorted")
        require_unique(symbols, f"Rust root export {package_name} symbol")
        packages_seen.append(package_name)
    require_unique(packages_seen, "Rust root export package")

    generated = baseline["generated_outputs"]
    if not isinstance(generated, list) or not generated:
        raise ModelError("generated output baseline is empty")
    generated_paths: list[str] = []
    for entry in generated:
        if not isinstance(entry, dict):
            raise ModelError("generated output baseline entry must be an object")
        _exact(entry, {"path", "sha256"}, "generated output baseline")
        generated_paths.append(_safe_relative(entry["path"], "generated output baseline"))
        _sha256_value(entry["sha256"], "generated output baseline")
    if generated_paths != sorted(generated_paths):
        raise ModelError("generated output baseline is not sorted")
    require_unique(generated_paths, "generated output baseline path")

    return baseline


def render_public_api_consumer(baseline: dict[str, Any]) -> str:
    lines = [
        "// Generated by tools/generate_reconstruction_interface_baseline.py.",
        "// SPDX-License-Identifier: GPL-2.0-only",
        "#![allow(unused_imports)]",
        "",
    ]
    for crate in baseline["rust_root_exports"]:
        lines.extend(
            f"use {crate['import']}::{symbol} as _;" for symbol in crate["symbols"]
        )
        lines.append("")
    lines.append("pub fn compile_only_consumer() {}")
    return "\n".join(lines) + "\n"


def verify_historical_generated_output_projection(baseline: dict[str, Any]) -> None:
    # The clean public root retains this path/hash projection; original objects live
    # only in the independently verified private history mirror.
    if (
        _generated_output_projection_digest(baseline["generated_outputs"])
        != INTERFACE_GENERATED_OUTPUT_PROJECTION_SHA256
    ):
        raise ModelError("historical generated output projection differs")
