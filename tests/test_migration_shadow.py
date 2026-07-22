# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.migration import (
    execute_shadow_comparison,
    load_shadow_fixture,
    run_shadow_comparison,
    validate_shadow_result,
)
from hfxdev.model import ModelError, load_json, sha256_file


FIXTURE = ROOT / "tests/fixtures/shadow/qualified-lifecycle-v1.json"


class MigrationShadowTests(unittest.TestCase):
    def test_fixture_records_bind_to_the_frozen_legacy_inventory(self) -> None:
        fixture = load_shadow_fixture(ROOT, FIXTURE)
        self.assertEqual(fixture["provenance"]["source_id"], "engineering-laboratory")
        self.assertTrue(fixture["provenance"]["boundary"]["read_only"])
        self.assertFalse(fixture["provenance"]["side_effects"]["hardware_queried"])
        self.assertFalse(
            fixture["provenance"]["side_effects"]["hardware_writes_executed"]
        )

    def test_comparator_matches_every_required_domain_deterministically(self) -> None:
        first = execute_shadow_comparison(ROOT, FIXTURE)
        second = execute_shadow_comparison(ROOT, FIXTURE)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "matched")
        self.assertEqual(
            {domain["domain"] for domain in first["domains"]},
            {
                "profile-selection",
                "presence-state",
                "capabilities",
                "transaction-validation",
                "diagnostic-findings",
            },
        )
        self.assertFalse(first["side_effects"]["hardware_queried"])
        self.assertFalse(first["side_effects"]["hardware_writes_executed"])
        self.assertFalse(first["authority"]["publication_authorized"])

    def test_changed_legacy_object_is_rejected_before_comparison(self) -> None:
        fixture = load_json(FIXTURE)
        fixture["provenance"]["source_records"][0]["object"] = "0" * 40
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "does not match inventory"):
                load_shadow_fixture(ROOT, path)

    def test_contradictory_comparator_result_is_rejected(self) -> None:
        fixture = load_shadow_fixture(ROOT, FIXTURE)
        comparison = execute_shadow_comparison(ROOT, FIXTURE)
        comparison["checkpoints"][0]["presence_states"]["matched"] = False
        with self.assertRaisesRegex(ModelError, "contradictory presence-state"):
            validate_shadow_result(comparison, fixture)

    def test_status_cannot_hide_a_typed_divergence(self) -> None:
        fixture = load_shadow_fixture(ROOT, FIXTURE)
        comparison = execute_shadow_comparison(ROOT, FIXTURE)
        comparison["checkpoints"][0]["presence_states"]["next"]["mouse-1"] = (
            "sleeping"
        )
        comparison["checkpoints"][0]["presence_states"]["matched"] = False
        comparison["checkpoints"][0]["matched"] = False
        comparison["domains"][1]["mismatches"] = 1
        comparison["domains"][1]["matched"] = False
        comparison["differences"] = [
            {
                "sequence": 1,
                "domain": "presence-state",
                "description": (
                    "legacy and next presence-state decisions differ at event 1"
                ),
            }
        ]
        with self.assertRaisesRegex(ModelError, "status contradicts"):
            validate_shadow_result(comparison, fixture)

    def test_cli_artifacts_bind_source_materials_and_zero_hardware_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            run = run_shadow_comparison(ROOT, FIXTURE, output)
            evidence = load_json(run.evidence)
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(run.status, "matched")
            self.assertEqual(evidence["source"]["revision"], revision)
            self.assertEqual(evidence["comparison_sha256"], sha256_file(run.comparison))
            self.assertEqual(
                evidence["hardware"],
                {"queried": False, "writes_executed": False, "generations": []},
            )
            self.assertFalse(evidence["publication_authorized"])


if __name__ == "__main__":
    unittest.main()
