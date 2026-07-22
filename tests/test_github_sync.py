# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.github_sync import (
    MISSING,
    _repository_payload,
    _ruleset_payload,
    apply,
    plan,
    verify,
)
from hfxdev.governance import load_github_governance
from hfxdev.model import ModelError


class FakeGitHubApi:
    def __init__(self, *, ruleset_drift: bool = False, pages_enabled: bool = False) -> None:
        self.governance = load_github_governance(ROOT)
        self.calls: list[tuple[str, str, object]] = []
        self.ruleset_drift = ruleset_drift
        self.pages_enabled = pages_enabled

    def call(
        self,
        method: str,
        endpoint: str,
        payload: object = None,
        *,
        allow_missing: bool = False,
    ) -> object:
        self.calls.append((method, endpoint, payload))
        repo = f"/repos/{self.governance.owner}/{self.governance.repository}"
        if method != "GET":
            return None
        if endpoint == repo:
            value = _repository_payload(self.governance)
            value["security_and_analysis"] = {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            }
            return value
        if endpoint == f"{repo}/topics":
            return {"names": list(self.governance.topics)}
        if endpoint == f"{repo}/labels?per_page=100":
            return [
                {
                    "name": label.name,
                    "color": label.color,
                    "description": label.description,
                }
                for label in self.governance.labels
            ]
        if endpoint == f"{repo}/private-vulnerability-reporting":
            return {"enabled": True}
        if endpoint == f"{repo}/vulnerability-alerts":
            return None
        if endpoint == f"{repo}/automated-security-fixes":
            return {"enabled": True, "paused": False}
        if endpoint == f"{repo}/pages":
            return {
                "build_type": "workflow",
                "html_url": self.governance.homepage,
            } if self.pages_enabled else MISSING
        if endpoint == f"{repo}/environments/github-pages":
            return {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }
            } if self.pages_enabled else MISSING
        if endpoint == f"{repo}/rulesets?includes_parents=false":
            return [{"id": 7, "name": "Protected main", "enforcement": "active"}]
        if endpoint == f"{repo}/rulesets/7":
            value = deepcopy(_ruleset_payload(self.governance))
            pull_request = next(
                rule for rule in value["rules"] if rule["type"] == "pull_request"
            )
            pull_request["parameters"].pop(
                "automatic_copilot_code_review_enabled"
            )
            pull_request["parameters"]["required_reviewers"] = []
            if self.ruleset_drift:
                pull_request["parameters"]["required_approving_review_count"] = 2
            return value
        raise AssertionError(f"unexpected fake GitHub endpoint: {method} {endpoint}")


class GitHubSyncTests(unittest.TestCase):
    def test_plan_has_no_release_tag_package_or_hardware_operation(self) -> None:
        result = plan(ROOT)
        self.assertEqual(
            set(result.components),
            {"repository", "labels", "security", "pages", "ruleset"},
        )
        serialized = result.as_json().lower()
        self.assertIn('"product_publication_authorized": false', serialized)
        self.assertIn('"release_or_hardware_operation_available": false', serialized)
        for component in ("release", "tag", "package", "hardware"):
            with self.subTest(component=component), self.assertRaises(ModelError):
                plan(ROOT, [component])

    def test_apply_is_idempotent_and_bounded_to_reviewed_repository_surfaces(self) -> None:
        api = FakeGitHubApi(pages_enabled=True)
        result = apply(ROOT, api=api)
        self.assertEqual(result.mode, "apply")
        endpoints = "\n".join(endpoint for _, endpoint, _ in api.calls)
        for fragment in ("/releases", "/git/tags", "/packages", "/actions/runners"):
            self.assertNotIn(fragment, endpoints)
        deletes = {
            endpoint for method, endpoint, _ in api.calls if method == "DELETE"
        }
        self.assertIn(
            f"/repos/{api.governance.owner}/{api.governance.repository}/pages",
            deletes,
        )
        self.assertTrue(any(endpoint.endswith("/environments/github-pages") for endpoint in deletes))
        ruleset_payload = next(
            payload
            for method, endpoint, payload in api.calls
            if method == "PUT" and endpoint.endswith("/rulesets/7")
        )
        self.assertEqual(ruleset_payload["bypass_actors"], [])

    def test_verify_compares_complete_reviewed_remote_state(self) -> None:
        result = verify(ROOT, api=FakeGitHubApi())
        self.assertEqual(result.mode, "verify")
        self.assertTrue(
            all(operation["status"] == "current" for operation in result.operations)
        )

    def test_verify_rejects_semantic_ruleset_drift_after_normalization(self) -> None:
        with self.assertRaisesRegex(
            ModelError, "GitHub remote state differs from canonical governance"
        ):
            verify(ROOT, api=FakeGitHubApi(ruleset_drift=True))


if __name__ == "__main__":
    unittest.main()
