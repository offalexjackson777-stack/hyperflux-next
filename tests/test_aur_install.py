# SPDX-License-Identifier: GPL-2.0-only

"""Contracts for the edition install plans of the AUR recipe."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import unittest

from arch_split_support import ROOT, candidate_split

from hfxdev.arch_split import load_arch_split
from hfxdev.aur_install import (
    DEFAULT_PKGEXT,
    install_plan,
    install_plans,
    package_file,
    pkgbuild_guidance,
    render_guide,
)
from hfxdev.model import ModelError


class InstallPlanContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.core, cls.full = install_plans(cls.split)
        cls.full_only = {
            output.package_name for output in cls.split.outputs if output.edition == "full"
        }

    def test_core_is_receiver_dkms_plus_core(self) -> None:
        self.assertEqual(("hyperflux-next-dkms", "hyperflux-next-linux"), self.core.packages)
        self.assertEqual("hyperflux-next-linux", self.core.request)
        self.assertEqual("paru -S hyperflux-next-linux", self.core.helper_command)

    def test_full_is_every_output(self) -> None:
        self.assertEqual(
            tuple(output.package_name for output in self.split.outputs), self.full.packages
        )
        self.assertEqual("hyperflux-next-full", self.full.request)
        self.assertEqual("paru -S hyperflux-next-full", self.full.helper_command)

    def test_manual_core_path_never_installs_a_full_output(self) -> None:
        self.assertFalse(self.full_only & set(self.core.packages))
        build, install = self.core.manual_commands
        self.assertEqual("makepkg -s", build)
        arguments = install.split()
        self.assertEqual(["sudo", "pacman", "-U"], arguments[:3])
        self.assertEqual(list(self.core.files), arguments[3:])
        for name in self.full_only:
            self.assertNotIn(name + "-", install)

    def test_no_instruction_builds_and_installs_in_one_step(self) -> None:
        text = render_guide(self.split) + "\n".join(pkgbuild_guidance(self.split, "x86_64"))
        commands = [
            line
            for plan in (self.core, self.full)
            for line in (*plan.manual_commands, plan.helper_command)
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotRegex(command, r"makepkg\s+(-\w*i|--install)")
        self.assertIn("Do not run makepkg -i or makepkg -si", text)

    def test_archive_names_follow_makepkg(self) -> None:
        self.assertEqual(
            "hyperflux-next-dkms-2.1.0-1-x86_64.pkg.tar.zst",
            package_file(self.split, "hyperflux-next-dkms", "x86_64", DEFAULT_PKGEXT),
        )
        with self.assertRaisesRegex(ModelError, "not an output"):
            package_file(self.split, "openrazer-driver-dkms", "x86_64", DEFAULT_PKGEXT)
        with self.assertRaisesRegex(ModelError, "PKGEXT"):
            package_file(self.split, "hyperflux-next-dkms", "x86_64", ".zip")

    def test_plans_refuse_unsafe_inputs(self) -> None:
        with self.assertRaisesRegex(ModelError, "two-package topology"):
            install_plan(load_arch_split(ROOT), "linux")
        with self.assertRaisesRegex(ModelError, "unknown edition"):
            install_plan(self.split, "minimal")
        with self.assertRaisesRegex(ModelError, "not a recipe architecture"):
            install_plan(self.split, "linux", architecture="aarch64")

    def test_a_core_closure_reaching_full_is_refused(self) -> None:
        core = self.split.output("core")
        leaking = dataclasses.replace(
            core, sibling_dependencies=(*core.sibling_dependencies, "openrazer-driver")
        )
        split = dataclasses.replace(
            self.split,
            outputs=tuple(leaking if output.id == "core" else output for output in self.split.outputs),
        )
        with self.assertRaisesRegex(ModelError, "Core would install a Full output"):
            install_plans(split)

    def test_cli_prints_the_plan_and_refuses_the_two_package_release(self) -> None:
        environment = {**os.environ, "LC_ALL": "C"}
        printed = subprocess.run(
            [sys.executable, str(ROOT / "hfx"), "package", "aur-install", "--candidate-release", "2.1.0-1"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout
        self.assertEqual(render_guide(self.split), printed)
        refused = subprocess.run(
            [sys.executable, str(ROOT / "hfx"), "package", "aur-install"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("two-package topology", refused.stderr)


if __name__ == "__main__":
    unittest.main()
