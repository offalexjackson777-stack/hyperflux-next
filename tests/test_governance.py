# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.ci import container_invocation
from hfxdev.generators.governance import (
    bug_report,
    codeql_workflow,
    dependency_review_workflow,
    documentation_report,
    documentation_workflow,
    experience_plan,
    feature_request,
    full_verification_workflow,
    hardware_research,
    hardware_qualification,
    pages_workflow,
    protection_plan,
    verification_workflow,
)
from hfxdev.generators.supply_chain import spdx_json
from hfxdev.governance import load_github_governance
from hfxdev.model import ModelError
from hfxdev.supply_chain import load_dependency_inventory


def _uses(value: object) -> list[str]:
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            if key == "uses" and isinstance(child, str):
                result.append(child)
            result.extend(_uses(child))
        return result
    if isinstance(value, list):
        return [item for child in value for item in _uses(child)]
    return []


class GitHubGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.governance = load_github_governance(ROOT)

    def test_public_source_is_authorized_while_product_publication_remains_locked(self) -> None:
        source = json.loads((ROOT / "governance" / "github.json").read_text(encoding="utf-8"))
        self.assertEqual(self.governance.remote_state, "public-pre-release")
        interlock = source["publication_interlock"]
        self.assertTrue(interlock["source_repository_authorized"])
        self.assertTrue(interlock["pages_deployment_authorized"])
        self.assertTrue(interlock["collaboration_features_authorized"])
        for key in (
            "product_publication_authorized",
            "release_workflows_authorized",
            "package_publication_authorized",
            "tag_creation_authorized",
            "hardware_ci_authorized",
        ):
            self.assertFalse(interlock[key])
        plan = json.loads(protection_plan(self.governance))
        self.assertTrue(plan["apply_authorized"])
        self.assertEqual(plan["ruleset"]["bypass_actors"], [])
        self.assertEqual(
            set(plan["excluded_components"]),
            {
                "package-publication",
                "release-tags",
                "release-publication",
                "hardware-ci",
            },
        )

    def test_every_workflow_dependency_is_an_exact_reviewed_commit(self) -> None:
        workflows = {
            "verification": verification_workflow(self.governance),
            "full": full_verification_workflow(self.governance),
            "documentation": documentation_workflow(self.governance),
            "codeql": codeql_workflow(self.governance),
            "dependency-review": dependency_review_workflow(self.governance),
            "pages": pages_workflow(self.governance),
        }
        allowed = {action.commit for action in self.governance.actions}
        observed: set[str] = set()
        for name, text in workflows.items():
            with self.subTest(workflow=name):
                document = yaml.safe_load(text)
                self.assertIsInstance(document, dict)
                for use in _uses(document):
                    match = re.search(r"@([0-9a-f]{40})$", use)
                    self.assertIsNotNone(match, use)
                    assert match is not None
                    observed.add(match.group(1))
                    self.assertIn(match.group(1), allowed)
                self.assertNotIn("pull_request_target", text)
                self.assertNotIn("--privileged", text)
                self.assertNotIn("--device", text)
                if name == "pages":
                    self.assertIn("deploy-pages", text)
                    self.assertIn("upload-pages-artifact", text)
                    self.assertIn("github-pages", text)
                    self.assertIn("Product release authority | Locked", text)
                else:
                    self.assertNotIn("deploy-pages", text)
                    self.assertNotIn("upload-pages-artifact", text)
        self.assertEqual(observed, allowed)

    def test_host_ci_control_plane_imports_without_site_packages(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "hfx"), "ci", "prepare", "--help"],
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--image IMAGE", result.stdout)

    def test_software_workflows_use_bounded_container_runner_and_no_release_trigger(self) -> None:
        fast = yaml.safe_load(verification_workflow(self.governance))
        full = yaml.safe_load(full_verification_workflow(self.governance))
        docs = yaml.safe_load(documentation_workflow(self.governance))
        fast_text = verification_workflow(self.governance)
        full_text = full_verification_workflow(self.governance)
        docs_text = documentation_workflow(self.governance)
        self.assertIn("--changed-from", fast_text)
        self.assertIn("./hfx ci verify", fast_text)
        self.assertIn("./hfx ci summary", fast_text)
        self.assertIn("$GITHUB_STEP_SUMMARY", fast_text)
        self.assertIn("build/ci/fast/result.json", fast_text)
        self.assertIn("--lane full", full_text)
        self.assertIn("build/ci/full/result.json", full_text)
        self.assertIn("./hfx ci docs", docs_text)
        self.assertNotIn("release", fast["on"])
        self.assertNotIn("release", full["on"])
        self.assertNotIn("release", docs["on"])
        self.assertEqual(fast["permissions"], {"contents": "read"})
        self.assertEqual(docs["permissions"], {"contents": "read"})
        self.assertEqual(
            set(self.governance.required_checks),
            {
                "Verification / Fast software",
                "CodeQL / Analyze (c-cpp)",
                "CodeQL / Analyze (python)",
                "Dependency review / Dependency review",
                "Documentation / Portal contracts",
            },
        )

    @patch("hfxdev.ci.shutil.which", return_value="/usr/bin/docker")
    def test_ci_invocations_separate_fetch_from_networkless_execution(self, _which) -> None:
        prepare = container_invocation(
            ROOT,
            image=self.governance.development_image,
            operation="prepare",
            engine="docker",
            uid=1000,
            gid=1000,
        )
        verify = container_invocation(
            ROOT,
            image=self.governance.development_image,
            operation="verify",
            lane="fast",
            output=Path("build/ci/test-fast"),
            changed_from="a" * 40,
            engine="docker",
            uid=1000,
            gid=1000,
        )
        docs = container_invocation(
            ROOT,
            image=self.governance.development_image,
            operation="docs",
            output=Path("build/ci/test-portal"),
            engine="docker",
            uid=1000,
            gid=1000,
        )
        self.assertEqual(prepare.network, "bridge")
        self.assertEqual(verify.network, "none")
        self.assertEqual(docs.network, "none")
        for invocation in (prepare, verify, docs):
            command = " ".join(invocation.command)
            self.assertIn("--cap-drop ALL", command)
            self.assertIn("no-new-privileges:true", command)
            self.assertNotIn("--privileged", command)
            self.assertNotIn("--device", command)
        self.assertIn("--changed-from " + "a" * 40, " ".join(verify.command))
        self.assertIn("docs build", " ".join(docs.command))
        self.assertIn("docs verify", " ".join(docs.command))

    @patch("hfxdev.ci.shutil.which", return_value="/usr/bin/docker")
    def test_ci_output_revision_and_image_inputs_fail_closed(self, _which) -> None:
        cases = (
            {"image": "../image", "output": Path("build/ci/result"), "changed_from": None},
            {"image": "valid:ci", "output": Path("../result"), "changed_from": None},
            {"image": "valid:ci", "output": Path("/tmp/result"), "changed_from": None},
            {"image": "valid:ci", "output": Path("build/ci/result"), "changed_from": "main"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ModelError):
                container_invocation(
                    ROOT,
                    operation="verify",
                    lane="fast",
                    engine="docker",
                    uid=1000,
                    gid=1000,
                    **values,
                )

    def test_issue_forms_request_bounded_private_data_free_context(self) -> None:
        bug = bug_report(self.governance)
        hardware = hardware_qualification(self.governance)
        research = hardware_research(self.governance)
        documentation = documentation_report(self.governance)
        feature = feature_request(self.governance)
        self.assertIn("support-bundle --preview", bug)
        self.assertIn("Never attach hardware serials", bug)
        self.assertIn("does not authorize receiver queries", hardware)
        self.assertIn("No serial, stable host identifier", hardware)
        self.assertIn("support-bundle --preview", hardware)
        self.assertIn("Doctor reference", hardware)
        self.assertIn("research identifies candidates", research.lower())
        self.assertIn("canonical source", documentation.lower())
        self.assertIn("universal", feature.lower())

    def test_collaboration_security_and_service_plans_are_complete_and_bounded(self) -> None:
        plan = json.loads(experience_plan(self.governance))
        self.assertTrue(plan["apply_authorized"])
        self.assertTrue(plan["source_repository_authorized"])
        self.assertTrue(plan["pages_deployment_authorized"])
        self.assertFalse(plan["product_publication_authorized"])
        self.assertEqual(plan["external_apps_installed"], [])
        self.assertEqual(
            {category["id"] for category in plan["discussions"]["categories"]},
            {"announcements", "hardware-qualification", "help", "ideas"},
        )
        self.assertEqual(
            {view["layout"] for view in plan["project"]["views"]},
            {"table", "board", "roadmap"},
        )
        self.assertTrue(
            {"area", "evidence-level", "release-gate", "priority", "qualification-state"}
            <= {field["id"] for field in plan["project"]["fields"]}
        )
        self.assertEqual(
            set(plan["security_posture"]),
            {
                "private_vulnerability_reporting",
                "dependency_graph",
                "dependabot_security_updates",
                "secret_scanning",
                "secret_scanning_push_protection",
                "code_scanning",
                "source_sbom",
                "artifact_attestations",
            },
        )
        evaluations = {item["id"]: item for item in plan["service_evaluations"]}
        self.assertEqual(evaluations["dependabot"]["decision"], "preferred-native")
        self.assertEqual(evaluations["renovate"]["decision"], "not-selected")
        self.assertEqual(evaluations["codecov"]["decision"], "not-selected")
        self.assertEqual(evaluations["openssf-scorecard"]["decision"], "deferred")

    def test_workflow_actions_are_part_of_the_source_sbom(self) -> None:
        inventory = load_dependency_inventory(ROOT)
        self.assertEqual(len(inventory.workflow_actions), 10)
        document = json.loads(spdx_json(inventory))
        names = {package["name"] for package in document["packages"]}
        self.assertTrue(
            {action.repository for action in self.governance.actions} <= names
        )


if __name__ == "__main__":
    unittest.main()
