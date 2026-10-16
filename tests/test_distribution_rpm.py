# SPDX-License-Identifier: GPL-2.0-only

"""RPM-format Core packages built from the Arch split outputs."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import shutil
import subprocess
import sys
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

from hfxdev import distribution_rpm  # noqa: E402
from hfxdev.arch_split_package import split_edition_stages  # noqa: E402
from hfxdev.distribution_package import build_distribution_package  # noqa: E402
from hfxdev.package_pipeline import ArtifactSet  # noqa: E402
from hfxdev.distribution_rpm import (  # noqa: E402
    GENERATED_HEADER,
    RPMLINT_CONFIG_NAME,
    RPMLINT_FILTERS,
    RPMLINT_FILTERS_FILE,
    RPMLINT_OVERRIDE_FILE,
    build_rpm_packages,
    changelog_date,
    file_entries,
    parse_cpio,
    rpm_plans,
    rpm_relation,
    rpmlint_config,
    rpmlint_configs,
    rpmlint_override_config,
    spec_text,
    target_outputs,
    verify_rpm_package,
)
from hfxdev.distributions import load_distribution_catalog  # noqa: E402
from hfxdev.linux_runtime import load_linux_runtime  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402


RPM_TARGETS = ("fedora", "opensuse")


def rpm_tools_available() -> bool:
    return all(shutil.which(tool) for tool in ("rpmbuild", "rpm", "rpm2cpio", "rpmkeys"))


def cpio_member(name: str, mode: int, content: bytes, *, links: int = 1) -> bytes:
    encoded = name.encode("utf-8") + b"\0"
    fields = [1, mode, 0, 0, links, EPOCH, len(content), 0, 0, 0, 0, len(encoded), 0]
    header = b"070701" + b"".join(f"{field:08x}".encode("ascii") for field in fields)
    member = header + encoded
    member += b"\0" * (-len(member) % 4)
    member += content
    member += b"\0" * (-len(member) % 4)
    return member


class RpmPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.runtime = load_linux_runtime(ROOT)
        cls.plans = {
            target_id: rpm_plans(
                cls.split, cls.catalog.targets[target_id], cls.runtime, cls.catalog
            )
            for target_id in RPM_TARGETS
        }

    def test_rpm_targets_carry_the_core_split_outputs_only(self) -> None:
        for target_id in RPM_TARGETS:
            with self.subTest(target=target_id):
                target = self.catalog.targets[target_id]
                self.assertEqual(
                    [output.id for output in target_outputs(self.split, target)],
                    ["receiver-dkms", "core"],
                )
                self.assertEqual(
                    [plan.package_name for plan in self.plans[target_id]],
                    ["hyperflux-next-dkms", "hyperflux-next-linux"],
                )
        for target_id in ("arch", "ubuntu", "debian"):
            with self.subTest(target=target_id), self.assertRaisesRegex(
                ModelError, "not an RPM-format target"
            ):
                target_outputs(self.split, self.catalog.targets[target_id])

    def test_dependencies_map_roles_per_distribution_and_pin_siblings(self) -> None:
        expected = {
            "fedora": ("libgcc", "systemd-udev"),
            "opensuse": ("libgcc_s1", "udev"),
        }
        for target_id, (compiler, device) in expected.items():
            with self.subTest(target=target_id):
                dkms, core = self.plans[target_id]
                self.assertEqual((dkms.version, dkms.release), ("2.1.0", "1"))
                self.assertEqual(dkms.architecture, "noarch")
                self.assertEqual(dkms.depends, ("dkms",))
                self.assertEqual(dkms.scriptlet_requires, (("post", "dkms"), ("preun", "dkms")))
                self.assertEqual(dkms.conflicts, ("hyperflux-v2-linux-dkms",))
                self.assertEqual(dkms.file_name, "hyperflux-next-dkms-2.1.0-1.noarch.rpm")
                self.assertEqual(core.architecture, "x86_64")
                self.assertEqual(
                    core.depends,
                    (
                        "glibc >= 2.34",
                        compiler,
                        "systemd",
                        device,
                        "hyperflux-next-dkms = 2.1.0-1",
                    ),
                )
                self.assertEqual(core.scriptlet_requires, (("post", "systemd"),))
                self.assertEqual(core.config_files, ("/etc/hyperflux-next/bridge.json",))
                self.assertEqual(core.file_name, "hyperflux-next-linux-2.1.0-1.x86_64.rpm")
                self.assertIn(self.catalog.targets[target_id].release.distribution, core.description[0])

    def test_scriptlets_never_start_services_or_load_modules(self) -> None:
        for target_id in RPM_TARGETS:
            dkms, core = self.plans[target_id]
            self.assertEqual([name for name, _ in dkms.scripts], ["post", "preun"])
            self.assertEqual([name for name, _ in core.scripts], ["post", "postun"])
            post = dict(dkms.scripts)["post"]
            self.assertIn("dkms add -m hid-hyperflux-next -v 2.1.0 --rpm_safe_upgrade", post)
            # The upgrade lock directory exists before `dkms add` takes the lock.
            self.assertLess(
                post.index("systemd-tmpfiles --create dkms.conf"), post.index("dkms add")
            )
            self.assertIn('dkms build -m hid-hyperflux-next -v 2.1.0 -k "$kernel"', post)
            self.assertIn(
                "dkms remove -m hid-hyperflux-next -v 2.1.0 --all --rpm_safe_upgrade",
                dict(dkms.scripts)["preun"],
            )
            core_post = dict(core.scripts)["post"]
            self.assertIn("systemd-sysusers hyperflux-next.conf", core_post)
            self.assertIn("systemd-tmpfiles --create hyperflux-next.conf", core_post)
            self.assertLess(core_post.index("systemd-sysusers"), core_post.index("systemd-tmpfiles"))
            self.assertIn("if [ -d /run/systemd/system ]; then", core_post)
            for plan in self.plans[target_id]:
                for _, script in plan.scripts:
                    self.assertTrue(script.startswith(GENERATED_HEADER + "\n"))
                    self.assertTrue(script.endswith("\nexit 0"))
                    for forbidden in (
                        "enable",
                        "start",
                        "restart",
                        "stop",
                        "modprobe",
                        "insmod",
                        "dkms install",
                        "%systemd",
                        "preset",
                    ):
                        self.assertNotIn(forbidden, script)

    def test_relations_translate_or_fail_closed(self) -> None:
        self.assertEqual(rpm_relation("foo", "x"), "foo")
        self.assertEqual(rpm_relation("foo=1.0-1", "x"), "foo = 1.0-1")
        self.assertEqual(rpm_relation("foo>=1.0", "x"), "foo >= 1.0")
        self.assertEqual(rpm_relation("foo<1.0", "x"), "foo < 1.0")
        self.assertEqual(rpm_relation("glibc >= 2.34", "x"), "glibc >= 2.34")
        for value in ("Foo bar", "foo=", "foo (>= 1)", "foo>>1", "foo ~ 1"):
            with self.subTest(value=value), self.assertRaises(ModelError):
                rpm_relation(value, "x")

    def test_spec_escapes_macros_and_lists_every_file(self) -> None:
        self.assertEqual(changelog_date(EPOCH), "Thu Jan 01 2026")
        plans = self.plans["fedora"]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            stages = synthetic_edition_stages(self.split, workspace)
            del stages["full"]
            split_root = workspace / "split"
            split_root.mkdir()
            split_stages = split_edition_stages(
                self.split,
                stages,
                split_root,
                EPOCH,
                target_outputs(self.split, self.catalog.targets["fedora"]),
            )
            entries = {
                plan.output: file_entries(plan, split_stages[plan.output].root) for plan in plans
            }
        core_entries = entries["core"]
        self.assertIn(("file", "/etc/hyperflux-next/bridge.json", 0o644), core_entries)
        self.assertTrue(all("hyperflux" in path for kind, path, _ in core_entries if kind == "dir"))
        self.assertNotIn("/usr/lib/systemd/system", [path for _, path, _ in core_entries])
        text = spec_text(self.split, plans, entries, EPOCH)
        self.assertTrue(text.startswith(GENERATED_HEADER + "\n"))
        self.assertIn("%package -n hyperflux-next-dkms\n", text)
        self.assertIn("BuildArch: noarch\n", text)
        self.assertIn("Requires(post): dkms\n", text)
        self.assertIn("Requires: hyperflux-next-dkms = 2.1.0-1\n", text)
        self.assertIn(
            "%attr(0644,root,root) %config(noreplace) /etc/hyperflux-next/bridge.json\n", text
        )
        self.assertIn("%license /usr/share/licenses/hyperflux-next-linux/LICENSE\n", text)
        self.assertIn("%dir %attr(2751,root,root) /etc/hyperflux-next\n", text)
        # Shell parameter expansion survives rpmbuild's macro pass.
        self.assertIn("kernel=${build%%/build}", text)
        self.assertIn("* Thu Jan 01 2026 HyperFlux Next Build System", text)
        self.assertNotIn("%systemd_post", text)
        broken = dataclasses.replace(plans[0], summary="one\ntwo")
        with self.assertRaisesRegex(ModelError, "cannot be written into a spec"):
            spec_text(self.split, (broken, plans[1]), entries, EPOCH)
        mixed = dataclasses.replace(plans[0], release="2")
        with self.assertRaisesRegex(ModelError, "share one version"):
            spec_text(self.split, (mixed, plans[1]), entries, EPOCH)

    def test_cpio_parser_reads_newc_and_fails_closed(self) -> None:
        archive = (
            cpio_member("./etc", 0o40755, b"", links=2)
            + cpio_member("./etc/a", 0o100644, b"abc")
            + cpio_member("TRAILER!!!", 0, b"")
        )
        self.assertEqual(
            parse_cpio(archive),
            [
                ("./etc", 0o40755, 0, 0, EPOCH, b""),
                ("./etc/a", 0o100644, 0, 0, EPOCH, b"abc"),
            ],
        )
        hard_link = cpio_member("./a", 0o100644, b"x", links=2) + cpio_member("TRAILER!!!", 0, b"")
        for name, value in (
            ("not cpio", b"x" * 200),
            ("odc", b"070707" + archive[6:]),
            ("truncated", archive[:-150]),
            ("hard link", hard_link),
            ("missing trailer", cpio_member("./a", 0o100644, b"x")),
        ):
            with self.subTest(case=name), self.assertRaises(ModelError):
                parse_cpio(value)


class RpmLintTests(unittest.TestCase):
    # Errors rpmlint reported for these packages in the Fedora 44 and
    # openSUSE Tumbleweed images.
    ACCEPTED = (
        "hyperflux-next-linux.x86_64: E: non-standard-dir-perm /etc/hyperflux-next 2751",
        "hyperflux-next-linux.x86_64: E: incorrect-fsf-address /usr/share/licenses/hyperflux-next-linux/LICENSE",
        "hyperflux-next-linux.x86_64: E: explicit-lib-dependency libgcc",
        "hyperflux-next-linux.x86_64: E: explicit-lib-dependency libgcc_s1",
        'hyperflux-next-linux.x86_64: E: systemd-tmpfile-entry-unauthorized (Badness: 10) /usr/lib/tmpfiles.d/hyperflux-next.conf "d /run/hyperflux-next 2750 hyperflux-next hyperflux -"',
        "hyperflux-next-linux.x86_64: E: systemd-service-without-service_del_preun hyperflux-next-bridge.service",
        "hyperflux-next-linux.x86_64: E: systemd-service-without-service_add_post hyperflux-next-bridge.service",
        "hyperflux-next-dkms.noarch: E: spelling-error ('openSUSE', '%description -l en_US openSUSE -> opens Use, open SUSE, open-SUSE')",
        "hyperflux-next-dkms.noarch: E: spelling-error ('linux', '%description -l en_US linux -> Linux, linkup')",
        "hyperflux-next-linux.x86_64: E: permissions-missing-requires missing 'permissions' in PreReq",
        "hyperflux-next-linux.x86_64: E: permissions-missing-postin (Badness: 10) missing %set_permissions /etc/hyperflux-next in %post",
        "hyperflux-next-linux.x86_64: E: permissions-directory-setuid-bit (Badness: 10) /etc/hyperflux-next is packaged with setuid/setgid bits (02751)",
        "hyperflux-next-dkms.noarch: E: filelist-forbidden-fhs23 /usr/src/hid-hyperflux-next-2.1.0",
        "hyperflux-next-dkms.noarch: E: devel-file-in-non-devel-package (Badness: 50) /usr/src/hid-hyperflux-next-2.1.0/hyperflux-next-core.c",
    )
    # Neighbouring findings that must stay errors.
    REJECTED = (
        "hyperflux-next-linux.x86_64: E: non-standard-dir-perm /etc/hyperflux-next 0777",
        "hyperflux-next-linux.x86_64: E: non-standard-dir-perm /var/lib/hyperflux-next 2751",
        "hyperflux-next-linux.x86_64: E: explicit-lib-dependency libssl3",
        "hyperflux-next-linux.x86_64: E: incorrect-fsf-address /usr/share/doc/hyperflux-next-linux/README",
        "hyperflux-next-linux.x86_64: E: devel-file-in-non-devel-package /usr/include/hyperflux_next.h",
        "hyperflux-next-dkms.noarch: E: devel-file-in-non-devel-package /usr/include/hyperflux_next.h",
        "hyperflux-next-linux.x86_64: E: systemd-service-without-service_del_preun other.service",
        "hyperflux-next-linux.x86_64: E: systemd-tmpfile-entry-unauthorized /usr/lib/tmpfiles.d/other.conf x",
        "hyperflux-next-linux.x86_64: E: spelling-error ('hyperflxu', '%description -l en_US hyperflxu')",
        "hyperflux-next-linux.x86_64: E: setuid-binary /usr/bin/hyperfluxctl root 04755",
        "other-package.x86_64: E: non-standard-dir-perm /etc/hyperflux-next 2751",
        "hyperflux-next-linux.x86_64: W: non-standard-dir-perm /etc/hyperflux-next 2751",
    )

    def setUp(self) -> None:
        import re
        import tomllib

        self.config = tomllib.loads(rpmlint_config())
        self.filters = [re.compile(pattern) for pattern in self.config["Filters"]]

    def accepted(self, line: str) -> bool:
        return any(pattern.search(line) for pattern in self.filters)

    def test_config_holds_exactly_the_reasoned_filters(self) -> None:
        self.assertEqual(set(self.config), {"Filters"})
        self.assertEqual(
            self.config["Filters"],
            [pattern for _, patterns in RPMLINT_FILTERS for pattern in patterns],
        )
        text = rpmlint_config()
        self.assertTrue(text.startswith(GENERATED_HEADER + "\n"))
        for reason, patterns in RPMLINT_FILTERS:
            self.assertTrue(reason.endswith("."))
            self.assertIn(f"# {reason}", text)
            for pattern in patterns:
                # Each filter names one of these packages and only accepts errors.
                self.assertRegex(pattern, r"^hyperflux-next-(dkms|linux|\(dkms\|linux\))\\\.")
                self.assertIn(": E: ", pattern)

    def test_override_only_unblocks_the_named_filters(self) -> None:
        import tomllib

        # rpmlint replaces lists only from files whose name has ".override.";
        # the filters must merge with the distribution's own.
        self.assertEqual(
            [name for name, _ in rpmlint_configs()],
            [RPMLINT_FILTERS_FILE, RPMLINT_OVERRIDE_FILE],
        )
        self.assertNotIn(".override.", RPMLINT_FILTERS_FILE)
        self.assertIn(".override.", RPMLINT_OVERRIDE_FILE)
        for name, _ in rpmlint_configs():
            self.assertTrue(name.endswith(".toml"))
        text = rpmlint_override_config()
        self.assertTrue(text.startswith(GENERATED_HEADER + "\n"))
        self.assertEqual(tomllib.loads(text), {"BlockedFilters": []})

    def test_filters_accept_the_known_errors_and_nothing_near_them(self) -> None:
        for line in self.ACCEPTED:
            self.assertTrue(self.accepted(line), line)
        for line in self.REJECTED:
            self.assertFalse(self.accepted(line), line)

    def test_unwritable_filters_fail_closed(self) -> None:
        for pattern in (
            "hyperflux-next-linux\\.x86_64: E: it's",
            "hyperflux-next-linux\\.x86_64: E: a\nb",
            "E: non-standard-dir-perm",
            "hyperflux-next-linux: E: (",
        ):
            with self.subTest(pattern=pattern), mock.patch.object(
                distribution_rpm, "RPMLINT_FILTERS", (("Reason.", (pattern,)),)
            ):
                with self.assertRaises(ModelError):
                    rpmlint_config()


class RpmDocumentationTests(unittest.TestCase):
    def test_rpm_sections_are_unpublished_and_explain_activation(self) -> None:
        from hfxdev.generators.distributions import markdown
        from hfxdev.install import load_first_run_activation

        text = markdown(
            load_distribution_catalog(ROOT),
            load_linux_runtime(ROOT),
            load_first_run_activation(ROOT),
            candidate_split(),
        )
        debian_start = text.index("## Debian 13 (CI-built, not published yet)")
        fedora_start = text.index("## Fedora 44 (CI-built, not published yet)")
        opensuse_start = text.index("## openSUSE Tumbleweed (CI-built, not published yet)")
        self.assertLess(debian_start, fedora_start)
        self.assertLess(fedora_start, opensuse_start)
        fedora = text[fedora_start:opensuse_start]
        opensuse = text[opensuse_start:]
        for section, distribution, compiler, device, glibc in (
            (fedora, "Fedora", "libgcc", "systemd-udev", "2.43"),
            (opensuse, "openSUSE", "libgcc_s1", "udev", "2.44"),
        ):
            for expected in (
                "`.rpm` packages",
                f"no Full edition for {distribution}.",
                "| `hyperflux-next-dkms` | `2.1.0-1` | `noarch` | `dkms` |",
                f"`{compiler}`, `systemd`, `{device}`, `hyperflux-next-dkms = 2.1.0-1`",
                "never loads the driver",
                f"the glibc {glibc} that",
                f"### Secure Boot on {distribution}",
                "mokutil --sb-state",
                "sudo mokutil --import /var/lib/dkms/mok.pub",
                "MokManager",
                "modinfo -F signer hid_hyperflux_next",
            ):
                self.assertIn(expected, section)
            for forbidden in ("update-secureboot-policy", "shim-signed", "hyperflux-next-full"):
                self.assertNotIn(forbidden, section)
        self.assertNotIn("dkms.service", fedora)
        self.assertIn("sudo systemctl enable dkms.service", opensuse)
        self.assertLess(
            opensuse.index("dkms.service"), opensuse.index("### Secure Boot on openSUSE")
        )


@unittest.skipUnless(rpm_tools_available(), "rpmbuild, rpm, rpm2cpio and rpmkeys are required")
class RpmBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.runtime = load_linux_runtime(ROOT)
        cls._temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._temporary.name)
        cls.built = cls._build(cls.workspace / "first", "fedora")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @classmethod
    def _build(cls, workspace: Path, target_id: str) -> tuple:
        workspace.mkdir()
        target = cls.catalog.targets[target_id]
        stages = synthetic_edition_stages(cls.split, workspace)
        del stages["full"]
        split_root = workspace / "split"
        split_root.mkdir()
        split_stages = split_edition_stages(
            cls.split, stages, split_root, EPOCH, target_outputs(cls.split, target)
        )
        packages = workspace / "packages"
        packages.mkdir()
        build_root = workspace / "build"
        build_root.mkdir()
        return build_rpm_packages(
            repository_root=ROOT,
            workspace_root=build_root,
            packages=packages,
            split=cls.split,
            target=target,
            runtime=cls.runtime,
            catalog=cls.catalog,
            epoch=EPOCH,
            stages=split_stages,
        )

    def _query(self, package: Path, *arguments: str) -> str:
        return subprocess.run(
            ["rpm", "-qp", "--nosignature", *arguments, str(package)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout

    def test_rpmbuild_builds_verified_reproducible_packages(self) -> None:
        again = self._build(self.workspace / "second", "fedora")
        self.assertEqual(
            [package.name for _, _, package in self.built],
            ["hyperflux-next-dkms-2.1.0-1.noarch.rpm", "hyperflux-next-linux-2.1.0-1.x86_64.rpm"],
        )
        for (plan, _, package), (_, _, repeated) in zip(self.built, again, strict=True):
            self.assertEqual(package.read_bytes(), repeated.read_bytes(), plan.package_name)
            requires = self._query(package, "--requires")
            for item in plan.depends:
                self.assertIn(item + "\n", requires)
        core = self.built[1][2]
        self.assertEqual(
            self._query(core, "--configfiles"), "/etc/hyperflux-next/bridge.json\n"
        )
        self.assertNotIn("dkms install", self._query(self.built[0][2], "--scripts"))

    def test_opensuse_builds_with_its_own_role_packages(self) -> None:
        built = self._build(self.workspace / "opensuse", "opensuse")
        requires = self._query(built[1][2], "--requires")
        self.assertIn("libgcc_s1\n", requires)
        self.assertIn("udev\n", requires)
        self.assertNotIn("systemd-udev", requires)

    def _verify(self, index: int, **changes) -> None:
        plan, stage, package = self.built[index]
        package = changes.pop("package", package)
        output = self.split.output(plan.output)
        arguments = {
            "plan": plan,
            "output": output,
            "stage": stage,
            "entries": file_entries(plan, self.workspace / "first" / "build" / f"rpm-{plan.output}"),
            "epoch": EPOCH,
            "forbidden": (b"/hyperflux-private-build/",),
            "workspace": self.workspace / "first" / "build",
        }
        arguments.update(changes)
        verify_rpm_package(package, **arguments)

    def test_verification_mutations_fail_closed(self) -> None:
        self._verify(0)
        self._verify(1)
        dkms, dkms_stage, _ = self.built[0]
        core, core_stage, core_package = self.built[1]
        core_root = self.workspace / "first" / "build" / "rpm-core"
        entries = file_entries(core, core_root)
        changed_mode = tuple(
            (kind, path, 0o600 if path.endswith("bridge.json") else mode)
            for kind, path, mode in entries
        )
        tampered = self.workspace / "tampered.rpm"
        content = bytearray(core_package.read_bytes())
        content[-64] ^= 0xFF
        tampered.write_bytes(bytes(content))
        output = self.split.output("core")
        cases = {
            "extra requirement": (1, {"plan": dataclasses.replace(core, depends=(*core.depends, "openrgb"))}),
            "missing scriptlet requirement": (0, {"plan": dataclasses.replace(dkms, scriptlet_requires=(("post", "dkms"),))}),
            "other scriptlet": (1, {"plan": dataclasses.replace(core, scripts=(("post", "exit 0"), *core.scripts[1:]))}),
            "missing configuration": (1, {"plan": dataclasses.replace(core, config_files=())}),
            "other conflict": (0, {"plan": dataclasses.replace(dkms, conflicts=())}),
            "extra provide": (0, {"plan": dataclasses.replace(dkms, provides=("hyperflux-dkms",))}),
            "other summary": (1, {"plan": dataclasses.replace(core, summary="Something else")}),
            "other architecture": (1, {"plan": dataclasses.replace(core, architecture="aarch64")}),
            "other epoch": (1, {"epoch": EPOCH + 1}),
            "file mode": (1, {"entries": changed_mode}),
            "missing file": (1, {"entries": entries[:-1]}),
            "payload digest": (1, {"stage": dataclasses.replace(core_stage, payload_sha256="0" * 64)}),
            "payload count": (0, {"stage": dataclasses.replace(dkms_stage, file_count=dkms_stage.file_count + 1)}),
            "directory mode": (1, {"output": dataclasses.replace(output, directory_modes=(("/etc/hyperflux-next", 0o755),))}),
            "private path": (1, {"forbidden": (b"hyperflux-next-bridge",)}),
            "corrupted package": (1, {"package": tampered}),
        }
        for name, (index, changes) in cases.items():
            with self.subTest(case=name), self.assertRaises(ModelError):
                self._verify(index, **changes)
        with self.assertRaisesRegex(ModelError, "DIGESTS NOT OK"):
            self._verify(1, package=tampered)

    def test_distro_command_writes_a_schema_shaped_manifest(self) -> None:
        for distribution, release in (("fedora", "44"), ("opensuse", "Tumbleweed")):
            with self.subTest(distribution=distribution):
                self._distro_command(distribution, release)

    def _distro_command(self, distribution: str, release: str) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "rpm-package-build.schema.json").read_text(encoding="utf-8")
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

            def stage(_root: Path, _manifest: Path, destination: Path, component: str):
                self.assertEqual(component, "linux")
                shutil.copytree(stages[component].root, destination)
                return stages[component].__class__(
                    root=destination,
                    inventory=stages[component].inventory,
                    distribution_component=component,
                    payload_sha256=stages[component].payload_sha256,
                    file_count=stages[component].file_count,
                )

            with (
                mock.patch.object(distribution_rpm, "load_artifact_set", return_value=artifacts),
                mock.patch.object(distribution_rpm, "stage_rootfs", side_effect=stage),
                mock.patch.object(distribution_rpm, "load_arch_split", return_value=self.split),
            ):
                result = build_distribution_package(
                    ROOT, build_manifest, distribution, workspace / "out"
                )
            self.assertEqual(result.distribution, distribution)
            self.assertEqual(result.architecture, "x86_64")
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest), set(schema["required"]))
            self.assertEqual(manifest["schema"], schema["properties"]["schema"]["const"])
            self.assertEqual(manifest["$schema"], schema["$id"])
            self.assertIn(manifest["distribution"], schema["properties"]["distribution"]["enum"])
            self.assertEqual(manifest["release"]["version"], release)
            self.assertRegex(
                manifest["release"]["version"],
                schema["properties"]["release"]["properties"]["version"]["pattern"],
            )
            self.assertEqual(manifest["maturity"], "ci-only")
            package_schema = schema["$defs"]["package"]
            for package in manifest["packages"]:
                self.assertEqual(set(package), set(package_schema["required"]))
                for key, rule in package_schema["properties"].items():
                    if "pattern" in rule:
                        self.assertRegex(package[key], rule["pattern"])
                for relation in package["requires"]:
                    self.assertRegex(
                        relation, package_schema["properties"]["requires"]["items"]["pattern"]
                    )
                path = result.manifest.parent / "packages" / package["file"]
                self.assertTrue(path.is_file())
            lint = result.manifest.parent / RPMLINT_CONFIG_NAME
            self.assertEqual(
                sorted(path.name for path in lint.iterdir()),
                sorted(name for name, _ in rpmlint_configs()),
            )
            for name, text in rpmlint_configs():
                self.assertEqual((lint / name).read_text(encoding="utf-8"), text)
            self.assertEqual(
                [package["file"] for package in manifest["packages"]],
                ["hyperflux-next-dkms-2.1.0-1.noarch.rpm", "hyperflux-next-linux-2.1.0-1.x86_64.rpm"],
            )


if __name__ == "__main__":
    unittest.main()
