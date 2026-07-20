# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.testgraph import TestCatalog, format_plan, load_test_catalog


class TestGraphTests(unittest.TestCase):
    def test_catalog_is_dependency_ordered_and_software_only(self) -> None:
        catalog = load_test_catalog(ROOT)
        seen: set[str] = set()
        for node in catalog.ordered():
            with self.subTest(test=node.id):
                self.assertLessEqual(set(node.dependencies), seen)
                self.assertEqual(node.hardware_requirement, "none")
                self.assertFalse(node.writes_hardware)
                seen.add(node.id)

    def test_catalog_uses_trusted_runner_identifiers(self) -> None:
        catalog = load_test_catalog(ROOT)
        for node in catalog.nodes:
            with self.subTest(test=node.id):
                self.assertNotIn(" ", node.runner)
                self.assertNotIn("/", node.runner)
                self.assertNotIn(";", node.runner)

    def test_cycle_is_rejected(self) -> None:
        catalog = load_test_catalog(ROOT)
        first, second, *remaining = catalog.nodes
        cyclic = TestCatalog(
            nodes=(
                replace(first, dependencies=(second.id,)),
                replace(second, dependencies=(first.id,)),
                *remaining,
            )
        )
        with self.assertRaisesRegex(ModelError, "dependency cycle"):
            cyclic.ordered()

    def test_plan_explains_hardware_and_dependencies(self) -> None:
        plan = format_plan(load_test_catalog(ROOT))
        self.assertIn("zero hardware writes", plan)
        self.assertIn("depends=", plan)
        self.assertIn("simulator-contracts", plan)


if __name__ == "__main__":
    unittest.main()
