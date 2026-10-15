# SPDX-License-Identifier: GPL-2.0-only

import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from hfxdev.arch_split import load_arch_split
from hfxdev.arch_update_policy import (
    _REQUIRED_TOOLS,
    _host_dependency_assumptions,
    verify_arch_openrazer_update_policy,
)
from hfxdev.distribution_package import DistributionPackageArtifact
from hfxdev.model import ModelError
from hfxdev.verify_packaging import _arch_full_edition_packages


ROOT = Path(__file__).resolve().parents[1]


def _metadata_package(directory: Path, name: str, lines: tuple[str, ...]) -> Path:
    payload = "\n".join(
        (
            f"pkgname = {name}",
            f"pkgbase = {name}",
            "pkgver = 2.1.0-1",
            "builddate = 1700000000",
            "size = 0",
            "arch = x86_64",
            *lines,
        )
    ).encode("utf-8") + b"\n"
    archive = directory / f"{name}-2.1.0-1-x86_64.pkg.tar"
    with tarfile.open(archive, mode="w", format=tarfile.GNU_FORMAT) as package:
        member = tarfile.TarInfo(".PKGINFO")
        member.size = len(payload)
        member.mtime = 1_700_000_000
        package.addfile(member, io.BytesIO(payload))
    subprocess.run(["zstd", "--quiet", "--rm", str(archive)], check=True)
    return archive.with_suffix(".tar.zst")


class ArchUpdatePolicyTests(unittest.TestCase):
    @staticmethod
    def _completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def test_exact_sibling_dependency_does_not_query_host_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full = root / "full.pkg.tar.zst"
            linux = root / "linux.pkg.tar.zst"
            full.touch()
            linux.touch()
            queries: list[list[str]] = []

            def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[:3] == ["bsdtar", "-xOf", str(full)]:
                    return self._completed(
                        command,
                        "pkgname = hyperflux-next-full\n"
                        "pkgver = 2.0.0-1\n"
                        "depend = glibc\n"
                        "depend = hyperflux-next-linux=2.0.0-1\n",
                    )
                if command[:3] == ["bsdtar", "-xOf", str(linux)]:
                    return self._completed(
                        command,
                        "pkgname = hyperflux-next-linux\n"
                        "pkgver = 2.0.0-1\n",
                    )
                queries.append(command)
                self.assertEqual(command, ["pacman", "-Q", "glibc"])
                return self._completed(command, "glibc 2.42+r17+g4f4a56e2f0ed-1\n")

            with patch("hfxdev.arch_update_policy._run", side_effect=run):
                assumptions = _host_dependency_assumptions(
                    full,
                    dependency_packages=(linux,),
                    timeout_seconds=10,
                )

            self.assertEqual(
                assumptions,
                (
                    "glibc=2.42+r17+g4f4a56e2f0ed-1",
                    "hyperflux-next-linux=2.0.0-1",
                ),
            )
            self.assertEqual(queries, [["pacman", "-Q", "glibc"]])

    def test_sibling_dependency_must_match_exact_built_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full = root / "full.pkg.tar.zst"
            linux = root / "linux.pkg.tar.zst"
            full.touch()
            linux.touch()

            def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                metadata = (
                    "pkgname = hyperflux-next-full\n"
                    "pkgver = 2.0.0-1\n"
                    "depend = hyperflux-next-linux>=2.0.0-1\n"
                    if str(full) in command
                    else "pkgname = hyperflux-next-linux\npkgver = 2.0.0-1\n"
                )
                return self._completed(command, metadata)

            with patch("hfxdev.arch_update_policy._run", side_effect=run), self.assertRaisesRegex(
                ModelError, "not exact-version bound"
            ):
                _host_dependency_assumptions(
                    full,
                    dependency_packages=(linux,),
                    timeout_seconds=10,
                )

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in _REQUIRED_TOOLS),
        "pacman tooling is unavailable",
    )
    def test_split_driver_package_keeps_openrazer_dependency_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = _metadata_package(
                root,
                "hyperflux-next-openrazer-driver-dkms",
                (
                    "provides = openrazer-driver-dkms=3.12.4",
                    "conflict = openrazer-driver-dkms",
                ),
            )
            core = _metadata_package(root, "hyperflux-next-linux", ())
            full = _metadata_package(
                root,
                "hyperflux-next-full",
                (
                    "depend = hyperflux-next-linux=2.1.0-1",
                    "depend = hyperflux-next-openrazer-driver-dkms=2.1.0-1",
                ),
            )

            verify_arch_openrazer_update_policy(
                full,
                package_name="hyperflux-next-full",
                dependency_packages=(core, driver),
                workspace=root / "policy",
                timeout_seconds=60,
            )

    def test_full_edition_packages_follow_the_active_topology(self) -> None:
        split = load_arch_split(ROOT)
        packages = tuple(
            DistributionPackageArtifact(
                component=output.id,
                package_name=output.package_name,
                package=Path(output.package_name),
                payload_sha256="",
                payload_file_count=0,
            )
            for output in split.outputs
        )
        full, dependencies = _arch_full_edition_packages(ROOT, packages)
        self.assertEqual(full.package_name, split.edition_output("full").package_name)
        self.assertEqual(
            tuple(package.package_name for package in (*dependencies, full)),
            split.install_set("full"),
        )
        with self.assertRaisesRegex(ModelError, "lacks the Full edition package set"):
            _arch_full_edition_packages(ROOT, packages[1:])


if __name__ == "__main__":
    unittest.main()
