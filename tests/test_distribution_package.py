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
    _arch_hook,
    _arch_install,
    _arch_pkgbuild,
    _tar_payload,
)
from hfxdev.distributions import load_distribution_catalog
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, sha256_file


class DistributionPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_linux_runtime(ROOT)
        cls.catalog = load_distribution_catalog(ROOT)
        cls.arch = cls.catalog.targets["arch"]

    def test_arch_recipe_uses_one_payload_and_preserves_configuration(self) -> None:
        recipe = _arch_pkgbuild(
            self.runtime,
            self.catalog,
            self.arch,
            "x86_64",
            "3.14.6",
            "a" * 64,
        )
        self.assertIn("source=('payload.tar')", recipe)
        self.assertIn("noextract=('payload.tar')", recipe)
        self.assertIn("backup=('etc/hyperflux-next/bridge.json')", recipe)
        self.assertIn("options=('!strip' '!debug')", recipe)
        self.assertNotIn("usr/bin/hyperfluxctl usr/", recipe)
        for dependency in self.arch.dependencies:
            if dependency != "python":
                self.assertIn(dependency, recipe)
        self.assertIn("'python>=3.14'", recipe)
        self.assertIn("'python<3.15'", recipe)

    def test_arch_recipe_shell_quotes_human_metadata(self) -> None:
        catalog = replace(self.catalog, description="Receiver's transport")
        recipe = _arch_pkgbuild(
            self.runtime,
            catalog,
            self.arch,
            "x86_64",
            "3.14.6",
            "a" * 64,
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

    def test_arch_hooks_delegate_lifecycle_without_hardware_logic(self) -> None:
        install = _arch_install(self.runtime)
        transaction = _arch_hook(self.runtime)
        activation = self.runtime.operations.activation_path
        for command in ("fresh-install", "pre-update", "pre-remove"):
            self.assertIn(f"{activation} {command}", install)
        self.assertIn(f"Exec = {activation} post-update", transaction)
        self.assertIn("When = PostTransaction", transaction)
        self.assertIn("udevadm control --reload", install)
        for forbidden in ("modprobe", "unbind", "rebind", "hidraw", "1532"):
            self.assertNotIn(forbidden, install + transaction)

    def test_payload_tar_is_reproducible_and_root_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload"
            file = payload / "usr/share/hyperflux-next/value"
            file.parent.mkdir(parents=True)
            file.write_text("stable\n", encoding="utf-8")
            file.chmod(0o644)
            first = root / "first.tar"
            second = root / "second.tar"
            _tar_payload(payload, first, 1_700_000_000)
            _tar_payload(payload, second, 1_700_000_000)
            self.assertEqual(sha256_file(first), sha256_file(second))
            import tarfile

            with tarfile.open(first) as archive:
                member = archive.getmember("usr/share/hyperflux-next/value")
                self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o644))


if __name__ == "__main__":
    unittest.main()
