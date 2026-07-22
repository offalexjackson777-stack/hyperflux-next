# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable
from urllib.parse import quote

from .governance import GitHubGovernance, load_github_governance
from .model import ModelError


COMPONENTS = ("repository", "labels", "security", "pages", "ruleset")
FORBIDDEN_COMPONENTS = {"release", "tag", "package", "hardware"}
API_VERSION = "2026-03-10"
MISSING = object()


@dataclass(frozen=True)
class SyncResult:
    mode: str
    repository: str
    components: tuple[str, ...]
    operations: tuple[dict[str, Any], ...]

    def as_json(self) -> str:
        return json.dumps(
            {
                "schema": "hyperflux-github-sync-result-v1",
                "mode": self.mode,
                "repository": self.repository,
                "components": list(self.components),
                "operations": list(self.operations),
                "product_publication_authorized": False,
                "release_or_hardware_operation_available": False,
            },
            indent=2,
            sort_keys=True,
        ) + "\n"


class GhApi:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self._runner = runner

    def call(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_missing: bool = False,
    ) -> Any:
        command = [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            endpoint,
        ]
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = json.dumps(payload, separators=(",", ":"))
        completed = self._runner(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            if allow_missing and "HTTP 404" in completed.stderr:
                return MISSING
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown error"
            raise ModelError(f"GitHub API {method} {endpoint} failed: {detail}")
        output = completed.stdout.strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise ModelError(f"GitHub API {method} {endpoint} returned invalid JSON") from error


def _select_components(values: list[str] | None) -> tuple[str, ...]:
    selected = tuple(values or COMPONENTS)
    if not selected or len(set(selected)) != len(selected):
        raise ModelError("GitHub sync components must be unique and non-empty")
    unknown = set(selected) - set(COMPONENTS)
    if unknown:
        if unknown & FORBIDDEN_COMPONENTS:
            raise ModelError("release, tag, package, and hardware components are unavailable")
        raise ModelError(f"unknown GitHub sync component: {', '.join(sorted(unknown))}")
    return selected


def _repository_payload(governance: GitHubGovernance) -> dict[str, Any]:
    return {
        "description": governance.description,
        "homepage": governance.homepage,
        "visibility": governance.visibility,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": False,
        "has_discussions": True,
        "allow_merge_commit": False,
        "allow_squash_merge": False,
        "allow_rebase_merge": True,
        "allow_auto_merge": True,
        "delete_branch_on_merge": True,
    }


def _ruleset_payload(governance: GitHubGovernance) -> dict[str, Any]:
    return {
        "name": "Protected main",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": [f"refs/heads/{governance.default_branch}"],
                "exclude": [],
            }
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": governance.required_approvals,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": governance.require_code_owner_reviews,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                    "automatic_copilot_code_review_enabled": False,
                    "allowed_merge_methods": ["rebase"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": governance.strict_required_status_checks,
                    "do_not_enforce_on_create": True,
                    "required_status_checks": [
                        {"context": check} for check in governance.required_checks
                    ],
                },
            },
        ],
    }


def _ruleset_comparison_view(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        ref_name = value["conditions"]["ref_name"]
        includes = sorted(ref_name["include"])
        excludes = sorted(ref_name["exclude"])
        normalized_rules: list[dict[str, Any]] = []
        for source_rule in value["rules"]:
            rule_type = source_rule["type"]
            rule: dict[str, Any] = {"type": rule_type}
            if "parameters" in source_rule:
                parameters = dict(source_rule["parameters"])
                if rule_type == "pull_request":
                    parameters.pop("required_reviewers", None)
                    parameters.setdefault("automatic_copilot_code_review_enabled", False)
                    parameters["allowed_merge_methods"] = sorted(
                        parameters["allowed_merge_methods"]
                    )
                elif rule_type == "required_status_checks":
                    contexts = sorted(
                        check["context"]
                        for check in parameters["required_status_checks"]
                    )
                    parameters["required_status_checks"] = [
                        {"context": context} for context in contexts
                    ]
                rule["parameters"] = parameters
            normalized_rules.append(rule)
        normalized_rules.sort(key=lambda rule: rule["type"])
    except (KeyError, TypeError):
        return None
    return {
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "bypass_actors": value.get("bypass_actors"),
        "conditions": {
            "ref_name": {
                "include": includes,
                "exclude": excludes,
            }
        },
        "rules": normalized_rules,
    }


def _desired_plan(governance: GitHubGovernance, components: tuple[str, ...]) -> SyncResult:
    operations: list[dict[str, Any]] = []
    if "repository" in components:
        operations.extend(
            [
                {"component": "repository", "operation": "reconcile-about-and-features"},
                {"component": "repository", "operation": "replace-topics", "count": len(governance.topics)},
                {"component": "repository", "operation": "upload-social-preview-via-reviewed-ui", "asset": governance.social_preview_asset},
                {"component": "repository", "operation": "reconcile-discussion-categories-via-reviewed-ui", "count": len(governance.discussions)},
                {
                    "component": "repository",
                    "operation": "reconcile-roadmap-via-reviewed-ui",
                    "number": governance.project_number,
                    "title": governance.project_title,
                    "url": governance.project_url,
                },
            ]
        )
    if "labels" in components:
        operations.append({"component": "labels", "operation": "upsert", "count": len(governance.labels)})
    if "security" in components:
        operations.extend(
            {"component": "security", "operation": identifier, "desired": plan.state}
            for identifier, plan in governance.security_posture
            if identifier != "artifact_attestations"
        )
    if "pages" in components:
        operations.extend(
            [
                {"component": "pages", "operation": "disable-pages"},
                {"component": "pages", "operation": "remove-pages-environment"},
            ]
        )
    if "ruleset" in components:
        operations.append(
            {
                "component": "ruleset",
                "operation": "reconcile-protected-main",
                "checks": list(governance.required_checks),
                "bypass_actors": [],
            }
        )
    return SyncResult(
        mode="plan",
        repository=f"{governance.owner}/{governance.repository}",
        components=components,
        operations=tuple(operations),
    )


def plan(root: Path, components: list[str] | None = None) -> SyncResult:
    governance = load_github_governance(root)
    return _desired_plan(governance, _select_components(components))


def _apply_repository(api: GhApi, governance: GitHubGovernance) -> list[dict[str, Any]]:
    repo = f"/repos/{governance.owner}/{governance.repository}"
    api.call("PATCH", repo, _repository_payload(governance))
    api.call("PUT", f"{repo}/topics", {"names": list(governance.topics)})
    return [
        {"component": "repository", "status": "applied", "operation": "about-and-features"},
        {"component": "repository", "status": "applied", "operation": "topics"},
        {"component": "repository", "status": "ui-required", "operation": "social-preview"},
        {"component": "repository", "status": "ui-required", "operation": "discussion-categories"},
        {"component": "repository", "status": "ui-required", "operation": "roadmap"},
    ]


def _apply_labels(api: GhApi, governance: GitHubGovernance) -> list[dict[str, Any]]:
    repo = f"/repos/{governance.owner}/{governance.repository}"
    current = api.call("GET", f"{repo}/labels?per_page=100")
    if not isinstance(current, list):
        raise ModelError("GitHub label inventory is malformed")
    by_name = {item.get("name"): item for item in current if isinstance(item, dict)}
    operations: list[dict[str, Any]] = []
    for label in governance.labels:
        payload = {"name": label.name, "color": label.color, "description": label.description}
        existing = by_name.get(label.name)
        if existing is None:
            api.call("POST", f"{repo}/labels", payload)
            status = "created"
        elif existing.get("color", "").lower() != label.color or existing.get("description") != label.description:
            api.call("PATCH", f"{repo}/labels/{quote(label.name, safe='')}", payload)
            status = "updated"
        else:
            status = "current"
        operations.append({"component": "labels", "label": label.name, "status": status})
    return operations


def _apply_security(api: GhApi, governance: GitHubGovernance) -> list[dict[str, Any]]:
    repo = f"/repos/{governance.owner}/{governance.repository}"
    operations: list[dict[str, Any]] = []
    for endpoint, name in (
        (f"{repo}/private-vulnerability-reporting", "private-vulnerability-reporting"),
        (f"{repo}/vulnerability-alerts", "dependency-graph-and-alerts"),
        (f"{repo}/automated-security-fixes", "dependabot-security-updates"),
    ):
        api.call("PUT", endpoint)
        operations.append({"component": "security", "operation": name, "status": "enabled"})
    api.call(
        "PATCH",
        repo,
        {
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            }
        },
    )
    operations.append({"component": "security", "operation": "secret-scanning", "status": "enabled"})
    operations.append({"component": "security", "operation": "codeql", "status": "workflow-owned"})
    operations.append({"component": "security", "operation": "source-sbom", "status": "repository-owned"})
    return operations


def _apply_pages(api: GhApi, governance: GitHubGovernance) -> list[dict[str, Any]]:
    repo = f"/repos/{governance.owner}/{governance.repository}"
    current = api.call("GET", f"{repo}/pages", allow_missing=True)
    environment_endpoint = f"{repo}/environments/github-pages"
    environment = api.call("GET", environment_endpoint, allow_missing=True)
    if current is not MISSING:
        api.call("DELETE", f"{repo}/pages")
    if environment is not MISSING:
        api.call("DELETE", environment_endpoint)
    return [
        {"component": "pages", "operation": "site", "status": "disabled"},
        {
            "component": "pages",
            "operation": "deployment-environment",
            "status": "removed",
        },
    ]


def _apply_ruleset(api: GhApi, governance: GitHubGovernance) -> list[dict[str, Any]]:
    repo = f"/repos/{governance.owner}/{governance.repository}"
    current = api.call("GET", f"{repo}/rulesets?includes_parents=false")
    if not isinstance(current, list):
        raise ModelError("GitHub ruleset inventory is malformed")
    match = next((item for item in current if item.get("name") == "Protected main"), None)
    payload = _ruleset_payload(governance)
    if match is None:
        api.call("POST", f"{repo}/rulesets", payload)
        status = "created"
    else:
        api.call("PUT", f"{repo}/rulesets/{match['id']}", payload)
        status = "updated"
    return [{"component": "ruleset", "operation": "protected-main", "status": status}]


def apply(
    root: Path,
    components: list[str] | None = None,
    *,
    api: GhApi | None = None,
) -> SyncResult:
    governance = load_github_governance(root)
    selected = _select_components(components)
    if not governance.source_repository_authorized:
        raise ModelError("GitHub source repository synchronization is not authorized")
    client = api or GhApi()
    operations: list[dict[str, Any]] = []
    handlers = {
        "repository": _apply_repository,
        "labels": _apply_labels,
        "security": _apply_security,
        "pages": _apply_pages,
        "ruleset": _apply_ruleset,
    }
    for component in selected:
        if component == "pages" and governance.pages_deployment_authorized:
            raise ModelError("Pages disablement conflicts with an authorized deployment")
        operations.extend(handlers[component](client, governance))
    return SyncResult(
        mode="apply",
        repository=f"{governance.owner}/{governance.repository}",
        components=selected,
        operations=tuple(operations),
    )


def verify(
    root: Path,
    components: list[str] | None = None,
    *,
    api: GhApi | None = None,
) -> SyncResult:
    governance = load_github_governance(root)
    selected = _select_components(components)
    client = api or GhApi()
    repo = f"/repos/{governance.owner}/{governance.repository}"
    operations: list[dict[str, Any]] = []
    if "repository" in selected:
        value = client.call("GET", repo)
        topics = client.call("GET", f"{repo}/topics")
        expected = _repository_payload(governance)
        mismatches = [key for key, desired in expected.items() if value.get(key) != desired]
        if topics.get("names") != list(governance.topics):
            mismatches.append("topics")
        operations.append({"component": "repository", "status": "current" if not mismatches else "drifted", "mismatches": mismatches})
    if "labels" in selected:
        current = client.call("GET", f"{repo}/labels?per_page=100")
        by_name = {item.get("name"): item for item in current}
        drift = [label.name for label in governance.labels if label.name not in by_name or by_name[label.name].get("color", "").lower() != label.color or by_name[label.name].get("description") != label.description]
        operations.append({"component": "labels", "status": "current" if not drift else "drifted", "labels": drift})
    if "security" in selected:
        value = client.call("GET", repo)
        security = value.get("security_and_analysis") or {}
        secret = (security.get("secret_scanning") or {}).get("status")
        push = (security.get("secret_scanning_push_protection") or {}).get("status")
        private = client.call("GET", f"{repo}/private-vulnerability-reporting")
        alerts = client.call("GET", f"{repo}/vulnerability-alerts", allow_missing=True)
        fixes = client.call("GET", f"{repo}/automated-security-fixes", allow_missing=True)
        private_enabled = isinstance(private, dict) and private.get("enabled") is True
        alerts_enabled = alerts is not MISSING
        fixes_enabled = (
            fixes is not MISSING
            and isinstance(fixes, dict)
            and fixes.get("enabled") is True
        )
        current = (
            secret == "enabled"
            and push == "enabled"
            and private_enabled
            and alerts_enabled
            and fixes_enabled
        )
        operations.append(
            {
                "component": "security",
                "status": "current" if current else "drifted",
                "secret_scanning": secret,
                "push_protection": push,
                "private_reporting": private_enabled,
                "dependency_alerts": alerts_enabled,
                "dependabot_security_updates": fixes_enabled,
                "codeql": "workflow-owned",
                "source_sbom": "repository-owned",
            }
        )
    if "pages" in selected:
        pages = client.call("GET", f"{repo}/pages", allow_missing=True)
        environment = client.call("GET", f"{repo}/environments/github-pages", allow_missing=True)
        current = pages is MISSING and environment is MISSING
        operations.append(
            {
                "component": "pages",
                "status": "current" if current else "drifted",
                "site": "disabled" if pages is MISSING else "enabled",
                "environment": "absent" if environment is MISSING else "present",
            }
        )
    if "ruleset" in selected:
        rulesets = client.call("GET", f"{repo}/rulesets?includes_parents=false")
        match = next((item for item in rulesets if item.get("name") == "Protected main" and item.get("enforcement") == "active"), None)
        actual = (
            client.call("GET", f"{repo}/rulesets/{match['id']}")
            if match is not None
            else None
        )
        expected = _ruleset_payload(governance)
        ruleset_current = _ruleset_comparison_view(actual) == _ruleset_comparison_view(
            expected
        )
        operations.append(
            {
                "component": "ruleset",
                "status": "current" if ruleset_current else "drifted",
                "id": match.get("id") if match else None,
            }
        )
    if any(item.get("status") == "drifted" for item in operations):
        raise ModelError("GitHub remote state differs from canonical governance")
    return SyncResult(
        mode="verify",
        repository=f"{governance.owner}/{governance.repository}",
        components=selected,
        operations=tuple(operations),
    )
