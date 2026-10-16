# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Iterator
from urllib.parse import unquote, urljoin

from .model import (
    ModelError,
    load_foundation,
    load_json,
    repository_files,
    require_unique,
    sha256_file,
)
from .assurance import load_design_coverage
from .coverage import load_rust_coverage_policy
from .arch_update_policy import verify_arch_openrazer_update_policy
from .atlas import load_repository_atlas
from .development import load_development_environment
from .documentation_preview import (
    RELEASE_EVIDENCE_ENVIRONMENT,
    RELEASE_EVIDENCE_MODES,
    load_documentation_render_audit,
)
from .errors import load_error_catalog
from .formal_model import load_formal_model, run_formal_model
from .governance import load_github_governance
from .integrations import load_integration_catalog
from .install import load_install_manifest
from .knowledge import compiled_knowledge_catalog, load_knowledge_inputs
from .linux_runtime import load_linux_runtime
from .licensing import verify_licensing_policy
from .local_companion import load_local_companion
from .owner_reports import load_owner_report_policy, load_owner_reports
from .native_command import (
    load_native_command_catalog,
    load_native_command_policy,
    transformed_native_command_catalog,
)
from .native_control import (
    load_native_control_policy,
    load_native_method_inventory,
    transformed_native_inventory,
)
from .native_probe import (
    c_function_source,
    load_native_probe_catalog,
    load_native_probe_policy,
    transformed_native_probe_catalog,
)
from .openrazer import (
    effective_event_regex,
    extract_openrazer_catalog,
    load_imported_metadata,
    transformed_metadata,
    verify_global_event_file_matcher,
)
from .openrazer_native_route import (
    load_native_deployment,
    load_native_route_catalog,
    prepare_openrazer_native_source,
    transformed_native_route_catalog,
)
from .openrazer_version_matrix import (
    load_openrazer_version_compatibility,
    load_openrazer_version_matrix,
)
from .openrgb import extract_openrgb_catalog
from .openrgb_native_route import prepare_openrgb_native_source
from .package_pipeline import StageResult, build_artifacts, load_artifact_set, stage_rootfs
from .distribution_package import build_distribution_package
from .performance import (
    load_performance_budgets,
    verify_package_performance_budgets,
    verify_static_performance_budgets,
)
from .protected_behavior import load_protected_behavior
from .quality import load_quality_audit
from .render import rendered_binary_files, rendered_files
from .profiles import load_profile_inputs
from .publication_privacy import verify_publication_privacy
from .protocol import load_protocol_catalog
from .release import load_release_gates
from .supply_chain import load_dependency_inventory
from .testgraph import TestNode, load_test_catalog
from .toolchains import toolchain_environment, verify_current_toolchain


FUTURE_MTIME_TOLERANCE_SECONDS = 5.0
INSTALLED_SCHEMA_ROOT = Path("/usr/share/hyperflux-next/schemas")

_BYTE_FROZEN_WIRE = "byte-frozen: canonical wire bytes compared by protocol tests"
_BYTE_FROZEN_SERDE = "byte-frozen: golden of serde output compared byte for byte"
_BYTE_FROZEN_VIEW = "byte-frozen: Rust qualification view golden; the view rejects unknown keys"
_FOREIGN_FORMAT = "foreign format: read by an external tool that defines its own keys"
_NO_SCHEMA_GENERATED = "generated output with no repository schema yet"

# Tracked JSON files that may omit "$schema". Every other tracked JSON file
# must declare one, so a new file cannot skip schema coverage silently.
SCHEMA_DECLARATION_EXEMPTIONS: dict[str, str] = {
    ".devcontainer/devcontainer.json": _FOREIGN_FORMAT,
    "apps/device-qualification/tests/fixtures/rust-qualified-mouse.json": _BYTE_FROZEN_VIEW,
    "apps/device-qualification/tests/fixtures/rust-qualified-pair.json": _BYTE_FROZEN_VIEW,
    "apps/device-qualification/tests/fixtures/rust-qualified-report.json": _BYTE_FROZEN_VIEW,
    "assurance/generated/hyperflux-next.spdx.json": _FOREIGN_FORMAT,
    "governance/generated/github-experience-plan.json": _NO_SCHEMA_GENERATED,
    "governance/generated/github-labels.json": _NO_SCHEMA_GENERATED,
    "governance/generated/github-protection-plan.json": _NO_SCHEMA_GENERATED,
    "packaging/generated/install-plan.json": _NO_SCHEMA_GENERATED,
    **{
        f"protocol/v{version}/fixtures/transaction-request-canonical.json": _BYTE_FROZEN_WIRE
        for version in range(1, 7)
    },
    "sdk/cpp/vendor/manifest.json": "no repository schema yet for the vendored dependency manifest",
    "spec/catalog/integrations/catalog.json": (
        "generated output; the integration catalog schema covers the source, not this view"
    ),
    "spec/catalog/profiles/catalog.json": _NO_SCHEMA_GENERATED,
    "tests/fixtures/cli/hyperfluxctl-needs-attention.json": _BYTE_FROZEN_SERDE,
    "tests/fixtures/cli/hyperfluxctl-ready.json": _BYTE_FROZEN_SERDE,
    "tests/fixtures/generated/profile-compositions.json": _NO_SCHEMA_GENERATED,
    **{
        f"tests/fixtures/protocol/{name}.json": _BYTE_FROZEN_WIRE
        for name in (
            "v1/lease-conflict-response",
            "v1/negotiate-request",
            "v1/snapshot-response",
            "v2/negotiate-request",
            "v2/snapshot-response",
            "v2/static-transaction-request",
            "v3/negotiate-request",
            "v3/off-transaction-request",
            "v3/snapshot-response",
            "v4/negotiate-request",
            "v4/snapshot-response",
            "v5/integration-view-request",
            "v5/integration-view-response",
            "v5/negotiate-request",
            "v5/snapshot-response",
            "v6/negotiate-request",
            "v6/snapshot-response",
            "v7/native-enumerate-request",
            "v7/native-enumerate-response",
            "v7/native-transact-request",
            "v7/native-transact-response",
            "v7/native-watch-request",
            "v7/native-watch-response",
            "v7/negotiate-request",
            "v7/snapshot-response",
        )
    },
    "toolchains/documentation-render/package-lock.json": _FOREIGN_FORMAT,
    "toolchains/documentation-render/package.json": _FOREIGN_FORMAT,
}

from .verify_compiled import _check_build_cache_clock, _run_command


def _check_constitution(constitution: dict) -> None:
    if constitution.get("schema") != "hyperflux-architecture-constitution-v1":
        raise ModelError("unsupported architecture constitution schema")
    invariant_ids = [entry["id"] for entry in constitution["invariants"]]
    require_unique(invariant_ids, "architecture invariant id")
    component_ids = [entry["id"] for entry in constitution["components"]]
    require_unique(component_ids, "architecture component id")
    if constitution["publication_interlock"].get("remote_repository_created") is not True:
        raise ModelError("public-source phase requires the remote repository authority")
    if constitution.get("phase") != "public-arch-release":
        raise ModelError("architecture phase must identify the published Arch release")
    if constitution["publication_interlock"].get("publication_authorized") is not True:
        raise ModelError("published Arch source must record explicit publication authorization")


def _check_generated(root: Path) -> None:
    for path, expected in rendered_files(root).items():
        if not path.is_file():
            raise ModelError(f"missing generated file: {path.relative_to(root)}")
        if path.read_text(encoding="utf-8") != expected:
            raise ModelError(f"stale generated file: {path.relative_to(root)}; run ./hfx generate")
    for path, expected in rendered_binary_files(root).items():
        if not path.is_file():
            raise ModelError(f"missing generated file: {path.relative_to(root)}")
        if path.read_bytes() != expected:
            raise ModelError(f"stale generated file: {path.relative_to(root)}; run ./hfx generate")


def _check_repository_paths(root: Path) -> None:
    verify_publication_privacy(root)
    absolute_home = re.compile(r"/home/[A-Za-z0-9_.-]+/")
    for path in repository_files(root):
        if path.suffix in {".pyc", ".png", ".jpg", ".zst"} or path.name == "LICENSE":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if absolute_home.search(text):
            raise ModelError(f"private absolute path found in {path.relative_to(root)}")


def _check_profile_contract(root: Path) -> None:
    load_profile_inputs(root)


def _check_integration_contract(root: Path) -> None:
    load_integration_catalog(root)
    load_imported_metadata(root)
    load_native_control_policy(root)
    load_native_method_inventory(root)
    load_native_probe_policy(root)
    load_native_probe_catalog(root)
    load_native_command_policy(root)
    load_native_command_catalog(root)
    load_openrazer_version_matrix(root)
    load_openrazer_version_compatibility(root)


def _check_toolchain(root: Path, _node: TestNode) -> None:
    _check_build_cache_clock(root)
    verify_current_toolchain(root)


def _check_schema_contracts(root: Path) -> None:
    schema_ids: list[str] = []
    for path in sorted((root / "schemas").glob("*.schema.json")):
        schema = load_json(path)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ModelError(f"{path.relative_to(root)}: unsupported JSON Schema draft")
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("https://hyperflux.dev/schemas/"):
            raise ModelError(f"{path.relative_to(root)}: invalid canonical schema id")
        schema_ids.append(schema_id)
    require_unique(schema_ids, "JSON Schema id")

    documents = {
        path.relative_to(root).as_posix(): load_json(path)
        for path in repository_files(root)
        if path.suffix == ".json"
    }
    _check_schema_declarations(documents)
    _check_schema_references(
        {
            relative: value
            for relative, value in documents.items()
            if relative.endswith(".schema.json")
        }
    )
    for relative, value in documents.items():
        schema_reference = value.get("$schema")
        if not isinstance(schema_reference, str) or schema_reference.startswith("https://"):
            continue
        target = _resolve_schema_reference(root, root / relative, schema_reference)
        if target is None or not target.is_file():
            raise ModelError(f"{relative}: missing schema reference {schema_reference}")

    scenario_schema = load_json(root / "schemas" / "simulator-scenario.schema.json")
    event_limit = scenario_schema["properties"]["events"]["maxItems"]
    if event_limit != 4096:
        raise ModelError("simulator scenario event limit must remain 4096")
    replay = load_json(root / "tests" / "fixtures" / "replay" / "qualified-lifecycle-v1.json")
    provenance = replay.get("provenance", {})
    if provenance.get("source") != "sanitized-replay":
        raise ModelError("committed replay fixture must identify sanitized-replay provenance")
    if provenance.get("hardware_claim_authority") is not False:
        raise ModelError("replay fixture must not claim hardware authority")
    if provenance.get("private_identifiers_exported") is not False:
        raise ModelError("replay fixture must declare zero private identifiers")
    if len(replay.get("events", [])) > event_limit:
        raise ModelError("replay fixture exceeds the bounded event limit")
    load_test_catalog(root)
    load_linux_runtime(root)
    load_install_manifest(root)


def _check_schema_declarations(
    documents: dict[str, dict],
    exemptions: dict[str, str] = SCHEMA_DECLARATION_EXEMPTIONS,
) -> None:
    """Every JSON document declares $schema unless it is on the exemption list."""
    for relative, reason in sorted(exemptions.items()):
        if not reason.strip():
            raise ModelError(f"{relative}: schema declaration exemption needs a reason")
        if relative not in documents:
            raise ModelError(f"{relative}: schema declaration exemption names no tracked JSON file")
        if "$schema" in documents[relative]:
            raise ModelError(f"{relative}: declares $schema; remove its schema declaration exemption")
    undeclared = sorted(
        relative
        for relative, value in documents.items()
        if "$schema" not in value and relative not in exemptions
    )
    if undeclared:
        raise ModelError(
            "JSON files must declare $schema or carry a reasoned exemption in "
            f"SCHEMA_DECLARATION_EXEMPTIONS: {', '.join(undeclared)}"
        )


_SCHEMA_MAP_KEYWORDS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)
_LITERAL_KEYWORDS = frozenset({"const", "default", "enum", "examples"})


def _check_schema_references(schemas: dict[str, dict]) -> None:
    """Every $ref in every schema names a registered $id and an existing pointer.

    References resolve against the referring schema's $id, as a standard
    registry would, so a relative reference that lands on an unregistered
    URL is refused here instead of failing later in a validator.
    """
    registry: dict[str, dict] = {}
    for relative, schema in sorted(schemas.items()):
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            continue
        if schema_id in registry:
            raise ModelError(f"{relative}: duplicate JSON Schema id {schema_id}")
        registry[schema_id] = schema
    for relative, schema in sorted(schemas.items()):
        base = schema.get("$id")
        for location, reference in _schema_references(schema, "#", top=True):
            label = f"{relative} {location}"
            if not isinstance(reference, str):
                raise ModelError(f"{label}: $ref must be a string")
            if reference.startswith("#"):
                target, fragment = schema, reference[1:]
            elif not isinstance(base, str):
                raise ModelError(f"{label}: $ref {reference} needs a schema $id to resolve against")
            else:
                uri, _, fragment = urljoin(base, reference).partition("#")
                if uri not in registry:
                    raise ModelError(f"{label}: $ref {reference} names no registered schema $id")
                target = registry[uri]
            if not _json_pointer_resolves(target, fragment):
                raise ModelError(f"{label}: $ref {reference} points at nothing")


def _schema_references(
    node: Any,
    location: str,
    *,
    top: bool = False,
) -> Iterator[tuple[str, Any]]:
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _schema_references(item, f"{location}/{index}")
        return
    if not isinstance(node, dict):
        return
    if "$id" in node and not top:
        raise ModelError(f"{location}: nested $id is not supported")
    for key, value in node.items():
        child = f"{location}/{_json_pointer_escape(key)}"
        if key == "$ref":
            yield location, value
        elif key in _LITERAL_KEYWORDS:
            continue
        elif key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for name, subschema in value.items():
                yield from _schema_references(subschema, f"{child}/{_json_pointer_escape(name)}")
        else:
            yield from _schema_references(value, child)


def _json_pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _json_pointer_resolves(document: Any, fragment: str) -> bool:
    if fragment == "":
        return True
    if not fragment.startswith("/"):
        # Plain-name ($anchor) fragments are not used by any repository schema.
        return False
    node = document
    for raw in unquote(fragment)[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return False
    return True


def _resolve_schema_reference(
    root: Path,
    document: Path,
    schema_reference: str,
) -> Path | None:
    reference = Path(schema_reference)
    if not reference.is_absolute():
        return (document.parent / reference).resolve()
    if reference.parent != INSTALLED_SCHEMA_ROOT or reference.name in {"", ".", ".."}:
        return None
    return root / "schemas" / reference.name


def _check_protocol_contract(root: Path) -> None:
    load_protocol_catalog(root)


def _check_error_contract(root: Path) -> None:
    load_error_catalog(root)


def _run_foundation_contracts(root: Path, _node: TestNode) -> None:
    constitution = load_foundation(root)
    _check_constitution(constitution)
    load_design_coverage(root)


def _run_schema_contracts(root: Path, _node: TestNode) -> None:
    _check_schema_contracts(root)
    load_local_companion(root)


def _run_profile_contracts(root: Path, _node: TestNode) -> None:
    _check_profile_contract(root)


def _run_integration_contracts(root: Path, _node: TestNode) -> None:
    _check_integration_contract(root)


def _run_protocol_contracts(root: Path, _node: TestNode) -> None:
    _check_protocol_contract(root)


def _run_error_contracts(root: Path, _node: TestNode) -> None:
    _check_error_contract(root)


def _run_generated_freshness(root: Path, _node: TestNode) -> None:
    _check_generated(root)


def _run_privacy_boundary(root: Path, _node: TestNode) -> None:
    _check_repository_paths(root)


def _run_toolchain_contract(root: Path, node: TestNode) -> None:
    _check_toolchain(root, node)


def _run_development_environment_contracts(root: Path, _node: TestNode) -> None:
    load_development_environment(root)


def _run_governance_contracts(root: Path, _node: TestNode) -> None:
    load_github_governance(root)


def release_evidence_mode() -> str:
    """Return how strictly the committed documentation render record is checked.

    Ordinary pull requests check its structure; release pull requests into the
    default branch require it to describe the exact documents being released.
    """
    mode = os.environ.get(RELEASE_EVIDENCE_ENVIRONMENT, "structure")
    if mode not in RELEASE_EVIDENCE_MODES:
        raise ModelError(
            f"{RELEASE_EVIDENCE_ENVIRONMENT} must be one of: "
            + ", ".join(RELEASE_EVIDENCE_MODES)
        )
    return mode


def _run_repository_documentation_contracts(root: Path, node: TestNode) -> None:
    if release_evidence_mode() == "required":
        load_documentation_render_audit(root, release_evidence=True)
    _run_command(
        root,
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_repository_experience",
            "tests.test_documentation_evidence",
            "tests.test_journey",
            "-v",
        ],
        "repository documentation contracts",
        node.timeout_seconds,
    )


def _run_device_qualification_console_contracts(root: Path, node: TestNode) -> None:
    _run_command(
        root,
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_device_qualification_console",
            "-v",
        ],
        "device qualification console contracts",
        node.timeout_seconds,
    )


def _run_assurance_contracts(root: Path, _node: TestNode) -> None:
    load_dependency_inventory(root)
    verify_licensing_policy(root)
    load_release_gates(root)
    load_protected_behavior(root)
    metrics = load_performance_budgets(root)
    verify_static_performance_budgets(root, metrics)
    load_rust_coverage_policy(root)
    load_quality_audit(root)
    load_owner_report_policy(root)
    load_owner_reports(root)


def _run_formal_model_contracts(root: Path, _node: TestNode) -> None:
    run_formal_model(load_formal_model(root))
