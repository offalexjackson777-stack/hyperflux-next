from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.dependency_sync import (
    DEPENDENCIES,
    FIXTURE_LOCK,
    GOVERNANCE,
    RUST_NOTICES,
    sync_action_pins,
    sync_dependency_update,
    sync_rust_crates,
)
from hfxdev.model import ModelError

COPIED = (
    GOVERNANCE,
    DEPENDENCIES,
    RUST_NOTICES,
    FIXTURE_LOCK,
    Path("Cargo.lock"),
    Path(".github/workflows/codeql.yml"),
    Path(".github/workflows/verification.yml"),
)
OLD_CODEQL = "24c54180a607b1449ed407dd24f251e4e9147c8d"
NEW_CODEQL = "6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee"


class DependencySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for path in COPIED:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, self.root / path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def read(self, path: Path) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def write(self, path: Path, text: str) -> None:
        (self.root / path).write_text(text, encoding="utf-8")

    def codeql_pin(self) -> tuple[str, str]:
        actions = json.loads(self.read(GOVERNANCE))["automation"]["actions"]
        codeql = next(item for item in actions if item["id"] == "codeql")
        return codeql["version"], codeql["commit"]

    def bump_codeql(self, *paths: Path) -> None:
        current_version, current_commit = self.codeql_pin()
        for path in paths:
            self.write(
                path,
                self.read(path).replace(
                    f"{current_commit} # {current_version}", f"{NEW_CODEQL} # v99.0.0"
                ),
            )

    def bump_crate(self, name: str, new: str) -> str:
        lock = self.read(Path("Cargo.lock"))
        marker = f'name = "{name}"\nversion = "'
        start = lock.index(marker) + len(marker)
        old = lock[start : lock.index('"', start)]
        self.write(Path("Cargo.lock"), lock[:start] + new + lock[start + len(old) :])
        return old

    def test_nothing_to_carry_leaves_every_file_alone(self) -> None:
        before = {path: self.read(path) for path in COPIED}
        self.assertEqual([], sync_dependency_update(self.root))
        self.assertEqual(before, {path: self.read(path) for path in COPIED})

    def test_workflow_action_bump_moves_the_governance_pin(self) -> None:
        self.bump_codeql(Path(".github/workflows/codeql.yml"))
        changes = sync_action_pins(self.root)
        self.assertEqual(["github/codeql-action"], [change.name for change in changes])
        self.assertEqual(("v99.0.0", NEW_CODEQL), self.codeql_pin())
        self.assertNotIn(OLD_CODEQL, self.read(GOVERNANCE))

    def test_workflows_that_disagree_about_one_action_stop_the_sync(self) -> None:
        workflow = Path(".github/workflows/codeql.yml")
        version, commit = self.codeql_pin()
        text = self.read(workflow)
        self.write(
            workflow,
            text.replace(f"{commit} # {version}", f"{NEW_CODEQL} # v99.0.0", 1),
        )
        with self.assertRaisesRegex(ModelError, "more than one pin for: github/codeql-action"):
            sync_action_pins(self.root)

    def test_crate_bump_moves_inventory_notice_and_fixture_lock(self) -> None:
        old = self.bump_crate("serde_json", "1.0.999")
        changes = sync_rust_crates(self.root)
        self.assertEqual([("serde_json", old, "1.0.999")], [(c.name, c.old, c.new) for c in changes])
        inventory = json.loads(self.read(DEPENDENCIES))["rust_registry_packages"]
        self.assertIn(
            "1.0.999", [item["version"] for item in inventory if item["name"] == "serde_json"]
        )
        self.assertIn("\nserde_json 1.0.999 (license: ", self.read(RUST_NOTICES))
        self.assertNotIn(f"serde_json {old} ", self.read(RUST_NOTICES))
        self.assertIn('name = "serde_json"\nversion = "1.0.999"', self.read(FIXTURE_LOCK))

    def test_build_only_crate_bump_moves_its_reason_line(self) -> None:
        old = self.bump_crate("syn", "3.0.999")
        sync_rust_crates(self.root)
        notices = self.read(RUST_NOTICES)
        self.assertIn("- syn 3.0.999: used only while compiling", notices)
        self.assertNotIn(f"- syn {old}:", notices)

    def test_new_crate_needs_a_person(self) -> None:
        lock = self.read(Path("Cargo.lock"))
        self.write(
            Path("Cargo.lock"),
            lock
            + '\n[[package]]\nname = "brand-new"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "' + "0" * 64 + '"\n',
        )
        with self.assertRaisesRegex(ModelError, "license review by a person; added: brand-new"):
            sync_rust_crates(self.root)


if __name__ == "__main__":
    unittest.main()
