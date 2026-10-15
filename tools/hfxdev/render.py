# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .arch_repository import load_arch_repository_policy
from .assurance import load_design_coverage
from .atlas import load_repository_atlas
from .development import load_development_environment
from .errors import load_error_catalog
from .formal_model import load_formal_model, run_formal_model
from .generators.assurance import design_coverage_markdown
from .generators.atlas import collection_readme as atlas_collection_readme
from .generators.atlas import folder_readme as atlas_folder_readme
from .generators.atlas import markdown as atlas_markdown
from .generators.formal_model import formal_model_markdown
from .generators.governance import (
    bug_report as github_bug_report,
    codeowners as github_codeowners,
    codeql_workflow as github_codeql_workflow,
    dependency_review_workflow as github_dependency_review_workflow,
    dependabot as github_dependabot,
    device_support as github_device_support,
    documentation_report as github_documentation_report,
    experience_plan as github_experience_plan,
    feature_request as github_feature_request,
    full_verification_workflow as github_full_verification_workflow,
    issue_config as github_issue_config,
    labels_plan as github_labels_plan,
    markdown as github_governance_markdown,
    protection_plan as github_protection_plan,
    pull_request_template as github_pull_request_template,
    release_notes as github_release_notes,
    verification_workflow as github_verification_workflow,
)
from .generators.performance import performance_budgets_markdown
from .generators.release import release_gates_markdown
from .generators.readme import (
    compatibility_summary as readme_compatibility_summary,
    development_badge as readme_development_badge,
    license_badge as readme_license_badge,
    recommended_package as readme_recommended_package,
    release_status as readme_release_status,
    release_summary as readme_release_summary,
)
from .generators.social_preview import render_social_preview
from .generators.supply_chain import spdx_json, supply_chain_markdown
from .generators.domain import (
    cpp_json as domain_cpp_json,
    cpp_types,
    markdown as domain_markdown,
    python_types,
    rust_types,
)
from .generators.distributions import markdown as distribution_markdown
from .generators.development import (
    containerfile as development_containerfile,
    devcontainer as development_devcontainer,
    markdown as development_markdown,
)
from .generators.errors import (
    cpp_catalog as error_cpp_catalog,
    markdown as error_markdown,
    python_catalog as error_python_catalog,
    rust_catalog as error_rust_catalog,
)
from .generators.kernel_uapi import (
    c_header as kernel_uapi_c_header,
    markdown as kernel_uapi_markdown,
    rust_bindings as kernel_uapi_rust_bindings,
)
from .generators.licensing import markdown as licensing_markdown
from .generators.local_companion import markdown as local_companion_markdown
from .generators.knowledge import (
    compiled_json as knowledge_compiled_json,
    render_knowledge_markdown,
)
from .generators.linux_runtime import (
    default_bridge_configuration,
    desktop_bridge_proxy_service,
    desktop_bridge_socket,
    dkms_configuration,
    kernel_version_header,
    markdown as linux_runtime_markdown,
    package_environment,
    python_constants as linux_runtime_python_constants,
    python_version_module,
    rust_constants as linux_runtime_rust_constants,
    systemd_service,
    sysusers,
    tmpfiles,
    udev_rules,
)
from .generators.openrazer_native import (
    bridge_drop_in as openrazer_bridge_drop_in,
    driver_preflight_service as openrazer_driver_preflight_service,
    markdown as openrazer_native_markdown,
    relay_service as openrazer_native_relay_service,
    rust_constants as openrazer_native_rust_constants,
    modules_load as openrazer_native_modules_load,
    sysusers as openrazer_native_sysusers,
)
from .generators.profiles import (
    compiled_json as profile_compiled_json,
    cpp_catalog as profile_cpp_catalog,
    fixtures_json as profile_fixtures_json,
    kernel_receiver_table,
    markdown as profile_markdown,
    python_catalog as profile_python_catalog,
    rust_catalog as profile_rust_catalog,
)
from .generators.protocol import (
    cpp_json as protocol_cpp_json,
    cpp_types as protocol_cpp_types,
    markdown as protocol_markdown,
    python_types as protocol_python_types,
    rust_types as protocol_rust_types,
    rust_registry as protocol_rust_registry,
)
from .kernel_uapi import load_kernel_uapi_registry
from .knowledge import compiled_knowledge_catalog
from .integrations import compiled_catalog as compiled_integration_catalog
from .generators.integrations import (
    compiled_json as integration_compiled_json,
    cpp_catalog as integration_cpp_catalog,
    cpp_openrgb_source_validated_routes,
    markdown as integration_markdown,
    python_catalog as integration_python_catalog,
    python_openrazer_metadata,
    rust_catalog as integration_rust_catalog,
)
from .generators.install import compiled_plan as compiled_install_plan
from .generators.install import full_component_descriptor
from .generators.install import markdown as install_markdown
from .governance import load_github_governance
from .install import load_first_run_activation, load_install_manifest
from .distributions import load_distribution_catalog
from .documentation import load_document_catalog, replace_generated_blocks
from .model import load_foundation, load_json
from .linux_runtime import load_linux_runtime
from .licensing import load_licensing_policy
from .local_companion import load_local_companion
from .native_command import load_native_command_catalog
from .openrazer import load_imported_metadata
from .openrazer_native_route import (
    load_native_deployment,
    load_native_route_catalog,
)
from .source_validated import load_source_validated_catalog
from .openrazer_version_matrix import load_openrazer_version_compatibility
from .performance import load_performance_budgets
from .profiles import compiled_catalog, composition_fixtures
from .protocol import load_protocol_registry
from .release import load_release_context
from .public_readiness import public_readiness
from .supply_chain import load_dependency_inventory
from .testgraph import load_test_catalog, markdown as testgraph_markdown


GENERATED_NOTICE = "> Generated by `./hfx generate`. Do not edit manually.\n"


def architecture_markdown(
    constitution: dict[str, Any],
    profiles: dict[str, Any],
    source_validated: dict[str, Any],
) -> str:
    lines = [
        "# Architecture Constitution",
        "",
        GENERATED_NOTICE.rstrip(),
        "",
        f"Product phase: **{constitution['phase']}**",
        "",
        "## Direction Of Responsibility",
        "",
        "```mermaid",
        "flowchart TB",
    ]
    direction = constitution["direction"]
    for component in direction:
        lines.append(f'    {component}["{component.title()}"]')
    for left, right in zip(direction, direction[1:]):
        lines.append(f"    {left} --> {right}")
    official = profiles["official_receiver_scope"]
    devices = source_validated["devices"]
    openrazer = sum(
        device["integrations"]["openrazer"]["state"] == "source-validated"
        for device in devices
    )
    openrgb = sum(
        device["integrations"]["openrgb"]["state"] == "source-validated"
        for device in devices
    )
    operational = sum(
        device["operational_state"] == "source-validated" for device in devices
    )
    lines.extend(
        [
            "```",
            "",
            "## Receiver Authority Boundary",
            "",
            f"The generated receiver authority is exact `{official['vendor_id']:04X}:{official['receiver_product_id']:04X}` plus {len(official['entries'])} official child identities from `{official['snapshot_id']}`. It yields {operational} operational child routes: {openrazer} through merged OpenRazer source and {openrgb} through merged OpenRGB source. Application admission is independent, and hardware assurance is non-authorizing.",
            "",
            "Direct USB participates only in exact serial-fingerprint precedence. Bluetooth and unrelated USB devices remain outside HyperFlux authority. The complete per-device result is generated in [Supported Hardware](supported-hardware.md).",
            "",
            "## Invariants",
            "",
        ]
    )
    for invariant in constitution["invariants"]:
        lines.append(f"- `{invariant['id']}` {invariant['statement']}")
    lines.extend(["", "## Component Ownership", ""])
    for component in constitution["components"]:
        owns = ", ".join(component["owns"])
        forbidden = ", ".join(component["must_not_own"])
        lines.extend(
            [
                f"### {component['id'].replace('-', ' ').title()}",
                "",
                f"Owns: {owns}.",
                "",
                f"Must not own: {forbidden}.",
                "",
            ]
        )
    interlock = constitution["publication_interlock"]
    lines.extend(
        [
            "## Publication Interlock",
            "",
            f"Remote repository created: **{str(interlock['remote_repository_created']).lower()}**",
            "",
            f"Publication authorized: **{str(interlock['publication_authorized']).lower()}**",
            "",
            "Required gates:",
            "",
        ]
    )
    lines.extend(f"- `{gate}`" for gate in interlock["required_gate_ids"])
    return "\n".join(lines) + "\n"


def rendered_files(root: Path) -> dict[Path, str]:
    constitution = load_foundation(root)
    domain_catalog = load_json(root / "schemas" / "domain-catalog.json")
    profiles = compiled_catalog(root)
    knowledge = compiled_knowledge_catalog(root)
    profile_fixtures = composition_fixtures(root)
    protocol_registry = load_protocol_registry(root)
    protocol = protocol_registry.current
    errors = load_error_catalog(root)
    kernel_uapi = load_kernel_uapi_registry(root)
    integrations = compiled_integration_catalog(root)
    openrazer_metadata = load_imported_metadata(root)
    openrazer_userspace_compatibility = load_openrazer_version_compatibility(root)
    openrazer_command_catalog = load_native_command_catalog(root)
    openrazer_native = load_native_deployment(root)
    openrazer_native_routes = load_native_route_catalog(root)
    source_validated_devices = load_source_validated_catalog(root)
    test_catalog = load_test_catalog(root)
    linux_runtime = load_linux_runtime(root)
    distribution_catalog = load_distribution_catalog(root)
    first_run_activation = load_first_run_activation(root)
    design_coverage = load_design_coverage(root)
    dependency_inventory = load_dependency_inventory(root)
    release_context = load_release_context(root)
    performance_budgets = load_performance_budgets(root)
    formal_model = load_formal_model(root)
    formal_model_result = run_formal_model(formal_model)
    development_environment = load_development_environment(root)
    github_governance = load_github_governance(root)
    repository_atlas = load_repository_atlas(root)
    document_catalog = load_document_catalog(root)
    readiness = public_readiness(root)
    arch_repository = load_arch_repository_policy(root)
    local_companion = load_local_companion(root)
    licensing_policy = load_licensing_policy(root)
    readme_renderers = {
        "release-summary": lambda: readme_release_summary(readiness, arch_repository),
        "release-status": lambda: readme_release_status(readiness),
        "recommended-package": lambda: readme_recommended_package(
            distribution_catalog
        ),
        "compatibility-summary": lambda: readme_compatibility_summary(
            readiness, openrazer_native, source_validated_devices
        ),
    }
    hybrid_documents = {
        root / document.path: replace_generated_blocks(
            (root / document.path).read_text(encoding="utf-8"),
            document.generated_block_ids,
            {
                identifier: readme_renderers[identifier]
                for identifier in document.generated_block_ids
            },
        )
        for document in document_catalog.documents
        if document.mode == "hybrid"
    }
    files = {
        **hybrid_documents,
        root / "generated" / "public-readiness.json": json.dumps(
            readiness, indent=2, sort_keys=True
        )
        + "\n",
        root / "docs" / "assets" / "badge-state.svg": readme_development_badge(),
        root / "docs" / "assets" / "badge-license.svg": readme_license_badge(),
        root / ".devcontainer" / "Containerfile": development_containerfile(
            development_environment
        ),
        root / ".devcontainer" / "devcontainer.json": development_devcontainer(
            development_environment
        ),
        root / "docs" / "generated" / "development-environment.md": development_markdown(
            development_environment, integrations["upstreams"]
        ),
        root / "docs" / "generated" / "local-companion.md": local_companion_markdown(
            local_companion
        ),
        root / "docs" / "legal" / "licensing.md": licensing_markdown(
            licensing_policy
        ),
        root / "docs" / "generated" / "github-governance.md": github_governance_markdown(
            github_governance, arch_repository
        ),
        root / ".github" / "CODEOWNERS": github_codeowners(github_governance),
        root / ".github" / "pull_request_template.md": github_pull_request_template(),
        root / ".github" / "ISSUE_TEMPLATE" / "config.yml": github_issue_config(
            github_governance
        ),
        root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml": github_bug_report(
            github_governance
        ),
        root / ".github" / "ISSUE_TEMPLATE" / "device_support.yml": github_device_support(
            github_governance
        ),
        root / ".github" / "ISSUE_TEMPLATE" / "documentation.yml": github_documentation_report(
            github_governance
        ),
        root / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml": github_feature_request(
            github_governance
        ),
        root / ".github" / "dependabot.yml": github_dependabot(github_governance),
        root / ".github" / "release.yml": github_release_notes(github_governance),
        root / ".github" / "workflows" / "verification.yml": github_verification_workflow(
            github_governance
        ),
        root / ".github" / "workflows" / "full-verification.yml": github_full_verification_workflow(
            github_governance
        ),
        root / ".github" / "workflows" / "codeql.yml": github_codeql_workflow(
            github_governance
        ),
        root / ".github" / "workflows" / "dependency-review.yml": github_dependency_review_workflow(
            github_governance
        ),
        root / "governance" / "generated" / "github-protection-plan.json": github_protection_plan(
            github_governance
        ),
        root / "governance" / "generated" / "github-labels.json": github_labels_plan(
            github_governance
        ),
        root / "governance" / "generated" / "github-experience-plan.json": github_experience_plan(
            github_governance
        ),
        root / "docs" / "generated" / "architecture.md": architecture_markdown(
            constitution, profiles, source_validated_devices
        ),
        root / "docs" / "generated" / "repository-atlas.md": atlas_markdown(
            repository_atlas
        ),
        root / "docs" / "generated" / "design-coverage.md": design_coverage_markdown(
            design_coverage, release_context.target
        ),
        root / "docs" / "generated" / "formal-model.md": formal_model_markdown(
            formal_model, formal_model_result
        ),
        root / "docs" / "generated" / "performance-budgets.md": performance_budgets_markdown(
            performance_budgets
        ),
        root / "docs" / "generated" / "release-gates.md": release_gates_markdown(
            release_context
        ),
        root / "docs" / "generated" / "supply-chain.md": supply_chain_markdown(
            dependency_inventory
        ),
        root / "assurance" / "generated" / "hyperflux-next.spdx.json": spdx_json(
            dependency_inventory
        ),
        root / "docs" / "generated" / "domain-types.md": domain_markdown(domain_catalog),
        root / "crates" / "hfx-domain" / "src" / "generated.rs": rust_types(domain_catalog),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "domain_types.py": python_types(domain_catalog),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "domain_types.hpp": cpp_types(domain_catalog),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "domain_json.hpp": domain_cpp_json(domain_catalog),
        root / "generated" / "profiles" / "catalog.json": profile_compiled_json(profiles),
        root / "generated" / "knowledge" / "catalog.json": knowledge_compiled_json(
            knowledge
        ),
        root / "tests" / "fixtures" / "generated" / "profile-compositions.json": profile_fixtures_json(profile_fixtures),
        root / "docs" / "generated" / "supported-hardware.md": profile_markdown(
            profiles, source_validated_devices
        ),
        root / "docs" / "generated" / "device-knowledge.md": render_knowledge_markdown(
            knowledge
        ),
        root / "crates" / "hfx-profiles" / "src" / "generated.rs": profile_rust_catalog(profiles),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "profile_catalog.py": profile_python_catalog(profiles),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "profile_catalog.hpp": profile_cpp_catalog(profiles),
        root / "driver" / "kernel" / "generated" / "hyperflux_receiver_profiles.inc": kernel_receiver_table(profiles),
        root / "docs" / "generated" / "verification.md": testgraph_markdown(test_catalog),
        root / "docs" / "generated" / "bridge-protocol.md": protocol_markdown(protocol),
        root / "crates" / "hfx-protocol" / "src" / "generated.rs": protocol_rust_types(protocol),
        root / "crates" / "hfx-protocol" / "src" / "generated_versions.rs": protocol_rust_registry(protocol_registry),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "protocol_types.py": protocol_python_types(protocol),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "protocol_types.hpp": protocol_cpp_types(protocol),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "protocol_json.hpp": protocol_cpp_json(protocol),
        root / "docs" / "generated" / "error-catalog.md": error_markdown(errors),
        root / "crates" / "hfx-errors" / "src" / "generated.rs": error_rust_catalog(errors),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "error_catalog.py": error_python_catalog(errors),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "error_catalog.hpp": error_cpp_catalog(errors),
        root / "driver" / "kernel" / "uapi" / "hyperflux_next.h": kernel_uapi_c_header(kernel_uapi),
        root / "crates" / "hfx-kernel-transport" / "src" / "generated.rs": kernel_uapi_rust_bindings(kernel_uapi),
        root / "docs" / "generated" / "kernel-uapi.md": kernel_uapi_markdown(kernel_uapi),
        root / "generated" / "integrations" / "catalog.json": integration_compiled_json(integrations),
        root / "docs" / "generated" / "integrations.md": integration_markdown(integrations),
        root / "crates" / "hfx-integration-model" / "src" / "generated.rs": integration_rust_catalog(integrations),
        root
        / "crates"
        / "hfx-integration-model"
        / "src"
        / "openrazer_native_generated.rs": openrazer_native_rust_constants(
            openrazer_native
        ),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "integration_catalog.py": integration_python_catalog(integrations),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "openrazer_metadata.py": python_openrazer_metadata(openrazer_metadata),
        root / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "integration_catalog.hpp": integration_cpp_catalog(integrations),
        root
        / "integrations"
        / "openrgb"
        / "include"
        / "hyperflux"
        / "openrgb"
        / "generated_source_validated_routes.hpp": cpp_openrgb_source_validated_routes(
            source_validated_devices
        ),
        root / "crates" / "hfx-runtime" / "src" / "generated.rs": linux_runtime_rust_constants(linux_runtime),
        root / "sdk" / "python" / "hyperflux_sdk" / "generated" / "linux_runtime.py": linux_runtime_python_constants(linux_runtime),
        root / "driver" / "kernel" / "hyperflux-next-version.h": kernel_version_header(linux_runtime),
        root / "packaging" / "generated" / "dkms.conf": dkms_configuration(linux_runtime),
        root / "packaging" / "generated" / "hyperflux-next-bridge.service": systemd_service(
            linux_runtime
        ),
        root
        / "packaging"
        / "generated"
        / "hyperflux-next-bridge.service.d"
        / "50-full.conf": openrazer_bridge_drop_in(openrazer_native),
        root / "packaging" / "generated" / "full-component.json": full_component_descriptor(
            linux_runtime
        ),
        root
        / "packaging"
        / "generated"
        / linux_runtime.desktop_access.socket_unit: desktop_bridge_socket(linux_runtime),
        root
        / "packaging"
        / "generated"
        / linux_runtime.desktop_access.proxy_service_unit: desktop_bridge_proxy_service(
            linux_runtime
        ),
        root
        / "packaging"
        / "generated"
        / openrazer_native.relay.service_unit: openrazer_native_relay_service(
            openrazer_native, linux_runtime
        ),
        root
        / "packaging"
        / "generated"
        / openrazer_native.relay.preflight_service_unit: openrazer_driver_preflight_service(
            openrazer_native, linux_runtime
        ),
        root / "packaging" / "generated" / "openrazer.sysusers": openrazer_native_sysusers(
            openrazer_native
        ),
        root
        / "packaging"
        / "generated"
        / "hyperflux-next-openrazer.modules-load.conf": openrazer_native_modules_load(
            openrazer_native
        ),
        root / "packaging" / "generated" / "hyperflux-next.sysusers": sysusers(linux_runtime),
        root / "packaging" / "generated" / "hyperflux-next.tmpfiles": tmpfiles(linux_runtime),
        root / "packaging" / "generated" / "99-hyperflux-next.rules": udev_rules(linux_runtime),
        root / "packaging" / "generated" / "bridge.json": default_bridge_configuration(linux_runtime),
        root / "packaging" / "generated" / "runtime.env": package_environment(linux_runtime),
        root / "docs" / "generated" / "linux-runtime.md": linux_runtime_markdown(linux_runtime),
        root
        / "docs"
        / "generated"
        / "openrazer-native-deployment.md": openrazer_native_markdown(
            openrazer_native,
            linux_runtime,
            openrazer_userspace_compatibility,
            openrazer_command_catalog,
        ),
        root / "docs" / "generated" / "distributions.md": distribution_markdown(
            distribution_catalog, linux_runtime, first_run_activation
        ),
        root / "sdk" / "python" / "hyperflux_sdk" / "_version.py": python_version_module(
            linux_runtime, "GPL-2.0-or-later"
        ),
        root / "sdk" / "python" / "LICENSE": (root / "LICENSE").read_text(
            encoding="utf-8"
        ),
    }
    for version in protocol_registry.versions:
        suffix = f"v{version.version}"
        files[root / "crates" / "hfx-protocol" / "src" / f"generated_{suffix}.rs"] = (
            protocol_rust_types(version.catalog)
        )
        files[
            root
            / "sdk"
            / "python"
            / "hyperflux_sdk"
            / "generated"
            / f"protocol_{suffix}_types.py"
        ] = protocol_python_types(version.catalog)
        files[
            root
            / "sdk"
            / "cpp"
            / "include"
            / "hyperflux"
            / "generated"
            / f"protocol_{suffix}_types.hpp"
        ] = protocol_cpp_types(version.catalog, namespace=f"hyperflux::{suffix}")
        files[
            root
            / "sdk"
            / "cpp"
            / "include"
            / "hyperflux"
            / "generated"
            / f"protocol_{suffix}_json.hpp"
        ] = protocol_cpp_json(
            version.catalog,
            namespace=f"hyperflux::{suffix}",
            types_header=f"protocol_{suffix}_types.hpp",
        )
        files[root / "docs" / "generated" / f"bridge-protocol-{suffix}.md"] = (
            protocol_markdown(version.catalog, title=f"Bridge Protocol {suffix.upper()}")
        )
    for node in repository_atlas.nodes:
        readme_path = f"{node.path}/README.md"
        if readme_path in node.generated_files:
            files[root / readme_path] = atlas_folder_readme(
                repository_atlas,
                node,
                {record.path: record.title for record in document_catalog.documents},
            )
    node_paths = {node.path for node in repository_atlas.nodes}
    architecture_node = repository_atlas.by_id["architecture"]
    collection_readmes = sorted(
        path
        for path in architecture_node.generated_files
        if path.endswith("/README.md")
        and path.removesuffix("/README.md") not in node_paths
    )
    for relative in collection_readmes:
        collection = relative.removesuffix("/README.md")
        files[root / relative] = atlas_collection_readme(
            repository_atlas, collection
        )
    install_plan_path = root / "packaging" / "generated" / "install-plan.json"
    installation_document_path = root / "docs" / "generated" / "installation.md"
    install_manifest = load_install_manifest(
        root,
        projected_files=(*files, install_plan_path, installation_document_path),
    )
    files[install_plan_path] = compiled_install_plan(install_manifest, linux_runtime)
    files[installation_document_path] = install_markdown(
        install_manifest, linux_runtime, openrazer_native
    )
    return files


def rendered_binary_files(root: Path) -> dict[Path, bytes]:
    knowledge = compiled_knowledge_catalog(root)
    atlas = load_repository_atlas(root)
    test_catalog = load_test_catalog(root)
    return {
        root / "docs" / "assets" / "social-preview.png": render_social_preview(
            candidates=len(knowledge["candidates"]),
            subsystems=len(atlas.nodes),
            verification_nodes=len(test_catalog.nodes),
            hardware_writes=sum(node.writes_hardware for node in test_catalog.nodes),
        )
    }


def write_generated(root: Path) -> None:
    for path, content in rendered_files(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path, content in rendered_binary_files(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
