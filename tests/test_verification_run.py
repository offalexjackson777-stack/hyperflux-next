# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.testgraph import TestCatalog, TestNode
from hfxdev.verification_run import run_verification


def node(node_id: str, runner: str, dependencies: tuple[str, ...] = ()) -> TestNode:
    return TestNode(
        id=node_id,
        title=f"{node_id} title",
        owned_domain="verification",
        lanes=("fast", "full-software"),
        runner=runner,
        required_capabilities=(),
        hardware_requirement="none",
        writes_hardware=False,
        expected_duration_seconds=0,
        timeout_seconds=10,
        dependencies=dependencies,
        isolation="shared",
        cache_inputs=("architecture/constitution.json",),
        produced_evidence=(f"{node_id}-evidence",),
        resume_policy="rerun",
    )


class VerificationRunTests(unittest.TestCase):
    def test_records_failures_blocks_dependents_and_continues_independent_nodes(self) -> None:
        catalog = TestCatalog(
            nodes=(
                node("first", "pass"),
                node("failure", "fail", ("first",)),
                node("independent", "inspect", ("first",)),
                node("dependent", "pass", ("failure",)),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"

            def pass_runner(_root: Path, _node: TestNode) -> None:
                return None

            def fail_runner(_root: Path, _node: TestNode) -> None:
                raise RuntimeError(f"failure at {ROOT}/private/input")

            def inspect_runner(_root: Path, _node: TestNode) -> None:
                current = json.loads((output / "result.json").read_text(encoding="utf-8"))
                states = {entry["id"]: entry["status"] for entry in current["nodes"]}
                self.assertEqual(states["failure"], "failed")
                self.assertEqual(states["independent"], "running")

            outcome = run_verification(
                ROOT,
                catalog,
                {"pass": pass_runner, "fail": fail_runner, "inspect": inspect_runner},
                lane="fast",
                output=output,
            )

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.failed_nodes, ("failure", "dependent"))
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            states = {entry["id"]: entry for entry in result["nodes"]}
            self.assertEqual(states["first"]["status"], "passed")
            self.assertEqual(states["failure"]["status"], "failed")
            self.assertEqual(states["independent"]["status"], "passed")
            self.assertEqual(states["dependent"]["status"], "blocked")
            self.assertNotIn(str(ROOT), states["failure"]["error"])

            evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
            self.assertFalse(evidence["hardware"]["queried"])
            self.assertFalse(evidence["hardware"]["writes_executed"])
            self.assertFalse(evidence["publication_authorized"])
            self.assertEqual(len(evidence["upstreams"]), 3)

            suite = ElementTree.parse(output / "junit.xml").getroot()
            self.assertEqual(suite.attrib["failures"], "1")
            self.assertEqual(suite.attrib["skipped"], "1")
            annotations = json.loads(
                (output / "annotations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {annotation["annotation_level"] for annotation in annotations},
                {"failure", "warning"},
            )
            self.assertIn("HyperFlux Verification", (output / "summary.md").read_text())

    def test_missing_runner_is_a_structured_failure(self) -> None:
        catalog = TestCatalog(nodes=(node("missing", "not-registered"),))
        with tempfile.TemporaryDirectory() as temporary:
            outcome = run_verification(
                ROOT,
                catalog,
                {},
                lane="fast",
                output=Path(temporary) / "evidence",
            )
            self.assertEqual(outcome.failed_nodes, ("missing",))
            result = json.loads((outcome.output / "result.json").read_text())
            self.assertIn("trusted runner is unavailable", result["nodes"][0]["error"])

    def test_nonempty_output_directory_is_rejected(self) -> None:
        catalog = TestCatalog(nodes=(node("only", "pass"),))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            (output / "existing").write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "absent or empty"):
                run_verification(
                    ROOT,
                    catalog,
                    {"pass": lambda _root, _node: None},
                    lane="fast",
                    output=output,
                )

    def test_software_lane_rejects_hardware_authority_before_execution(self) -> None:
        unsafe = replace(
            node("unsafe", "pass"),
            hardware_requirement="required",
            writes_hardware=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ModelError, "hardware-authorized nodes"):
                run_verification(
                    ROOT,
                    TestCatalog(nodes=(unsafe,)),
                    {"pass": lambda _root, _node: self.fail("runner must not execute")},
                    lane="fast",
                    output=Path(temporary) / "evidence",
                )


if __name__ == "__main__":
    unittest.main()
