# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
from typing import Any

from ..openrazer_native_route import OpenRazerNativeDeployment


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


def release_status(readiness: dict[str, Any]) -> str:
    publication = readiness["publication"]
    baseline = readiness["baseline"]
    return (
        "> [!IMPORTANT]\n"
        f"> **{publication['label']}.** {publication['summary']} "
        f"{baseline['summary']}"
    )


def recommended_package(*, version: str = "2.0.0-1") -> str:
    return f"""The recommended Arch experience is **Full `{version}`**, which keeps the lean
Core transport and adds packaged integration for stock OpenRazer, Polychromatic,
and OpenRGB. No public package channel is authorized yet. After publication,
the ordinary command will be:

```sh
sudo pacman -S hyperflux-next-full
```

See [Arch installation](docs/user/installation.md) and the
[distribution roadmap](docs/project/roadmap.md)."""


def compatibility_summary(
    readiness: dict[str, Any],
    openrazer: OpenRazerNativeDeployment,
    source_validated_devices: dict[str, Any],
) -> str:
    devices = source_validated_devices["devices"]
    operational = sum(
        device["operational_state"] == "source-validated" for device in devices
    )
    research = len(devices) - operational
    return f"""- Stock OpenRazer `{openrazer.compatibility.verified_upstream_version}` userspace,
  stock Polychromatic, and stock OpenRGB remain application owners.
- The source-validated catalog contains {operational} operational routes and
  {research} research-only candidates; physical fixtures do not form an allowlist.
- Restoration ships disabled. Input-reactive Ripple and OpenRGB's false Quartz
  colorway label remain accepted external limitations.
- Current 2.0 software status: {readiness['software']['summary']}"""
