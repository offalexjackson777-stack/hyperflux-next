# SPDX-License-Identifier: GPL-2.0-only

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hfxdev.arch_update_policy import _host_dependency_assumptions
from hfxdev.model import ModelError


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


if __name__ == "__main__":
    unittest.main()
