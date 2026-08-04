# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _registry_pins(path: Path) -> set[tuple[str, str, str, str]]:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        (package["name"], package["version"], package["source"], package["checksum"])
        for package in lock["package"]
        if "source" in package
    }


class PublicApiConsumerTests(unittest.TestCase):
    def test_compile_only_consumer_uses_the_workspace_dependency_graph(self) -> None:
        self.assertEqual(
            _registry_pins(ROOT / "tests/fixtures/rust-public-api-consumer/Cargo.lock"),
            _registry_pins(ROOT / "Cargo.lock"),
        )
        result = subprocess.run(
            [
                "cargo",
                "check",
                "--offline",
                "--locked",
                "--manifest-path",
                "tests/fixtures/rust-public-api-consumer/Cargo.toml",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
