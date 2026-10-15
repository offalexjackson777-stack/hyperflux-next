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
    public_support_tier,
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


def release_status(
    readiness: dict[str, Any], repository: ArchRepositoryPolicy
) -> str:
    publication = readiness["publication"]
    if publication["state"] != "ready":
        paragraph = f"**{publication['label']}.** {publication['summary']}"
        lines = textwrap.wrap(
            paragraph,
            width=78,
            break_long_words=False,
            break_on_hyphens=False,
        )
        return "\n".join(["> [!IMPORTANT]", *(f"> {line}" for line in lines)])
    version = repository.product_release_tag.removeprefix("v")
    return f"""> [!IMPORTANT]
> **HyperFlux Next {version} is the current release.** It is available for Arch
> Linux x86_64 from the signed HyperFlux package repository.
> Read the [{repository.product_release_tag} release notes]({repository.product_release_url})."""


def recommended_package(catalog: DistributionCatalog) -> str:
    arch = catalog.targets["arch"]
    core = arch.components["linux"]
    full = arch.components["full"]
    return f"""You need Arch Linux on an x86_64 computer and the receiver, with your keyboard
and mouse paired to it.

1. **Add the HyperFlux package repository.** You do this once. Follow
   [step 1 of the installation guide](docs/user/installation.md#1-add-the-hyperflux-repository-one-time-only).
   It is a signed project-operated Arch repository,
   not an official Arch Linux repository.
2. **Install HyperFlux Next.**

   ```bash
   sudo pacman -Syu {full.package_name}
   ```

   This installs Full, the recommended package. It brings Core with it. If you
   only want the receiver driver and the SDK, without the app connections,
   install Core instead: `sudo pacman -Syu {core.package_name}`.
3. **Turn on HyperFlux and restart.** Keep the receiver plugged in.

   ```bash
   sudo systemctl enable hyperflux-next-bridge.service \\
     "hyperflux-next-client@$(id -u).socket"
   sudo reboot
   ```

4. **Check that it works.** After you log in again, run:

   ```bash
   hyperfluxctl doctor
   ```

   You should see `HyperFlux Next is ready.` If you see
   `HyperFlux Next needs attention.` instead, do what its `Do:` line says.

Later updates arrive with your normal `sudo pacman -Syu`."""


def _wrapped(text: str) -> str:
    return "\n".join(
        textwrap.wrap(
            text,
            width=80,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


_APPLICATION_LABELS = {
    ("openrazer", "openrgb"): None,
    ("openrazer",): "OpenRazer and Polychromatic only",
    ("openrgb",): "OpenRGB only",
}


def _device_label(device: dict[str, Any]) -> str:
    kind = device["identity"]["device_kind"]
    working = tuple(
        name
        for name in ("openrazer", "openrgb")
        if device["integrations"][name]["state"] == "source-validated"
    )
    note = _APPLICATION_LABELS.get(working)
    detail = kind if note is None else f"{kind}, {note}"
    return f"{device['model_name']} ({detail})"


def _series(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def compatibility_summary(
    readiness: dict[str, Any],
    openrazer: OpenRazerNativeDeployment,
    source_validated_devices: dict[str, Any],
) -> str:
    devices = source_validated_devices["devices"]
    counts = public_support_tier_counts(devices)
    grouped = {
        tier: [device for device in devices if public_support_tier(device) == tier]
        for tier in (
            PHYSICALLY_PROVEN,
            SOURCE_QUALIFIED_OPERATIONAL,
            UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
        )
    }
    tested = "\n".join(
        f"- {_device_label(device)}" for device in grouped[PHYSICALLY_PROVEN]
    )
    expected = "\n".join(
        f"- {_device_label(device)}"
        for device in grouped[SOURCE_QUALIFIED_OPERATIONAL]
    )
    waiting = _wrapped(
        f"**Not usable yet.** {counts[UPSTREAM_BLOCKED_OR_RESEARCH_ONLY]} more "
        "devices are known, but they need support in OpenRazer or OpenRGB first: "
        + _series(
            [device["model_name"] for device in grouped[UPSTREAM_BLOCKED_OR_RESEARCH_ONLY]]
        )
        + "."
    )
    expected_intro = _wrapped(
        f"**Expected to work.** Nobody has tested these "
        f"{counts[SOURCE_QUALIFIED_OPERATIONAL]} devices with HyperFlux on real "
        "hardware yet, but they should work:"
    )
    return f"""Unless a device says otherwise, it works in OpenRazer, Polychromatic, and
OpenRGB.

**Tested on real hardware.** These work through the receiver:

{tested}

{expected_intro}

{expected}

{waiting}

HyperFlux Next is tested with OpenRazer `{openrazer.compatibility.verified_upstream_version}`."""
