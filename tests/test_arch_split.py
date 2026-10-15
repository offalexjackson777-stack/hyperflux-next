# SPDX-License-Identifier: GPL-2.0-only

"""PKG-002 and PKG-003: one four-output topology with honest relations."""

from __future__ import annotations

from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arch_split_support import (  # noqa: E402
    EPOCH,
    ROOT,
    candidate_split,
    makepkg_available,
    mutated_authority,
    synthetic_edition_stages,
)
from hfxdev.arch_split import load_arch_split, relation_name  # noqa: E402
from hfxdev.arch_split_package import (  # noqa: E402
    build_split_packages,
    output_contract,
    product_fields,
    render_repository_pkgbuild,
    split_edition_stages,
)
from hfxdev.distribution_package import _arch_metadata  # noqa: E402
from hfxdev.distributions import load_distribution_catalog  # noqa: E402
from hfxdev.install import load_install_manifest  # noqa: E402
from hfxdev.linux_runtime import load_linux_runtime  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402


PACKAGES = (
    "hyperflux-next-dkms",
    "hyperflux-next-linux",
    "hyperflux-next-openrazer-driver-dkms",
    "hyperflux-next-full",
)


class ArchSplitTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()
        cls.catalog = load_distribution_catalog(ROOT)
        cls.install = load_install_manifest(ROOT)

    def test_current_release_keeps_the_two_package_topology(self) -> None:
        runtime = load_linux_runtime(ROOT)
        current = load_arch_split(ROOT)
        self.assertEqual(
            current.full_version,
            f"{runtime.product.version}-{runtime.product.package_release}",
        )
        self.assertFalse(current.active, "2.0.1-1 must stay on two packages (PKG-001)")
        self.assertTrue(self.split.active)
        self.assertEqual(self.split.first_release, "2.1.0-1")

    def test_four_outputs_with_the_edition_products_as_requests(self) -> None:
        self.assertEqual(tuple(item.package_name for item in self.split.outputs), PACKAGES)
        target = self.catalog.targets["arch"]
        for edition in ("linux", "full"):
            primary = self.split.edition_output(edition)
            self.assertEqual(primary.package_name, target.component(edition).package_name)
            self.assertEqual(primary.description, target.component(edition).description)

    def test_outputs_partition_each_edition_exactly(self) -> None:
        for edition in ("linux", "full"):
            builds = [
                build
                for output in self.split.outputs
                if output.edition == edition
                for build in output.builds
            ]
            self.assertCountEqual(
                builds, [build.id for build in self.install.builds_for(edition)]
            )
            files = sorted(
                path
                for output in self.split.outputs
                if output.edition == edition
                for path in output.file_destinations
            )
            expected = sorted(str(item.destination) for item in self.install.files_for(edition))
            expected += [
                build.destination
                for build in self.install.builds_for(edition)
                if build.kind != "source-transform"
            ]
            self.assertEqual(files, sorted(expected))

    def test_every_installed_path_has_one_owner(self) -> None:
        for output in self.split.outputs:
            for path in output.file_destinations:
                self.assertEqual(self.split.owner_of(path), output.id)
            for tree in output.tree_destinations:
                self.assertEqual(self.split.owner_of(tree + "/dkms.conf"), output.id)
        self.assertEqual(
            self.split.owner_of("/usr/src/openrazer-driver-3.12.4/driver/razerkbd_driver.c"),
            "openrazer-driver",
        )
        with self.assertRaisesRegex(ModelError, "no output owns"):
            self.split.owner_of("/usr/lib/unowned")

    def test_sibling_dependencies_are_exact_versions(self) -> None:
        version = self.split.full_version
        self.assertEqual(version, "2.1.0-1")
        siblings = {
            output.package_name: [
                item for item in output.dependencies if relation_name(item) in PACKAGES
            ]
            for output in self.split.outputs
        }
        self.assertEqual(
            siblings,
            {
                "hyperflux-next-dkms": [],
                "hyperflux-next-linux": [f"hyperflux-next-dkms={version}"],
                "hyperflux-next-openrazer-driver-dkms": [f"hyperflux-next-dkms={version}"],
                "hyperflux-next-full": [
                    f"hyperflux-next-linux={version}",
                    f"hyperflux-next-openrazer-driver-dkms={version}",
                ],
            },
        )

    def test_install_sets_close_over_the_edition(self) -> None:
        self.assertEqual(
            self.split.install_set("linux"), ("hyperflux-next-dkms", "hyperflux-next-linux")
        )
        self.assertEqual(self.split.install_set("full"), PACKAGES)

    def test_external_dependencies_match_the_edition_contracts(self) -> None:
        target = self.catalog.targets["arch"]
        for edition in ("linux", "full"):
            external = {
                item
                for output in self.split.outputs
                if output.edition == edition
                for item in output.dependencies
                if relation_name(item) not in PACKAGES
            }
            expected = {
                item
                for item in target.component(edition).dependencies
                if relation_name(item) not in PACKAGES
            }
            self.assertEqual(external, expected)

    def test_core_preserves_configuration_and_its_directory_mode(self) -> None:
        core = self.split.output("core")
        self.assertEqual(core.backup, ("etc/hyperflux-next/bridge.json",))
        runtime = load_linux_runtime(ROOT)
        self.assertEqual(
            core.directory_modes,
            (
                (
                    runtime.bridge.configuration_directory,
                    runtime.bridge.directory_modes.configuration,
                ),
            ),
        )
        for output in self.split.outputs:
            if output.id != "core":
                self.assertEqual(output.backup, ())


class ArchSplitRelationTests(unittest.TestCase):
    """PKG-003: stock OpenRazer is an alternative, never an obsolete package."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()

    def test_only_the_driver_output_relates_to_stock_openrazer(self) -> None:
        for output in self.split.outputs:
            names = {
                relation_name(item)
                for item in (*output.conflicts, *output.provides, *output.replaces)
            }
            if output.id == "openrazer-driver":
                self.assertIn("openrazer-driver-dkms", names)
            else:
                self.assertNotIn("openrazer-driver-dkms", names)

    def test_driver_conflicts_and_provides_but_never_replaces(self) -> None:
        driver = self.split.output("openrazer-driver")
        self.assertEqual(driver.conflicts, ("openrazer-driver-dkms",))
        self.assertEqual(
            driver.provides,
            (
                "openrazer-driver-dkms=3.12.4",
                "hyperflux-openrazer-driver-abi=3.12.4",
            ),
        )
        self.assertEqual(driver.replaces, ())
        for output in self.split.outputs:
            self.assertNotIn(
                "openrazer-driver-dkms", [relation_name(item) for item in output.replaces]
            )

    def test_only_the_retired_connector_relation_remains_on_full(self) -> None:
        full = self.split.output("full")
        self.assertEqual(full.replaces, ("hyperflux-next-openrgb=0.0.0_dev.1-2",))
        self.assertEqual(full.conflicts, ("hyperflux-next-openrgb=0.0.0_dev.1-2",))
        (record,) = self.split.retired_relations
        self.assertEqual(record.output, "full")
        self.assertEqual(record.relation, "hyperflux-next-openrgb=0.0.0_dev.1-2")
        self.assertTrue(record.reason)
        self.assertEqual(record.expires_before_release, "2.2.0-1")

    def test_retired_relation_expires(self) -> None:
        with self.assertRaisesRegex(ModelError, "expired at 2.2.0-1"):
            load_arch_split(ROOT, release=("2.2.0", 1))
        load_arch_split(ROOT, release=("2.1.9", 3))

    def test_mutations_fail_closed(self) -> None:
        def output(value: dict, output_id: str) -> dict:
            return next(item for item in value["outputs"] if item["id"] == output_id)

        cases = {
            "replace stock driver": (
                lambda value: output(value, "openrazer-driver")["replaces"].append(
                    "openrazer-driver-dkms"
                ),
                "may not be replaced",
            ),
            "full relates to stock driver": (
                lambda value: output(value, "full")["conflicts"].append(
                    "openrazer-driver-dkms"
                ),
                "only the transformed-driver output",
            ),
            "application relation": (
                lambda value: output(value, "full")["conflicts"].append("polychromatic"),
                "application relations are forbidden",
            ),
            "unversioned provide": (
                lambda value: output(value, "openrazer-driver")["provides"].append(
                    "openrazer-driver"
                ),
                "exact transformed source version",
            ),
            "unrecorded replace": (
                lambda value: output(value, "full")["replaces"].append("hyperflux-legacy=1-1"),
                "has no retired-relation record",
            ),
            "broad retired replace": (
                lambda value: (
                    output(value, "full")["replaces"].__setitem__(0, "hyperflux-next-openrgb"),
                    value["retired_relations"][0].__setitem__(
                        "relation", "hyperflux-next-openrgb"
                    ),
                ),
                "must name one exact",
            ),
            "dangling retired record": (
                lambda value: output(value, "full")["replaces"].clear(),
                "no matching replace",
            ),
            "build in two outputs": (
                lambda value: output(value, "full")["builds"].append(
                    "openrazer-native-driver"
                ),
                "full build assignment",
            ),
            "payload dropped": (
                lambda value: output(value, "core")["payloads"].remove("system-users"),
                "missing \\['system-users'\\]",
            ),
            "core depends on full": (
                lambda value: output(value, "core")["sibling_dependencies"].append(
                    "openrazer-driver"
                ),
                "Core outputs may not depend on Full outputs",
            ),
            "full request misses driver": (
                lambda value: output(value, "full")["sibling_dependencies"].remove(
                    "openrazer-driver"
                ),
                "Full request must resolve every output",
            ),
            "renamed edition product": (
                lambda value: output(value, "full").__setitem__(
                    "package_name", "hyperflux-next-everything"
                ),
                "unchanged hyperflux-next-full",
            ),
            "unknown field": (
                lambda value: output(value, "core").__setitem__("install", "x.install"),
                "unknown fields install",
            ),
            "external dependency drift": (
                lambda value: output(value, "full")["dependencies"].remove("hidapi"),
                "external dependencies differ",
            ),
            "optional applications on a sibling": (
                lambda value: output(value, "receiver-dkms").__setitem__(
                    "optional_dependencies", "edition"
                ),
                "only the edition request",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label), mutated_authority(mutate):
                with self.assertRaisesRegex(ModelError, message):
                    candidate_split()


class ArchSplitRepositoryBuildTests(unittest.TestCase):
    """The signed repository builds the same four outputs without install scripts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.split = candidate_split()

    def test_repository_recipe_has_four_functions_and_no_install_scripts(self) -> None:
        digests = {output.id: f"{index:064x}" for index, output in enumerate(self.split.outputs)}
        recipe = render_repository_pkgbuild(self.split, "x86_64", digests)
        self.assertIn(
            "pkgname=('hyperflux-next-dkms' 'hyperflux-next-linux' "
            "'hyperflux-next-openrazer-driver-dkms' 'hyperflux-next-full')",
            recipe,
        )
        self.assertIn("pkgver=2.1.0\n", recipe)
        self.assertIn("pkgrel=1\n", recipe)
        self.assertNotIn("install=", recipe)
        for output in self.split.outputs:
            self.assertIn(f"package_{output.package_name}() {{", recipe)
        self.assertIn("  replaces=()\n  backup=()\n  bsdtar -xf \"$srcdir/payload-openrazer-driver.tar\"", recipe)

    def test_split_stages_preserve_every_edition_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            stages = synthetic_edition_stages(self.split, workspace)
            split_root = workspace / "split"
            split_root.mkdir()
            result = split_edition_stages(self.split, stages, split_root, EPOCH)
            self.assertEqual(
                sum(item.file_count for item in result.values()),
                sum(item.file_count for item in stages.values()),
            )
            core = result["core"].root / "etc" / "hyperflux-next"
            self.assertEqual(core.stat().st_mode & 0o7777, self.split.output("core").directory_modes[0][1])
            self.assertFalse((result["receiver-dkms"].root / "etc").exists())

    def test_misplaced_stage_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            stages = synthetic_edition_stages(self.split, workspace)
            stray = stages["linux"].root / "usr" / "src" / "openrazer-driver-3.12.4" / "x.c"
            stray.parent.mkdir(parents=True)
            stray.write_text("x\n")
            split_root = workspace / "split"
            split_root.mkdir()
            with self.assertRaisesRegex(ModelError, "staged by linux but belongs to"):
                split_edition_stages(self.split, stages, split_root, EPOCH)

    @unittest.skipUnless(makepkg_available(), "makepkg, fakeroot, bsdtar, and zstd are required")
    def test_makepkg_builds_four_verified_outputs_without_install_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            stages = synthetic_edition_stages(self.split, workspace)
            packages = workspace / "packages"
            packages.mkdir()
            build_root = workspace / "build"
            build_root.mkdir()
            artifacts = build_split_packages(
                repository_root=ROOT,
                workspace_root=build_root,
                packages=packages,
                split=self.split,
                architecture="x86_64",
                epoch=EPOCH,
                stages=stages,
            )
            self.assertEqual(tuple(item.package_name for item in artifacts), PACKAGES)
            for artifact in artifacts:
                output = self.split.by_package(artifact.package_name)
                with tarfile.open(artifact.package, "r:*") as archive:
                    names = archive.getnames()
                    self.assertNotIn(".INSTALL", names)
                    info = archive.extractfile(".PKGINFO")
                    assert info is not None
                    metadata = _arch_metadata(info.read(), "PKGINFO")
                self.assertEqual(
                    product_fields(metadata), output_contract(self.split, output, "x86_64")
                )

    def test_inactive_split_refuses_four_output_build(self) -> None:
        with self.assertRaisesRegex(ModelError, "keeps the two-package release"):
            build_split_packages(
                repository_root=ROOT,
                workspace_root=Path("/nonexistent"),
                packages=Path("/nonexistent"),
                split=load_arch_split(ROOT),
                architecture="x86_64",
                epoch=EPOCH,
                stages={},
            )


if __name__ == "__main__":
    unittest.main()
