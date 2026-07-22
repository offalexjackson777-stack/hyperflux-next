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
    "collaboration",
    "security_posture",
    "service_evaluations",
    "ownership",
    "protection",
    "labels",
    "automation",
}
ACTION_IDS = {
    "build-push",
    "cache",
    "checkout",
    "configure-pages",
    "codeql",
    "dependency-review",
    "deploy-pages",
    "setup-buildx",
    "upload-artifact",
    "upload-pages-artifact",
}
ACTION_REPOSITORIES = {
    "build-push": "docker/build-push-action",
    "cache": "actions/cache",
    "checkout": "actions/checkout",
    "configure-pages": "actions/configure-pages",
    "codeql": "github/codeql-action",
    "dependency-review": "actions/dependency-review-action",
    "deploy-pages": "actions/deploy-pages",
    "setup-buildx": "docker/setup-buildx-action",
    "upload-artifact": "actions/upload-artifact",
    "upload-pages-artifact": "actions/upload-pages-artifact",
}
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v[0-9]+(?:\.[0-9]+){0,2}$")
OWNER = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
LABEL_COLOR = re.compile(r"^[0-9a-f]{6}$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DISCUSSION_FORMATS = {
    "announcements": "announcement",
    "hardware-qualification": "discussion",
    "help": "q-and-a",
    "ideas": "discussion",
}
PROJECT_FIELD_IDS = {
    "area",
    "evidence-level",
    "release-gate",
    "priority",
    "qualification-state",
    "target-date",
}
SERVICE_DECISIONS = {
    "codecov": "not-selected",
    "dependabot": "preferred-native",
    "openssf-scorecard": "deferred",
    "renovate": "not-selected",
}
JOB_SUMMARY_SECTIONS = (
    "source-revision",
    "affected-domains",
    "selection",
    "failures",
    "timings",
    "generated-freshness",
    "performance-budgets",
    "release-gate-impact",
)


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
class DiscussionCategory:
    id: str
    name: str
    format: str
    description: str


@dataclass(frozen=True)
class ProjectView:
    id: str
    name: str
    layout: str
    group_by: str
    date_field: str


@dataclass(frozen=True)
class ProjectField:
    id: str
    name: str
    data_type: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class SecurityPlan:
    state: str
    activation_boundary: str
    data_boundary: str


@dataclass(frozen=True)
class ServiceEvaluation:
    id: str
    service: str
    decision: str
    benefits: tuple[str, ...]
    required_permissions: tuple[str, ...]
    privacy: str
    maintenance_cost: str
    rationale: str


@dataclass(frozen=True)
class GitHubGovernance:
    owner: str
    repository: str
    default_branch: str
    remote_state: str
    visibility: str
    description: str
    homepage: str
    social_preview_asset: str
    topics: tuple[str, ...]
    pages_source: str
    source_repository_authorized: bool
    pages_deployment_authorized: bool
    collaboration_features_authorized: bool
    product_publication_authorized: bool
    release_workflows_authorized: bool
    package_publication_authorized: bool
    tag_creation_authorized: bool
    hardware_ci_authorized: bool
    discussions: tuple[DiscussionCategory, ...]
    project_title: str
    project_views: tuple[ProjectView, ...]
    project_fields: tuple[ProjectField, ...]
    security_posture: tuple[tuple[str, SecurityPlan], ...]
    service_evaluations: tuple[ServiceEvaluation, ...]
    default_owners: tuple[str, ...]
    ownership_rules: tuple[OwnershipRule, ...]
    required_checks: tuple[str, ...]
    required_approvals: int
    labels: tuple[GovernanceLabel, ...]
    runner: str
    development_image: str
    dependency_update_day: str
    full_verification_cron: str
    job_summary_sections: tuple[str, ...]
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


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ModelError(f"{label}: expected a lowercase identifier")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{label}: expected non-empty text")
    return value


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
    if value["schema"] != "hyperflux-github-governance-v3":
        raise ModelError("unsupported GitHub governance schema")

    repository = _exact(
        value["repository"],
        {
            "owner",
            "name",
            "default_branch",
            "remote_state",
            "visibility",
            "description",
            "homepage",
            "social_preview_asset",
            "topics",
            "pages_source",
        },
        "GitHub repository",
    )
    expected_repository = {
        "owner": "offalexjackson777-stack",
        "name": "hyperflux-next",
        "default_branch": "main",
        "remote_state": "public-pre-release",
        "visibility": "public",
        "description": "Evidence-bound Linux support for devices paired through Razer HyperFlux V2. Unreleased.",
        "homepage": "https://offalexjackson777-stack.github.io/hyperflux-next/",
        "social_preview_asset": "docs/assets/social-preview.png",
        "topics": [
            "cpp",
            "device-driver",
            "evidence-based",
            "hardware-support",
            "hid",
            "hyperflux",
            "kernel-module",
            "linux",
            "openrazer",
            "openrgb",
            "polychromatic",
            "python",
            "razer",
            "rgb-lighting",
            "rust",
        ],
        "pages_source": "github-actions",
    }
    if repository != expected_repository:
        raise ModelError("GitHub repository identity or public pre-release metadata drifted")
    preview = root / repository["social_preview_asset"]
    if not preview.is_file() or preview.is_symlink():
        raise ModelError("GitHub social preview asset is unavailable")

    interlock = _exact(
        value["publication_interlock"],
        {
            "source_repository_authorized",
            "pages_deployment_authorized",
            "collaboration_features_authorized",
            "product_publication_authorized",
            "release_workflows_authorized",
            "package_publication_authorized",
            "tag_creation_authorized",
            "hardware_ci_authorized",
        },
        "GitHub publication interlock",
    )
    if (
        interlock["source_repository_authorized"] is not True
        or interlock["pages_deployment_authorized"] is not True
        or interlock["collaboration_features_authorized"] is not True
        or any(
            interlock[key]
            for key in (
                "product_publication_authorized",
                "release_workflows_authorized",
                "package_publication_authorized",
                "tag_creation_authorized",
                "hardware_ci_authorized",
            )
        )
    ):
        raise ModelError("GitHub public-source authorization or product-release interlocks drifted")

    collaboration = _exact(
        value["collaboration"], {"discussions", "project"}, "GitHub collaboration"
    )
    discussions_value = _exact(
        collaboration["discussions"],
        {"apply_authorized", "categories"},
        "GitHub Discussions plan",
    )
    if discussions_value["apply_authorized"] is not True:
        raise ModelError("GitHub Discussions application must remain authorized")
    raw_categories = discussions_value["categories"]
    if not isinstance(raw_categories, list):
        raise ModelError("GitHub Discussions categories must be an array")
    discussions: list[DiscussionCategory] = []
    for index, raw in enumerate(raw_categories):
        item = _exact(
            raw,
            {"id", "name", "format", "description"},
            f"GitHub Discussions category {index}",
        )
        identifier = _identifier(item["id"], f"GitHub Discussions category {index} id")
        if DISCUSSION_FORMATS.get(identifier) != item["format"]:
            raise ModelError(f"GitHub Discussions category {identifier}: unreviewed format")
        discussions.append(
            DiscussionCategory(
                identifier,
                _text(item["name"], f"GitHub Discussions category {identifier} name"),
                item["format"],
                _text(
                    item["description"],
                    f"GitHub Discussions category {identifier} description",
                ),
            )
        )
    require_unique([category.id for category in discussions], "GitHub Discussions category id")
    if {category.id for category in discussions} != set(DISCUSSION_FORMATS):
        raise ModelError("GitHub Discussions category plan is incomplete")

    project = _exact(
        collaboration["project"],
        {"apply_authorized", "title", "views", "fields"},
        "GitHub Project plan",
    )
    if project["apply_authorized"] is not True:
        raise ModelError("GitHub Project application must remain authorized")
    project_title = _text(project["title"], "GitHub Project title")
    raw_fields = project["fields"]
    if not isinstance(raw_fields, list):
        raise ModelError("GitHub Project fields must be an array")
    project_fields: list[ProjectField] = []
    for index, raw in enumerate(raw_fields):
        item = _exact(
            raw,
            {"id", "name", "data_type", "options"},
            f"GitHub Project field {index}",
        )
        identifier = _identifier(item["id"], f"GitHub Project field {index} id")
        options_value = item["options"]
        if not isinstance(options_value, list):
            raise ModelError(f"GitHub Project field {identifier}: options must be an array")
        options = tuple(options_value)
        if any(not isinstance(option, str) or not option.strip() for option in options):
            raise ModelError(f"GitHub Project field {identifier}: malformed option")
        require_unique(options, f"GitHub Project field {identifier} option")
        if item["data_type"] == "single-select" and not options:
            raise ModelError(f"GitHub Project field {identifier}: select options are empty")
        if item["data_type"] == "date" and options:
            raise ModelError(f"GitHub Project field {identifier}: date fields cannot have options")
        if item["data_type"] not in {"single-select", "date"}:
            raise ModelError(f"GitHub Project field {identifier}: unsupported data type")
        project_fields.append(
            ProjectField(
                identifier,
                _text(item["name"], f"GitHub Project field {identifier} name"),
                item["data_type"],
                options,
            )
        )
    require_unique([field.id for field in project_fields], "GitHub Project field id")
    if {field.id for field in project_fields} != PROJECT_FIELD_IDS:
        raise ModelError("GitHub Project field plan is incomplete")
    field_types = {field.id: field.data_type for field in project_fields}

    raw_views = project["views"]
    if not isinstance(raw_views, list):
        raise ModelError("GitHub Project views must be an array")
    project_views: list[ProjectView] = []
    for index, raw in enumerate(raw_views):
        item = _exact(
            raw,
            {"id", "name", "layout", "group_by", "date_field"},
            f"GitHub Project view {index}",
        )
        identifier = _identifier(item["id"], f"GitHub Project view {index} id")
        if item["layout"] not in {"table", "board", "roadmap"}:
            raise ModelError(f"GitHub Project view {identifier}: unsupported layout")
        if item["group_by"] != "none" and item["group_by"] not in field_types:
            raise ModelError(f"GitHub Project view {identifier}: unknown grouping field")
        if item["date_field"] != "none" and field_types.get(item["date_field"]) != "date":
            raise ModelError(f"GitHub Project view {identifier}: unknown date field")
        project_views.append(
            ProjectView(
                identifier,
                _text(item["name"], f"GitHub Project view {identifier} name"),
                item["layout"],
                item["group_by"],
                item["date_field"],
            )
        )
    require_unique([view.id for view in project_views], "GitHub Project view id")
    if {view.layout for view in project_views} != {"table", "board", "roadmap"}:
        raise ModelError("GitHub Project must retain table, board, and roadmap views")

    security_value = _exact(
        value["security_posture"],
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
        "GitHub security posture",
    )
    expected_security = {
        "private_vulnerability_reporting": ("required-enabled", "public-source-pre-release"),
        "dependency_graph": ("required-enabled", "public-source-pre-release"),
        "dependabot_security_updates": ("required-enabled", "public-source-pre-release"),
        "secret_scanning": ("required-when-available", "public-source-pre-release"),
        "secret_scanning_push_protection": ("required-when-available", "public-source-pre-release"),
        "code_scanning": ("required-enabled", "public-source-pre-release"),
        "source_sbom": ("implemented", "current-generation"),
        "artifact_attestations": ("deferred", "after-release-workflow-authorization"),
    }
    security_posture: list[tuple[str, SecurityPlan]] = []
    for identifier, (expected_state, expected_boundary) in expected_security.items():
        item = _exact(
            security_value[identifier],
            {"state", "activation_boundary", "data_boundary"},
            f"GitHub security plan {identifier}",
        )
        if (item["state"], item["activation_boundary"]) != (
            expected_state,
            expected_boundary,
        ):
            raise ModelError(f"GitHub security plan {identifier}: reviewed boundary drifted")
        security_posture.append(
            (
                identifier,
                SecurityPlan(
                    item["state"],
                    item["activation_boundary"],
                    _text(item["data_boundary"], f"GitHub security plan {identifier} data boundary"),
                ),
            )
        )

    raw_evaluations = value["service_evaluations"]
    if not isinstance(raw_evaluations, list):
        raise ModelError("GitHub service evaluations must be an array")
    service_evaluations: list[ServiceEvaluation] = []
    for index, raw in enumerate(raw_evaluations):
        item = _exact(
            raw,
            {
                "id",
                "service",
                "decision",
                "benefits",
                "required_permissions",
                "privacy",
                "maintenance_cost",
                "rationale",
            },
            f"GitHub service evaluation {index}",
        )
        identifier = _identifier(item["id"], f"GitHub service evaluation {index} id")
        if SERVICE_DECISIONS.get(identifier) != item["decision"]:
            raise ModelError(f"GitHub service evaluation {identifier}: unreviewed decision")
        if item["maintenance_cost"] not in {"low", "medium", "high"}:
            raise ModelError(f"GitHub service evaluation {identifier}: unsupported maintenance cost")
        benefits = _strings(item["benefits"], f"GitHub service evaluation {identifier} benefits")
        permissions = _strings(
            item["required_permissions"],
            f"GitHub service evaluation {identifier} permissions",
        )
        service_evaluations.append(
            ServiceEvaluation(
                identifier,
                _text(item["service"], f"GitHub service evaluation {identifier} service"),
                item["decision"],
                benefits,
                permissions,
                _text(item["privacy"], f"GitHub service evaluation {identifier} privacy"),
                item["maintenance_cost"],
                _text(item["rationale"], f"GitHub service evaluation {identifier} rationale"),
            )
        )
    require_unique([item.id for item in service_evaluations], "GitHub service evaluation id")
    if [item.id for item in service_evaluations] != sorted(SERVICE_DECISIONS):
        raise ModelError("GitHub service evaluations must be complete and sorted")

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
            "prevent_deletions",
            "prevent_force_pushes",
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
            "prevent_deletions",
            "prevent_force_pushes",
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
        {
            "runner",
            "development_image",
            "dependency_update_day",
            "full_verification_cron",
            "job_summary",
            "actions",
        },
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
    summary = _exact(
        automation["job_summary"], {"enabled", "sections"}, "GitHub Actions summary"
    )
    if summary["enabled"] is not True or tuple(summary["sections"]) != JOB_SUMMARY_SECTIONS:
        raise ModelError("GitHub Actions summary contract is incomplete or reordered")
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
        visibility=repository["visibility"],
        description=repository["description"],
        homepage=repository["homepage"],
        social_preview_asset=repository["social_preview_asset"],
        topics=tuple(repository["topics"]),
        pages_source=repository["pages_source"],
        source_repository_authorized=interlock["source_repository_authorized"],
        pages_deployment_authorized=interlock["pages_deployment_authorized"],
        collaboration_features_authorized=interlock[
            "collaboration_features_authorized"
        ],
        product_publication_authorized=interlock["product_publication_authorized"],
        release_workflows_authorized=interlock["release_workflows_authorized"],
        package_publication_authorized=interlock["package_publication_authorized"],
        tag_creation_authorized=interlock["tag_creation_authorized"],
        hardware_ci_authorized=interlock["hardware_ci_authorized"],
        discussions=tuple(discussions),
        project_title=project_title,
        project_views=tuple(project_views),
        project_fields=tuple(project_fields),
        security_posture=tuple(security_posture),
        service_evaluations=tuple(service_evaluations),
        default_owners=default_owners,
        ownership_rules=tuple(rules),
        required_checks=required_checks,
        required_approvals=approvals,
        labels=tuple(labels),
        runner=automation["runner"],
        development_image=automation["development_image"],
        dependency_update_day=automation["dependency_update_day"],
        full_verification_cron=automation["full_verification_cron"],
        job_summary_sections=JOB_SUMMARY_SECTIONS,
        actions=tuple(actions),
    )
