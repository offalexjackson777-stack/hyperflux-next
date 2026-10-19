# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import json
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
    def __init__(
        self,
        *,
        ruleset_drift: bool = False,
        pages: str = "current",
        pages_branch: bool = True,
    ) -> None:
        self.governance = load_github_governance(ROOT)
        self.calls: list[tuple[str, str, object]] = []
        self.ruleset_drift = ruleset_drift
        # "missing", "workflow" (Pages built by a workflow from main), or "current".
        self.pages = pages
        self.pages_branch = pages_branch

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
        if endpoint == f"{repo}/pages" and method in {"POST", "PUT"}:
            # GitHub serves its own github.io address over HTTPS.
            self.pages = "current"
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
            if self.pages == "missing":
                return MISSING
            source = self.governance.pages_source
            return {
                "build_type": "workflow" if self.pages == "workflow" else source.build_type,
                "source": {
                    "branch": "main" if self.pages == "workflow" else source.branch,
                    "path": "/",
                },
                "https_enforced": True,
                "html_url": "https://offalexjackson777-stack.github.io/hyperflux-next/",
            }
        if endpoint == f"{repo}/branches/gh-pages":
            return {"name": "gh-pages"} if self.pages_branch else MISSING
        if endpoint == f"{repo}/rulesets?includes_parents=false":
            return [{"id": 7, "name": "Protected main", "enforcement": "active"}]
        if endpoint == f"{repo}/rulesets/7":
            value = deepcopy(_ruleset_payload(self.governance))
            pull_request = next(
                rule for rule in value["rules"] if rule["type"] == "pull_request"
            )
            pull_request["parameters"]["required_reviewers"] = []
            pull_request["parameters"]["server_managed_metadata"] = {
                "enabled": True
            }
            if self.ruleset_drift:
                pull_request["parameters"]["required_approving_review_count"] = 2
            return value
        raise AssertionError(f"unexpected fake GitHub endpoint: {method} {endpoint}")


class GitHubSyncTests(unittest.TestCase):
    def test_plan_has_no_release_tag_package_or_hardware_operation(self) -> None:
        result = plan(ROOT)
        self.assertEqual(result.as_json(), plan(ROOT).as_json())
        self.assertEqual(
            set(result.components),
            {"repository", "labels", "security", "pages", "ruleset"},
        )
        serialized = result.as_json().lower()
        self.assertNotIn("authorized", serialized)
        self.assertIn('"release_or_hardware_operation_available": false', serialized)
        for component in ("release", "tag", "package", "hardware"):
            with self.subTest(component=component), self.assertRaises(ModelError):
                plan(ROOT, [component])

    def test_apply_is_idempotent_and_bounded_to_reviewed_repository_surfaces(self) -> None:
        api = FakeGitHubApi(pages="workflow")
        result = apply(ROOT, api=api)
        self.assertEqual(result.mode, "apply")
        endpoints = "\n".join(endpoint for _, endpoint, _ in api.calls)
        for fragment in ("/releases", "/git/tags", "/packages", "/actions/runners"):
            self.assertNotIn(fragment, endpoints)
        deletes = [endpoint for method, endpoint, _ in api.calls if method == "DELETE"]
        self.assertEqual(deletes, [])
        pages_writes = [
            (method, payload)
            for method, endpoint, payload in api.calls
            if method != "GET" and endpoint.endswith("/pages")
        ]
        self.assertEqual(
            pages_writes,
            [("PUT", {"build_type": "legacy", "source": {"branch": "gh-pages", "path": "/"}})],
        )
        self.assertIn(
            {
                "component": "pages",
                "operation": "serve-site-from-branch",
                "status": "updated",
                "branch": "gh-pages",
                "path": "/",
            },
            result.operations,
        )
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

    def test_plan_serves_pages_from_the_gh_pages_branch(self) -> None:
        result = plan(ROOT, ["pages"])
        self.assertEqual(
            result.operations,
            (
                {
                    "component": "pages",
                    "operation": "serve-site-from-branch",
                    "build_type": "legacy",
                    "branch": "gh-pages",
                    "path": "/",
                    "https_enforced": True,
                },
            ),
        )
        self.assertNotIn("disable", result.as_json())

    def test_apply_creates_pages_when_missing_and_leaves_current_pages_alone(self) -> None:
        missing = FakeGitHubApi(pages="missing")
        result = apply(ROOT, ["pages"], api=missing)
        writes = [(method, payload) for method, _, payload in missing.calls if method != "GET"]
        self.assertEqual(
            writes,
            [("POST", {"build_type": "legacy", "source": {"branch": "gh-pages", "path": "/"}})],
        )
        self.assertEqual(result.operations[0]["status"], "created")

        current = FakeGitHubApi(pages="current")
        result = apply(ROOT, ["pages"], api=current)
        self.assertEqual([call for call in current.calls if call[0] != "GET"], [])
        self.assertEqual(result.operations[0]["status"], "current")

    def test_apply_refuses_pages_before_the_site_branch_exists(self) -> None:
        api = FakeGitHubApi(pages="workflow", pages_branch=False)
        with self.assertRaisesRegex(ModelError, "repositories publish --execute"):
            apply(ROOT, ["pages"], api=api)
        self.assertEqual([call for call in api.calls if call[0] != "GET"], [])

    def test_apply_enforces_https_when_it_is_off(self) -> None:
        api = FakeGitHubApi(pages="current")
        original = api.call

        def without_https(method, endpoint, payload=None, *, allow_missing=False):
            value = original(method, endpoint, payload, allow_missing=allow_missing)
            if method == "GET" and endpoint.endswith("/pages") and isinstance(value, dict):
                return {**value, "https_enforced": False}
            return value

        api.call = without_https
        apply(ROOT, ["pages"], api=api)
        self.assertIn(("PUT", {"https_enforced": True}), [(m, p) for m, _, p in api.calls])

    def test_verify_reports_pages_built_from_main_as_drift(self) -> None:
        with self.assertRaisesRegex(
            ModelError, "GitHub remote state differs from canonical governance"
        ):
            verify(ROOT, ["pages"], api=FakeGitHubApi(pages="workflow"))
        result = verify(ROOT, ["pages"], api=FakeGitHubApi(pages="current"))
        self.assertEqual(result.operations[0]["mismatches"], [])

    def test_verify_rejects_semantic_ruleset_drift_after_normalization(self) -> None:
        with self.assertRaisesRegex(
            ModelError, "GitHub remote state differs from canonical governance"
        ):
            verify(ROOT, api=FakeGitHubApi(ruleset_drift=True))

    def test_main_accepts_only_squash_merges_which_github_signs(self) -> None:
        governance = load_github_governance(ROOT)
        self.assertTrue(_repository_payload(governance)["allow_squash_merge"])
        pull_request = next(
            rule
            for rule in _ruleset_payload(governance)["rules"]
            if rule["type"] == "pull_request"
        )
        self.assertEqual(pull_request["parameters"]["allowed_merge_methods"], ["squash"])

    def test_homepage_is_the_package_site(self) -> None:
        governance = load_github_governance(ROOT)
        site = json.loads((ROOT / "packaging/repository-site.json").read_text(encoding="utf-8"))
        self.assertEqual(governance.homepage, site["site_url"] + "/")
        self.assertEqual(_repository_payload(governance)["homepage"], governance.homepage)


if __name__ == "__main__":
    unittest.main()
