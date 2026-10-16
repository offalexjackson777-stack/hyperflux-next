# SPDX-License-Identifier: GPL-2.0-only

"""Debian-format Core packages built from the Arch split outputs."""

from __future__ import annotations

import gzip
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arch_split_support import (  # noqa: E402
    EPOCH,
    ROOT,
    candidate_split,
    synthetic_edition_stages,
)

from hfxdev.arch_split_package import split_edition_stages  # noqa: E402
from hfxdev.distribution_debian import (  # noqa: E402
    build_debian_packages,
    changelog_bytes,
    control_text,
    copyright_text,
    debian_plans,
    debian_relation,
    glibc_requirements,
    parse_control,
    target_outputs,
)
from hfxdev.distributions import load_distribution_catalog  # noqa: E402
from hfxdev.linux_runtime import load_linux_runtime  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402


def dpkg_deb_available() -> bool:
    return shutil.which("dpkg-deb") is not None


class DebianPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.runtime = load_linux_runtime(ROOT)
        cls.target = cls.catalog.targets["ubuntu"]
        cls.plans = debian_plans(cls.split, cls.target, cls.runtime, cls.catalog)

    def test_ubuntu_carries_the_core_split_outputs_only(self) -> None:
        self.assertEqual(
            [output.id for output in target_outputs(self.split, self.target)],
            ["receiver-dkms", "core"],
        )
        self.assertEqual(
            [plan.package_name for plan in self.plans],
            ["hyperflux-next-dkms", "hyperflux-next-linux"],
        )
        with self.assertRaisesRegex(ModelError, "not a Debian-format target"):
            target_outputs(self.split, self.catalog.targets["arch"])

    def test_dependencies_map_roles_and_pin_siblings(self) -> None:
        dkms, core = self.plans
        self.assertEqual(dkms.version, "2.1.0-1")
        self.assertEqual(dkms.architecture, "all")
        self.assertEqual(dkms.section, "kernel")
        self.assertEqual(dkms.depends, ("dkms",))
        self.assertEqual(dkms.conflicts, ("hyperflux-v2-linux-dkms",))
        self.assertEqual(core.architecture, "amd64")
        self.assertEqual(
            core.depends,
            (
                "libc6 (>= 2.34)",
                "libgcc-s1",
                "systemd",
                "udev",
                "hyperflux-next-dkms (= 2.1.0-1)",
            ),
        )
        self.assertEqual(core.conffiles, ("/etc/hyperflux-next/bridge.json",))
        self.assertEqual(core.file_name, "hyperflux-next-linux_2.1.0-1_amd64.deb")

    def test_maintainer_scripts_never_start_services_or_load_modules(self) -> None:
        dkms, core = self.plans
        self.assertEqual([name for name, _ in dkms.scripts], ["postinst", "prerm"])
        self.assertEqual([name for name, _ in core.scripts], ["postinst", "postrm"])
        postinst = dict(dkms.scripts)["postinst"]
        self.assertIn(
            '/usr/lib/dkms/common.postinst hid-hyperflux-next 2.1.0 "" "" "${2:-}"', postinst
        )
        self.assertIn("dkms remove -m hid-hyperflux-next -v 2.1.0 --all", dict(dkms.scripts)["prerm"])
        core_postinst = dict(core.scripts)["postinst"]
        self.assertIn("systemd-sysusers hyperflux-next.conf", core_postinst)
        self.assertIn("systemd-tmpfiles --create hyperflux-next.conf", core_postinst)
        self.assertLess(
            core_postinst.index("systemd-sysusers"), core_postinst.index("systemd-tmpfiles")
        )
        self.assertIn("if [ -d /run/systemd/system ]; then", core_postinst)
        for plan in self.plans:
            for _, script in plan.scripts:
                self.assertTrue(script.startswith("#!/bin/sh\n"))
                self.assertIn("\nset -e\n", script)
                self.assertTrue(script.endswith("exit 0\n"))
                for forbidden in (
                    "enable",
                    "start",
                    "restart",
                    "modprobe",
                    "insmod",
                    "deb-systemd",
                ):
                    self.assertNotIn(forbidden, script)

    def test_control_copyright_and_changelog_are_canonical(self) -> None:
        core = self.plans[1]
        fields = parse_control(control_text(core, 42).encode("utf-8"))
        self.assertEqual(fields["Installed-Size"], "42")
        self.assertEqual(
            fields["Maintainer"], "HyperFlux Next Build System <build@hyperflux.invalid>"
        )
        self.assertTrue(fields["Description"].startswith(core.synopsis + "\n "))
        copyright_value = copyright_text(core)
        self.assertTrue(
            copyright_value.startswith(
                "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            )
        )
        self.assertIn("License: GPL-2\n", copyright_value)
        self.assertIn("/usr/share/common-licenses/GPL-2", copyright_value)
        changelog = changelog_bytes(core, EPOCH)
        self.assertEqual(changelog, changelog_bytes(core, EPOCH))
        self.assertEqual(changelog[4:8], b"\0\0\0\0")
        text = gzip.decompress(changelog).decode("utf-8")
        self.assertTrue(text.startswith("hyperflux-next-linux (2.1.0-1) noble; urgency=medium\n"))
        self.assertIn("  Thu, 01 Jan 2026 00:00:00 +0000\n", text)

    def test_relations_translate_or_fail_closed(self) -> None:
        self.assertEqual(debian_relation("foo", "x"), "foo")
        self.assertEqual(debian_relation("foo=1.0-1", "x"), "foo (= 1.0-1)")
        self.assertEqual(debian_relation("foo>=1.0", "x"), "foo (>= 1.0)")
        self.assertEqual(debian_relation("foo<1.0", "x"), "foo (<< 1.0)")
        self.assertEqual(debian_relation("libc6 (>= 2.34)", "x"), "libc6 (>= 2.34)")
        for value in ("Foo", "foo=", "foo (~ 1)", "foo>>1"):
            with self.subTest(value=value), self.assertRaises(ModelError):
                debian_relation(value, "x")

    def test_glibc_requirements_separate_weak_references(self) -> None:
        symbols = "\n".join(
            [
                "0000000000000000      DF *UND*\t0000000000000000 (GLIBC_2.34) __libc_start_main",
                "0000000000000000  w   DF *UND*\t0000000000000000 (GLIBC_2.39) pidfd_spawnp",
                "0000000000000000      DF *UND*\t0000000000000000 (GLIBC_2.2.5) getenv",
                "0000000000000000  w   D  *UND*\t0000000000000000              __gmon_start__",
                "0000000000001000 g    DF .text\t0000000000000010  Base        local",
            ]
        )
        self.assertEqual(glibc_requirements(symbols, "x"), ((2, 34), (2, 39)))
        with self.assertRaisesRegex(ModelError, "private glibc"):
            glibc_requirements(
                "0000000000000000      DO *UND*\t0000000000000000 (GLIBC_PRIVATE) _dl_argv", "x"
            )

    def test_split_subset_must_match_the_staged_editions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            stages = synthetic_edition_stages(self.split, workspace)
            del stages["full"]
            split_root = workspace / "split"
            split_root.mkdir()
            with self.assertRaisesRegex(ModelError, "selected outputs differ"):
                split_edition_stages(self.split, stages, split_root, EPOCH)
            result = split_edition_stages(
                self.split, stages, split_root, EPOCH, target_outputs(self.split, self.target)
            )
            self.assertEqual(list(result), ["receiver-dkms", "core"])
            self.assertEqual(
                sum(item.file_count for item in result.values()), stages["linux"].file_count
            )


@unittest.skipUnless(dpkg_deb_available(), "dpkg-deb is required")
class DebianBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.runtime = load_linux_runtime(ROOT)
        cls.target = cls.catalog.targets["ubuntu"]

    def _build(self, workspace: Path) -> tuple:
        stages = synthetic_edition_stages(self.split, workspace)
        del stages["full"]
        split_root = workspace / "split"
        split_root.mkdir()
        split_stages = split_edition_stages(
            self.split, stages, split_root, EPOCH, target_outputs(self.split, self.target)
        )
        packages = workspace / "packages"
        packages.mkdir()
        build_root = workspace / "build"
        build_root.mkdir()
        return build_debian_packages(
            repository_root=ROOT,
            workspace_root=build_root,
            packages=packages,
            split=self.split,
            target=self.target,
            runtime=self.runtime,
            catalog=self.catalog,
            epoch=EPOCH,
            stages=split_stages,
        )

    def test_dpkg_deb_builds_verified_reproducible_packages(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            built = self._build(Path(first))
            again = self._build(Path(second))
            self.assertEqual(
                [package.name for _, _, package in built],
                ["hyperflux-next-dkms_2.1.0-1_all.deb", "hyperflux-next-linux_2.1.0-1_amd64.deb"],
            )
            for (plan, _, package), (_, _, repeated) in zip(built, again, strict=True):
                self.assertEqual(package.read_bytes(), repeated.read_bytes(), plan.package_name)
                info = subprocess.run(
                    ["dpkg-deb", "--field", str(package), "Package", "Version", "Depends"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
                self.assertIn(f"Package: {plan.package_name}\n", info)
                self.assertIn(f"Depends: {', '.join(plan.depends)}\n", info)
                payload = subprocess.run(
                    ["dpkg-deb", "--fsys-tarfile", str(package)],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
                with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                    names = archive.getnames()
                    self.assertIn(f"./usr/share/doc/{plan.package_name}/copyright", names)
                    self.assertIn(f"./usr/share/doc/{plan.package_name}/changelog.Debian.gz", names)
                    for member in archive.getmembers():
                        self.assertEqual((member.uid, member.gid), (0, 0))
                        self.assertEqual(member.mtime, EPOCH)
            core_control = subprocess.run(
                ["dpkg-deb", "--ctrl-tarfile", str(built[1][2])],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(core_control)) as archive:
                members = {Path(name).name for name in archive.getnames()} - {"", "."}
                self.assertEqual(members, {"conffiles", "control", "md5sums", "postinst", "postrm"})
                conffiles = archive.extractfile("./conffiles")
                assert conffiles is not None
                self.assertEqual(conffiles.read(), b"/etc/hyperflux-next/bridge.json\n")


if __name__ == "__main__":
    unittest.main()
