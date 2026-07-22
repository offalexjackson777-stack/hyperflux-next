# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.ci import _linked_worktree_common_dir, container_invocation
from hfxdev.generators.governance import (
    EXACT_SOURCE_REVISION,
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
    repository_experience_workflow,
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


def _values_for_key(value: object, expected_key: str) -> list[object]:
    if isinstance(value, dict):
        result = [child for key, child in value.items() if key == expected_key]
        for child in value.values():
            result.extend(_values_for_key(child, expected_key))
        return result
    if isinstance(value, list):
        return [
            item
            for child in value
            for item in _values_for_key(child, expected_key)
        ]
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
            "repository-experience": repository_experience_workflow(self.governance),
        }
        allowed = {action.commit for action in self.governance.actions}
        observed: set[str] = set()
        for name, text in workflows.items():
            with self.subTest(workflow=name):
                document = yaml.safe_load(text)
                self.assertIsInstance(document, dict)
                checkouts = [
                    step
                    for job in document["jobs"].values()
                    for step in job["steps"]
                    if step.get("name") == "Check out exact source"
                ]
                self.assertTrue(checkouts)
                for checkout in checkouts:
                    self.assertEqual(checkout["with"]["ref"], EXACT_SOURCE_REVISION)
                for revision in _values_for_key(document, "HFX_SOURCE_REVISION"):
                    self.assertEqual(revision, EXACT_SOURCE_REVISION)
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
        self.assertIn(".hfx/cargo", fast_text)
        self.assertIn("$GITHUB_STEP_SUMMARY", fast_text)
        self.assertIn("build/ci/fast/result.json", fast_text)
        self.assertIn("--lane full", full_text)
        self.assertIn("build/ci/full/result.json", full_text)
        self.assertIn("./hfx ci docs", docs_text)
        self.assertNotIn("release", fast["on"])
        self.assertNotIn("release", full["on"])
        self.assertNotIn("release", docs["on"])
        self.assertIn("pull_request", full["on"])
        self.assertEqual(self.governance.protection_profile, "solo-maintainer")
        self.assertEqual(self.governance.trusted_maintainers, 1)
        self.assertEqual(self.governance.required_approvals, 0)
        self.assertFalse(self.governance.require_code_owner_reviews)
        self.assertTrue(self.governance.strict_required_status_checks)
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
                "Full verification / Full software",
                "Repository experience / Link checks",
                "Repository experience / Pages preview",
            },
        )

    def test_repository_experience_jobs_are_required_and_bounded(self) -> None:
        workflow = yaml.safe_load(repository_experience_workflow(self.governance))
        self.assertEqual(
            {job["name"] for job in workflow["jobs"].values()},
            {"Link checks", "Pages preview"},
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertIn("pull_request", workflow["on"])
        self.assertNotIn("push", workflow["on"])
        text = repository_experience_workflow(self.governance)
        self.assertNotIn("deploy-pages", text)
        self.assertNotIn("upload-pages-artifact", text)
        self.assertNotIn("--device", text)

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
            self.assertIn(
                "CARGO_HOME=/workspaces/hyperflux-next/.hfx/cargo",
                command,
            )
            self.assertIn("USER=hyperflux", command)
            self.assertIn("LOGNAME=hyperflux", command)
        prepare_command = " ".join(prepare.command)
        self.assertIn("./hfx upstream prepare --output .hfx/upstreams", prepare_command)
        self.assertIn("CARGO_NET_OFFLINE=false cargo fetch --locked", prepare_command)
        self.assertIn("CARGO_NET_OFFLINE=false", prepare_command)
        self.assertIn("CARGO_NET_OFFLINE=true", " ".join(verify.command))
        self.assertIn("--changed-from " + "a" * 40, " ".join(verify.command))
        self.assertIn("docs build", " ".join(docs.command))
        self.assertIn("docs verify", " ".join(docs.command))

    @patch("hfxdev.ci.shutil.which", return_value="/usr/bin/podman")
    def test_ci_invocation_keeps_host_identity_with_rootless_podman(self, _which) -> None:
        invocation = container_invocation(
            ROOT,
            image=self.governance.development_image,
            operation="prepare",
            engine="podman",
            uid=1000,
            gid=1001,
        )
        self.assertIn("--userns=keep-id", invocation.command)
        self.assertIn("1000:1001", invocation.command)

    @patch("hfxdev.ci.shutil.which", return_value="/usr/bin/docker")
    def test_ci_invocation_names_an_arbitrary_numeric_identity(self, _which) -> None:
        invocation = container_invocation(
            ROOT,
            image=self.governance.development_image,
            operation="verify",
            lane="full",
            output=Path("build/ci/arbitrary-identity"),
            engine="docker",
            uid=1001,
            gid=121,
        )
        command = " ".join(invocation.command)
        self.assertIn("--user 1001:121", command)
        self.assertIn("USER=hyperflux", command)
        self.assertIn("LOGNAME=hyperflux", command)

    def test_ci_discovers_linked_worktree_common_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "worktree"
            common = base / "source" / ".git"
            git_dir = common / "worktrees" / "worktree"
            root.mkdir()
            git_dir.mkdir(parents=True)
            (root / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            self.assertEqual(_linked_worktree_common_dir(root), common)

    @patch("hfxdev.ci.shutil.which", return_value="/usr/bin/podman")
    def test_linked_worktree_git_metadata_is_mounted_read_only(self, _which) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            common = Path(temporary) / ".git"
            common.mkdir()
            with patch(
                "hfxdev.ci._linked_worktree_common_dir", return_value=common
            ):
                invocation = container_invocation(
                    ROOT,
                    image=self.governance.development_image,
                    operation="prepare",
                    engine="podman",
                    uid=1000,
                    gid=1001,
                )
            self.assertIn(f"{common}:{common}:ro", invocation.command)

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
        self.assertEqual(plan["project"]["number"], 1)
        self.assertEqual(
            plan["project"]["url"],
            "https://github.com/users/offalexjackson777-stack/projects/1",
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
