#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


SCHEMA = "hyperflux-package-ownership-qualification-v1"
MAX_METADATA_BYTES = 4 * 1024 * 1024
APPLICATION_RELATIONS = {
    "polychromatic",
    "openrazer-daemon",
    "python-openrazer",
    "openrgb",
}
DRIVER_OVERLAP = {
    "usr/lib/sysusers.d/openrazer.conf",
    "usr/lib/udev/razer_mount",
    "usr/lib/udev/rules.d/99-razer.rules",
}
FORBIDDEN_CORE_PREFIXES = (
    "etc/xdg/",
    "usr/include/",
    "usr/lib/python",
    "usr/share/applications/",
    "usr/share/hyperflux-next/lab/",
    "usr/share/hyperflux-next/schemas/",
    "usr/share/polychromatic/",
    "usr/src/openrazer-driver-",
)
FULL_REQUIRED_FILES = {
    "usr/lib/hyperflux-next/hyperflux-next-uhid-relay",
    "usr/lib/modules-load.d/hyperflux-next-openrazer.conf",
    "usr/lib/openrgb/plugins/hyperflux-next-openrgb.so",
    "usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf",
    "usr/lib/systemd/system/hyperflux-next-openrazer-driver-preflight.service",
    "usr/lib/systemd/system/hyperflux-next-openrazer-relay.service",
    "usr/lib/sysusers.d/openrazer.conf",
    "usr/lib/udev/razer_mount",
    "usr/lib/udev/rules.d/99-razer.rules",
    "usr/share/hyperflux-next/components/full.json",
    "usr/share/licenses/hyperflux-next-full/LICENSE",
    "usr/src/openrazer-driver-3.12.4/dkms.conf",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_member(path: Path, member: str) -> bytes:
    completed = subprocess.run(
        ["bsdtar", "-xOf", str(path), member],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_METADATA_BYTES:
        raise RuntimeError(f"cannot read {member} from {path.name}")
    return completed.stdout


def package_info(path: Path) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for raw_line in archive_member(path, ".PKGINFO").decode(
        "utf-8", errors="strict"
    ).splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        key, separator, value = raw_line.partition(" = ")
        if not separator or not key or not value:
            raise RuntimeError(f"malformed package metadata: {path.name}")
        result.setdefault(key, []).append(value)
    return {key: tuple(values) for key, values in result.items()}


def mtree_unescape(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return re.sub(r"\\([0-7]{3})", replace, value)


def package_mtree(path: Path) -> dict[str, dict[str, str]]:
    raw = archive_member(path, ".MTREE")
    try:
        text = gzip.decompress(raw).decode("utf-8", errors="strict")
    except (gzip.BadGzipFile, UnicodeDecodeError) as error:
        raise RuntimeError(f"invalid mtree metadata: {path.name}") from error
    result: dict[str, dict[str, str]] = {}
    defaults: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("/set "):
            defaults.update(
                field.split("=", 1) for field in line[5:].split() if "=" in field
            )
            continue
        if line.startswith("/unset "):
            for key in line[7:].split():
                defaults.pop(key, None)
            continue
        fields = line.split()
        if not fields or not fields[0].startswith("./"):
            raise RuntimeError(f"invalid mtree entry: {path.name}")
        name = mtree_unescape(fields[0][2:])
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"unsafe mtree entry: {path.name}")
        attributes = dict(defaults)
        attributes.update(
            field.split("=", 1) for field in fields[1:] if "=" in field
        )
        if name in result:
            raise RuntimeError(f"duplicate mtree entry: {name}")
        result[name] = attributes
    if not result:
        raise RuntimeError(f"empty mtree metadata: {path.name}")
    return result


def payload_entries(entries: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        name: attributes
        for name, attributes in entries.items()
        if not name.startswith(".")
    }


def non_directory_paths(entries: dict[str, dict[str, str]]) -> set[str]:
    return {
        name
        for name, attributes in entries.items()
        if attributes.get("type", "file") != "dir"
    }


def relation_name(value: str) -> str:
    return re.split(r"[<>= ]", value, maxsplit=1)[0]


def package_record(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"package is not a regular file: {path}")
    info = package_info(path)
    if len(info.get("pkgname", ())) != 1 or len(info.get("pkgver", ())) != 1:
        raise RuntimeError(f"package identity is ambiguous: {path.name}")
    entries = payload_entries(package_mtree(path))
    return {
        "path": path,
        "name": info["pkgname"][0],
        "version": info["pkgver"][0],
        "sha256": sha256_file(path),
        "info": info,
        "entries": entries,
        "files": non_directory_paths(entries),
    }


def assert_no_application_relations(record: dict[str, Any]) -> None:
    for field in ("conflict", "provides", "replaces"):
        forbidden = APPLICATION_RELATIONS & {
            relation_name(value) for value in record["info"].get(field, ())
        }
        if forbidden:
            raise RuntimeError(
                f"{record['name']} has forbidden application relationships: "
                + ", ".join(sorted(forbidden))
            )


def assert_component_contract(linux: dict[str, Any], full: dict[str, Any]) -> None:
    if linux["name"] != "hyperflux-next-linux" or linux["version"] != "2.0.1-1":
        raise RuntimeError("core component identity differs from the release contract")
    if full["name"] != "hyperflux-next-full" or full["version"] != linux["version"]:
        raise RuntimeError("full component identity differs from the release contract")
    for record in (linux, full):
        assert_no_application_relations(record)
    if set(linux["info"].get("conflict", ())) != {"hyperflux-v2-linux-dkms"}:
        raise RuntimeError("core conflicts differ from the receiver-driver boundary")
    if linux["info"].get("replaces", ()) or linux["info"].get("provides", ()):
        raise RuntimeError("core must not replace or provide OpenRazer integration packages")
    expected_core_dependencies = {"dkms", "glibc", "libgcc", "systemd"}
    if set(linux["info"].get("depend", ())) != expected_core_dependencies:
        raise RuntimeError("core dependencies differ from the direct runtime audit")
    expected_full_dependencies = {
        "glibc",
        "dkms",
        "libgcc",
        "libstdc++",
        "qt6-base",
        "hidapi",
        "systemd",
        f"hyperflux-next-linux={linux['version']}",
    }
    if set(full["info"].get("depend", ())) != expected_full_dependencies:
        raise RuntimeError("full dependencies differ from the direct ELF audit")
    expected_optional_dependencies = {
        "openrgb: Stock OpenRGB host for the HyperFlux plugin; OpenRGB 1.0/plugin API 5 verified",
        "openrazer-daemon: Stock OpenRazer service for compatible application access",
        "python-openrazer: Official Python OpenRazer client API",
        "polychromatic: Stock graphical OpenRazer frontend",
    }
    if set(full["info"].get("optdepend", ())) != expected_optional_dependencies:
        raise RuntimeError("full optional dependencies differ from the application boundary")
    expected_full_replacements = {
        "hyperflux-next-openrgb=0.0.0_dev.1-2",
        "openrazer-driver-dkms",
    }
    if set(full["info"].get("conflict", ())) != expected_full_replacements:
        raise RuntimeError("full conflicts differ from the integration boundary")
    if set(full["info"].get("replaces", ())) != expected_full_replacements:
        raise RuntimeError("full replacements differ from the integration boundary")
    if set(full["info"].get("provides", ())) != {
        "openrazer-driver-dkms=3.12.4",
        "hyperflux-openrazer-driver-abi=3.12.4",
    }:
        raise RuntimeError("full provides differ from the patched driver ABI")

    sibling_files: set[str] = set()
    for record in (linux, full):
        overlap = sibling_files & record["files"]
        if overlap:
            raise RuntimeError(f"sibling payload collision: {sorted(overlap)[0]}")
        sibling_files.update(record["files"])
    missing_full = FULL_REQUIRED_FILES - full["files"]
    if missing_full:
        raise RuntimeError(f"full integration payload is incomplete: {sorted(missing_full)[0]}")
    if any(
        path.startswith(prefix)
        for path in linux["files"]
        for prefix in FORBIDDEN_CORE_PREFIXES
    ):
        raise RuntimeError("core package contains application or development payload")
    forbidden_names = {
        "hyperflux-next-activation.service",
        "hyperflux-next-confirmation.service",
        "hyperflux-next-openrazer-session@.service",
        "hyperflux-next-openrazer-session@.path",
    }
    if any(Path(path).name in forbidden_names for path in linux["files"]):
        raise RuntimeError("core package contains removed lifecycle units")
    if FULL_REQUIRED_FILES & linux["files"]:
        raise RuntimeError("core package retained a Full integration payload")


def compare_application(
    candidates: tuple[dict[str, Any], ...], stock: dict[str, Any]
) -> dict[str, Any]:
    overlaps = sorted(
        set().union(*(candidate["files"] for candidate in candidates)) & stock["files"]
    )
    if overlaps:
        raise RuntimeError(
            f"candidate payload collides with {stock['name']}: {overlaps[0]}"
        )
    return {
        "package": stock["name"],
        "version": stock["version"],
        "sha256": stock["sha256"],
        "candidate_file_overlap": [],
    }


def old_polychromatic_takeover(
    old: dict[str, Any], polychromatic: dict[str, Any], new_files: set[str]
) -> dict[str, Any]:
    overlap = sorted(old["files"] & polychromatic["files"])
    identical: list[str] = []
    modified: list[str] = []
    for path in overlap:
        old_entry = old["entries"][path]
        stock_entry = polychromatic["entries"][path]
        old_identity = (
            old_entry.get("type", "file"),
            old_entry.get("sha256digest"),
            old_entry.get("link"),
        )
        stock_identity = (
            stock_entry.get("type", "file"),
            stock_entry.get("sha256digest"),
            stock_entry.get("link"),
        )
        (identical if old_identity == stock_identity else modified).append(path)
    if len(overlap) != 319 or len(identical) != 314 or len(modified) != 5:
        raise RuntimeError("old Polychromatic takeover inventory drifted")
    if new_files & set(overlap):
        raise RuntimeError("split payload retained a stock Polychromatic takeover path")
    expected_modified = {
        "usr/lib/python3.14/site-packages/polychromatic/common.py",
        "usr/lib/python3.14/site-packages/polychromatic/controller/shared.py",
        "usr/lib/python3.14/site-packages/polychromatic/middleman.py",
        "usr/lib/python3.14/site-packages/polychromatic/procpid.py",
        "usr/share/polychromatic/devicemaps/maps.json",
    }
    if set(modified) != expected_modified:
        raise RuntimeError("old modified Polychromatic file set drifted")
    old_relations = {
        field: set(old["info"].get(field, ()))
        for field in ("conflict", "provides", "replaces")
    }
    if any("polychromatic" not in values for values in old_relations.values()):
        raise RuntimeError("old monolith no longer records its Polychromatic takeover")
    return {
        "overlap_count": len(overlap),
        "byte_identical_count": len(identical),
        "modified_count": len(modified),
        "modified_paths": modified,
        "retained_by_split": [],
    }


def driver_overlap(
    candidates: tuple[dict[str, Any], ...], driver: dict[str, Any]
) -> dict[str, Any]:
    overlaps = set().union(*(candidate["files"] for candidate in candidates)) & driver["files"]
    if overlaps != DRIVER_OVERLAP:
        raise RuntimeError("driver package overlap differs from the declared replacement")
    return {
        "package": driver["name"],
        "version": driver["version"],
        "sha256": driver["sha256"],
        "intentional_overlap": sorted(overlaps),
    }


def write_json(path: Path, value: Any) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--old-monolith", type=Path, required=True)
    parser.add_argument("--polychromatic", type=Path, required=True)
    parser.add_argument("--openrazer-driver", type=Path, required=True)
    parser.add_argument("--openrazer-daemon", type=Path, action="append", required=True)
    parser.add_argument("--python-openrazer", type=Path, action="append", required=True)
    parser.add_argument("--stock-openrgb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    linux = package_record(arguments.linux.resolve())
    full = package_record(arguments.full.resolve())
    old = package_record(arguments.old_monolith.resolve())
    polychromatic = package_record(arguments.polychromatic.resolve())
    driver = package_record(arguments.openrazer_driver.resolve())
    daemons = [package_record(path.resolve()) for path in arguments.openrazer_daemon]
    clients = [package_record(path.resolve()) for path in arguments.python_openrazer]
    stock_openrgb = package_record(arguments.stock_openrgb.resolve())
    candidates = (linux, full)
    assert_component_contract(linux, full)
    new_files = set().union(*(record["files"] for record in candidates))

    applications = [
        compare_application(candidates, polychromatic),
        compare_application(candidates, stock_openrgb),
        *(compare_application(candidates, record) for record in daemons),
        *(compare_application(candidates, record) for record in clients),
    ]
    result = {
        "schema": SCHEMA,
        "candidate_packages": [
            {
                "name": record["name"],
                "version": record["version"],
                "sha256": record["sha256"],
                "file_count": len(record["files"]),
            }
            for record in candidates
        ],
        "sibling_payload_overlap": [],
        "application_collisions": applications,
        "driver_replacement": driver_overlap(candidates, driver),
        "old_monolith": {
            "name": old["name"],
            "version": old["version"],
            "sha256": old["sha256"],
            "polychromatic_takeover": old_polychromatic_takeover(
                old, polychromatic, new_files
            ),
            "obsolete_file_count": len(old["files"] - new_files),
        },
        "verdict": "passed",
    }
    write_json(arguments.output.resolve(), result)
    print("package-ownership-qualification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
