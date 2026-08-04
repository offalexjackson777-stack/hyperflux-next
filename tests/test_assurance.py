# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.formal_model import load_formal_model, run_formal_model
from hfxdev.generators.supply_chain import spdx_json
from hfxdev.model import ModelError, load_json, sha256_file
from hfxdev.performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
    verify_static_performance_budgets,
)
from hfxdev.protected_behavior import load_protected_behavior
from hfxdev.release import load_release_context, load_release_gates
from hfxdev.supply_chain import load_dependency_inventory


class AssuranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = load_dependency_inventory(ROOT)
        cls.metrics = load_performance_budgets(ROOT)

    def test_dependency_inventory_exactly_covers_locked_and_declared_sources(self) -> None:
        self.assertEqual(len(self.inventory.rust_packages), 29)
        self.assertEqual(
            self.inventory.rust_security.advisory_database_revision,
            "308808d74a1462ec8b09c1e76938471c53b55dcc",
        )
        self.assertEqual(self.inventory.rust_security.expected_advisory_count, 1178)
        self.assertEqual(self.inventory.rust_security.duplicate_names, ())
        self.assertTrue(self.inventory.rust_security.deny_vulnerabilities)
        self.assertTrue(self.inventory.rust_security.deny_warnings)
        self.assertEqual(len(self.inventory.python_projects), 1)
        self.assertEqual(len(self.inventory.python_packages), 1)
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

    def test_release_gates_match_constitution_and_record_publication(self) -> None:
        context = load_release_context(ROOT)
        gates = context.gates
        self.assertEqual(load_release_gates(ROOT), gates)
        self.assertEqual(len(gates), 10)
        self.assertEqual(context.target.product_version, "2.0.0")
        self.assertEqual(context.target.package_release, 1)
        self.assertEqual(context.target.state, "published-arch-release")
        self.assertTrue(all(gate.status == "verified" for gate in gates))
        self.assertEqual(gates[-1].id, "HFX-GATE-PUBLICATION-DECISION")
        self.assertEqual(gates[-1].status, "verified")
        self.assertTrue(gates[-1].publication_authorization_required)

    def test_protected_behavior_freezes_claims_and_the_natural_wake_gap(self) -> None:
        baseline = load_protected_behavior(ROOT)
        self.assertEqual(
            baseline.source_revision,
            "0f5b13e8a22cc19f0bdd5d70b4f97cc679b1728a",
        )
        self.assertIn("claim.child.00cd.complete-led-map", baseline.claim_ids)
        self.assertIn("claim.child.0296.complete-keyboard-map", baseline.claim_ids)
        self.assertEqual(
            baseline.known_gap_ids,
            ("natural-device-wake-restoration",),
        )

    def test_protected_behavior_separates_integrations_from_core_changes(self) -> None:
        baseline = load_protected_behavior(ROOT)
        integration_change = ("integrations/openrgb/src/controller.cpp",)
        self.assertTrue(baseline.integration_only(integration_change))
        self.assertEqual(baseline.protected_changes(integration_change), ())
        mixed_advanced_change = (
            "knowledge/reviewed-facts.json",
            "crates/hfx-core/src/restoration.rs",
        )
        self.assertFalse(baseline.integration_only(mixed_advanced_change))
        self.assertTrue(
            baseline.advanced_change_requires_approval(mixed_advanced_change)
        )

    def test_candidate_baseline_binds_the_exact_protected_system(self) -> None:
        baseline = load_json(ROOT / "assurance" / "candidate-baseline.json")
        self.assertEqual(baseline["schema"], "hyperflux-candidate-baseline-v1")
        self.assertEqual(
            baseline["baseline"]["revision"],
            "13975278cda3c7284b1478602ac1cdbd6849b1f4",
        )
        self.assertEqual(
            baseline["packages"]["candidate"]["sha256"],
            "4f7849b4a815f1b62120c878ff68f839484fc50d923b95830224d58e3f20d054",
        )
        self.assertEqual(
            baseline["packages"]["rollback"]["sha256"],
            "183d37658d192e977b0c2f381660aeb118a0f736bfb33ec8ac691130d42cb456",
        )
        self.assertEqual(
            sha256_file(ROOT / "assurance" / "protected-behavior.json"),
            baseline["protected_behavior"]["sha256"],
        )

        self.assertEqual(
            baseline["profiles"]["source_sha256"],
            "6c14575bdb3887301e5f440e6ff7350da718960c91fc4ae516a43f76bf02a1ac",
        )
        baseline_runtime_digests = {
            profile["profile_id"]: profile["sha256"]
            for profile in baseline["profiles"]["runtime"]
        }
        self.assertEqual(
            baseline_runtime_digests,
            {
                "child.razer.basilisk-v3-pro-35k.00cd": (
                    "62f114182069895ae0c72ebdd7165e1bb774d41eceeeead155eda449df69f8cf"
                ),
                "child.razer.deathstalker-v2-pro-tkl.0296": (
                    "46703d44d594392e0cb289f6151a7c6b3b200805503a4cf036747d3c347f0d63"
                ),
                "receiver.razer.hyperflux-v2.1532-00cf": (
                    "ff258c7eb0ff16ee7df5aa9964aad7cf9fe0915b4252897aa561421a05ee96c6"
                ),
                "surface.razer.hyperflux-v2-hard-edition": (
                    "0b00ef59eb8c039695c760de855bb7d7d55f1be9a32f6f2d1ed90d039d7796b9"
                ),
            },
        )

        catalog = load_json(ROOT / "generated" / "profiles" / "catalog.json")
        current_profiles = {
            profile["profile_id"]: profile for profile in catalog["profiles"]
        }
        for protected_profile_id in (
            "child.razer.basilisk-v3-pro-35k.00cd",
            "child.razer.deathstalker-v2-pro-tkl.0296",
        ):
            profile = current_profiles[protected_profile_id]
            baseline_digest = baseline_runtime_digests[protected_profile_id]
            self.assertNotEqual(profile["runtime_sha256"], baseline_digest)
            self.assertEqual(
                [
                    migration
                    for migration in profile["qualification_migrations"]
                    if migration["from_runtime_sha256"] == baseline_digest
                ],
                [
                    {
                        "from_runtime_sha256": baseline_digest,
                        "preserve_stage_roles": ["lighting", "map", "telemetry"],
                    }
                ],
            )
        self.assertEqual(
            current_profiles["surface.razer.hyperflux-v2-hard-edition"][
                "runtime_sha256"
            ],
            baseline_runtime_digests["surface.razer.hyperflux-v2-hard-edition"],
        )
        self.assertNotEqual(
            current_profiles["receiver.razer.hyperflux-v2.1532-00cf"][
                "runtime_sha256"
            ],
            baseline_runtime_digests["receiver.razer.hyperflux-v2.1532-00cf"],
        )

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
