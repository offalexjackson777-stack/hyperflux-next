# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.testgraph import TestCatalog, format_plan, load_test_catalog, select_tests


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

    def test_python_unit_budget_covers_concurrent_full_lane_execution(self) -> None:
        catalog = load_test_catalog(ROOT)
        python_unit = next(node for node in catalog.nodes if node.id == "python-unit")

        self.assertEqual(python_unit.expected_duration_seconds, 60)
        self.assertEqual(python_unit.timeout_seconds, 120)

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

    def test_openrgb_change_selects_dependencies_and_downstream_consumers(self) -> None:
        selection = select_tests(
            load_test_catalog(ROOT),
            "full-software",
            ("integrations/openrgb/src/plugin.cpp",),
        )
        selected = {node.id for node in selection.nodes}
        self.assertEqual(selection.mode, "changed-paths")
        self.assertLessEqual(
            {
                "foundation-contracts",
                "integration-contracts",
                "profile-contracts",
                "cpp-sdk-contracts",
                "openrgb-adapter-contracts",
                "openrgb-thread-sanitizer",
                "package-contracts",
            },
            selected,
        )

    def test_unknown_change_fails_closed_to_the_entire_lane(self) -> None:
        catalog = load_test_catalog(ROOT)
        selection = select_tests(catalog, "fast", ("new-domain/unknown.file",))
        expected = {node.id for node in catalog.nodes if "fast" in node.lanes}
        self.assertEqual(selection.mode, "changed-paths-fail-closed")
        self.assertEqual(selection.unmatched_paths, ("new-domain/unknown.file",))
        self.assertEqual({node.id for node in selection.nodes}, expected)

    def test_verifier_change_selects_the_entire_lane(self) -> None:
        catalog = load_test_catalog(ROOT)
        selection = select_tests(
            catalog,
            "fast",
            ("tools/hfxdev/verification_run.py",),
        )
        expected = {node.id for node in catalog.nodes if "fast" in node.lanes}
        self.assertEqual(selection.mode, "changed-paths-critical")
        self.assertEqual({node.id for node in selection.nodes}, expected)

    def test_changed_path_must_remain_inside_the_repository(self) -> None:
        with self.assertRaisesRegex(ModelError, "invalid changed repository path"):
            select_tests(load_test_catalog(ROOT), "fast", ("../outside",))
        with self.assertRaisesRegex(ModelError, "invalid changed repository path"):
            select_tests(load_test_catalog(ROOT), "fast", ("unsafe\npath",))


if __name__ == "__main__":
    unittest.main()
