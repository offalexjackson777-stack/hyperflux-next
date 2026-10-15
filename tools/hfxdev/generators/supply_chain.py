# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import re
from typing import Any

from ..arch_repository import (
    ArchRepositoryPolicy,
    checksum_only_asset_names,
    signed_asset_names,
)
from ..supply_chain import DependencyInventory


def _spdx_id(kind: str, name: str, version: str = "") -> str:
    suffix = "-".join(part for part in (kind, name, version) if part)
    normalized = re.sub(r"[^A-Za-z0-9.-]+", "-", suffix).strip("-")
    return f"SPDXRef-{normalized}"


def _package(
    *,
    spdx_id: str,
    name: str,
    version: str,
    license_expression: str,
    download_location: str = "NOASSERTION",
    checksum: str | None = None,
    purl: str | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "SPDXID": spdx_id,
        "name": name,
        "versionInfo": version,
        "downloadLocation": download_location,
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": license_expression,
        "copyrightText": "NOASSERTION",
    }
    if checksum is not None:
        value["checksums"] = [{"algorithm": "SHA256", "checksumValue": checksum}]
    if purl is not None:
        value["externalRefs"] = [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl,
            }
        ]
    if comment is not None:
        value["comment"] = comment
    return value


def spdx_json(inventory: DependencyInventory) -> str:
    root_id = _spdx_id("Package", "hyperflux-next", inventory.workspace_version)
    packages: list[dict[str, Any]] = [
        _package(
            spdx_id=root_id,
            name="hyperflux-next",
            version=inventory.workspace_version,
            license_expression=inventory.repository_license,
            comment="Canonical source package described by this repository inventory.",
        )
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    ids_by_cargo_name: dict[str, str] = {}
    for package in inventory.workspace_packages:
        package_id = _spdx_id("Cargo", package.name, package.version)
        ids_by_cargo_name[package.name] = package_id
        packages.append(
            _package(
                spdx_id=package_id,
                name=package.name,
                version=package.version,
                license_expression=package.license_expression,
                purl=f"pkg:cargo/{package.name}@{package.version}",
                comment="HyperFlux workspace crate.",
            )
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": package_id,
            }
        )
    for package in inventory.rust_packages:
        package_id = _spdx_id("Cargo", package.name, package.version)
        ids_by_cargo_name[package.name] = package_id
        packages.append(
            _package(
                spdx_id=package_id,
                name=package.name,
                version=package.version,
                license_expression=package.license_expression,
                download_location="https://crates.io/",
                checksum=package.checksum,
                purl=f"pkg:cargo/{package.name}@{package.version}",
                comment="Checksummed Cargo.lock registry dependency.",
            )
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_id,
            }
        )
    all_cargo = (*inventory.workspace_packages, *inventory.rust_packages)
    for package in all_cargo:
        source_id = ids_by_cargo_name[package.name]
        for dependency in package.dependencies:
            dependency_name = dependency.split(" ", 1)[0]
            target_id = ids_by_cargo_name.get(dependency_name)
            if target_id is not None:
                relationships.append(
                    {
                        "spdxElementId": source_id,
                        "relationshipType": "DEPENDS_ON",
                        "relatedSpdxElement": target_id,
                    }
                )

    for project in inventory.python_projects:
        package_id = _spdx_id("Python", project.name, inventory.workspace_version)
        packages.append(
            _package(
                spdx_id=package_id,
                name=project.name,
                version=inventory.workspace_version,
                license_expression=project.license_expression,
                purl=f"pkg:pypi/{project.name}@{inventory.workspace_version}",
                comment=f"Repository Python project declared by {project.path}.",
            )
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": package_id,
            }
        )
    for package in inventory.python_packages:
        package_id = _spdx_id("PythonExternal", package.name, package.specifier or "any")
        packages.append(
            _package(
                spdx_id=package_id,
                name=package.name,
                version=package.specifier or "distribution-provided",
                license_expression=package.license_expression,
                download_location="NOASSERTION",
                comment=(
                    "Distribution-provided build dependency."
                    if package.scope == "build-system"
                    else "Optional distribution-provided runtime dependency."
                ),
            )
        )
        relationships.append(
            {
                "spdxElementId": package_id,
                "relationshipType": (
                    "BUILD_DEPENDENCY_OF"
                    if package.scope == "build-system"
                    else "OPTIONAL_DEPENDENCY_OF"
                ),
                "relatedSpdxElement": root_id,
            }
        )
    for package in inventory.vendored_packages:
        package_id = _spdx_id("Vendored", package.name, package.version)
        packages.append(
            _package(
                spdx_id=package_id,
                name=package.name,
                version=package.version,
                license_expression=package.license_expression,
                download_location=package.repository,
                checksum=package.sha256,
                comment=f"Vendored source at {package.path}.",
            )
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": package_id,
            }
        )
    for package in inventory.upstream_packages:
        package_id = _spdx_id("Upstream", package.id, package.commit)
        packages.append(
            _package(
                spdx_id=package_id,
                name=package.name,
                version=package.version,
                license_expression=package.license_expression,
                download_location=package.repository,
                comment=f"Pinned integration source commit {package.commit}.",
            )
        )
        relationships.append(
            {
                "spdxElementId": package_id,
                "relationshipType": "BUILD_DEPENDENCY_OF",
                "relatedSpdxElement": root_id,
            }
        )
    for action in inventory.workflow_actions:
        package_id = _spdx_id("GitHubAction", action.repository, action.commit)
        packages.append(
            _package(
                spdx_id=package_id,
                name=action.repository,
                version=action.commit,
                license_expression=action.license_expression,
                download_location=f"https://github.com/{action.repository}",
                comment=f"Immutable GitHub Actions pin reviewed as {action.version}.",
            )
        )
        relationships.append(
            {
                "spdxElementId": package_id,
                "relationshipType": "BUILD_DEPENDENCY_OF",
                "relatedSpdxElement": root_id,
            }
        )

    packages.sort(key=lambda item: item["SPDXID"])
    relationships.sort(
        key=lambda item: (
            item["spdxElementId"],
            item["relationshipType"],
            item["relatedSpdxElement"],
        )
    )
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "HyperFlux Next source dependency inventory",
        "documentNamespace": (
            "https://hyperflux.dev/spdx/hyperflux-next/"
            f"{inventory.authority_sha256}"
        ),
        "creationInfo": {
            "created": inventory.inventory_created,
            "creators": ["Tool: hyperflux-next-hfxdev"],
            "licenseListVersion": "3.27",
        },
        "documentDescribes": [root_id],
        "packages": packages,
        "relationships": relationships,
    }
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def _signed(value: bool) -> str:
    return "signed" if value else "not signed"


def _artifact_integrity(
    inventory: DependencyInventory, repository: ArchRepositoryPolicy
) -> list[str]:
    integrity = inventory.artifact_integrity
    signed = ", ".join(f"`{name}`" for name in signed_asset_names(repository))
    checksum_only = ", ".join(f"`{name}`" for name in checksum_only_asset_names(repository))
    packages = ", ".join(f"`{package.filename}`" for package in repository.packages)
    attestations = (
        integrity.sigstore_signatures
        or integrity.slsa_provenance
        or integrity.remote_build_attestation
    )
    lines = [
        "## How Each Download Is Protected",
        "",
        "Each published download is listed with exactly what protects it from tampering.",
        "",
        "| Download | Contents | Protection |",
        "| --- | --- | --- |",
        "| Source SBOM | SPDX 2.3 inventory of source and build dependencies | "
        "Generated from this repository; not signed, and not an SBOM of package files |",
        f"| Release [`{repository.product_release_tag}`]({repository.product_release_url}) | "
        f"{packages} and `{integrity.human_release_checksum_asset}` | "
        f"`{integrity.human_release_checksum_asset}` is "
        f"{_signed(integrity.human_release_checksum_signed)}; it is a checksum list "
        "fetched over HTTPS |",
        f"| Arch repository [`{repository.release_tag}`]({repository.release_url}) | "
        f"{signed} | Each has a detached OpenPGP `.sig` from subkey "
        f"`{repository.signing_subkey_fingerprint}`; pacman requires "
        f"`{repository.signature_level}` |",
        f"| Arch repository [`{repository.release_tag}`]({repository.release_url}) | "
        f"{checksum_only} | Not signed; the repository verifier recomputes or checks each one "
        "against `assurance/arch-repository.json` |",
        f"| Git tags and commits | `{repository.product_release_tag}` and its history | "
        f"Tags are {_signed(integrity.release_tags_signed)}; commits are "
        f"{_signed(integrity.release_commits_signed)} |",
        "| Remote build attestations | Sigstore signatures, SLSA provenance, hosted build "
        "attestations | "
        + ("Published" if attestations else "None exist; they are future work")
        + " |",
        "",
        f"The signing subkey belongs to primary key `{repository.primary_fingerprint}`. "
        f"The `{repository.release_tag}` GitHub release is a machine-managed prerelease that "
        f"serves pacman; `{repository.product_release_tag}` is the human-facing release.",
        "",
        "| Package | Version | SHA-256 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{package.name}` | `{package.version}` | `{package.sha256}` |"
        for package in repository.packages
    )
    lines.append("")
    return lines


def supply_chain_markdown(
    inventory: DependencyInventory, repository: ArchRepositoryPolicy
) -> str:
    lines = [
        "# Supply Chain",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        "This page lists everything HyperFlux is built from and how each release download is signed or checksummed, for anyone who wants to verify a download or review third-party code.",
        "",
        "The dependency list is built without network access. It covers `Cargo.lock`, the Python project files, vendored sources, the pinned app sources, and the GitHub workflow actions, each pinned to an exact commit.",
        "",
        *_artifact_integrity(inventory, repository),
        "## Dependencies",
        "",
        f"- Workspace crates: {len(inventory.workspace_packages)}",
        f"- Checksummed Rust registry packages: {len(inventory.rust_packages)}",
        f"- Repository Python projects: {len(inventory.python_projects)}",
        f"- Distribution-provided Python packages: {len(inventory.python_packages)}",
        f"- Vendored source packages: {len(inventory.vendored_packages)}",
        f"- Pinned application upstreams: {len(inventory.upstream_packages)}",
        f"- Immutable workflow actions: {len(inventory.workflow_actions)}",
        f"- Dependency list SHA-256: `{inventory.authority_sha256}`",
        "",
        "## Allowed Licenses",
        "",
    ]
    lines.extend(f"- `{expression}`" for expression in inventory.allowed_licenses)
    lines.extend(
        [
            "",
            "## Software Bill Of Materials",
            "",
            "The SPDX 2.3 list of source and build dependencies is [`assurance/generated/hyperflux-next.spdx.json`](../../assurance/generated/hyperflux-next.spdx.json).",
            "It covers source and build dependencies only. It does not list the files inside the packages or their digests.",
            "",
        ]
    )
    lines.extend(
        [
            "## GitHub Workflow Actions",
            "",
            "| Action | Reviewed version | Commit | License |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| `{action.repository}` | `{action.version}` | `{action.commit}` | `{action.license_expression}` |"
        for action in inventory.workflow_actions
    )
    lines.append("")
    return "\n".join(lines)
