# SPDX-License-Identifier: GPL-2.0-only

"""Debian-format Core and Full packages built from the Arch split outputs."""

from __future__ import annotations

import gzip
import io
import json
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

from unittest import mock  # noqa: E402

from hfxdev import distribution_debian  # noqa: E402
from hfxdev.arch_split_package import split_edition_stages  # noqa: E402
from hfxdev.distribution_package import build_distribution_package  # noqa: E402
from hfxdev.package_pipeline import ArtifactSet  # noqa: E402
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


EXPECTED_FILES = [
    "hyperflux-next-dkms_2.1.0-1_all.deb",
    "hyperflux-next-full_2.1.0-1_amd64.deb",
    "hyperflux-next-linux_2.1.0-1_amd64.deb",
    "hyperflux-next-openrazer-driver-dkms_2.1.0-1_all.deb",
]


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

    def test_ubuntu_carries_both_editions(self) -> None:
        self.assertEqual(
            [output.id for output in target_outputs(self.split, self.target)],
            ["receiver-dkms", "core", "openrazer-driver", "full"],
        )
        self.assertEqual(
            [plan.package_name for plan in self.plans],
            [
                "hyperflux-next-dkms",
                "hyperflux-next-linux",
                "hyperflux-next-openrazer-driver-dkms",
                "hyperflux-next-full",
            ],
        )
        with self.assertRaisesRegex(ModelError, "not a Debian-format target"):
            target_outputs(self.split, self.catalog.targets["arch"])

    def test_dependencies_map_roles_and_pin_siblings(self) -> None:
        dkms, core, driver, full = self.plans
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
        for plan in (dkms, core, full):
            self.assertEqual((plan.provides, plan.replaces), ((), ()))
        self.assertEqual(full.conflicts, ())
        self.assertEqual(driver.section, "kernel")
        self.assertEqual(
            driver.depends, ("dkms", "udev", "systemd", "hyperflux-next-dkms (= 2.1.0-1)")
        )
        self.assertEqual(driver.conflicts, ("openrazer-driver-dkms", "openrazer-kernel-modules-dkms"))
        self.assertEqual(driver.replaces, driver.conflicts)
        self.assertIn("openrazer-driver-dkms (= 3.12.4~ubuntu26.10.1)", driver.provides)
        self.assertIn("hyperflux-openrazer-driver-abi (= 3.12.4)", driver.provides)
        self.assertEqual(
            full.depends,
            (
                "libc6 (>= 2.34)",
                "libgcc-s1",
                "systemd",
                "udev",
                "hyperflux-next-linux (= 2.1.0-1)",
                "hyperflux-next-openrazer-driver-dkms (= 2.1.0-1)",
            ),
        )
        for plan in self.plans:
            for dependency in ("qt6-base", "libqt6", "hidapi", "libhidapi", "libstdc++"):
                self.assertFalse(
                    any(item.startswith(dependency) for item in plan.depends), plan.package_name
                )
        self.assertIn("no OpenRGB in Ubuntu 26.10 can load it", "\n".join(full.extended_description))

    def test_maintainer_scripts_never_start_services_or_load_modules(self) -> None:
        dkms, core, driver, full = self.plans
        self.assertEqual([name for name, _ in dkms.scripts], ["postinst", "prerm"])
        self.assertEqual([name for name, _ in core.scripts], ["postinst", "postrm"])
        self.assertEqual([name for name, _ in driver.scripts], ["postinst", "prerm", "postrm"])
        self.assertEqual([name for name, _ in full.scripts], ["postinst", "postrm"])
        driver_postinst = dict(driver.scripts)["postinst"]
        self.assertIn(
            '/usr/lib/dkms/common.postinst openrazer-driver 3.12.4 "" "" "${2:-}"',
            driver_postinst,
        )
        self.assertIn("systemd-sysusers openrazer.conf", driver_postinst)
        self.assertIn(
            "dkms remove -m openrazer-driver -v 3.12.4 --all", dict(driver.scripts)["prerm"]
        )
        postinst = dict(dkms.scripts)["postinst"]
        self.assertIn(
            '/usr/lib/dkms/common.postinst hid-hyperflux-next 2.1.1 "" "" "${2:-}"', postinst
        )
        self.assertIn("dkms remove -m hid-hyperflux-next -v 2.1.1 --all", dict(dkms.scripts)["prerm"])
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
        self.assertTrue(text.startswith("hyperflux-next-linux (2.1.0-1) stonking; urgency=medium\n"))
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
            core_outputs = tuple(
                output
                for output in target_outputs(self.split, self.target)
                if output.id in {"receiver-dkms", "core"}
            )
            split_root = workspace / "split"
            split_root.mkdir()
            with self.assertRaisesRegex(ModelError, "selected outputs differ"):
                split_edition_stages(self.split, stages, split_root, EPOCH, core_outputs)
            del stages["full"]
            core_root = workspace / "core"
            core_root.mkdir()
            result = split_edition_stages(self.split, stages, core_root, EPOCH, core_outputs)
            self.assertEqual(list(result), ["receiver-dkms", "core"])
            self.assertEqual(
                sum(item.file_count for item in result.values()), stages["linux"].file_count
            )


@unittest.skipUnless(dpkg_deb_available(), "dpkg-deb is required")
class DebianDocumentationTests(unittest.TestCase):
    def test_ubuntu_section_is_unpublished_and_explains_secure_boot(self) -> None:
        from hfxdev.generators.distributions import markdown, section_heading
        from hfxdev.install import load_first_run_activation

        catalog = load_distribution_catalog(ROOT)
        text = markdown(
            catalog,
            load_linux_runtime(ROOT),
            load_first_run_activation(ROOT),
            candidate_split(),
        )
        ubuntu = text[text.index(section_heading(catalog.targets["ubuntu"])):]
        for expected in (
            "| `hyperflux-next-dkms` | `2.1.0-1` | `all` | `dkms` |",
            "`hyperflux-next-dkms (= 2.1.0-1)`",
            "mokutil --sb-state",
            "sudo update-secureboot-policy --enroll-key",
            "sudo mokutil --import /var/lib/shim-signed/mok/MOK.der",
            "MokManager",
            "modinfo -F signer hid_hyperflux_next",
        ):
            self.assertIn(expected, ubuntu)
        self.assertIn("| `hyperflux-next-full` | `2.1.0-1` | `amd64` |", ubuntu)
        self.assertIn("**Full on Ubuntu has no OpenRGB plugin.**", ubuntu)
        self.assertIn("Ubuntu 26.10 packages a development snapshot of OpenRGB from before 1.0", ubuntu)
        self.assertIn("Ubuntu 26.10 packages OpenRazer 3.12.4 as `openrazer-daemon`.", ubuntu)

    def test_debian_section_uses_the_dkms_signing_key(self) -> None:
        from hfxdev.generators.distributions import markdown, section_heading
        from hfxdev.install import load_first_run_activation

        catalog = load_distribution_catalog(ROOT)
        text = markdown(
            catalog,
            load_linux_runtime(ROOT),
            load_first_run_activation(ROOT),
            candidate_split(),
        )
        start = text.index(section_heading(catalog.targets["ubuntu"]))
        debian_start = text.index(section_heading(catalog.targets["debian"]))
        self.assertLess(start, debian_start)
        ubuntu = text[start:debian_start]
        debian = text[debian_start:]
        self.assertIn("carry both editions, Core and Full.", ubuntu)
        for expected in (
            "(`forky`)",
            "carry both editions, Core and Full.",
            "**Full on Debian includes the OpenRGB plugin.**",
            "built inside Debian 14 against Debian's own Qt 5",
            "`sudo gpasswd -a \"$USER\" openrazer`",
            "### Secure Boot on Debian",
            "sudo mokutil --import /var/lib/dkms/mok.pub",
            "glibc 2.43 that Debian 14 ships",
            "modinfo -F signer hid_hyperflux_next",
        ):
            self.assertIn(expected, debian)
        self.assertNotIn("update-secureboot-policy", debian)
        self.assertNotIn("ppa:openrazer", debian[: debian.index("### Secure Boot on Debian")])


class DebianBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.runtime = load_linux_runtime(ROOT)
        cls.target = cls.catalog.targets["ubuntu"]

    def _build(self, workspace: Path) -> tuple:
        stages = synthetic_edition_stages(self.split, workspace)
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
                sorted(package.name for _, _, package in built),
                EXPECTED_FILES,
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
                [
                    "dpkg-deb",
                    "--ctrl-tarfile",
                    str(next(path for plan, _, path in built if plan.package_name.endswith("-linux"))),
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(core_control)) as archive:
                members = {Path(name).name for name in archive.getnames()} - {"", "."}
                self.assertEqual(members, {"conffiles", "control", "md5sums", "postinst", "postrm"})
                conffiles = archive.extractfile("./conffiles")
                assert conffiles is not None
                self.assertEqual(conffiles.read(), b"/etc/hyperflux-next/bridge.json\n")

    def test_distro_command_writes_a_schema_shaped_manifest(self) -> None:
        for distribution, codename in (("ubuntu", "stonking"), ("debian", "forky")):
            with self.subTest(distribution=distribution):
                self._distro_command(distribution, codename)

    def _distro_command(self, distribution: str, codename: str) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "debian-package-build.schema.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "stages-root").mkdir()
            stages = synthetic_edition_stages(self.split, workspace / "stages-root")
            build_manifest = workspace / "artifacts.json"
            build_manifest.write_text("{}\n", encoding="utf-8")
            artifacts = ArtifactSet(
                root=workspace,
                revision="0" * 40,
                source_date_epoch=EPOCH,
                install_manifest_sha256="1" * 64,
                linux_runtime_sha256="2" * 64,
                python="3.14",
                target="x86_64-unknown-linux-gnu",
                artifacts=(),
                omitted=(),
            )

            def stage(
                _root: Path,
                _manifest: Path,
                destination: Path,
                component: str,
                *,
                openrgb_plugin: bool = True,
                openrgb_plugin_module: Path | None = None,
            ):
                self.assertIn(component, {"linux", "full"})
                # Debian 14 carries its own plugin build; Ubuntu leaves it out.
                self.assertEqual(openrgb_plugin, plugin or component == "linux")
                self.assertEqual(
                    openrgb_plugin_module,
                    plugin_module if plugin and component == "full" else None,
                )
                shutil.copytree(stages[component].root, destination)
                return stages[component].__class__(
                    root=destination,
                    inventory=stages[component].inventory,
                    distribution_component=component,
                    payload_sha256=stages[component].payload_sha256,
                    file_count=stages[component].file_count,
                )

            plugin = distribution == "debian"
            plugin_module = workspace / "plugin" / "hyperflux-next-openrgb.so"
            built: list[str] = []

            def build_plugin(_root, _artifacts, target, _work):
                built.append(target.id)
                return plugin_module

            with (
                mock.patch.object(distribution_debian, "load_artifact_set", return_value=artifacts),
                mock.patch.object(distribution_debian, "stage_rootfs", side_effect=stage),
                mock.patch.object(distribution_debian, "load_arch_split", return_value=self.split),
                mock.patch.object(
                    distribution_debian, "build_release_openrgb_plugin", side_effect=build_plugin
                ),
            ):
                result = build_distribution_package(
                    ROOT, build_manifest, distribution, workspace / "out"
                )
            self.assertEqual(built, ["debian"] if plugin else [])
            self.assertEqual(result.distribution, distribution)
            self.assertEqual(result.architecture, "amd64")
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest), set(schema["required"]))
            self.assertEqual(manifest["schema"], schema["properties"]["schema"]["const"])
            self.assertEqual(manifest["$schema"], schema["$id"])
            self.assertEqual(manifest["release"]["codename"], codename)
            self.assertEqual(
                manifest["maturity"],
                load_distribution_catalog(ROOT).targets[distribution].maturity,
            )
            package_schema = schema["$defs"]["package"]
            for package in manifest["packages"]:
                self.assertEqual(set(package), set(package_schema["required"]))
                for key, rule in package_schema["properties"].items():
                    if "pattern" in rule:
                        self.assertRegex(package[key], rule["pattern"])
                for relation in package["depends"]:
                    self.assertRegex(
                        relation, package_schema["properties"]["depends"]["items"]["pattern"]
                    )
                path = result.manifest.parent / "packages" / package["file"]
                self.assertTrue(path.is_file())
            self.assertEqual([package["file"] for package in manifest["packages"]], EXPECTED_FILES)
            full = next(
                package for package in manifest["packages"] if package["name"] == "hyperflux-next-full"
            )
            # Full depends on the libraries its plugin links only where it carries one.
            self.assertEqual(
                "libqt5widgets5t64" in full["depends"] and "libhidapi-hidraw0" in full["depends"],
                plugin,
            )
            self.assertFalse(any("qt6" in relation for relation in full["depends"]))


class DistributionDispatchTests(unittest.TestCase):
    def test_unknown_distribution_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ModelError, "only the Arch production package target"):
                build_distribution_package(
                    ROOT, Path(temporary) / "missing.json", "gentoo", Path(temporary) / "out"
                )


if __name__ == "__main__":
    unittest.main()
