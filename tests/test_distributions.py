# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.distributions import DEPENDENCY_ROLES, load_distribution_catalog
from hfxdev.model import ModelError, load_json


class DistributionCatalogTests(unittest.TestCase):
    def test_all_distribution_targets_share_required_roles(self) -> None:
        catalog = load_distribution_catalog(ROOT)
        self.assertEqual(set(catalog.targets), {"arch", "debian", "rpm"})
        for target in catalog.targets.values():
            self.assertEqual(set(target.dependency_roles), DEPENDENCY_ROLES)
            self.assertEqual(
                target.architecture_for("x86_64-unknown-linux-gnu"),
                "amd64" if target.id == "debian" else "x86_64",
            )
            path = target.python_discovery_for("3.14.6")
            self.assertTrue(path.startswith("/usr/"))
            self.assertTrue(path.endswith("hyperflux-next.pth"))
            self.assertNotIn("@", path)
            dependencies = target.dependencies_for("3.14.6")
            if "@python_major_minor@" in target.python_discovery_path:
                python_package = target.dependency_roles["python"]
                self.assertIn(f"{python_package}>=3.14", dependencies)
                self.assertIn(f"{python_package}<3.15", dependencies)

    def test_duplicate_optional_packages_and_unknown_targets_fail_closed(self) -> None:
        value = load_json(ROOT / "packaging" / "distributions.json")
        cases = []
        duplicate = copy.deepcopy(value)
        duplicate["targets"]["arch"]["optional_dependencies"].append(
            copy.deepcopy(duplicate["targets"]["arch"]["optional_dependencies"][0])
        )
        cases.append(duplicate)
        extra = copy.deepcopy(value)
        extra["targets"]["other"] = copy.deepcopy(extra["targets"]["arch"])
        cases.append(extra)
        escaped = copy.deepcopy(value)
        escaped["targets"]["rpm"]["python_discovery_path"] = (
            "/usr/lib/../escape/@python_major_minor@/bad.pth"
        )
        cases.append(escaped)
        control_character = copy.deepcopy(value)
        control_character["targets"]["debian"]["optional_dependencies"][0][
            "purpose"
        ] = "native adapter\nmalformed metadata"
        cases.append(control_character)

        for index, candidate in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "packaging").mkdir()
                (root / "runtime").mkdir()
                (root / "schemas").mkdir()
                (root / "uapi").mkdir()
                (root / "driver/kernel/uapi").mkdir(parents=True)
                shutil.copy(ROOT / "runtime/linux.json", root / "runtime/linux.json")
                shutil.copy(ROOT / "uapi/kernel-uapi.json", root / "uapi/kernel-uapi.json")
                (root / "packaging/distributions.json").write_text(
                    json.dumps(candidate), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_distribution_catalog(root)


if __name__ == "__main__":
    unittest.main()
