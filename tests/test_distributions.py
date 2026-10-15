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
    load_distribution_catalog,
)
from hfxdev.model import ModelError, load_json


class DistributionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = load_json(ROOT / "packaging" / "distributions.json")
        cls.catalog = load_distribution_catalog(ROOT)

    def test_arch_split_packages_match_the_bridge_only_contract(self) -> None:
        self.assertEqual(set(self.catalog.targets), {"arch"})
        target = self.catalog.targets["arch"]
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
                "hyperflux-next-linux=2.0.2-1",
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
