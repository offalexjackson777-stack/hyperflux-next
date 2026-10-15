# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
import textwrap
from typing import Any

from ..arch_repository import ArchRepositoryPolicy
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


def _wrapped_bullet(text: str) -> str:
    return "\n".join(
        textwrap.wrap(
            text,
            width=80,
            initial_indent="- ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def release_summary(readiness: dict[str, Any], repository: ArchRepositoryPolicy) -> str:
    """One plain sentence for landing pages; Release Status keeps the detail."""
    if readiness["publication"]["state"] != "ready":
        return (
            "**HyperFlux Next is not released yet.** The source is available for "
            "review; there is no package to install."
        )
    version = repository.product_release_tag.removeprefix("v")
    return f"""**HyperFlux Next {version} is available for Arch Linux x86_64.** Read the
[{repository.product_release_tag} release notes]({repository.product_release_url})."""


def release_status(readiness: dict[str, Any]) -> str:
    publication = readiness["publication"]
    paragraph = f"**{publication['label']}.** {publication['summary']}"
    lines = textwrap.wrap(
        paragraph,
        width=78,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return "\n".join(["> [!IMPORTANT]", *(f"> {line}" for line in lines)])


def recommended_package(catalog: DistributionCatalog) -> str:
    arch = catalog.targets["arch"]
    core = arch.components["linux"]
    full = arch.components["full"]
    return f"""Choose one edition. Full is recommended for desktops; Core is an advanced choice.

{_wrapped_bullet(f"**Full (`{full.package_name}`):** {full.description}.")}
{_wrapped_bullet(f"**Core (`{core.package_name}`):** {core.description}.")}

Stock OpenRGB, OpenRazer, and Polychromatic are separate Arch packages. Install
the ones you want to use; HyperFlux does not install or replace them.

### Install

1. Add the signed HyperFlux Pacman repository once. Follow the
   [one-time setup](docs/user/installation.md#1-add-the-hyperflux-repository---one-time-only).
   It is a signed project-operated Arch repository, not an official Arch Linux repository.
2. Install Full:

   ```bash
   sudo pacman -Syu hyperflux-next-full
   ```

   Full pulls in the matching Core package. To install Core alone instead, run
   `sudo pacman -Syu hyperflux-next-linux`.
3. Enable the services and reboot. Keep the receiver plugged in.

   ```bash
   sudo systemctl enable hyperflux-next-bridge.service \\
     "hyperflux-next-client@$(id -u).socket"
   sudo reboot
   ```

4. After you log in, check the result:

   ```bash
   hyperfluxctl status
   hyperfluxctl doctor
   ```

   Doctor prints `HyperFlux Next is ready.` when everything works. Otherwise it
   names the first problem and one thing to do about it.

Later updates arrive with your normal `sudo pacman -Syu`. The
[first-run guide](docs/user/first-run.md) shows how to open your applications,
and the [installation guide](docs/user/installation.md) covers key checks and
the offline package fallback."""


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
