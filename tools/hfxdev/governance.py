# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


ROOT_KEYS = {
    "$schema",
    "schema",
    "repository",
    "publication_interlock",
    "ownership",
    "protection",
    "labels",
    "automation",
}
ACTION_IDS = {
    "build-push",
    "cache",
    "checkout",
    "codeql",
    "dependency-review",
    "setup-buildx",
    "upload-artifact",
}
ACTION_REPOSITORIES = {
    "build-push": "docker/build-push-action",
    "cache": "actions/cache",
    "checkout": "actions/checkout",
    "codeql": "github/codeql-action",
    "dependency-review": "actions/dependency-review-action",
    "setup-buildx": "docker/setup-buildx-action",
    "upload-artifact": "actions/upload-artifact",
}
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v[0-9]+(?:\.[0-9]+){0,2}$")
OWNER = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
LABEL_COLOR = re.compile(r"^[0-9a-f]{6}$")


@dataclass(frozen=True)
class ActionPin:
    id: str
    repository: str
    version: str
    commit: str
    license_expression: str

    @property
    def uses(self) -> str:
        return f"{self.repository}@{self.commit}"


@dataclass(frozen=True)
class OwnershipRule:
    paths: tuple[str, ...]
    owners: tuple[str, ...]


@dataclass(frozen=True)
class GovernanceLabel:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class GitHubGovernance:
    owner: str
    repository: str
    default_branch: str
    remote_state: str
    planned_pages_source: str
    default_owners: tuple[str, ...]
    ownership_rules: tuple[OwnershipRule, ...]
    required_checks: tuple[str, ...]
    required_approvals: int
    labels: tuple[GovernanceLabel, ...]
    runner: str
    development_image: str
    dependency_update_day: str
    full_verification_cron: str
    actions: tuple[ActionPin, ...]

    @property
    def actions_by_id(self) -> dict[str, ActionPin]:
        return {action.id: action for action in self.actions}


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelError(f"{label}: missing or unknown fields")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ModelError(f"{label}: expected a non-empty string array")
    require_unique(value, label)
    return tuple(value)


def _owners(value: Any, label: str) -> tuple[str, ...]:
    owners = _strings(value, label)
    if any(OWNER.fullmatch(owner) is None for owner in owners):
        raise ModelError(f"{label}: invalid GitHub owner")
    return owners


def _ownership_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if not value.startswith("/") or ".." in path.parts or any(character.isspace() for character in value):
        raise ModelError(f"{label}: unsafe CODEOWNERS path")
    return value


def load_github_governance(root: Path) -> GitHubGovernance:
    value = _exact(
        load_json(root / "governance" / "github.json"), ROOT_KEYS, "GitHub governance"
    )
    if value["$schema"] != "../schemas/github-governance.schema.json":
        raise ModelError("GitHub governance has a non-canonical schema reference")
    if value["schema"] != "hyperflux-github-governance-v1":
        raise ModelError("unsupported GitHub governance schema")

    repository = _exact(
        value["repository"],
        {"owner", "name", "default_branch", "remote_state", "planned_pages_source"},
        "GitHub repository",
    )
    if repository != {
        "owner": "offalexjackson777-stack",
        "name": "hyperflux-next",
        "default_branch": "main",
        "remote_state": "not-created",
        "planned_pages_source": "github-actions",
    }:
        raise ModelError("GitHub repository identity or unpublished state drifted")

    interlock = _exact(
        value["publication_interlock"],
        {
            "publication_authorized",
            "pages_deployment_authorized",
            "release_workflows_authorized",
            "hardware_ci_authorized",
        },
        "GitHub publication interlock",
    )
    if any(interlock.values()):
        raise ModelError("GitHub publication, deployment, release, and hardware CI must remain locked")

    ownership = _exact(value["ownership"], {"default", "rules"}, "GitHub ownership")
    default_owners = _owners(ownership["default"], "default CODEOWNERS")
    raw_rules = ownership["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ModelError("GitHub ownership requires path rules")
    rules: list[OwnershipRule] = []
    paths_seen: list[str] = []
    for index, raw in enumerate(raw_rules):
        item = _exact(raw, {"paths", "owners"}, f"CODEOWNERS rule {index}")
        paths = tuple(
            _ownership_path(path, f"CODEOWNERS rule {index}")
            for path in _strings(item["paths"], f"CODEOWNERS rule {index} paths")
        )
        paths_seen.extend(paths)
        rules.append(OwnershipRule(paths, _owners(item["owners"], f"CODEOWNERS rule {index}")))
    require_unique(paths_seen, "CODEOWNERS path")

    protection = _exact(
        value["protection"],
        {
            "linear_history",
            "required_approvals",
            "dismiss_stale_reviews",
            "require_code_owner_reviews",
            "require_conversation_resolution",
            "required_checks",
        },
        "GitHub protection",
    )
    if any(
        protection[key] is not True
        for key in (
            "linear_history",
            "dismiss_stale_reviews",
            "require_code_owner_reviews",
            "require_conversation_resolution",
        )
    ):
        raise ModelError("GitHub branch protection must retain every reviewed safeguard")
    approvals = protection["required_approvals"]
    if isinstance(approvals, bool) or not isinstance(approvals, int) or not 1 <= approvals <= 6:
        raise ModelError("GitHub required approvals must be from 1 through 6")
    required_checks = _strings(protection["required_checks"], "required GitHub check")
    if tuple(sorted(required_checks)) != required_checks:
        raise ModelError("required GitHub checks must be sorted")

    raw_labels = value["labels"]
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ModelError("GitHub label catalog is empty")
    labels: list[GovernanceLabel] = []
    for index, raw in enumerate(raw_labels):
        item = _exact(raw, {"name", "color", "description"}, f"GitHub label {index}")
        if (
            not isinstance(item["name"], str)
            or not item["name"].strip()
            or not isinstance(item["description"], str)
            or not item["description"].strip()
            or not isinstance(item["color"], str)
            or LABEL_COLOR.fullmatch(item["color"]) is None
        ):
            raise ModelError(f"GitHub label {index}: malformed label")
        labels.append(GovernanceLabel(item["name"], item["color"], item["description"]))
    require_unique([label.name for label in labels], "GitHub label name")
    if [label.name for label in labels] != sorted(label.name for label in labels):
        raise ModelError("GitHub labels must be sorted")

    automation = _exact(
        value["automation"],
        {"runner", "development_image", "dependency_update_day", "full_verification_cron", "actions"},
        "GitHub automation",
    )
    if automation["runner"] != "ubuntu-24.04":
        raise ModelError("GitHub automation must use the reviewed runner")
    if automation["development_image"] != "hyperflux-next-dev:ci":
        raise ModelError("GitHub automation development image name drifted")
    if automation["dependency_update_day"] not in {"monday", "tuesday", "wednesday", "thursday", "friday"}:
        raise ModelError("GitHub dependency update day is unsupported")
    if automation["full_verification_cron"] != "17 3 * * 1":
        raise ModelError("GitHub full-verification cadence drifted")
    raw_actions = automation["actions"]
    if not isinstance(raw_actions, list):
        raise ModelError("GitHub actions must be an array")
    actions: list[ActionPin] = []
    for index, raw in enumerate(raw_actions):
        item = _exact(
            raw,
            {"id", "repository", "version", "commit", "license_expression"},
            f"GitHub action {index}",
        )
        if (
            item["id"] not in ACTION_IDS
            or ACTION_REPOSITORIES.get(item["id"]) != item["repository"]
            or not isinstance(item["version"], str)
            or VERSION.fullmatch(item["version"]) is None
            or not isinstance(item["commit"], str)
            or COMMIT.fullmatch(item["commit"]) is None
            or item["license_expression"] not in {"MIT", "Apache-2.0"}
        ):
            raise ModelError(f"GitHub action {index}: unreviewed action identity or pin")
        actions.append(
            ActionPin(
                item["id"],
                item["repository"],
                item["version"],
                item["commit"],
                item["license_expression"],
            )
        )
    require_unique([action.id for action in actions], "GitHub action id")
    if {action.id for action in actions} != ACTION_IDS:
        raise ModelError("GitHub action inventory is incomplete")
    if [action.id for action in actions] != sorted(action.id for action in actions):
        raise ModelError("GitHub actions must be sorted by id")

    return GitHubGovernance(
        owner=repository["owner"],
        repository=repository["name"],
        default_branch=repository["default_branch"],
        remote_state=repository["remote_state"],
        planned_pages_source=repository["planned_pages_source"],
        default_owners=default_owners,
        ownership_rules=tuple(rules),
        required_checks=required_checks,
        required_approvals=approvals,
        labels=tuple(labels),
        runner=automation["runner"],
        development_image=automation["development_image"],
        dependency_update_day=automation["dependency_update_day"],
        full_verification_cron=automation["full_verification_cron"],
        actions=tuple(actions),
    )
