# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .model import ModelError, load_json, require_unique


CATALOG_KEYS = {"$schema", "schema", "tests"}
TEST_KEYS = {
    "id",
    "title",
    "owned_domain",
    "lanes",
    "runner",
    "required_capabilities",
    "hardware_requirement",
    "writes_hardware",
    "expected_duration_seconds",
    "timeout_seconds",
    "dependencies",
    "isolation",
    "cache_inputs",
    "produced_evidence",
    "resume_policy",
}
LANES = {"fast", "full-software", "hardware"}
HARDWARE_REQUIREMENTS = {"none", "optional", "required"}
ISOLATION_LEVELS = {"shared", "exclusive-process", "exclusive-system", "exclusive-hardware"}
RESUME_POLICIES = {"rerun", "reuse-verified", "checkpoint"}
KNOWN_RUNNERS = {
    "assurance-contracts",
    "foundation-contracts",
    "schema-contracts",
    "integration-contracts",
    "profile-contracts",
    "protocol-contracts",
    "error-contracts",
    "formal-model-contracts",
    "generated-freshness",
    "privacy-boundary",
    "python-unit",
    "toolchain-contract",
    "development-environment-contracts",
    "documentation-portal-contracts",
    "rust-format",
    "rust-clippy",
    "rust-unit",
    "simulator-contracts",
    "cpp-sdk-contracts",
    "openrgb-adapter-contracts",
    "openrgb-thread-sanitizer",
    "openrazer-metadata-contracts",
    "openrazer-compatibility-contracts",
    "polychromatic-adapter-contracts",
    "kernel-profile-contracts",
    "package-contracts",
}
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class TestNode:
    id: str
    title: str
    owned_domain: str
    lanes: tuple[str, ...]
    runner: str
    required_capabilities: tuple[str, ...]
    hardware_requirement: str
    writes_hardware: bool
    expected_duration_seconds: int
    timeout_seconds: int
    dependencies: tuple[str, ...]
    isolation: str
    cache_inputs: tuple[str, ...]
    produced_evidence: tuple[str, ...]
    resume_policy: str


@dataclass(frozen=True)
class TestCatalog:
    nodes: tuple[TestNode, ...]

    @property
    def by_id(self) -> dict[str, TestNode]:
        return {node.id: node for node in self.nodes}

    def ordered(self) -> tuple[TestNode, ...]:
        by_id = self.by_id
        remaining = {node.id: set(node.dependencies) for node in self.nodes}
        ordered: list[TestNode] = []
        while remaining:
            ready = [node.id for node in self.nodes if node.id in remaining and not remaining[node.id]]
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise ModelError(f"verification dependency cycle includes: {cycle}")
            for node_id in ready:
                ordered.append(by_id[node_id])
                del remaining[node_id]
                for dependencies in remaining.values():
                    dependencies.discard(node_id)
        return tuple(ordered)


@dataclass(frozen=True)
class TestSelection:
    lane: str
    mode: str
    nodes: tuple[TestNode, ...]
    changed_paths: tuple[str, ...]
    unmatched_paths: tuple[str, ...]


CRITICAL_CHANGE_PATTERNS = (
    "hfx",
    "schemas/test-catalog.schema.json",
    "schemas/verification-*.schema.json",
    "tools/hfxdev/cli.py",
    "tools/hfxdev/testgraph.py",
    "tools/hfxdev/verification_run.py",
    "tools/hfxdev/verify.py",
    "verification/tests.json",
)


def _matches(path: str, pattern: str) -> bool:
    return PurePosixPath(path).full_match(pattern)


def _changed_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ModelError(f"invalid changed repository path: {value}")
    return value


def select_tests(
    catalog: TestCatalog,
    lane: str,
    changed_paths: tuple[str, ...] | list[str] | None = None,
) -> TestSelection:
    if lane not in {"fast", "full-software"}:
        raise ModelError(f"unsupported software verification lane: {lane}")
    ordered = catalog.ordered()
    eligible = {node.id for node in ordered if lane in node.lanes}
    if not eligible:
        raise ModelError(f"verification lane has no tests: {lane}")
    if changed_paths is None:
        return TestSelection(lane, "lane", tuple(node for node in ordered if node.id in eligible), (), ())

    changed = tuple(sorted({_changed_path(value) for value in changed_paths}))
    if not changed:
        return TestSelection(
            lane,
            "changed-paths-no-diff",
            tuple(node for node in ordered if node.id in eligible),
            (),
            (),
        )
    if any(
        _matches(path, pattern)
        for path in changed
        for pattern in CRITICAL_CHANGE_PATTERNS
    ):
        return TestSelection(
            lane,
            "changed-paths-critical",
            tuple(node for node in ordered if node.id in eligible),
            changed,
            (),
        )

    selected: set[str] = set()
    unmatched: list[str] = []
    for path in changed:
        matched = {
            node.id
            for node in ordered
            if node.id in eligible
            and any(_matches(path, pattern) for pattern in node.cache_inputs)
        }
        if not matched:
            unmatched.append(path)
        selected.update(matched)
    if unmatched:
        return TestSelection(
            lane,
            "changed-paths-fail-closed",
            tuple(node for node in ordered if node.id in eligible),
            changed,
            tuple(unmatched),
        )

    by_id = catalog.by_id
    expanded = True
    while expanded:
        expanded = False
        for node in ordered:
            if node.id in eligible and node.id not in selected and set(node.dependencies) & selected:
                selected.add(node.id)
                expanded = True
        for node_id in tuple(selected):
            for dependency in by_id[node_id].dependencies:
                if dependency not in selected:
                    selected.add(dependency)
                    expanded = True
    return TestSelection(
        lane,
        "changed-paths",
        tuple(node for node in ordered if node.id in selected),
        changed,
        (),
    )


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise ModelError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise ModelError(f"{label}: unknown fields {', '.join(extra)}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ModelError(f"{label}: invalid identifier")
    return value


def _string_list(value: Any, label: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModelError(f"{label}: must be a string array")
    if len(value) != len(set(value)):
        raise ModelError(f"{label}: duplicate values")
    result = tuple(value)
    if identifiers:
        for item in result:
            _identifier(item, label)
    return result


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > 86400:
        raise ModelError(f"{label}: must be an integer from {minimum} through 86400")
    return value


def _cache_inputs(root: Path, values: Any, label: str) -> tuple[str, ...]:
    patterns = _string_list(values, label)
    if not patterns:
        raise ModelError(f"{label}: at least one cache input is required")
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts:
            raise ModelError(f"{label}: cache input must stay inside the repository: {pattern}")
        if not any(root.glob(pattern)):
            raise ModelError(f"{label}: cache input matches nothing: {pattern}")
    return patterns


def _node(root: Path, value: Any, index: int) -> TestNode:
    if not isinstance(value, dict):
        raise ModelError(f"verification test {index}: must be an object")
    label = f"verification test {value.get('id', index)}"
    _require_exact_keys(value, TEST_KEYS, label)
    node_id = _identifier(value["id"], f"{label} id")
    title = value["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ModelError(f"{label}: title must contain 1 through 160 characters")
    domain = _identifier(value["owned_domain"], f"{label} owned_domain")
    lanes = _string_list(value["lanes"], f"{label} lanes")
    if not lanes or not set(lanes) <= LANES:
        raise ModelError(f"{label}: invalid or empty lane selection")
    runner = _identifier(value["runner"], f"{label} runner")
    if runner not in KNOWN_RUNNERS:
        raise ModelError(f"{label}: runner is not implemented: {runner}")
    capabilities = _string_list(
        value["required_capabilities"], f"{label} required_capabilities", identifiers=True
    )
    hardware = value["hardware_requirement"]
    if hardware not in HARDWARE_REQUIREMENTS:
        raise ModelError(f"{label}: invalid hardware requirement")
    writes_hardware = value["writes_hardware"]
    if not isinstance(writes_hardware, bool):
        raise ModelError(f"{label}: writes_hardware must be boolean")
    if writes_hardware and hardware != "required":
        raise ModelError(f"{label}: a hardware-writing test must require hardware")
    if "hardware" in lanes and hardware != "required":
        raise ModelError(f"{label}: hardware lane tests must require hardware")
    if set(lanes) & {"fast", "full-software"} and hardware != "none":
        raise ModelError(f"{label}: software lanes may not require hardware")
    expected = _positive_integer(
        value["expected_duration_seconds"], f"{label} expected_duration_seconds", allow_zero=True
    )
    timeout = _positive_integer(value["timeout_seconds"], f"{label} timeout_seconds")
    if expected > timeout:
        raise ModelError(f"{label}: expected duration exceeds timeout")
    dependencies = _string_list(value["dependencies"], f"{label} dependencies", identifiers=True)
    if node_id in dependencies:
        raise ModelError(f"{label}: cannot depend on itself")
    isolation = value["isolation"]
    if isolation not in ISOLATION_LEVELS:
        raise ModelError(f"{label}: invalid isolation level")
    cache_inputs = _cache_inputs(root, value["cache_inputs"], f"{label} cache_inputs")
    evidence = _string_list(value["produced_evidence"], f"{label} produced_evidence", identifiers=True)
    if not evidence:
        raise ModelError(f"{label}: at least one evidence output is required")
    resume_policy = value["resume_policy"]
    if resume_policy not in RESUME_POLICIES:
        raise ModelError(f"{label}: invalid resume policy")
    return TestNode(
        id=node_id,
        title=title.strip(),
        owned_domain=domain,
        lanes=lanes,
        runner=runner,
        required_capabilities=capabilities,
        hardware_requirement=hardware,
        writes_hardware=writes_hardware,
        expected_duration_seconds=expected,
        timeout_seconds=timeout,
        dependencies=dependencies,
        isolation=isolation,
        cache_inputs=cache_inputs,
        produced_evidence=evidence,
        resume_policy=resume_policy,
    )


def load_test_catalog(root: Path) -> TestCatalog:
    value = load_json(root / "verification" / "tests.json")
    _require_exact_keys(value, CATALOG_KEYS, "verification catalog")
    if value["schema"] != "hyperflux-test-catalog-v1":
        raise ModelError("unsupported verification catalog schema")
    tests = value["tests"]
    if not isinstance(tests, list) or not tests:
        raise ModelError("verification catalog must contain tests")
    nodes = tuple(_node(root, item, index) for index, item in enumerate(tests))
    require_unique([node.id for node in nodes], "verification test id")
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        unknown = sorted(set(node.dependencies) - by_id.keys())
        if unknown:
            raise ModelError(f"{node.id}: unknown dependencies {', '.join(unknown)}")
    catalog = TestCatalog(nodes=nodes)
    catalog.ordered()
    return catalog


def format_plan(
    catalog: TestCatalog,
    *,
    lane: str = "full-software",
    changed_paths: tuple[str, ...] | list[str] | None = None,
) -> str:
    selection = select_tests(catalog, lane, changed_paths)
    nodes = selection.nodes
    expected = sum(node.expected_duration_seconds for node in nodes)
    lines = [
        "HyperFlux Next verification plan",
        f"Tests: {len(nodes)} | expected serial duration: {expected}s",
        f"Selection: {selection.lane} ({selection.mode})",
        "",
    ]
    for index, node in enumerate(nodes, start=1):
        dependencies = ", ".join(node.dependencies) if node.dependencies else "none"
        execution = node.hardware_requirement
        if not node.writes_hardware:
            execution += ", zero hardware writes"
        lines.extend(
            [
                f"{index:02d}. {node.id} - {node.title}",
                f"    domain={node.owned_domain} runner={node.runner} isolation={node.isolation}",
                f"    hardware={execution} timeout={node.timeout_seconds}s resume={node.resume_policy}",
                f"    depends={dependencies}",
            ]
        )
    return "\n".join(lines)


def markdown(catalog: TestCatalog) -> str:
    nodes = catalog.ordered()
    lines = [
        "# Verification Graph",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        "The catalog contains trusted runner identifiers, not executable command strings.",
        "Every current node is software-only and has zero hardware-write authority.",
        "",
        "| Order | Test | Domain | Hardware | Writes | Timeout | Resume |",
        "| ---: | --- | --- | --- | --- | ---: | --- |",
    ]
    for index, node in enumerate(nodes, start=1):
        lines.append(
            f"| {index} | `{node.id}` | `{node.owned_domain}` | `{node.hardware_requirement}` | "
            f"`{str(node.writes_hardware).lower()}` | {node.timeout_seconds}s | `{node.resume_policy}` |"
        )
    lines.extend(["", "## Dependencies", "", "```mermaid", "flowchart LR"])
    for node in nodes:
        lines.append(f'    {node.id.replace("-", "_")}["{node.id}"]')
    for node in nodes:
        for dependency in node.dependencies:
            lines.append(
                f"    {dependency.replace('-', '_')} --> {node.id.replace('-', '_')}"
            )
    lines.extend(["```", ""])
    return "\n".join(lines)
