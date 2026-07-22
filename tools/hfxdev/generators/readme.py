# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from collections import Counter
from html import escape
from typing import Any

from ..atlas import RepositoryAtlas
from ..governance import GitHubGovernance
from ..release import ReleaseGate


def _badge(label: str, value: str, color: str) -> str:
    label_width = max(72, len(label) * 7 + 18)
    value_width = max(92, len(value) * 7 + 18)
    total = label_width + value_width
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="24" role="img" aria-label="{escape(label)}: {escape(value)}">
  <title>{escape(label)}: {escape(value)}</title>
  <rect width="{label_width}" height="24" fill="#252d35"/>
  <rect x="{label_width}" width="{value_width}" height="24" fill="{color}"/>
  <g fill="#edf3f2" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="11" text-anchor="middle">
    <text x="{label_width / 2:g}" y="16">{escape(label)}</text>
    <text x="{label_width + value_width / 2:g}" y="16">{escape(value)}</text>
  </g>
</svg>
"""


def development_badge() -> str:
    return _badge("state", "unreleased", "#b34a42")


def license_badge() -> str:
    return _badge("license", "GPL-2.0-only", "#287d6c")


def markdown(
    governance: GitHubGovernance,
    gates: tuple[ReleaseGate, ...],
    knowledge: dict[str, Any],
    atlas: RepositoryAtlas,
) -> str:
    repository_url = f"https://github.com/{governance.owner}/{governance.repository}"
    pages_url = governance.homepage.rstrip("/")
    gate_counts = Counter(gate.status for gate in gates)
    candidates = knowledge["candidates"]
    route_qualified = sum(
        candidate["hyperflux_support"] == "route-qualified" for candidate in candidates
    )
    research_only = sum(
        candidate["hyperflux_support"] == "candidate-only" for candidate in candidates
    )
    facts = sum(len(candidate["reviewed_facts"]) for candidate in candidates)
    gaps = sum(len(candidate["knowledge_gaps"]) for candidate in candidates)
    return f"""# HyperFlux Next

**Evidence-bound Linux support for devices paired through Razer HyperFlux V2.**

[![Verification]({repository_url}/actions/workflows/verification.yml/badge.svg)]({repository_url}/actions/workflows/verification.yml)
[![Documentation]({repository_url}/actions/workflows/pages.yml/badge.svg)]({repository_url}/actions/workflows/pages.yml)
![Development state](docs/assets/badge-state.svg)
![License](docs/assets/badge-license.svg)

> [!IMPORTANT]
> **Unreleased and evidence-bound.** The source and generated documentation are
> public for review, but there is no supported package channel or product
> release. Software verification, route qualification, lifecycle evidence, and
> a release decision remain separate facts; one never silently promotes another.

## Start Here

| Destination | Purpose |
| --- | --- |
| [Documentation]({pages_url}/) | Audience-guided product, development, and maintenance documentation |
| [Device Lab]({pages_url}/devices/) | Qualified routes, research candidates, provenance, conflicts, and unknowns |
| [Repository Atlas]({pages_url}/atlas/) | Canonical ownership, dependencies, generated projections, and safe change paths |
| [Repository State]({pages_url}/state/) | Release gates, evidence levels, verification budgets, and current blockers |
| [Installation status]({pages_url}/users/installation.html) | What a future package contains and why installation is not yet offered |
| [Architecture]({pages_url}/developers/architecture.html) | System boundaries and the one-writer transport model |
| [Contributing](CONTRIBUTING.md) | Schema-first changes, evidence expectations, and verification |
| [Security](SECURITY.md) | Private vulnerability reporting and disclosure policy |
| [Roadmap]({governance.project_url}) | Typed issues and qualification work organized in the GitHub Project |

## Architecture

```mermaid
flowchart LR
    Applications["Applications"] --> SDK["Versioned SDK"]
    SDK --> Bridge["Bridge and policy authority"]
    Bridge --> Kernel["Minimal HID transport"]
    Kernel --> Receiver["HyperFlux receiver and paired devices"]
```

Applications own user interaction, layouts, and effects. The SDK owns the typed
application boundary. The bridge is the sole userspace writer and owns policy,
qualification, scheduling, restoration, and outcomes. The kernel preserves
ordinary HID input and transports bounded generation-bound envelopes.

## Current Readiness

<!-- Generated from assurance/release-gates.json, generated/knowledge/catalog.json, and architecture/repository-atlas.json. -->

| Surface | Current evidence |
| --- | --- |
| Public source and documentation | Authorized pre-release surface; {len(atlas.nodes)} Atlas subsystems; generated Pages only |
| Software verification | {gate_counts['software-satisfied']} of {len(gates)} release gates software-satisfied |
| Hardware knowledge | {route_qualified} route-qualified profiles; {research_only} research-only candidates; {facts} reviewed facts; {gaps} explicit gaps |
| Remaining release evidence | {gate_counts['blocked-by-physical-evidence']} physical gate(s); {gate_counts['blocked-by-lifecycle-evidence']} lifecycle gate(s) |
| Product publication | Locked; no release, tag, package channel, or supported-product claim |
| Portal hardware access | Zero hardware writes and zero live device queries |

The compact table is generated by `./hfx generate`. Follow [Repository
State]({pages_url}/state/) for the complete, canonical explanation.

## Choose Your Next Action

| You are... | Go to... |
| --- | --- |
| A first-time visitor | [Product overview]({pages_url}/users/overview.html) and [installation status]({pages_url}/users/installation.html) |
| A developer | [Architecture]({pages_url}/developers/architecture.html), then the [Repository Atlas]({pages_url}/atlas/) |
| A prospective contributor | [CONTRIBUTING.md](CONTRIBUTING.md) and the [issue forms]({repository_url}/issues/new/choose) |
| A hardware tester | [Device Lab]({pages_url}/devices/) and the [device qualification form]({repository_url}/issues/new?template=device_qualification.yml) |
| A maintainer | [Repository State]({pages_url}/state/), [governance]({pages_url}/maintainers/github-governance.html), and the [roadmap]({governance.project_url}) |
| A security reporter | [SECURITY.md](SECURITY.md) and [private vulnerability reporting]({repository_url}/security/advisories/new) |

## Verify A Change

```sh
./hfx generate
./hfx verify --changed-from <commit>
./hfx verify --all
```

Generated files must be reproducible in one pass. Verification is change-aware,
networkless after exact upstream preparation, device-free, and incapable of
granting hardware or release authority.

<details>
<summary>Repository boundaries</summary>

- Unknown devices expose safe identity and passive observations but receive no
  writable capability.
- Imported upstream catalogs contribute provenance-bound knowledge, never raw
  transport authority.
- Release, package, tag, and hardware-writing workflows are absent until a
  separate authorization changes their canonical interlocks.
- The [Repository Atlas]({pages_url}/atlas/) is the authoritative directory map;
  folder READMEs are generated projections of it.

</details>

Project-owned kernel and core work is `GPL-2.0-only`. SDKs and integrations use
declared compatible exceptions. See [LICENSE-DECISION.md](LICENSE-DECISION.md).
"""
