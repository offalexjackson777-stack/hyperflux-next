# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
from typing import Any

from ..distributions import DistributionCatalog
from ..openrazer_native_route import OpenRazerNativeDeployment
from ..source_validated_catalog import (
    PHYSICALLY_PROVEN,
    SOURCE_QUALIFIED_OPERATIONAL,
    UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
    public_support_tier_counts,
)


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
    return _badge("state", "Arch 2.0", "#287d6c")


def license_badge() -> str:
    return _badge("license", "GPL-2.0-only", "#287d6c")


def release_status(readiness: dict[str, Any]) -> str:
    publication = readiness["publication"]
    return f"> [!IMPORTANT]\n> **{publication['label']}.** {publication['summary']}"


def recommended_package(catalog: DistributionCatalog) -> str:
    arch = catalog.targets["arch"]
    core = arch.components["linux"]
    full = arch.components["full"]
    return f"""**Core (`{core.package_name}`):** {core.description}.

**Full (`{full.package_name}`):** {full.description}.

The Arch Linux x86_64 packages are published in
[GitHub Release v2.0.0](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.0).
There is no pacman repository yet: verify `SHA256SUMS`, then install the two
downloaded files together with `pacman -U`. See
[installation](docs/user/installation.md) for the exact command."""


def compatibility_summary(
    readiness: dict[str, Any],
    openrazer: OpenRazerNativeDeployment,
    source_validated_devices: dict[str, Any],
) -> str:
    devices = source_validated_devices["devices"]
    counts = public_support_tier_counts(devices)
    return f"""- Stock OpenRazer `{openrazer.compatibility.verified_upstream_version}` userspace,
  stock Polychromatic, and stock OpenRGB remain application owners.
- {counts[PHYSICALLY_PROVEN]} devices are physically proven,
  {counts[SOURCE_QUALIFIED_OPERATIONAL]} are source-qualified operational
  candidates, and {counts[UPSTREAM_BLOCKED_OR_RESEARCH_ONLY]} are blocked or
  research-only. The physical fixtures are not an allowlist.
- Restoration ships disabled."""
