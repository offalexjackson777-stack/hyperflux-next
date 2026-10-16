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
    rpm_exact,
    rpm_version,
)
from hfxdev.model import ModelError, load_json


class DistributionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = load_json(ROOT / "packaging" / "distributions.json")
        cls.catalog = load_distribution_catalog(ROOT)

    def test_arch_split_packages_match_the_bridge_only_contract(self) -> None:
        self.assertEqual(
            tuple(self.catalog.targets), ("arch", "ubuntu", "debian", "fedora", "opensuse")
        )
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

    def test_debian_target_mirrors_ubuntu_on_its_own_release(self) -> None:
        ubuntu = self.catalog.targets["ubuntu"]
        target = self.catalog.targets["debian"]
        self.assertEqual((target.package_format, target.maturity), ("deb", "ci-only"))
        self.assertFalse(target.published)
        assert target.release is not None
        self.assertEqual(
            (
                target.release.distribution,
                target.release.version,
                target.release.codename,
                target.release.c_runtime_version,
            ),
            ("Debian", "13", "trixie", "2.41"),
        )
        self.assertEqual(tuple(target.components), ("linux",))
        self.assertEqual(target.dependency_roles, ubuntu.dependency_roles)
        self.assertEqual(target.component("linux"), ubuntu.component("linux"))

    def test_debian_mutations_fail_closed(self) -> None:
        def mutate(change):
            candidate = copy.deepcopy(self.value)
            change(candidate["targets"]["debian"], candidate)
            return candidate

        def set_release(key, value):
            def change(target, _value):
                target["release"][key] = value
            return change

        def drop_debian(_target, value):
            del value["targets"]["debian"]

        def reorder(_target, value):
            targets = value["targets"]
            value["targets"] = {
                "arch": targets["arch"],
                "debian": targets["debian"],
                "ubuntu": targets["ubuntu"],
            }

        def runtime_above_release(target, _value):
            target["dependency_roles"]["c-runtime"] = "libc6 (>= 2.42)"

        def ubuntu_release(target, value):
            target["release"] = copy.deepcopy(value["targets"]["ubuntu"]["release"])

        cases = {
            "missing target": drop_debian,
            "reordered targets": reorder,
            "other codename": set_release("codename", "bookworm"),
            "other version": set_release("version", "12"),
            "other distribution": set_release("distribution", "Devuan"),
            "runtime above release": runtime_above_release,
            "ubuntu release": ubuntu_release,
        }
        for name, change in cases.items():
            with self.subTest(case=name), patch(
                "hfxdev.distributions.load_json", return_value=mutate(change)
            ):
                with self.assertRaises(ModelError):
                    load_distribution_catalog(ROOT)

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

    def test_rpm_targets_carry_core_with_their_own_role_packages(self) -> None:
        arch_linux = self.catalog.targets["arch"].component("linux")
        expected = {
            "fedora": (
                ("Fedora", "44", "2.43"),
                ("dkms", "glibc >= 2.34", "libgcc", "systemd", "systemd-udev"),
            ),
            "opensuse": (
                ("openSUSE", "Tumbleweed", "2.44"),
                ("dkms", "glibc >= 2.34", "libgcc_s1", "systemd", "udev"),
            ),
        }
        for target_id, (release, dependencies) in expected.items():
            with self.subTest(target=target_id):
                target = self.catalog.targets[target_id]
                self.assertEqual((target.package_format, target.maturity), ("rpm", "ci-only"))
                self.assertFalse(target.published)
                assert target.release is not None
                self.assertEqual(
                    (
                        target.release.distribution,
                        target.release.version,
                        target.release.c_runtime_version,
                    ),
                    release,
                )
                self.assertIsNone(target.release.codename)
                self.assertEqual(target.architecture_for("x86_64-unknown-linux-gnu"), "x86_64")
                self.assertEqual(tuple(target.components), ("linux",))
                self.assertEqual(c_runtime_floor(target), "2.34")
                linux = target.component("linux")
                self.assertEqual(linux.package_name, arch_linux.package_name)
                self.assertEqual(linux.conflicts, arch_linux.conflicts)
                self.assertEqual(linux.dependencies, dependencies)

    def test_rpm_mutations_fail_closed(self) -> None:
        def mutate(target_id, change):
            candidate = copy.deepcopy(self.value)
            change(candidate["targets"][target_id], candidate)
            return candidate

        def set_release(key, value):
            def change(target, _value):
                target["release"][key] = value
            return change

        def set_role(role, package):
            def change(target, _value):
                target["dependency_roles"][role] = package
            return change

        def with_codename(target, _value):
            target["release"]["codename"] = "rawhide"

        def with_full(target, _value):
            target["components"]["full"] = copy.deepcopy(
                self.value["targets"]["arch"]["components"]["full"]
            )

        def drop_target(target_id):
            def change(_target, value):
                del value["targets"][target_id]
            return change

        def reorder(_target, value):
            targets = value["targets"]
            value["targets"] = {
                key: targets[key] for key in ("arch", "ubuntu", "debian", "opensuse", "fedora")
            }

        def as_deb(target, _value):
            target["package_format"] = "deb"

        def debian_conflict(target, _value):
            target["components"]["linux"]["conflicts"] = ["hyperflux-v2-linux-dkms (<< 2.0)"]

        def arch_conflict(target, _value):
            target["components"]["linux"]["conflicts"] = ["hyperflux-v2-linux-dkms<2.0"]

        def extra_dependency(target, _value):
            target["components"]["linux"]["dependencies"] = ["openrgb"]

        def published(target, _value):
            target["maturity"] = "published"

        def swap_roles(target, value):
            other = "opensuse" if value["targets"]["fedora"] is target else "fedora"
            target["dependency_roles"] = copy.deepcopy(value["targets"][other]["dependency_roles"])

        cases = {
            "missing target": lambda target_id: drop_target(target_id),
            "reordered targets": lambda _id: reorder,
            "deb format": lambda _id: as_deb,
            "published": lambda _id: published,
            "codename": lambda _id: with_codename,
            "full edition": lambda _id: with_full,
            "other version": lambda _id: set_release("version", "43"),
            "other distribution": lambda _id: set_release("distribution", "CentOS"),
            "runtime above release": lambda _id: set_role("c-runtime", "glibc >= 2.45"),
            "runtime without floor": lambda _id: set_role("c-runtime", "glibc"),
            "debian runtime name": lambda _id: set_role("c-runtime", "libc6 >= 2.34"),
            "debian relation syntax": lambda _id: set_role("c-runtime", "glibc (>= 2.34)"),
            "arch relation syntax": lambda _id: set_role("c-runtime", "glibc>=2.34"),
            "debian dkms provider": lambda _id: set_role("dkms", "dkms-debian"),
            "other distribution roles": lambda _id: swap_roles,
            "debian relation": lambda _id: debian_conflict,
            "arch relation": lambda _id: arch_conflict,
            "extra dependency": lambda _id: extra_dependency,
        }

        for target_id in ("fedora", "opensuse"):
            for name, factory in cases.items():
                with self.subTest(target=target_id, case=name), patch(
                    "hfxdev.distributions.load_json",
                    return_value=mutate(target_id, factory(target_id)),
                ):
                    with self.assertRaises(ModelError):
                        load_distribution_catalog(ROOT)

    def test_rpm_versions_and_relations(self) -> None:
        self.assertEqual(rpm_version("2.1.0", 1), "2.1.0-1")
        self.assertEqual(rpm_version("2.1.0-rc.1", 2), "2.1.0~rc.1-2")
        for version, release in (("v2", 1), ("2.1.0", 0), ("2.1.0 1", 1)):
            with self.subTest(version=version), self.assertRaises(ModelError):
                rpm_version(version, release)
        self.assertEqual(
            rpm_exact("hyperflux-next-dkms", "2.1.0-1"), "hyperflux-next-dkms = 2.1.0-1"
        )
        with self.assertRaises(ModelError):
            rpm_exact("hyperflux next", "2.1.0-1")

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
