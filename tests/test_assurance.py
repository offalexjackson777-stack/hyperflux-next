# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.formal_model import load_formal_model, run_formal_model
from hfxdev.generators.supply_chain import spdx_json
from hfxdev.model import ModelError
from hfxdev.performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
    verify_static_performance_budgets,
)
from hfxdev.release import load_release_gates
from hfxdev.supply_chain import load_dependency_inventory


class AssuranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = load_dependency_inventory(ROOT)
        cls.metrics = load_performance_budgets(ROOT)

    def test_dependency_inventory_exactly_covers_locked_and_declared_sources(self) -> None:
        self.assertEqual(len(self.inventory.rust_packages), 29)
        self.assertEqual(len(self.inventory.python_projects), 3)
        self.assertEqual(len(self.inventory.python_packages), 3)
        self.assertEqual(len(self.inventory.vendored_packages), 1)
        self.assertEqual(
            {package.id for package in self.inventory.upstream_packages},
            {"openrazer", "openrgb", "polychromatic"},
        )

    def test_spdx_inventory_is_deterministic_unique_and_privacy_safe(self) -> None:
        first = spdx_json(self.inventory)
        self.assertEqual(first, spdx_json(load_dependency_inventory(ROOT)))
        document = json.loads(first)
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        ids = [package["SPDXID"] for package in document["packages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("/home/", first)
        self.assertTrue(
            document["documentNamespace"].endswith(self.inventory.authority_sha256)
        )

    def test_release_gates_match_constitution_and_keep_publication_locked(self) -> None:
        gates = load_release_gates(ROOT)
        self.assertEqual(len(gates), 10)
        self.assertEqual(gates[-1].id, "HFX-GATE-PUBLICATION-DECISION")
        self.assertEqual(gates[-1].status, "publication-locked")
        self.assertTrue(gates[-1].publication_authorization_required)

    def test_static_and_package_performance_budgets_fail_closed(self) -> None:
        static = verify_static_performance_budgets(ROOT, self.metrics)
        self.assertEqual(static["bridge-command-queue-capacity"], 128.0)
        artifact_metrics = tuple(
            metric
            for metric in self.metrics
            if metric.measurement_kind in {"artifact-size", "staged-payload-size"}
        )
        artifact_sizes = {
            metric.selector: int(metric.maximum)
            for metric in artifact_metrics
            if metric.measurement_kind == "artifact-size"
        }
        result = verify_package_performance_budgets(
            artifact_metrics,
            artifact_sizes,
            next(
                int(metric.maximum)
                for metric in artifact_metrics
                if metric.measurement_kind == "staged-payload-size"
            ),
        )
        self.assertIn("staged-payload-size", result)
        constrained = tuple(
            replace(metric, maximum=1.0)
            if metric.id == "bridge-binary-size"
            else metric
            for metric in artifact_metrics
        )
        with self.assertRaisesRegex(ModelError, "bridge-binary-size"):
            verify_package_performance_budgets(constrained, artifact_sizes, 1)

    def test_bounded_model_is_deterministic_and_covers_every_transition(self) -> None:
        model = load_formal_model(ROOT)
        first = run_formal_model(model)
        second = run_formal_model(load_formal_model(ROOT))
        self.assertEqual(first, second)
        self.assertEqual(set(first.transition_names), set(model.required_transitions))
        self.assertGreaterEqual(first.states, 80)
        self.assertLessEqual(first.states, model.maximum_states)


if __name__ == "__main__":
    unittest.main()
