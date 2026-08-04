# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import gzip
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.distribution_package import (
    CANONICAL_ARCH_BUILD_ROOT,
    CANONICAL_ARCH_PACKAGER,
    _arch_metadata,
    _canonical_arch_mtree,
    _canonical_arch_buildinfo,
    _arch_install,
    _arch_pkgbuild,
    _assert_no_sibling_payload_overlap,
    _tar_payload,
)
from hfxdev.distribution_native import normalize_tree, package_directory_modes
from hfxdev.distributions import load_distribution_catalog
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, sha256_file
from hfxdev.package_pipeline import StageResult


class DistributionPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_linux_runtime(ROOT)
        cls.catalog = load_distribution_catalog(ROOT)
        cls.arch = cls.catalog.targets["arch"]

    def test_arch_recipe_builds_two_non_overlapping_components(self) -> None:
        recipe = _arch_pkgbuild(
            self.runtime,
            self.catalog,
            self.arch,
            "x86_64",
            {"linux": "a" * 64, "full": "b" * 64},
        )
        self.assertIn("pkgbase=hyperflux-next", recipe)
        self.assertIn(
            "pkgname=('hyperflux-next-linux' 'hyperflux-next-full')",
            recipe,
        )
        self.assertIn("source=('payload-linux.tar' 'payload-full.tar')", recipe)
        self.assertIn("backup=('etc/hyperflux-next/bridge.json')", recipe)
        self.assertIn("options=('!strip' '!debug')", recipe)
        self.assertIn("package_hyperflux-next-linux()", recipe)
        self.assertIn("package_hyperflux-next-full()", recipe)
        self.assertIn("install=hyperflux-next-linux.install", recipe)
        self.assertIn("install=hyperflux-next-full.install", recipe)
        self.assertIn(
            "'openrgb: Stock OpenRGB host for the HyperFlux plugin; "
            "OpenRGB 1.0rc3/plugin API 4 verified'",
            recipe,
        )
        self.assertIn("'qt5-base'", recipe)
        self.assertIn("'hidapi'", recipe)
        self.assertIn("'libgcc'", recipe)
        self.assertIn("'libstdc++'", recipe)
        self.assertIn(
            "'polychromatic: Stock graphical OpenRazer frontend'",
            recipe,
        )
        self.assertIn("'hyperflux-next-openrgb=0.0.0_dev.1-2'", recipe)
        self.assertIn("'hyperflux-openrazer-driver-abi=3.12.4'", recipe)
        self.assertNotIn("provides=('polychromatic'", recipe)
        self.assertNotIn("replaces=('polychromatic'", recipe)
        self.assertIn(f"url='{self.catalog.homepage}'", recipe)

    def test_arch_recipe_shell_quotes_human_metadata(self) -> None:
        catalog = replace(self.catalog, description="Receiver's transport")
        recipe = _arch_pkgbuild(
            self.runtime,
            catalog,
            self.arch,
            "x86_64",
            {"linux": "a" * 64, "full": "b" * 64},
        )
        self.assertIn("pkgdesc='Receiver'\\''s transport'", recipe)

    def test_arch_buildinfo_uses_machine_independent_paths(self) -> None:
        value = _canonical_arch_buildinfo(
            b"format = 2\nbuilddir = /tmp/random-a\nstartdir = /tmp/random-b\n"
        ).decode("utf-8")
        self.assertIn(f"builddir = {CANONICAL_ARCH_BUILD_ROOT}\n", value)
        self.assertIn(f"startdir = {CANONICAL_ARCH_BUILD_ROOT}\n", value)
        self.assertNotIn("/tmp/", value)
        with self.assertRaises(ModelError):
            _canonical_arch_buildinfo(b"format = 2\nstartdir = /tmp/random\n")
        self.assertRegex(CANONICAL_ARCH_PACKAGER, r"^.+ <[^@]+@[^>]+>$")

    def test_arch_metadata_preserves_repeated_fields_and_rejects_malformed_input(
        self,
    ) -> None:
        metadata = _arch_metadata(
            b"# generated\npkgname = hyperflux-next-linux\nlicense = A\nlicense = B\n",
            "test metadata",
        )
        self.assertEqual(metadata["pkgname"], ("hyperflux-next-linux",))
        self.assertEqual(metadata["license"], ("A", "B"))
        with self.assertRaises(ModelError):
            _arch_metadata(b"missing separator\n", "test metadata")
        with self.assertRaises(ModelError):
            _arch_metadata(b"\xff\n", "test metadata")

    def test_arch_mtree_is_deterministic_and_describes_final_content(self) -> None:
        directory = tarfile.TarInfo("usr/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        file = tarfile.TarInfo("usr/value")
        file.mode = 0o644
        content = b"stable\n"
        file.size = len(content)
        mtree = tarfile.TarInfo(".MTREE")
        mtree.mode = 0o644
        entries = [(file, content), (mtree, b"old"), (directory, None)]
        first = _canonical_arch_mtree(entries, 1_700_000_000)
        second = _canonical_arch_mtree(list(reversed(entries)), 1_700_000_000)
        self.assertEqual(first, second)
        inventory = gzip.decompress(first).decode("utf-8")
        self.assertIn("./usr type=dir uid=0 gid=0 mode=755", inventory)
        self.assertIn(
            "./usr/value type=file uid=0 gid=0 mode=644 "
            "time=1700000000.0 size=7 sha256digest=",
            inventory,
        )
        self.assertNotIn("./.MTREE", inventory)

    def test_arch_hook_performs_registration_only(self) -> None:
        core = _arch_install(self.runtime, "linux")
        full = _arch_install(self.runtime, "full")
        self.assertIn("udevadm control --reload", core)
        self.assertIn("udevadm control --reload", full)
        self.assertIn("systemd-sysusers hyperflux-next.conf", core)
        self.assertIn("systemd-tmpfiles --create hyperflux-next.conf", core)
        self.assertNotIn("openrazer.conf", core)
        self.assertIn("systemd-sysusers openrazer.conf", full)
        self.assertNotIn("hyperflux-next.conf", full)
        for forbidden in (
            "hyperflux-next-activate",
            "udevadm trigger",
            "modprobe",
            "systemctl stop",
            "systemctl start",
            "systemctl restart",
            "systemctl enable",
        ):
            self.assertNotIn(forbidden, core)
            self.assertNotIn(forbidden, full)

    def test_payload_tar_is_reproducible_and_root_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload"
            file = payload / "usr/share/hyperflux-next/value"
            file.parent.mkdir(parents=True)
            file.write_text("stable\n", encoding="utf-8")
            file.chmod(0o644)
            configuration = payload / "etc/hyperflux-next"
            configuration.mkdir(parents=True)
            configuration.chmod(
                self.runtime.bridge.directory_modes.configuration
            )
            (configuration / "bridge.json").write_text("{}\n", encoding="utf-8")
            first = root / "first.tar"
            second = root / "second.tar"
            _tar_payload(payload, first, 1_700_000_000)
            _tar_payload(payload, second, 1_700_000_000)
            self.assertEqual(sha256_file(first), sha256_file(second))
            import tarfile

            with tarfile.open(first) as archive:
                member = archive.getmember("usr/share/hyperflux-next/value")
                self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o644))
                self.assertEqual(
                    archive.getmember("etc/hyperflux-next").mode,
                    self.runtime.bridge.directory_modes.configuration,
                )

    def test_distribution_normalization_keeps_package_owned_directory_modes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration = root / "etc/hyperflux-next"
            configuration.mkdir(parents=True)
            sibling = root / "usr/share/hyperflux-next"
            sibling.mkdir(parents=True)
            normalize_tree(
                root,
                1_700_000_000,
                package_directory_modes(self.runtime),
            )
            self.assertEqual(
                configuration.stat().st_mode & 0o7777,
                self.runtime.bridge.directory_modes.configuration,
            )
            self.assertEqual(sibling.stat().st_mode & 0o7777, 0o0755)

    def test_final_sibling_payloads_are_disjoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stages = {}
            for component in ("linux", "full"):
                stage = root / component
                stage.mkdir()
                file = stage / "usr" / "share" / component / "payload"
                file.parent.mkdir(parents=True)
                file.write_text(component, encoding="utf-8")
                stages[component] = StageResult(
                    root=stage,
                    inventory=root / f"{component}.json",
                    distribution_component=component,
                    payload_sha256="a" * 64,
                    file_count=1,
                )
            _assert_no_sibling_payload_overlap(stages)

            conflict = stages["full"].root / "usr" / "share" / "linux" / "payload"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("conflict", encoding="utf-8")
            stages["full"] = replace(stages["full"], file_count=2)
            with self.assertRaisesRegex(ModelError, "sibling payload overlap"):
                _assert_no_sibling_payload_overlap(stages)


if __name__ == "__main__":
    unittest.main()
