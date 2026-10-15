# SPDX-License-Identifier: GPL-2.0-only

"""Edition install plans for the source-built AUR recipe.

One split pkgbase builds every output, and makepkg has no output selector:
`makepkg -si` installs all four packages, which would turn a Core request into
Full. The plans here derive, from the split authority alone, the exact package
set each edition installs, the helper request that selects it, and the
helper-free commands that build everything but install only that set.
"""

from __future__ import annotations

from dataclasses import dataclass

from .arch_split import ArchSplit
from .model import ModelError


DEFAULT_PKGEXT = ".pkg.tar.zst"
HELPER = "paru"
EDITION_LABELS = {"linux": "Core", "full": "Full"}
UNSAFE_BUILD = "makepkg -si"
RULE = (
    "Build with makepkg -s and install only the edition's exact package set with "
    "pacman -U; makepkg -si or makepkg -i installs every output and turns Core into Full."
)


@dataclass(frozen=True)
class InstallPlan:
    edition: str
    label: str
    request: str
    packages: tuple[str, ...]
    files: tuple[str, ...]
    helper_command: str
    manual_commands: tuple[str, ...]


def package_file(split: ArchSplit, package: str, architecture: str, pkgext: str) -> str:
    """Return the archive name makepkg writes for one output."""

    if package not in {output.package_name for output in split.outputs}:
        raise ModelError(f"AUR install plan: {package} is not an output of {split.pkgbase}")
    if not pkgext.startswith(".pkg.tar"):
        raise ModelError("AUR install plan: PKGEXT must be a .pkg.tar archive extension")
    return f"{package}-{split.full_version}-{architecture}{pkgext}"


def install_plan(
    split: ArchSplit,
    edition: str,
    *,
    architecture: str = "x86_64",
    pkgext: str = DEFAULT_PKGEXT,
) -> InstallPlan:
    if not split.active:
        raise ModelError(
            f"AUR install plan: {split.full_version} keeps the two-package topology; "
            f"the four-output split starts at {split.first_release}"
        )
    if edition not in EDITION_LABELS:
        raise ModelError(f"AUR install plan: unknown edition {edition}")
    if architecture not in split.architectures:
        raise ModelError(f"AUR install plan: {architecture} is not a recipe architecture")
    request = split.edition_output(edition).package_name
    packages = split.install_set(edition)
    files = tuple(package_file(split, package, architecture, pkgext) for package in packages)
    return InstallPlan(
        edition=edition,
        label=EDITION_LABELS[edition],
        request=request,
        packages=packages,
        files=files,
        helper_command=f"{HELPER} -S {request}",
        manual_commands=("makepkg -s", "sudo pacman -U " + " ".join(files)),
    )


def install_plans(
    split: ArchSplit, *, architecture: str = "x86_64", pkgext: str = DEFAULT_PKGEXT
) -> tuple[InstallPlan, ...]:
    plans = tuple(
        install_plan(split, edition, architecture=architecture, pkgext=pkgext)
        for edition in EDITION_LABELS
    )
    core, full = plans
    if not set(core.packages) < set(full.packages):
        raise ModelError("AUR install plan: Core must be a strict subset of Full")
    full_only = {
        output.package_name for output in split.outputs if output.edition == "full"
    }
    if full_only & set(core.packages):
        raise ModelError("AUR install plan: Core would install a Full output")
    if set(full.packages) != {output.package_name for output in split.outputs}:
        raise ModelError("AUR install plan: Full must install every output")
    return plans


def pkgbuild_guidance(split: ArchSplit, architecture: str) -> list[str]:
    """Comment lines for the PKGBUILD header; they never run."""

    lines = [
        "# Choose one edition. Do not run makepkg -i or makepkg -si: this pkgbase",
        "# builds every output, and installing them all turns Core into Full.",
    ]
    for plan in install_plans(split, architecture=architecture):
        lines.append(f"# {plan.label} with an AUR helper: {plan.helper_command}")
        lines.append(f"# {plan.label} without a helper (default PKGEXT and PKGDEST):")
        lines.extend(f"#   {command}" for command in plan.manual_commands)
    return lines


def render_guide(split: ArchSplit, architecture: str = "x86_64") -> str:
    """Plain-text install guide printed by `./hfx package aur-install`."""

    lines = [f"{split.pkgbase} {split.full_version} ({architecture})", RULE, ""]
    for plan in install_plans(split, architecture=architecture):
        lines.append(f"{plan.label}: {', '.join(plan.packages)}")
        lines.append(f"  helper:  {plan.helper_command}")
        for command in plan.manual_commands:
            lines.append(f"  manual:  {command}")
        lines.append("")
    lines.append(
        "If makepkg.conf sets PKGEXT or PKGDEST, use that extension or directory "
        "for the same package names; never widen the pacman -U set."
    )
    return "\n".join(lines) + "\n"
