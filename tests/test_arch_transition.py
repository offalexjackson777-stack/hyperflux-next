# SPDX-License-Identifier: GPL-2.0-only

"""Contracts for the two-to-four output transition and the optional AUR switch."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from arch_split_support import ROOT, candidate_split, two_package_split
from test_aur_recipe import source_repository

from hfxdev.arch_repository import (
    load_arch_repository_policy,
    repository_configuration,
    repository_setup_script,
)
from hfxdev.arch_split import load_arch_split
from hfxdev.arch_transition import (
    PRIOR_RELEASES,
    SWITCH_MARKER,
    PriorRelease,
    analyse_transition,
    check_pacman_safety,
    include_line,
    load_prior_topology,
    payload_items,
    rollback_script,
    switch_script,
)
from hfxdev.aur_recipe import generate_aur_recipe
from hfxdev.model import ModelError


PRIORS = {prior.release: prior for prior in PRIOR_RELEASES}


def _tag_available(prior: PriorRelease) -> bool:
    if prior.ref is None:
        return True
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{prior.ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == prior.commit


class PriorReleaseContracts(unittest.TestCase):
    """Each prior two-package release is read with its own pinned loaders."""

    def test_the_prior_releases_are_the_two_package_releases(self) -> None:
        self.assertEqual(("2.0.0-1", "2.0.1-1", "2.0.2-1"), tuple(PRIORS))
        self.assertTrue(all(prior.ref and prior.commit for prior in PRIORS.values()))
        self.assertTrue(load_arch_split(ROOT).active)
        self.assertEqual("2.1.0-1", load_arch_split(ROOT).full_version)

    def test_the_last_prior_release_is_two_packages(self) -> None:
        prior = PRIORS["2.0.2-1"]
        if not _tag_available(prior):
            self.skipTest(f"{prior.ref} is not available in this clone")
        topology = load_prior_topology(ROOT, prior)
        self.assertEqual(
            ("hyperflux-next-linux", "hyperflux-next-full"),
            tuple(package.name for package in topology.packages),
        )

    def test_pinned_tag_must_resolve_to_the_recorded_commit(self) -> None:
        prior = PRIORS["2.0.0-1"]
        if not _tag_available(prior):
            self.skipTest(f"{prior.ref} is not available in this clone")
        with self.assertRaisesRegex(ModelError, "moved to"):
            load_prior_topology(ROOT, dataclasses.replace(prior, commit="0" * 40))
        with self.assertRaisesRegex(ModelError, "not available"):
            load_prior_topology(ROOT, dataclasses.replace(prior, ref="refs/tags/hfx-no-such-tag"))
        with self.assertRaisesRegex(ModelError, "declares 2.0.0-1, not 2.0.0-2"):
            load_prior_topology(ROOT, dataclasses.replace(prior, release="2.0.0-2"))


class InPlaceUpgradeContracts(unittest.TestCase):
    """A signed-repository user reaches four outputs with -Syu, edition intact."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.full_only = {
            output.package_name for output in cls.split.outputs if output.edition == "full"
        }
        cls.topologies = {
            release: load_prior_topology(ROOT, prior)
            for release, prior in PRIORS.items()
            if _tag_available(prior)
        }
        if "2.0.2-1" not in cls.topologies:
            raise unittest.SkipTest(f"{PRIORS['2.0.2-1'].ref} is not available in this clone")
        cls.current = cls.topologies["2.0.2-1"]

    def _each_prior(self):
        for release in PRIORS:
            with self.subTest(prior=release):
                if release not in self.topologies:
                    self.skipTest(f"{PRIORS[release].ref} is not available in this clone")
                yield release, analyse_transition(self.split, self.topologies[release])

    def test_every_prior_release_upgrades_in_place(self) -> None:
        for release, report in self._each_prior():
            self.assertEqual((), report.problems)
            self.assertEqual("2.1.0-1", report.target_release)
            self.assertEqual(release, report.source_release)

    def test_core_stays_core_and_full_becomes_all_four(self) -> None:
        for _, report in self._each_prior():
            core, full = report.editions
            self.assertEqual(("hyperflux-next-linux",), core.before)
            self.assertEqual(self.split.install_set("linux"), core.after)
            self.assertFalse(self.full_only & set(core.after))
            self.assertEqual(self.split.install_set("full"), full.after)
            self.assertEqual(
                tuple(output.package_name for output in self.split.outputs), full.after
            )

    def test_every_prior_path_has_an_installed_successor(self) -> None:
        for _, report in self._each_prior():
            for edition in report.editions:
                self.assertEqual((), edition.dropped)
                self.assertGreater(edition.moved, 0)

    def test_the_broad_openrazer_replacement_is_disclosed_as_dropped(self) -> None:
        for release, report in self._each_prior():
            self.assertIn(
                f"{release} hyperflux-next-full replaced openrazer-driver-dkms; "
                "2.1.0-1 only conflicts with and provides it",
                report.disclosures,
            )
            for output in self.split.outputs:
                self.assertNotIn("openrazer-driver-dkms", [r.split("=")[0] for r in output.replaces])

    def _with_package(self, package_name: str, /, **changes):
        packages = tuple(
            dataclasses.replace(package, **changes) if package.name == package_name else package
            for package in self.current.packages
        )
        return dataclasses.replace(self.current, packages=packages)

    def _with_output(self, output_id: str, **changes):
        outputs = tuple(
            dataclasses.replace(output, **changes) if output.id == output_id else output
            for output in self.split.outputs
        )
        return dataclasses.replace(self.split, outputs=outputs)

    def _problems(self, split=None, prior=None) -> str:
        report = analyse_transition(split or self.split, prior or self.current)
        self.assertFalse(report.passed)
        return "\n".join(report.problems)

    def test_a_core_host_reaching_a_full_output_is_refused(self) -> None:
        core = self.split.output("core")
        split = self._with_output(
            "core", sibling_dependencies=(*core.sibling_dependencies, "openrazer-driver")
        )
        self.assertIn("-Syu would install Full outputs", self._problems(split=split))

    def test_a_stranded_prior_package_is_refused(self) -> None:
        legacy = dataclasses.replace(
            self.current.package("hyperflux-next-full"), name="hyperflux-next-legacy", depends=()
        )
        prior = dataclasses.replace(self.current, packages=(*self.current.packages, legacy))
        self.assertIn("hyperflux-next-legacy has no successor", self._problems(prior=prior))
        missing = dataclasses.replace(self.current, packages=self.current.packages[:1])
        self.assertIn("has no hyperflux-next-full to upgrade", self._problems(prior=missing))

    def test_a_target_that_does_not_upgrade_is_refused(self) -> None:
        prior = dataclasses.replace(self.current, release="2.1.0-3")
        self.assertIn("does not upgrade 2.1.0-3", self._problems(prior=prior))

    def test_a_new_conflict_forcing_a_removal_is_refused(self) -> None:
        split = self._with_output("core", conflicts=(*self.split.output("core").conflicts, "dkms-extra"))
        self.assertIn("newly conflicts with dkms-extra", self._problems(split=split))

    def test_a_conflict_inside_the_install_set_is_refused(self) -> None:
        split = self._with_output(
            "full", conflicts=(*self.split.output("full").conflicts, "hyperflux-next-dkms")
        )
        prior = self._with_package(
            "hyperflux-next-full",
            conflicts=(*self.current.package("hyperflux-next-full").conflicts, "hyperflux-next-dkms"),
        )
        self.assertIn(
            "hyperflux-next-full conflicts with hyperflux-next-dkms in the same set",
            self._problems(split=split, prior=prior),
        )

    def test_a_lost_or_changed_provide_is_refused(self) -> None:
        full = self.current.package("hyperflux-next-full")
        prior = self._with_package("hyperflux-next-full", provides=(*full.provides, "hyperflux-gone=1"))
        self.assertIn("hyperflux-gone is no longer provided", self._problems(prior=prior))
        changed = tuple(
            "openrazer-driver-dkms=3.12.3" if item.startswith("openrazer-driver-dkms=") else item
            for item in full.provides
        )
        prior = self._with_package("hyperflux-next-full", provides=changed)
        self.assertIn("openrazer-driver-dkms was provided at 3.12.3", self._problems(prior=prior))

    def test_a_core_path_moving_to_a_full_output_is_refused(self) -> None:
        tree = self.split.output("openrazer-driver").tree_destinations[0]
        linux = self.current.package("hyperflux-next-linux")
        prior = self._with_package("hyperflux-next-linux", files=(*linux.files, f"{tree}/dkms.conf"))
        self.assertIn("which -Syu does not install", self._problems(prior=prior))

    def test_a_path_without_successor_is_disclosed(self) -> None:
        linux = self.current.package("hyperflux-next-linux")
        prior = self._with_package("hyperflux-next-linux", files=(*linux.files, "/usr/share/hfx-gone"))
        report = analyse_transition(self.split, prior)
        self.assertTrue(report.passed)
        self.assertIn("/usr/share/hfx-gone", report.editions[0].dropped)

    def test_the_two_package_split_is_refused(self) -> None:
        with self.assertRaisesRegex(ModelError, "starts at 2.1.0-1"):
            analyse_transition(two_package_split(), self.current)


class PacmanSafetyContracts(unittest.TestCase):
    """No output or recipe edits Pacman configuration or deletes keys."""

    def test_the_payload_and_candidate_recipe_leave_pacman_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = source_repository(Path(directory))
            recipe = generate_aur_recipe(
                ROOT, commit=commit, source_repository=repository, split=candidate_split()
            )
        self.assertEqual((), check_pacman_safety(payload_items(ROOT), recipe.files["PKGBUILD"]))

    def test_hooks_config_edits_and_key_removal_are_refused(self) -> None:
        cases = {
            ("core", "/usr/share/libalpm/hooks/hfx.hook", b"[Trigger]\n"): "installs into Pacman state",
            ("core", "/etc/pacman.conf", b""): "installs into Pacman state",
            ("core", "/etc/pacman.d/hfx.conf", b""): "installs into Pacman state",
            ("core", "/usr/lib/hfx/x.sh", b"pacman-key --delete X\n"): "mentions pacman-key",
            ("core", "/usr/lib/hfx/y.sh", b"sed -i d /etc/pacman.conf\n"): "mentions pacman.conf",
        }
        for item, expected in cases.items():
            with self.subTest(destination=item[1]):
                self.assertIn(expected, "\n".join(check_pacman_safety((item,))))
        for pkgbuild, expected in {
            "install=hfx.install\n": "declares an install script",
            "package() {\n  pacman-key --lsign-key X\n}\n": "code mentions pacman-key",
        }.items():
            with self.subTest(pkgbuild=pkgbuild):
                self.assertIn(expected, "\n".join(check_pacman_safety((), pkgbuild)))
        self.assertEqual((), check_pacman_safety((), "# sudo pacman -U ... /etc/pacman.d\n"))


@unittest.skipUnless(shutil.which("pacman-conf"), "pacman-conf is not installed")
class SwitchScriptContracts(unittest.TestCase):
    """The switch comments out exactly one Include line and rollback restores it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_arch_repository_policy(ROOT)
        cls.split = candidate_split()
        cls.switch = switch_script(cls.policy)
        cls.rollback = rollback_script(cls.policy, cls.split)

    def setUp(self) -> None:
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.directory = Path(handle.name)
        self.include = self.directory / f"{self.policy.name}.conf"
        self.include.write_text(repository_configuration(self.policy), encoding="utf-8")
        self.conf = self.directory / "pacman.conf"
        self.original = (
            "[options]\nArchitecture = auto\n\n[core]\nServer = file:///srv/core\n\n"
            f"# Include = {self.include}\n\nInclude = {self.include}\n"
        )
        self.conf.write_text(self.original, encoding="utf-8")
        self.conf.chmod(0o640)
        self.environment = {
            **os.environ,
            "LC_ALL": "C",
            "HFX_PACMAN_CONF": str(self.conf),
            "HFX_REPOSITORY_INCLUDE": str(self.include),
        }

    def _run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def _repositories(self) -> list[str]:
        return subprocess.run(
            ["pacman-conf", "--config", str(self.conf), "--repo-list"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

    def _backups(self) -> list[Path]:
        return sorted(self.directory.glob("pacman.conf.hyperflux-next-aur-switch.*"))

    def test_the_script_targets_the_include_the_setup_script_writes(self) -> None:
        self.assertIn(f"include_line='{include_line(self.policy)}'", repository_setup_script(self.policy))
        self.assertIn(f"/etc/pacman.d/{self.policy.name}.conf", self.switch)

    def test_switch_then_rollback_round_trips(self) -> None:
        self.assertEqual(["core", self.policy.name], self._repositories())
        include_before = self.include.read_bytes()
        switched = self._run(self.switch)
        self.assertEqual(0, switched.returncode, switched.stderr)
        self.assertEqual(["core"], self._repositories())
        expected = self.original.replace(
            f"\nInclude = {self.include}\n", f"\n{SWITCH_MARKER}Include = {self.include}\n"
        )
        self.assertEqual(expected, self.conf.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(self.conf.stat().st_mode))
        (backup,) = self._backups()
        self.assertEqual(self.original, backup.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(backup.stat().st_mode))
        self.assertEqual(include_before, self.include.read_bytes())
        self.assertIn("Kept for rollback", switched.stdout)

        restored = self._run(self.rollback)
        self.assertEqual(0, restored.returncode, restored.stderr)
        self.assertEqual(self.original, self.conf.read_text(encoding="utf-8"))
        self.assertEqual(["core", self.policy.name], self._repositories())
        self.assertEqual(0o640, stat.S_IMODE(self.conf.stat().st_mode))
        self.assertEqual(include_before, self.include.read_bytes())
        self.assertIn("sudo pacman -Syuu", restored.stdout)
        for output in self.split.outputs:
            self.assertIn(output.package_name, restored.stdout)
        self.assertEqual(
            sorted([backup.name, "hyperflux-next.conf", "pacman.conf"]),
            sorted(path.name for path in self.directory.iterdir()),
        )

    def test_neither_script_touches_keys_or_the_include_file(self) -> None:
        for script in (self.switch, self.rollback):
            self.assertNotIn("pacman-key", script)
            commands = [line for line in script.splitlines() if not line.lstrip().startswith("printf")]
            self.assertFalse([line for line in commands if re.search(r"\bpacman\s", line)])
            removals = [line.strip() for line in script.splitlines() if "rm " in line]
            self.assertTrue(all(line.startswith('rm -f -- "$before"') for line in removals), removals)

    def test_switch_refuses_an_unexpected_configuration(self) -> None:
        cases = {
            "twice": self.original + f"Include = {self.include}\n",
            "absent": self.original.replace(f"\nInclude = {self.include}\n", "\n"),
            "inline": self.original + f"\n[{self.policy.name}]\nServer = file:///srv/other\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.conf.write_text(text, encoding="utf-8")
                for backup in self._backups():
                    backup.unlink()
                result = self._run(self.switch)
                self.assertEqual(1, result.returncode)
                self.assertEqual(text, self.conf.read_text(encoding="utf-8"))
                if name == "inline":
                    self.assertIn("still active; pacman.conf was restored", result.stderr)
                else:
                    self.assertIn("nothing changed", result.stderr)

    def test_switch_is_not_repeatable_and_rollback_needs_a_switch(self) -> None:
        self.assertEqual(1, self._run(self.rollback).returncode)
        self.assertEqual(self.original, self.conf.read_text(encoding="utf-8"))
        self.assertEqual(0, self._run(self.switch).returncode)
        switched = self.conf.read_text(encoding="utf-8")
        self.assertEqual(1, self._run(self.switch).returncode)
        self.assertEqual(switched, self.conf.read_text(encoding="utf-8"))

    def test_rollback_refuses_without_the_kept_include_file(self) -> None:
        self.assertEqual(0, self._run(self.switch).returncode)
        switched = self.conf.read_text(encoding="utf-8")
        self.include.rename(self.directory / "moved.conf")
        result = self._run(self.rollback)
        self.assertEqual(1, result.returncode)
        self.assertIn("nothing changed", result.stderr)
        self.assertEqual(switched, self.conf.read_text(encoding="utf-8"))


class TransitionCliContracts(unittest.TestCase):
    def _hfx(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "hfx"), "arch-transition", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )

    def test_check_reports_every_prior_release(self) -> None:
        result = self._hfx("check", "--candidate-release", "2.1.0-1")
        expected = [release for release, prior in PRIORS.items() if _tag_available(prior)]
        if len(expected) != len(PRIORS):
            self.assertEqual(1, result.returncode)
            self.assertIn("is not available in this clone", result.stderr)
            return
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["passed"])
        self.assertEqual(expected, [item["source_release"] for item in value["transitions"]])
        self.assertEqual([], value["pacman_safety_problems"])

    def test_scripts_are_printed_and_the_two_package_release_is_refused(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        split = candidate_split()
        switch = self._hfx("switch-script", "--candidate-release", "2.1.0-1")
        self.assertEqual(switch_script(policy), switch.stdout)
        rollback = self._hfx("rollback-script", "--candidate-release", "2.1.0-1")
        self.assertEqual(rollback_script(policy, split), rollback.stdout)
        refused = self._hfx("check", "--candidate-release", "2.0.2-1")
        self.assertEqual(1, refused.returncode)
        self.assertIn("starts at 2.1.0-1", refused.stderr)


if __name__ == "__main__":
    unittest.main()
