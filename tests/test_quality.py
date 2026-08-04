# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError, load_json
from hfxdev.quality import load_quality_audit


class QualityAuditTests(unittest.TestCase):
    def test_canonical_audit_binds_every_required_phase_five_review(self) -> None:
        audit = load_quality_audit(ROOT)
        self.assertEqual(len(audit.review_statuses), 13)
        self.assertEqual(audit.baseline_artifact_sizes["bridge-daemon"], 11381776)
        self.assertEqual(audit.baseline_staged_payload_bytes, 29357049)
        self.assertEqual(audit.relay_polling["idle_ms"], 10)

    def test_status_polling_and_optimization_overclaims_fail_closed(self) -> None:
        source = load_json(ROOT / "assurance" / "quality-audit.json")
        performance = load_json(ROOT / "assurance" / "performance-budgets.json")
        cases = (
            ("status", "enforced", "status or ordering"),
            ("order", None, "status or ordering"),
            ("poll", 11, "idle_ms"),
            ("artifact", True, "overclaims"),
        )
        for kind, replacement, message in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                for directory in (
                    "assurance",
                    "crates/hfx-uhid-relay/src",
                    "docs/developer",
                    "packaging",
                    "schemas",
                    "tests",
                    "toolchains",
                    "tools/hfxdev",
                    "verification",
                ):
                    (root / directory).mkdir(parents=True, exist_ok=True)
                for relative in {
                    path
                    for review in source["reviews"]
                    for path in review["evidence"]
                } | {
                    metric["source"] for metric in performance["metrics"]
                } | {
                    "Cargo.toml",
                    "assurance/performance-budgets.json",
                    "assurance/quality-audit.json",
                    "crates/hfx-uhid-relay/src/runtime.rs",
                    "toolchains/development-environment.json",
                    "verification/tests.json",
                }:
                    origin = ROOT / relative
                    destination = root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if origin.is_dir():
                        shutil.copytree(origin, destination, dirs_exist_ok=True)
                    else:
                        shutil.copy2(origin, destination)
                mutated = json.loads(json.dumps(source))
                if kind == "status":
                    mutated["reviews"][0]["status"] = replacement
                elif kind == "order":
                    mutated["reviews"][0], mutated["reviews"][1] = (
                        mutated["reviews"][1],
                        mutated["reviews"][0],
                    )
                elif kind == "poll":
                    mutated["measurements"]["relay_polling"]["idle_ms"] = replacement
                else:
                    mutated["measurements"]["build_profile_trial"]["artifact_produced"] = replacement
                (root / "assurance/quality-audit.json").write_text(
                    json.dumps(mutated, indent=2) + "\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ModelError, message):
                    load_quality_audit(root)


if __name__ == "__main__":
    unittest.main()
