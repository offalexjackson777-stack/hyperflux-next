# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.development import load_development_environment
from hfxdev.generators.development import containerfile, devcontainer
from hfxdev.model import ModelError
from hfxdev.upstreams import prepare_upstreams


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class DevelopmentEnvironmentTests(unittest.TestCase):
    def test_environment_is_digest_snapshot_and_toolchain_bound(self) -> None:
        environment = load_development_environment(ROOT)
        self.assertEqual(environment.platform, "linux/amd64")
        self.assertRegex(environment.image_digest, r"^sha256:[0-9a-f]{64}$")
        archive_path = environment.archive_date.replace("-", "/")
        self.assertEqual(
            environment.archive_mirror,
            f"https://archive.archlinux.org/repos/{archive_path}/$repo/os/$arch",
        )
        self.assertEqual(environment.archive_download_attempts, 3)
        self.assertTrue(environment.archive_disable_low_speed_timeout)
        self.assertEqual(environment.rust_toolchain, "1.95.0-x86_64-unknown-linux-gnu")
        self.assertEqual(
            environment.post_create_network_uses,
            ("pinned-upstream-checkouts", "locked-rust-crates"),
        )
        self.assertEqual(
            [package.name for package in environment.packages],
            sorted(package.name for package in environment.packages),
        )

    def test_container_has_no_hardware_or_privileged_escape_hatch(self) -> None:
        environment = load_development_environment(ROOT)
        rendered = containerfile(environment)
        self.assertIn(f"FROM --platform=linux/amd64 {environment.image}", rendered)
        self.assertIn("CARGO_NET_OFFLINE=true", rendered)
        self.assertIn("PIP_NO_INDEX=1", rendered)
        self.assertIn("DisableDownloadTimeout", rendered)
        self.assertIn("until pacman -Syu", rendered)
        self.assertIn("test \"$attempt\" -lt 3", rendered)
        self.assertNotIn("--privileged", rendered)
        self.assertNotIn("/dev/hidraw", rendered)
        self.assertNotIn("latest", rendered)
        self.assertNotIn("\n+", rendered)

    def test_devcontainer_prepares_pins_without_hiding_verification(self) -> None:
        value = json.loads(devcontainer(load_development_environment(ROOT)))
        self.assertIn(
            "./hfx upstream prepare --output .hfx/upstreams",
            value["postCreateCommand"],
        )
        self.assertIn(
            "CARGO_NET_OFFLINE=false cargo fetch --locked",
            value["postCreateCommand"],
        )
        self.assertNotIn("verify", value["postCreateCommand"])
        self.assertNotIn("runArgs", value)
        self.assertEqual(
            value["containerEnv"]["CARGO_HOME"],
            "/workspaces/hyperflux-next/.hfx/cargo",
        )
        self.assertEqual(value["containerEnv"]["CARGO_NET_OFFLINE"], "true")

    def test_environment_rejects_toolchain_package_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "toolchains").mkdir()
            (root / "schemas").mkdir()
            pins = json.loads((ROOT / "toolchains" / "pins.json").read_text(encoding="utf-8"))
            environment = json.loads(
                (ROOT / "toolchains" / "development-environment.json").read_text(
                    encoding="utf-8"
                )
            )
            next(
                package for package in environment["system_packages"] if package["name"] == "clang"
            )["version"] = "0.0.0-1"
            (root / "toolchains" / "pins.json").write_text(
                json.dumps(pins), encoding="utf-8"
            )
            (root / "toolchains" / "development-environment.json").write_text(
                json.dumps(environment), encoding="utf-8"
            )
            with self.assertRaisesRegex(ModelError, "clang pin differs"):
                load_development_environment(root)

    def test_environment_rejects_invalid_archive_transfer_policy(self) -> None:
        cases = (
            ("download_attempts", 0, "attempts must be between one and five"),
            ("download_attempts", True, "attempts must be between one and five"),
            ("disable_low_speed_timeout", "yes", "timeout policy must be boolean"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "toolchains").mkdir()
                pins = json.loads(
                    (ROOT / "toolchains" / "pins.json").read_text(encoding="utf-8")
                )
                environment = json.loads(
                    (ROOT / "toolchains" / "development-environment.json").read_text(
                        encoding="utf-8"
                    )
                )
                environment["archive"][field] = value
                (root / "toolchains" / "pins.json").write_text(
                    json.dumps(pins), encoding="utf-8"
                )
                (root / "toolchains" / "development-environment.json").write_text(
                    json.dumps(environment), encoding="utf-8"
                )
                with self.assertRaisesRegex(ModelError, message):
                    load_development_environment(root)


class UpstreamPreparationTests(unittest.TestCase):
    def _fixture(self, temporary: str) -> tuple[Path, dict[str, object], str]:
        base = Path(temporary)
        source = base / "source"
        source.mkdir()
        _git(source, "init", "--quiet")
        _git(source, "config", "user.name", "HyperFlux Test")
        _git(source, "config", "user.email", "test@hyperflux.invalid")
        (source / "contract.txt").write_text("pinned\n", encoding="utf-8")
        _git(source, "add", "contract.txt")
        _git(source, "commit", "--quiet", "-m", "fixture")
        commit = _git(source, "rev-parse", "HEAD")
        repository = source.resolve().as_uri()
        upstream = {
            "id": "fixture",
            "name": "Fixture",
            "repository": repository,
            "version": "1",
            "commit": commit,
            "license_expression": "GPL-2.0-only",
            "api_contract": "fixture",
            "uses": ["contract"],
            "build_network_access": False,
        }
        root = base / "repository"
        (root / "integrations").mkdir(parents=True)
        (root / "integrations" / "catalog.json").write_text("{}\n", encoding="utf-8")
        return root, upstream, repository

    def test_prepare_is_atomic_idempotent_and_path_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, upstream, _ = self._fixture(temporary)
            catalog = {"upstreams": [upstream], "adapters": []}
            with patch("hfxdev.upstreams.load_integration_catalog", return_value=catalog):
                first = prepare_upstreams(root)
                second = prepare_upstreams(root)
            self.assertEqual(first.fetched, ("fixture",))
            self.assertEqual(second.reused, ("fixture",))
            value = json.loads(second.manifest.read_text(encoding="utf-8"))
            self.assertFalse(value["network_access_executed"])
            self.assertEqual(value["upstreams"][0]["path"], "fixture")
            self.assertNotIn(temporary, second.manifest.read_text(encoding="utf-8"))
            self.assertEqual(_git(second.root / "fixture", "rev-parse", "HEAD"), upstream["commit"])
            self.assertNotEqual(
                subprocess.run(
                    ["git", "symbolic-ref", "-q", "HEAD"],
                    cwd=second.root / "fixture",
                    check=False,
                ).returncode,
                0,
            )

    def test_prepare_rejects_dirty_reuse_instead_of_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, upstream, _ = self._fixture(temporary)
            catalog = {"upstreams": [upstream], "adapters": []}
            with patch("hfxdev.upstreams.load_integration_catalog", return_value=catalog):
                prepared = prepare_upstreams(root)
                (prepared.root / "fixture" / "contract.txt").write_text(
                    "local change\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ModelError, "local modifications"):
                    prepare_upstreams(root)

    def test_prepare_rejects_symbolic_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repository"
            root.mkdir()
            target = base / "target"
            target.mkdir()
            link = base / "upstreams"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ModelError, "symbolic link"):
                prepare_upstreams(root, link)


if __name__ == "__main__":
    unittest.main()
