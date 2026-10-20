from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.distribution_debian import THIRD_PARTY_FILES

RUST_NOTICES = ROOT / "packaging/licenses/LICENSE-rust-crates"
HEADING = re.compile(r"^(?P<name>[a-z0-9_-]+) (?P<version>\S+) \(license: (?P<license>[^;]+); used under MIT\)$")
EXCLUDED = re.compile(r"^- (?P<name>[a-z0-9_-]+) (?P<version>\S+): (?:used only while compiling|built only for Windows)$")
RULE = "=" * 72
MIT_GRANT = "Permission is hereby granted, free of charge, to any person obtaining a copy"


class RustCrateNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RUST_NOTICES.read_text(encoding="utf-8")
        inventory = json.loads((ROOT / "assurance/dependencies.json").read_text(encoding="utf-8"))
        self.registry = {
            item["name"]: (item["version"], item["license_expression"])
            for item in inventory["rust_registry_packages"]
        }

    def sections(self) -> dict[str, tuple[str, str, str]]:
        parts = self.text.split(f"\n{RULE}\n")
        sections = {}
        for heading, body in zip(parts[1::2], parts[2::2]):
            match = HEADING.match(heading)
            self.assertIsNotNone(match, heading)
            sections[match["name"]] = (match["version"], match["license"], body)
        return sections

    def test_every_registry_crate_has_a_notice_or_a_reason(self) -> None:
        sections = self.sections()
        excluded = {
            match["name"]: match["version"]
            for line in self.text.splitlines()
            if (match := EXCLUDED.match(line))
        }
        self.assertFalse(set(sections) & set(excluded))
        self.assertEqual(set(sections) | set(excluded), set(self.registry))
        for name, version in excluded.items():
            self.assertEqual(version, self.registry[name][0], name)
        for name, (version, license_expression, body) in sections.items():
            with self.subTest(crate=name):
                self.assertEqual((version, license_expression), self.registry[name])
                self.assertIn("MIT", license_expression.split(" OR "))
                self.assertIn(MIT_GRANT, " ".join(body.split()))

    def test_debian_copyright_names_the_shipped_notices(self) -> None:
        entries = {(license_id, files): outputs for license_id, outputs, files, *_ in THIRD_PARTY_FILES}
        self.assertEqual(entries[("MIT", ".cargo/registry/src/*")], ("core", "full"))
        self.assertEqual(entries[("MIT", "sdk/cpp/vendor/include/*")], ("full",))
        self.assertTrue((ROOT / "sdk/cpp/vendor/include/nlohmann/json.hpp").is_file())


if __name__ == "__main__":
    unittest.main()
