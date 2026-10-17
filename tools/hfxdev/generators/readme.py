# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
import textwrap
from typing import Any

from ..arch_repository import ArchRepositoryPolicy
from ..distributions import DistributionCatalog
from ..openrazer_native_route import OpenRazerNativeDeployment
from ..source_validated_catalog import (
    OWNER_CONFIRMED,
    PHYSICALLY_PROVEN,
    SOURCE_QUALIFIED_OPERATIONAL,
    UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
    documented_support_tier,
    documented_support_tier_counts,
)


def _text_width(text: str) -> int:
    """Approximate rendered width of 11px Verdana, as status badges use."""
    narrow = set("fijlrt.,:;'|!()[] -")
    wide = set("mwMW")
    return round(sum(4.0 if c in narrow else 9.5 if c in wide else 7.0 for c in text))


def _badge(label: str, value: str, color: str) -> str:
    """A flat status badge drawn like the GitHub workflow badges beside it."""
    label_width = _text_width(label) + 12
    value_width = _text_width(value) + 12
    total = label_width + value_width
    label_text = _text_width(label)
    value_text = _text_width(value)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{escape(label)}: {escape(value)}">
  <title>{escape(label)}: {escape(value)}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#fff" stop-opacity=".15"/>
    <stop offset="1" stop-opacity=".15"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total}" height="20" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_width}" height="20" fill="#24292e"/>
    <rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_width / 2:g}" y="15" fill="#010101" fill-opacity=".3" textLength="{label_text}">{escape(label)}</text>
    <text x="{label_width / 2:g}" y="14" textLength="{label_text}">{escape(label)}</text>
    <text x="{label_width + value_width / 2:g}" y="15" fill="#010101" fill-opacity=".3" textLength="{value_text}">{escape(value)}</text>
    <text x="{label_width + value_width / 2:g}" y="14" textLength="{value_text}">{escape(value)}</text>
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
    return f"""**HyperFlux Next {version} is the current release.** Read the
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
    """The four steps every install guide shares, with Arch's commands named as Arch's."""
    arch = catalog.targets["arch"]
    core = arch.components["linux"]
    full = arch.components["full"]
    core_only = any(
        "full" not in target.components
        for target_id, target in catalog.targets.items()
        if target_id != "arch"
    )
    other_distributions = (
        "\n   The other distributions get Core only for now, without the app\n"
        "   connections; their guides give the `apt`, `dnf`, or `zypper` command."
        if core_only
        else ""
    )
    return f"""Every guide has the same four steps. You need an x86_64 computer and the
receiver, with your keyboard and mouse paired to it.

1. **Add the HyperFlux package repository.** You do this once, as your guide
   shows. On Arch Linux, follow
   [step 1 of the Arch Linux guide](docs/user/installation.md#1-add-the-hyperflux-repository-one-time-only).
   It is a signed repository run by the HyperFlux project, not an official
   repository of your distribution.
2. **Install HyperFlux Next.** On Arch Linux, install Full, the recommended
   package:

   ```bash
   sudo pacman -Syu {full.package_name}
   ```

   Full adds the connections to OpenRazer, Polychromatic, and OpenRGB, and
   brings Core with it. Core alone is the receiver driver, the background
   service, `hyperfluxctl`, and the SDK: `sudo pacman -Syu {core.package_name}`.{other_distributions}
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

On Arch Linux, later updates arrive with your normal system update."""


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
    owner_confirmed: dict[str, tuple[int, ...]] | None = None,
) -> str:
    owner_confirmed = owner_confirmed or {}
    devices = source_validated_devices["devices"]
    counts = documented_support_tier_counts(devices, owner_confirmed)
    grouped = {
        tier: [
            device
            for device in devices
            if documented_support_tier(device, owner_confirmed) == tier
        ]
        for tier in (
            PHYSICALLY_PROVEN,
            OWNER_CONFIRMED,
            SOURCE_QUALIFIED_OPERATIONAL,
            UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
        )
    }
    confirmed = ""
    if grouped[OWNER_CONFIRMED]:
        confirmed_list = "\n".join(
            f"- {_device_label(device)}" for device in grouped[OWNER_CONFIRMED]
        )
        confirmed_intro = _wrapped(
            "**Confirmed by owners.** People who own these devices checked that "
            "they work through the receiver; the project has not tested them itself:"
        )
        confirmed = f"{confirmed_intro}\n\n{confirmed_list}\n\n"
    tested = "\n".join(
        f"- {_device_label(device)}" for device in grouped[PHYSICALLY_PROVEN]
    )
    expected = "\n".join(
        f"- {_device_label(device)}"
        for device in grouped[SOURCE_QUALIFIED_OPERATIONAL]
    )
    waiting = _wrapped(
        f"**Not usable yet.** {counts[UPSTREAM_BLOCKED_OR_RESEARCH_ONLY]} more "
        "devices are known, but they wait for support in OpenRazer, and OpenRGB "
        "does not support them either: "
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
    expected_block = f"{expected_intro}\n\n{expected}\n\n" if expected else ""
    return f"""Unless a device says otherwise, it works in OpenRazer, Polychromatic, and
OpenRGB.

**Tested on real hardware.** These work through the receiver:

{tested}

{confirmed}{expected_block}{waiting}

HyperFlux Next is tested with OpenRazer `{openrazer.compatibility.verified_upstream_version}`."""
