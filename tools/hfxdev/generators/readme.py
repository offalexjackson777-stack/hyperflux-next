# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
from typing import Any

from ..governance import GitHubGovernance
from ..portal_metadata import canonical_url
from ..portal_model import PortalConfig


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
    readiness: dict[str, Any],
    portal: PortalConfig,
) -> str:
    repository_url = f"https://github.com/{governance.owner}/{governance.repository}"

    def route_url(identifier: str) -> str:
        return canonical_url(portal, portal.route(identifier).path)

    software = readiness["software"]
    hardware = readiness["hardware"]
    evidence = readiness["evidence"]
    publication = readiness["publication"]
    return f"""# HyperFlux Next

**Evidence-bound Linux support for devices paired through Razer HyperFlux V2.**

[![Verification]({repository_url}/actions/workflows/verification.yml/badge.svg)]({repository_url}/actions/workflows/verification.yml)
[![Documentation]({repository_url}/actions/workflows/pages.yml/badge.svg)]({repository_url}/actions/workflows/pages.yml)
![Development state](docs/assets/badge-state.svg)
![License](docs/assets/badge-license.svg)

> [!IMPORTANT]
> **{publication['label']} and evidence-bound.** {publication['summary']}

## Start

| I need to... | Go to... |
| --- | --- |
| Understand or eventually install HyperFlux | [Documentation]({route_url('home')}) and [installation status]({route_url('installation')}) |
| Check hardware evidence | [Device Lab]({route_url('device-lab')}) |
| Understand or change the code | [Architecture]({route_url('architecture')}), [Repository Atlas]({route_url('repository-atlas')}), and [Contributing](CONTRIBUTING.md) |
| Review blockers and evidence | [Repository State]({route_url('repository-state')}) and [Roadmap]({governance.project_url}) |
| Report a vulnerability | [Security policy](SECURITY.md) and [private reporting]({repository_url}/security/advisories/new) |

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

<!-- Generated from generated/public-readiness.json. -->

| Surface | Plain-language state |
| --- | --- |
| Product | **{publication['label']}**: {publication['summary']} |
| Software | {software['summary']} |
| Hardware | {hardware['summary']} |
| Remaining evidence | {evidence['summary']} |
| Documentation portal | Static and telemetry-free; no live device query or hardware write |

This table and the Pages home consume the same generated projection. [Repository
State]({route_url('repository-state')}) shows the detailed ledgers without changing the meaning
of these terms.

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
<summary>Engineering and publication boundaries</summary>

- Unknown devices expose safe identity and passive observations but receive no
  writable capability.
- Imported upstream catalogs contribute provenance-bound knowledge, never raw
  transport authority.
- Release, package, tag, and hardware-writing workflows are absent until a
  separate authorization changes their canonical interlocks.
- The [Repository Atlas]({route_url('repository-atlas')}) is the authoritative directory and
  ownership map; folder READMEs are generated projections.

</details>

Project-owned kernel and core work is `GPL-2.0-only`. SDKs and integrations use
declared compatible licenses. See [licensing policy](docs/legal/licensing.md).
"""
