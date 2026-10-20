# SPDX-License-Identifier: GPL-2.0-only

"""Carry a Dependabot update into the files that record it.

Dependabot edits only the files it understands: the generated workflow files
and the Cargo lock. HyperFlux Next records the same pins in its authorities
(governance/github.json and assurance/dependencies.json), in the Rust crate
license notices, and in the public API fixture lock. This module copies the
new pins into those files so that ./hfx generate can rebuild everything else.
It refuses anything that needs a person: a new or removed crate, two
versions of one crate, or workflows that disagree about one action.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .model import ModelError

GOVERNANCE = Path("governance/github.json")
DEPENDENCIES = Path("assurance/dependencies.json")
RUST_NOTICES = Path("packaging/licenses/LICENSE-rust-crates")
FIXTURE_LOCK = Path("tests/fixtures/rust-public-api-consumer/Cargo.lock")
WORKFLOWS = Path(".github/workflows")

USES = re.compile(
    r"^\s*-?\s*uses:\s*(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[A-Za-z0-9_./-]+)?"
    r"@(?P<commit>[0-9a-f]{40})\s+#\s*(?P<version>v\S+)\s*$"
)
PACKAGE_BLOCK = re.compile(r"(?ms)^\[\[package\]\]\n.*?(?=^\[\[package\]\]\n|\Z)")


@dataclass(frozen=True)
class Change:
    path: str
    name: str
    old: str
    new: str


def _workflow_pins(root: Path) -> dict[str, tuple[str, str]]:
    pins: dict[str, set[tuple[str, str]]] = {}
    for path in sorted((root / WORKFLOWS).glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = USES.match(line)
            if match is not None:
                pins.setdefault(match["repository"], set()).add(
                    (match["version"], match["commit"])
                )
    disagreeing = sorted(name for name, values in pins.items() if len(values) != 1)
    if disagreeing:
        raise ModelError(
            "workflows use more than one pin for: " + ", ".join(disagreeing)
        )
    return {name: next(iter(values)) for name, values in pins.items()}


def sync_action_pins(root: Path) -> list[Change]:
    path = root / GOVERNANCE
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    pins = _workflow_pins(root)
    changes: list[Change] = []
    for action in document["automation"]["actions"]:
        pin = pins.get(action["repository"])
        if pin is None or pin == (action["version"], action["commit"]):
            continue
        version, commit = pin
        old = f'"version": "{action["version"]}",\n        "commit": "{action["commit"]}"'
        new = f'"version": "{version}",\n        "commit": "{commit}"'
        if text.count(old) != 1:
            raise ModelError(f"cannot locate the pin of {action['repository']} in {GOVERNANCE}")
        text = text.replace(old, new)
        changes.append(Change(str(GOVERNANCE), action["repository"], action["version"], version))
    if changes:
        path.write_text(text, encoding="utf-8")
    return changes


def _registry_packages(lock_text: str) -> dict[str, dict[str, object]]:
    packages: dict[str, dict[str, object]] = {}
    for package in tomllib.loads(lock_text)["package"]:
        if "source" not in package:
            continue
        if package["name"] in packages:
            raise ModelError(f"Cargo.lock holds two versions of {package['name']}")
        packages[package["name"]] = package
    return packages


def _registry_blocks(lock_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for block in PACKAGE_BLOCK.findall(lock_text):
        if "\nsource = " in block:
            name = re.search(r'^name = "([^"]+)"$', block, re.M)
            if name is None:
                raise ModelError("Cargo.lock package without a name")
            blocks[name.group(1)] = block
    return blocks


def sync_rust_crates(root: Path) -> list[Change]:
    lock_text = (root / "Cargo.lock").read_text(encoding="utf-8")
    locked = _registry_packages(lock_text)
    dependencies_path = root / DEPENDENCIES
    dependencies_text = dependencies_path.read_text(encoding="utf-8")
    recorded = {
        item["name"]: item
        for item in json.loads(dependencies_text)["rust_registry_packages"]
    }
    added = sorted(set(locked) - set(recorded))
    removed = sorted(set(recorded) - set(locked))
    if added or removed:
        raise ModelError(
            "the crate set changed and needs a license review by a person"
            + (f"; added: {', '.join(added)}" if added else "")
            + (f"; removed: {', '.join(removed)}" if removed else "")
        )
    changes = [
        Change(str(DEPENDENCIES), name, str(recorded[name]["version"]), str(locked[name]["version"]))
        for name in sorted(locked)
        if locked[name]["version"] != recorded[name]["version"]
    ]
    if not changes:
        return []

    notices_path = root / RUST_NOTICES
    notices = notices_path.read_text(encoding="utf-8")
    for change in changes:
        entry = f'{{"name": "{change.name}", "version": "{change.old}",'
        if dependencies_text.count(entry) != 1:
            raise ModelError(f"cannot locate {change.name} in {DEPENDENCIES}")
        dependencies_text = dependencies_text.replace(
            entry, f'{{"name": "{change.name}", "version": "{change.new}",'
        )
        notices, count = re.subn(
            rf"^(- )?{re.escape(change.name)} {re.escape(change.old)}(:| \(license: )",
            lambda match: f"{match[1] or ''}{change.name} {change.new}{match[2]}",
            notices,
            flags=re.M,
        )
        if count != 1:
            raise ModelError(f"cannot locate {change.name} {change.old} in {RUST_NOTICES}")

    fixture_path = root / FIXTURE_LOCK
    fixture = fixture_path.read_text(encoding="utf-8")
    blocks = _registry_blocks(lock_text)
    for name, block in _registry_blocks(fixture).items():
        if name not in blocks:
            raise ModelError(f"{FIXTURE_LOCK} holds {name}, which Cargo.lock does not")
        fixture = fixture.replace(block.rstrip("\n"), blocks[name].rstrip("\n"))

    dependencies_path.write_text(dependencies_text, encoding="utf-8")
    notices_path.write_text(notices, encoding="utf-8")
    fixture_path.write_text(fixture, encoding="utf-8")
    return changes


def sync_dependency_update(root: Path) -> list[Change]:
    return [*sync_action_pins(root), *sync_rust_crates(root)]
