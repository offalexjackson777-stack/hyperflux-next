#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]
BEHAVIORAL_REFERENCE_COMMIT = "c348305bb9c6790141932ddf79f707e5223bb51c"
CHARACTERIZATION_COMMIT = "0ee607601187b7b4c34bb7ad8148321056c1a330"
EXPECTED_PACKAGES = {
    "linux": {
        "name": "hyperflux-next-linux",
        "sha256": "4f3c9ab25029f65e75b03e2cdf0a50aa00d2a043abb79b25d3082d468fe00697",
    },
    "full": {
        "name": "hyperflux-next-full",
        "sha256": "1cd840343323ea761e89bae175993c4c1547f0c2f4ae7a825e26ce1b308fa1d3",
    },
}
STOCK_APPLICATIONS = (
    "openrgb",
    "openrazer-daemon",
    "python-openrazer",
    "polychromatic",
)
FORBIDDEN_STOCK_PAYLOAD_PREFIXES = (
    "usr/bin/openrgb",
    "usr/bin/openrazer-daemon",
    "usr/bin/polychromatic",
    "usr/lib/python",
    "usr/share/openrgb",
    "usr/share/openrazer",
    "usr/share/polychromatic",
)


def _run(*arguments: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout if binary else result.stdout.decode("utf-8")


def _git_bytes(commit: str, path: str) -> bytes:
    return _run("git", "show", f"{commit}:{path}", binary=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pkginfo(package: Path) -> dict[str, list[str]]:
    content = _run("bsdtar", "-xOf", str(package), ".PKGINFO")
    values: dict[str, list[str]] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            raise ValueError(f"invalid PKGINFO line in {package.name}")
        values.setdefault(key, []).append(value)
    return {key: values[key] for key in sorted(values)}


def _payload_inventory(package: Path) -> list[dict[str, object]]:
    compressed = _run("bsdtar", "-xOf", str(package), ".MTREE", binary=True)
    inventory: list[dict[str, object]] = []
    for line in gzip.decompress(compressed).decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = shlex.split(line)
        name = fields[0].removeprefix("./")
        if not name or PurePosixPath(name).parts[0].startswith("."):
            continue
        attributes = dict(field.split("=", 1) for field in fields[1:])
        entry: dict[str, object] = {
            "path": name,
            "type": attributes["type"],
            "mode": attributes["mode"],
            "uid": int(attributes["uid"]),
            "gid": int(attributes["gid"]),
        }
        if attributes["type"] == "file":
            entry["size"] = int(attributes["size"])
            entry["sha256"] = attributes["sha256digest"]
        inventory.append(entry)
    return sorted(inventory, key=lambda entry: str(entry["path"]))


def _package(component: str, path: Path) -> dict[str, object]:
    expected = EXPECTED_PACKAGES[component]
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected["sha256"]:
        raise ValueError(f"{component} package SHA-256 differs from release 8")
    metadata = _pkginfo(path)
    if metadata.get("pkgname") != [expected["name"]]:
        raise ValueError(f"{component} package identity differs from release 8")
    return {
        "component": component,
        "archive_name": path.name,
        "archive_size": path.stat().st_size,
        "archive_sha256": actual_sha256,
        "pkginfo": metadata,
        "payload": _payload_inventory(path),
    }


def _public_declarations(content: str) -> set[str]:
    return set(
        re.findall(
            r"^pub\s+(?:struct|enum|type|trait|const|static|fn)\s+([A-Za-z_][A-Za-z0-9_]*)",
            content,
            flags=re.MULTILINE,
        )
    )


def _root_exports(commit: str, crate_path: str) -> list[str]:
    source = _git_bytes(commit, f"{crate_path}/src/lib.rs").decode("utf-8")
    symbols: set[str] = set()
    for match in re.finditer(r"^pub use\s+(.+?);", source, flags=re.MULTILINE | re.DOTALL):
        expression = re.sub(r"\s+", " ", match.group(1)).strip()
        if expression.endswith("::*"):
            module = expression.removesuffix("::*").split("::")[-1]
            module_source = _git_bytes(commit, f"{crate_path}/src/{module}.rs").decode(
                "utf-8"
            )
            symbols.update(_public_declarations(module_source))
            continue
        if "{" in expression:
            body = expression.split("{", 1)[1].rsplit("}", 1)[0]
            for value in body.split(","):
                name = value.strip().split(" as ")[-1]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    symbols.add(name)
            continue
        name = expression.split(" as ")[-1].split("::")[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            symbols.add(name)
    return sorted(symbols, key=lambda value: value.replace("_", "\0"))


def _rust_exports() -> list[dict[str, object]]:
    crates: list[dict[str, object]] = []
    tree = _run("git", "ls-tree", "-r", "--name-only", BEHAVIORAL_REFERENCE_COMMIT, "crates")
    for lib_path in sorted(
        line for line in tree.splitlines() if re.fullmatch(r"crates/[^/]+/src/lib\.rs", line)
    ):
        crate_path = lib_path.removesuffix("/src/lib.rs")
        manifest = tomllib.loads(
            _git_bytes(BEHAVIORAL_REFERENCE_COMMIT, f"{crate_path}/Cargo.toml").decode(
                "utf-8"
            )
        )
        package_name = manifest["package"]["name"]
        crates.append(
            {
                "package": package_name,
                "import": package_name.replace("-", "_"),
                "path": crate_path,
                "symbols": _root_exports(BEHAVIORAL_REFERENCE_COMMIT, crate_path),
            }
        )
    return crates


def _generated_outputs() -> list[dict[str, str]]:
    script = (
        "from pathlib import Path; import sys; sys.path.insert(0, 'tools'); "
        "from hfxdev.render import rendered_files, rendered_binary_files; "
        "r=Path('.').resolve(); "
        "print('\\n'.join(sorted(p.relative_to(r).as_posix() for p in "
        "(*rendered_files(r), *rendered_binary_files(r)))))"
    )
    rendered = subprocess.run(
        ["python3", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [
        {
            "path": path,
            "sha256": _sha256_bytes(_git_bytes(CHARACTERIZATION_COMMIT, path)),
        }
        for path in rendered
    ]


def _write_consumer(path: Path, exports: list[dict[str, object]]) -> None:
    lines = [
        "// Generated by tools/generate_reconstruction_interface_baseline.py.",
        "// SPDX-License-Identifier: GPL-2.0-only",
        "#![allow(unused_imports)]",
        "",
    ]
    for crate in exports:
        symbols = crate["symbols"]
        lines.extend(f"use {crate['import']}::{symbol} as _;" for symbol in symbols)
        lines.append("")
    lines.append("pub fn compile_only_consumer() {}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--consumer", type=Path, required=True)
    arguments = parser.parse_args()

    exports = _rust_exports()
    value = {
        "$schema": "../../schemas/reconstruction-interface-baseline.schema.json",
        "schema": "hyperflux-reconstruction-interface-baseline-v1",
        "behavioral_reference_commit": BEHAVIORAL_REFERENCE_COMMIT,
        "characterization_commit": CHARACTERIZATION_COMMIT,
        "release": {"version": "0.0.0_dev.1", "package_release": 8},
        "packages": [
            _package("linux", arguments.core.resolve()),
            _package("full", arguments.full.resolve()),
        ],
        "rust_root_exports": exports,
        "generated_outputs": _generated_outputs(),
        "stock_application_boundary": {
            "optional_packages": list(STOCK_APPLICATIONS),
            "forbidden_payload_prefixes": list(FORBIDDEN_STOCK_PAYLOAD_PREFIXES),
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_consumer(arguments.consumer, exports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
