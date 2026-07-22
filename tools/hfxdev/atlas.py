# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .model import ModelError, load_json, require_unique


ROOT_KEYS = {"$schema", "schema", "publication_state", "nodes"}
NODE_KEYS = {
    "id",
    "title",
    "path",
    "category",
    "status",
    "purpose",
    "owns",
    "must_not_own",
    "inputs",
    "outputs",
    "public_contracts",
    "canonical_files",
    "generated_files",
    "verification",
    "limitations",
    "safe_change_workflow",
    "related_docs",
    "depends_on",
}
CATEGORIES = {
    "architecture",
    "assurance",
    "runtime",
    "applications",
    "tooling",
    "delivery",
    "governance",
    "documentation",
}
STATUSES = {"implemented", "generated", "policy", "research-boundary"}


@dataclass(frozen=True)
class AtlasNode:
    id: str
    title: str
    path: str
    category: str
    status: str
    purpose: str
    owns: tuple[str, ...]
    must_not_own: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    public_contracts: tuple[str, ...]
    canonical_files: tuple[str, ...]
    generated_files: tuple[str, ...]
    verification: tuple[str, ...]
    limitations: tuple[str, ...]
    safe_change_workflow: tuple[str, ...]
    related_docs: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryAtlas:
    publication_state: str
    nodes: tuple[AtlasNode, ...]

    @property
    def by_id(self) -> dict[str, AtlasNode]:
        return {node.id: node for node in self.nodes}

    @property
    def used_by(self) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node.depends_on:
                values[dependency].append(node.id)
        return {key: tuple(sorted(value)) for key, value in values.items()}


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ModelError(f"{label}: {'; '.join(details)}")
    return value


def _string(value: Any, label: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ModelError(f"{label}: expected 1 through {maximum} characters")
    return value.strip()


def _strings(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = 16,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ModelError(f"{label}: expected {minimum} through {maximum} entries")
    result = tuple(_string(item, f"{label} entry") for item in value)
    require_unique(result, label)
    return result


def _path(value: Any, label: str) -> str:
    result = _string(value, label, 256)
    pure = PurePosixPath(result)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != result:
        raise ModelError(f"{label}: expected a safe repository path")
    return result


def _paths(
    root: Path,
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    directory: bool = False,
    must_exist: bool = True,
) -> tuple[str, ...]:
    values = _strings(value, label, minimum=minimum, maximum=24)
    paths = tuple(_path(item, f"{label} entry") for item in values)
    for relative in paths:
        if not must_exist:
            continue
        path = root / relative
        present = path.is_dir() if directory else path.is_file()
        if path.is_symlink() or not present:
            kind = "directory" if directory else "file"
            raise ModelError(f"{label}: {kind} is missing or symbolic: {relative}")
    return paths


def _assert_acyclic(nodes: tuple[AtlasNode, ...]) -> None:
    index = {node.id: node for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            raise ModelError(f"repository atlas dependency cycle includes {identifier}")
        visiting.add(identifier)
        for dependency in index[identifier].depends_on:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for node in nodes:
        visit(node.id)


def load_repository_atlas(root: Path) -> RepositoryAtlas:
    root = root.resolve()
    value = _exact(
        load_json(root / "architecture" / "repository-atlas.json"),
        ROOT_KEYS,
        "repository atlas",
    )
    if value["$schema"] != "../schemas/repository-atlas.schema.json":
        raise ModelError("repository atlas has a non-canonical schema reference")
    if value["schema"] != "hyperflux-repository-atlas-v1":
        raise ModelError("unsupported repository atlas schema")
    if value["publication_state"] != "public-source-pre-release":
        raise ModelError("repository atlas must identify the public pre-release boundary")
    raw_nodes = value["nodes"]
    if not isinstance(raw_nodes, list) or not 24 <= len(raw_nodes) <= 64:
        raise ModelError("repository atlas requires 24 through 64 subsystem nodes")
    test_ids = {
        item["id"]
        for item in load_json(root / "verification" / "tests.json")["tests"]
    }
    nodes: list[AtlasNode] = []
    for index, raw in enumerate(raw_nodes):
        item = _exact(raw, NODE_KEYS, f"repository atlas node {index}")
        identifier = _string(item["id"], f"repository atlas node {index} id", 64)
        category = _string(item["category"], f"repository atlas node {identifier} category")
        status = _string(item["status"], f"repository atlas node {identifier} status")
        if category not in CATEGORIES or status not in STATUSES:
            raise ModelError(f"repository atlas node {identifier}: invalid category or status")
        path = _path(item["path"], f"repository atlas node {identifier} path")
        if (root / path).is_symlink() or not (root / path).is_dir():
            raise ModelError(f"repository atlas node {identifier}: directory is missing or symbolic")
        verification = _strings(
            item["verification"], f"repository atlas node {identifier} verification"
        )
        unknown_tests = sorted(set(verification) - test_ids)
        if unknown_tests:
            raise ModelError(
                f"repository atlas node {identifier}: unknown verification {', '.join(unknown_tests)}"
            )
        nodes.append(
            AtlasNode(
                id=identifier,
                title=_string(item["title"], f"repository atlas node {identifier} title", 80),
                path=path,
                category=category,
                status=status,
                purpose=_string(item["purpose"], f"repository atlas node {identifier} purpose"),
                owns=_strings(item["owns"], f"repository atlas node {identifier} owns"),
                must_not_own=_strings(
                    item["must_not_own"], f"repository atlas node {identifier} forbidden ownership"
                ),
                inputs=_strings(item["inputs"], f"repository atlas node {identifier} inputs"),
                outputs=_strings(item["outputs"], f"repository atlas node {identifier} outputs"),
                public_contracts=_strings(
                    item["public_contracts"], f"repository atlas node {identifier} contracts"
                ),
                canonical_files=_paths(
                    root,
                    item["canonical_files"],
                    f"repository atlas node {identifier} canonical files",
                    minimum=1,
                ),
                generated_files=_paths(
                    root,
                    item["generated_files"],
                    f"repository atlas node {identifier} generated files",
                    must_exist=False,
                ),
                verification=verification,
                limitations=_strings(
                    item["limitations"], f"repository atlas node {identifier} limitations"
                ),
                safe_change_workflow=_strings(
                    item["safe_change_workflow"],
                    f"repository atlas node {identifier} safe workflow",
                    minimum=3,
                ),
                related_docs=_paths(
                    root,
                    item["related_docs"],
                    f"repository atlas node {identifier} related docs",
                    minimum=1,
                ),
                depends_on=_strings(
                    item["depends_on"],
                    f"repository atlas node {identifier} dependencies",
                    minimum=0,
                ),
            )
        )
    result = tuple(nodes)
    require_unique([node.id for node in result], "repository atlas node id")
    require_unique([node.path for node in result], "repository atlas directory")
    require_unique(
        [path for node in result for path in node.canonical_files],
        "repository atlas canonical-file owner",
    )
    identifiers = {node.id for node in result}
    for node in result:
        unknown = sorted(set(node.depends_on) - identifiers)
        if node.id in node.depends_on or unknown:
            raise ModelError(
                f"repository atlas node {node.id}: invalid dependencies {', '.join(unknown)}"
            )
    _assert_acyclic(result)
    return RepositoryAtlas(publication_state=value["publication_state"], nodes=result)
