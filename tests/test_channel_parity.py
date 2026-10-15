# SPDX-License-Identifier: GPL-2.0-only

"""Contracts proving the AUR and signed-repository channels are one product.

The repository channel is built by the real split builder. The AUR channel
runs the generated PKGBUILD package functions over one shared fake build
tree and packs each pkgdir with the PKGINFO those functions declare, which is
what makepkg writes. Both channels therefore start from identical build
outputs, so every difference the comparator finds is a channel difference.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Callable
import unittest

from arch_split_support import EPOCH, ROOT, candidate_split, two_package_split, makepkg_available
from test_aur_recipe import fake_build_tree, source_repository

from hfxdev.arch_split_package import build_split_packages
from hfxdev.aur_recipe import generate_aur_recipe, write_aur_recipe
from hfxdev.channel_parity import compare_channels, read_channel_package, recipe_source
from hfxdev.distribution_native import tree_digest, tree_files
from hfxdev.install import load_install_manifest
from hfxdev.model import ModelError
from hfxdev.package_pipeline import StageResult
from hfxdev.tree_metadata import normalize_tree


PKGINFO_SCRIPT = r"""
set -e
umask 022
source ./PKGBUILD
"package_$1"
emit() { local key=$1; shift; local item; for item in "$@"; do printf '%s = %s\n' "$key" "$item"; done; }
printf 'pkgname = %s\npkgbase = %s\nxdata = pkgtype=split\npkgver = %s-%s\n' "$1" "$pkgbase" "$pkgver" "$pkgrel"
printf 'pkgdesc = %s\nurl = %s\nbuilddate = 1\npackager = Unknown Packager\nsize = 0\narch = %s\n' "$pkgdesc" "$url" "${arch[0]}"
emit license "${license[@]}"
emit replaces "${replaces[@]}"
emit group "${groups[@]}"
emit conflict "${conflicts[@]}"
emit provides "${provides[@]}"
emit backup "${backup[@]}"
emit depend "${depends[@]}"
emit optdepend "${optdepends[@]}"
emit makedepend "${makedepends[@]}"
"""
ELF = b"\x7fELF\x02\x01\x01"


def _member(name: str, data: bytes, mode: int = 0o644) -> tuple[tarfile.TarInfo, io.BytesIO]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, io.BytesIO(data)


def pack(pkgdir: Path, pkginfo: str, archive: Path, *, install: bool = False) -> None:
    """Write a makepkg-shaped archive of one pkgdir."""

    with tarfile.open(archive, "w:gz") as handle:
        handle.addfile(*_member(".PKGINFO", pkginfo.encode("utf-8")))
        handle.addfile(*_member(".BUILDINFO", b"format = 2\n"))
        handle.addfile(*_member(".MTREE", b""))
        if install:
            handle.addfile(*_member(".INSTALL", b"post_install() { :; }\n"))
        for path in sorted(pkgdir.rglob("*")):
            relative = path.relative_to(pkgdir).as_posix()
            info = handle.gettarinfo(str(path), relative)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    handle.addfile(info, stream)
            else:
                handle.addfile(info)


@unittest.skipUnless(makepkg_available(), "makepkg, fakeroot, bsdtar, and zstd are required")
class ChannelParityContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.handle = tempfile.TemporaryDirectory()
        workspace = Path(cls.handle.name)
        cls.split = candidate_split()
        repository, cls.commit = source_repository(workspace)
        recipe = generate_aur_recipe(ROOT, commit=cls.commit, source_repository=repository, split=cls.split)
        cls.recipe = workspace / "recipe"
        write_aur_recipe(recipe, cls.recipe)

        manifest = load_install_manifest(ROOT)
        srcdir = workspace / "aur-build" / "src"
        fake_build_tree(srcdir, manifest)
        for build in manifest.builds:
            if build.kind == "cargo-binary":
                (srcdir / "target" / build.cargo_profile / build.target).write_bytes(ELF + build.id.encode())
            elif build.kind == "cmake-module":
                (srcdir / "build-openrgb-native" / build.output).write_bytes(ELF + build.id.encode())

        cls.pkgdirs: dict[str, Path] = {}
        cls.pkginfo: dict[str, str] = {}
        for output in cls.split.outputs:
            pkgdir = workspace / "aur-build" / "pkg" / output.package_name
            pkgdir.mkdir(parents=True)
            cls.pkginfo[output.package_name] = subprocess.run(
                ["bash", "-c", PKGINFO_SCRIPT, "pkginfo", output.package_name],
                cwd=cls.recipe,
                check=True,
                capture_output=True,
                text=True,
                env={"PATH": os.environ["PATH"], "srcdir": str(srcdir), "pkgdir": str(pkgdir)},
            ).stdout
            cls.pkgdirs[output.package_name] = pkgdir

        stages: dict[str, StageResult] = {}
        for edition in ("linux", "full"):
            root = workspace / f"{edition}-stage"
            root.mkdir()
            for output in cls.split.outputs:
                if output.edition == edition:
                    shutil.copytree(cls.pkgdirs[output.package_name], root, dirs_exist_ok=True, symlinks=True)
            modes = tuple(
                mode for output in cls.split.outputs if output.edition == edition for mode in output.directory_modes
            )
            normalize_tree(root, EPOCH, modes)
            files = tree_files(root)
            stages[edition] = StageResult(
                root=root,
                inventory=workspace / f"{edition}-inventory.json",
                distribution_component=edition,
                payload_sha256=tree_digest(root, files),
                file_count=len(files),
            )
        cls.repository = workspace / "repository"
        cls.repository.mkdir()
        build_root = workspace / "repository-build"
        build_root.mkdir()
        build_split_packages(
            repository_root=ROOT,
            workspace_root=build_root,
            packages=cls.repository,
            split=cls.split,
            architecture="x86_64",
            epoch=EPOCH,
            stages=stages,
        )
        cls.workspace = workspace

    @classmethod
    def tearDownClass(cls) -> None:
        cls.handle.cleanup()

    def aur_channel(
        self,
        mutate: Callable[[dict[str, Path], dict[str, str]], None] | None = None,
        *,
        install: str | None = None,
    ) -> Path:
        directory = Path(tempfile.mkdtemp(dir=self.workspace))
        pkgdirs: dict[str, Path] = {}
        for name, source in self.pkgdirs.items():
            pkgdirs[name] = directory / "pkg" / name
            shutil.copytree(source, pkgdirs[name], symlinks=True)
        pkginfo = dict(self.pkginfo)
        if mutate is not None:
            mutate(pkgdirs, pkginfo)
        output = directory / "packages"
        output.mkdir()
        for name, pkgdir in pkgdirs.items():
            pack(pkgdir, pkginfo[name], output / f"{name}-2.1.0-1-x86_64.pkg.tar.gz", install=name == install)
        return output

    def compare(self, aur: Path, **arguments):
        return compare_channels(self.split, repository=self.repository, aur=aur, **arguments)

    def test_identical_build_outputs_give_one_product(self) -> None:
        result = self.compare(self.aur_channel(), aur_recipe=self.recipe, repository_commit=self.commit)
        self.assertTrue(result.passed, result.problems)
        self.assertEqual(["AUR recipe is a qualification recipe, not a signed-tag publication"], list(result.disclosures))
        self.assertEqual({"repository_commit": self.commit, "aur_commit": self.commit}, result.sources)
        self.assertGreater(result.identical_files, 20)
        names = {output.package_name for output in self.split.outputs}
        self.assertEqual(names, set(result.packages["repository"]))
        self.assertEqual(names, set(result.packages["aur"]))
        for archive in self.repository.glob("*.pkg.tar.*"):
            self.assertIn(read_channel_package(archive).name, names)

    def test_compiled_differences_are_disclosed_not_hidden(self) -> None:
        binary = "/usr/bin/hyperfluxctl"
        owner = self.split.output(self.split.owner_of(binary)).package_name

        def rebuild(pkgdirs: dict[str, Path], _: dict[str, str]) -> None:
            pkgdirs[owner].joinpath(binary.lstrip("/")).write_bytes(ELF + b"independent toolchain")

        result = self.compare(self.aur_channel(rebuild))
        self.assertTrue(result.passed, result.problems)
        self.assertTrue(any(f"{owner} {binary} compiled bytes differ" in item for item in result.disclosures))
        self.assertIn("AUR recipe not supplied: source revision and version were not compared", result.disclosures)

    def test_channel_drift_fails(self) -> None:
        dkms = self.split.output("receiver-dkms").package_name
        core = self.split.output("core").package_name
        full = self.split.output("full").package_name
        core_file = next(path for path in self.split.output("core").file_destinations if not path.startswith("/usr/bin/"))

        def edit(pkgdirs, _):
            pkgdirs[core].joinpath(core_file.lstrip("/")).write_bytes(b"edited\n")

        def chmod(pkgdirs, _):
            pkgdirs[core].joinpath(core_file.lstrip("/")).chmod(0o600)

        def move(pkgdirs, _):
            source = pkgdirs[core].joinpath(core_file.lstrip("/"))
            target = pkgdirs[full].joinpath(core_file.lstrip("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)

        def depend(_, pkginfo):
            pkginfo[full] = pkginfo[full].replace(f"depend = {core}=2.1.0-1", f"depend = {core}")

        def provide(_, pkginfo):
            pkginfo[dkms] += "provides = openrazer-driver-dkms=3.12.4\n"

        def drop(pkgdirs, pkginfo):
            del pkgdirs[full], pkginfo[full]

        cases = {
            "content": (edit, None, f"{core} {core_file} non-compiled content differs"),
            "mode": (chmod, None, f"{core} {core_file} mode is"),
            "ownership": (move, None, f"which the split assigns to core"),
            "sibling version": (depend, None, f"aur {full} PKGINFO depend"),
            "provider": (provide, None, f"aur {dkms} PKGINFO provides"),
            "missing output": (drop, None, f"aur channel lacks ['{full}']"),
            "install script": (None, core, f"aur {core} carries an install script"),
        }
        for label, (mutate, install, message) in cases.items():
            with self.subTest(case=label):
                result = self.compare(self.aur_channel(mutate, install=install))
                self.assertFalse(result.passed)
                self.assertTrue(any(message in item for item in result.problems), result.problems)

    def test_source_revision_and_version_must_match(self) -> None:
        aur = self.aur_channel()
        result = self.compare(aur, aur_recipe=self.recipe, repository_commit="0" * 40)
        self.assertTrue(any("the repository packages 0000" in item for item in result.problems))
        result = self.compare(aur, aur_recipe=self.recipe)
        self.assertTrue(result.passed)
        self.assertIn("repository source commit not supplied: source revisions were not compared", result.disclosures)
        stale = self.workspace / "stale-recipe"
        shutil.copytree(self.recipe, stale)
        pkgbuild = stale / "PKGBUILD"
        pkgbuild.write_text(pkgbuild.read_text().replace("\npkgver=2.1.0\n", "\npkgver=2.0.2\n"))
        self.assertEqual("2.0.2-1", recipe_source(stale)["version"])
        result = self.compare(aur, aur_recipe=stale)
        self.assertTrue(any("AUR recipe builds 2.0.2-1" in item for item in result.problems))
        with self.assertRaisesRegex(ModelError, "full lowercase SHA-1"):
            self.compare(aur, repository_commit="HEAD")

    def test_cli_reports_parity_as_json(self) -> None:
        printed = subprocess.run(
            [
                sys.executable, str(ROOT / "hfx"), "package", "compare-channels",
                "--candidate-release", "2.1.0-1",
                "--repository", str(self.repository),
                "--aur", str(self.aur_channel()),
                "--aur-recipe", str(self.recipe),
                "--repository-commit", self.commit,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, printed.returncode, printed.stderr)
        self.assertIn('"passed": true', printed.stdout)


class ChannelParityRefusals(unittest.TestCase):
    def test_two_package_release_has_no_four_output_parity(self) -> None:
        from hfxdev.arch_split import load_arch_split

        with self.assertRaisesRegex(ModelError, "four-output split"):
            compare_channels(two_package_split(), repository=Path("/nonexistent"), aur=Path("/nonexistent"))

    def test_missing_channel_directory_is_refused(self) -> None:
        with self.assertRaisesRegex(ModelError, "channel directory does not exist"):
            compare_channels(candidate_split(), repository=Path("/nonexistent"), aur=Path("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
