# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.distributions import (
    COMPONENT_IDS,
    DEPENDENCY_ROLES,
    c_runtime_floor,
    debian_exact,
    debian_version,
    load_distribution_catalog,
)
from hfxdev.model import ModelError, load_json


class DistributionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = load_json(ROOT / "packaging" / "distributions.json")
        cls.catalog = load_distribution_catalog(ROOT)

    def test_arch_split_packages_match_the_bridge_only_contract(self) -> None:
        self.assertEqual(tuple(self.catalog.targets), ("arch", "ubuntu"))
        target = self.catalog.targets["arch"]
        self.assertEqual((target.package_format, target.maturity), ("pacman", "published"))
        self.assertIsNone(target.release)
        self.assertEqual(set(target.dependency_roles), DEPENDENCY_ROLES)
        self.assertEqual(tuple(target.components), COMPONENT_IDS)
        self.assertEqual(
            target.architecture_for("x86_64-unknown-linux-gnu"), "x86_64"
        )
        linux = target.component("linux")
        self.assertEqual(linux.conflicts, ("hyperflux-v2-linux-dkms",))
        self.assertEqual(linux.replaces, ())
        self.assertEqual(linux.provides, ())
        full = target.component("full")
        self.assertEqual(
            set(full.dependencies),
            {
                "dkms",
                "glibc",
                "libgcc",
                "systemd",
                "hyperflux-next-linux=2.1.0-1",
                "qt6-base",
                "hidapi",
                "libstdc++",
            },
        )
        self.assertEqual(
            {item.package for item in full.optional_dependencies},
            {"openrgb", "openrazer-daemon", "python-openrazer", "polychromatic"},
        )
        self.assertEqual(
            set(full.conflicts),
            {"hyperflux-next-openrgb=0.0.0_dev.1-2", "openrazer-driver-dkms"},
        )
        self.assertEqual(
            set(full.provides),
            {
                "openrazer-driver-dkms=3.12.4",
                "hyperflux-openrazer-driver-abi=3.12.4",
            },
        )
        self.assertEqual(set(full.replaces), set(full.conflicts))
        for component in target.components.values():
            relations = {
                *component.conflicts,
                *component.provides,
                *component.replaces,
            }
            self.assertFalse(
                any(
                    relation.split("=", 1)[0]
                    in {"polychromatic", "openrazer-daemon", "python-openrazer", "openrgb"}
                    for relation in relations
                )
            )

    def test_ubuntu_target_carries_only_core_with_debian_relations(self) -> None:
        target = self.catalog.targets["ubuntu"]
        self.assertEqual((target.package_format, target.maturity), ("deb", "ci-only"))
        self.assertFalse(target.published)
        self.assertIsNotNone(target.release)
        assert target.release is not None
        self.assertEqual(
            (target.release.version, target.release.codename), ("24.04", "noble")
        )
        self.assertEqual(target.architecture_for("x86_64-unknown-linux-gnu"), "amd64")
        with self.assertRaises(ModelError):
            target.architecture_for("aarch64-unknown-linux-gnu")
        self.assertEqual(tuple(target.components), ("linux",))
        with self.assertRaises(ModelError):
            target.component("full")
        self.assertEqual(
            target.dependency_roles,
            {
                "dkms": "dkms",
                "c-runtime": "libc6 (>= 2.34)",
                "compiler-runtime": "libgcc-s1",
                "service-manager": "systemd",
                "device-manager": "udev",
            },
        )
        self.assertEqual(c_runtime_floor(target), "2.34")
        linux = target.component("linux")
        arch_linux = self.catalog.targets["arch"].component("linux")
        self.assertEqual(linux.package_name, arch_linux.package_name)
        self.assertEqual(linux.conflicts, arch_linux.conflicts)
        self.assertEqual(
            linux.dependencies,
            ("dkms", "libc6 (>= 2.34)", "libgcc-s1", "systemd", "udev"),
        )

    def test_debian_versions_and_relations(self) -> None:
        self.assertEqual(debian_version("2.1.0", 1), "2.1.0-1")
        self.assertEqual(debian_version("2.1.0-rc.1", 2), "2.1.0~rc.1-2")
        for version, release in (("v2", 1), ("2.1.0", 0), ("2.1.0_1", 1)):
            with self.subTest(version=version), self.assertRaises(ModelError):
                debian_version(version, release)
        self.assertEqual(
            debian_exact("hyperflux-next-dkms", "2.1.0-1"),
            "hyperflux-next-dkms (= 2.1.0-1)",
        )
        with self.assertRaises(ModelError):
            debian_exact("HyperFlux", "2.1.0-1")

    def test_ubuntu_mutations_fail_closed(self) -> None:
        def mutate(change):
            candidate = copy.deepcopy(self.value)
            change(candidate["targets"]["ubuntu"], candidate)
            return candidate

        def with_full(target, _value):
            target["components"]["full"] = copy.deepcopy(
                self.value["targets"]["arch"]["components"]["full"]
            )

        def set_role(role, package):
            def change(target, _value):
                target["dependency_roles"][role] = package
            return change

        def set_field(*path_and_value):
            *path, final = path_and_value

            def change(target, _value):
                current = target
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = final
            return change

        def drop_ubuntu(_target, value):
            del value["targets"]["ubuntu"]

        def arch_not_published(_target, value):
            value["targets"]["arch"]["maturity"] = "ci-only"

        def arch_as_deb(_target, value):
            value["targets"]["arch"]["package_format"] = "deb"

        def reorder(_target, value):
            value["targets"] = {
                "ubuntu": value["targets"]["ubuntu"],
                "arch": value["targets"]["arch"],
            }

        def drop_release(target, _value):
            del target["release"]

        def arch_style_relation(target, _value):
            target["components"]["linux"]["conflicts"] = ["hyperflux-v2-linux-dkms=2.0.0-1"]

        def extra_dependency(target, _value):
            target["components"]["linux"]["dependencies"] = ["openrgb"]

        def extra_conflict(target, _value):
            target["components"]["linux"]["conflicts"].append("openrazer-driver-dkms")

        cases = {
            "full edition": with_full,
            "missing target": drop_ubuntu,
            "reordered targets": reorder,
            "missing release": drop_release,
            "arch maturity": arch_not_published,
            "arch format": arch_as_deb,
            "published": set_field("maturity", "published"),
            "pacman format": set_field("package_format", "pacman"),
            "unknown maturity": set_field("maturity", "nightly"),
            "other release": set_field("release", "codename", "jammy"),
            "runtime above release": set_role("c-runtime", "libc6 (>= 2.40)"),
            "runtime without floor": set_role("c-runtime", "libc6"),
            "arch runtime name": set_role("c-runtime", "glibc (>= 2.34)"),
            "arch relation syntax": set_role("c-runtime", "libc6>=2.34"),
            "arch udev provider": set_role("device-manager", "systemd"),
            "second architecture": set_field("architectures", {
                "x86_64-unknown-linux-gnu": "amd64",
                "aarch64-unknown-linux-gnu": "arm64",
            }),
            "arch relation": arch_style_relation,
            "extra dependency": extra_dependency,
            "extra conflict": extra_conflict,
            "renamed core": set_field("components", "linux", "package_name", "hyperflux-core"),
        }
        for name, change in cases.items():
            with self.subTest(case=name), patch(
                "hfxdev.distributions.load_json", return_value=mutate(change)
            ):
                with self.assertRaises(ModelError):
                    load_distribution_catalog(ROOT)

    def test_catalog_mutations_fail_closed(self) -> None:
        cases = []
        extra_target = copy.deepcopy(self.value)
        extra_target["targets"]["rpm"] = copy.deepcopy(
            extra_target["targets"]["arch"]
        )
        cases.append(extra_target)

        reordered = copy.deepcopy(self.value)
        components = reordered["targets"]["arch"]["components"]
        reordered["targets"]["arch"]["components"] = {
            "full": components["full"],
            "linux": components["linux"],
        }
        cases.append(reordered)

        takeover = copy.deepcopy(self.value)
        takeover["targets"]["arch"]["components"]["linux"]["replaces"].append(
            "polychromatic"
        )
        cases.append(takeover)

        application_hard_dependency = copy.deepcopy(self.value)
        application_hard_dependency["targets"]["arch"]["components"]["full"][
            "dependencies"
        ][1] = "openrgb"
        cases.append(application_hard_dependency)

        duplicate = copy.deepcopy(self.value)
        duplicate["targets"]["arch"]["components"]["full"][
            "optional_dependencies"
        ].append(copy.deepcopy(duplicate["targets"]["arch"]["components"]["full"]["optional_dependencies"][0]))
        cases.append(duplicate)

        for index, candidate in enumerate(cases):
            with self.subTest(index=index), patch(
                "hfxdev.distributions.load_json", return_value=candidate
            ):
                with self.assertRaises(ModelError):
                    load_distribution_catalog(ROOT)


if __name__ == "__main__":
    unittest.main()
