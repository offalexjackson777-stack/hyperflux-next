# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import re
from typing import Any

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


def supply_chain_markdown(inventory: DependencyInventory) -> str:
    lines = [
        "# Supply Chain",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        "The inventory is network-independent and binds Cargo.lock, Python project declarations, vendored sources, and pinned application upstreams.",
        "Release signing and remote attestations remain unavailable until publication is explicitly authorized.",
        "",
        "## Inventory",
        "",
        f"- Workspace crates: {len(inventory.workspace_packages)}",
        f"- Checksummed Rust registry packages: {len(inventory.rust_packages)}",
        f"- Repository Python projects: {len(inventory.python_projects)}",
        f"- Distribution-provided Python packages: {len(inventory.python_packages)}",
        f"- Vendored source packages: {len(inventory.vendored_packages)}",
        f"- Pinned application upstreams: {len(inventory.upstream_packages)}",
        f"- Authority SHA-256: `{inventory.authority_sha256}`",
        "",
        "## License Policy",
        "",
    ]
    lines.extend(f"- `{expression}`" for expression in inventory.allowed_licenses)
    lines.extend(
        [
            "",
            "## Generated SBOM",
            "",
            "The canonical SPDX 2.3 source inventory is [`assurance/generated/hyperflux-next.spdx.json`](../../assurance/generated/hyperflux-next.spdx.json).",
            "It describes source and build dependencies; a future authorized release must additionally bind this inventory to exact distributed artifact digests.",
            "",
        ]
    )
    return "\n".join(lines)
