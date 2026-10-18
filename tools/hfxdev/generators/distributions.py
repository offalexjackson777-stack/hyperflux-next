# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from ..arch_split import ArchSplit
from ..distributions import (
    DEBIAN_FORMAT_TARGET_IDS,
    RPM_FORMAT_TARGET_IDS,
    DistributionCatalog,
    DistributionComponent,
    DistributionRelease,
    DistributionTarget,
    c_runtime_floor,
)
from ..install import FirstRunActivation
from ..linux_runtime import LinuxRuntime
from ..model import ModelError


def _items(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{item}`" for item in values) or "none"


def _optional_items(component: DistributionComponent) -> str:
    values = component.optional_dependencies
    return ", ".join(
        f"`{item.package}` ({item.purpose})" for item in values
    ) or "none"


# How each release's DKMS signs the driver, and the key it asks the firmware to trust.
SECURE_BOOT_ENROLLMENT = {
    "ubuntu": (
        "sudo update-secureboot-policy --enroll-key",
        "sudo mokutil --import /var/lib/shim-signed/mok/MOK.der",
    ),
    "debian": (
        "sudo mokutil --import /var/lib/dkms/mok.pub",
        None,
    ),
    # DKMS outside Ubuntu keeps its signing key in /var/lib/dkms.
    "fedora": (
        "sudo mokutil --import /var/lib/dkms/mok.pub",
        None,
    ),
    "opensuse": (
        "sudo mokutil --import /var/lib/dkms/mok.pub",
        None,
    ),
}

# Releases whose DKMS ships `dkms.service` disabled. Without it DKMS installs
# the built driver only when the next kernel is installed.
DKMS_SERVICE_DISABLED = {"opensuse"}

# Why Full on each release leaves out the OpenRGB plugin. The plugin needs
# plugin interface 5, used by OpenRGB 1.0 final and 1.1. Checked 18 October 2026.
OPENRGB_PLUGIN_ABSENT = {
    "ubuntu": (
        "Ubuntu 26.10 packages a development snapshot of OpenRGB from before 1.0, "
        "which loads plugins built for plugin interface 4 only."
    ),
    "fedora": (
        "Fedora 44 packages an OpenRGB 1.0 release candidate, which loads plugins "
        "built for plugin interface 4 only."
    ),
    "opensuse": (
        "openSUSE Tumbleweed packages an OpenRGB 1.0 release candidate, which loads "
        "plugins built for plugin interface 4 only."
    ),
}

# How Full on each release other than Arch carries the OpenRGB plugin. A plugin
# must use the same Qt as the OpenRGB that loads it.
OPENRGB_PLUGIN_RELEASE_BUILD = {
    "debian": (
        "Debian 14 packages OpenRGB 1.0 final, plugin interface 5, built with Qt 5. "
        "So Full on Debian carries a copy of the plugin built inside Debian 14 against "
        "Debian's own Qt 5, from the same source as the Arch Linux one."
    ),
}

# RPM targets do not obsolete OpenRazer's own driver package: an Obsoletes
# would let a plain upgrade swap it for ours on a computer that never asked for
# Full. So the swap is an explicit step when that package is already installed.
RPM_DRIVER_SWAP = {
    "fedora": "`sudo dnf install --allowerasing {package}`",
    "opensuse": "`sudo zypper install {package}`, then choose the solution that deinstalls it",
}
# Where each release's stock OpenRazer service comes from, for Full users who
# want the stock OpenRazer applications.
OPENRAZER_SOURCE = {
    "ubuntu": "Ubuntu 26.10 packages OpenRazer 3.12.4 as `openrazer-daemon`.",
    "debian": "Debian 14 packages OpenRazer 3.12.4 as `openrazer-daemon`.",
    "fedora": (
        "Fedora does not package OpenRazer. OpenRazer's own repository on the "
        "openSUSE Build Service provides the stock service and Polychromatic; follow "
        "OpenRazer's Fedora instructions to add it."
    ),
    "opensuse": (
        "openSUSE does not package OpenRazer. OpenRazer's own repository on the "
        "openSUSE Build Service provides the stock service and Polychromatic; follow "
        "OpenRazer's openSUSE instructions to add it."
    ),
}

# The package manager command that removes packages on each release.
REMOVE_COMMAND = {
    "ubuntu": "sudo apt remove",
    "debian": "sudo apt remove",
    "fedora": "sudo dnf remove",
    "opensuse": "sudo zypper remove",
}


# Releases that are not final yet. Their own packages can still change.
PRERELEASE_NOTE = {
    "debian": (
        "Debian 14 is still Debian's testing release, so Debian's own packages can "
        "change before it is released."
    ),
}

# The install guide for each release, relative to this generated page.
INSTALL_GUIDE = {
    "ubuntu": "../user/install-ubuntu.md",
    "debian": "../user/install-debian.md",
    "fedora": "../user/install-fedora.md",
    "opensuse": "../user/install-opensuse.md",
}


def section_heading(target: DistributionTarget) -> str:
    """The heading of one release's section, which says whether it is published."""

    release = target.release
    if release is None:
        raise ModelError(f"the {target.id} target names no release")
    name = f"{release.distribution} {release.version}"
    if target.published:
        return f"## {name} (published, not yet confirmed on real hardware)"
    return f"## {name} (CI-built, not published yet)"


def _availability(target: DistributionTarget, kind: str, name: str) -> list[str]:
    """Where a release's packages come from, in two or three plain sentences."""

    if not target.published:
        return [
            f"HyperFlux builds `{kind}` packages for {name} in CI every week and whenever a "
            "pull request changes how they are made. They are not published, so there is "
            "nothing to install from yet. They carry both editions, Core and Full."
        ]
    text = (
        f"HyperFlux publishes `{kind}` packages for {name} in its signed package "
        f"repository. [Install on {target.release.distribution}]({INSTALL_GUIDE[target.id]}) "
        "shows how to add it. The packages are built and tested automatically, every week "
        "and whenever a pull request changes how they are made, but not yet confirmed on "
        "real hardware. They carry both editions, Core and Full."
    )
    if target.id in PRERELEASE_NOTE:
        text = f"{text} {PRERELEASE_NOTE[target.id]}"
    return [text]


def _removal(
    target_id: str,
    release: DistributionRelease,
    package_names: list[str],
    activation: FirstRunActivation,
    runtime: LinuxRuntime,
) -> list[str]:
    if target_id not in REMOVE_COMMAND:
        raise ModelError(f"the {target_id} target has no removal command")
    return [
        "",
        f"### Remove HyperFlux Next on {release.distribution}",
        "",
        "Removing the packages does not stop the HyperFlux services, so turn them off first.",
        "",
        "1. Turn off the HyperFlux services:",
        "",
        "   ```sh",
        f"   {activation.disable_command(runtime)}",
        "   ```",
        "",
        "2. Remove the packages:",
        "",
        "   ```sh",
        f"   {REMOVE_COMMAND[target_id]} {' '.join(package_names)}",
        "   ```",
        "",
        "   This also removes every built copy of the driver from DKMS.",
    ]


def _full_edition(target: DistributionTarget, plans: list) -> list[str]:
    release = target.release
    if release is None or target.id == "arch":
        raise ModelError(f"the {target.id} target must be a release other than Arch Linux")
    full = target.component("full")
    linux = target.component("linux")
    driver = [plan for plan in plans if plan.provides]
    if len(driver) != 1:
        raise ModelError(f"the {target.id} Full edition must have one package that provides")
    name = f"{release.distribution} {release.version}"
    carries = (
        "OpenRazer compatibility transport and the OpenRGB plugin, so stock OpenRazer "
        "applications and OpenRGB can use your devices too."
        if target.openrgb_plugin
        else "OpenRazer compatibility transport, so stock OpenRazer applications can use "
        "your devices too."
    )
    return [
        "",
        f"### Core and Full on {release.distribution}",
        "",
        f"Core is `{linux.package_name}`. Full is `{full.package_name}`: Core plus the "
        f"{carries}",
        "",
        _driver_swap(target, driver[0], full),
        f"- {OPENRAZER_SOURCE[target.id]}",
        f"- Optional stock applications: {_optional_items(full)}.",
        "- To use the stock OpenRazer service, add yourself to the `openrazer` group, then "
        "log out and back in: `sudo gpasswd -a \"$USER\" openrazer`.",
        "",
        *_openrgb_plugin_note(target, name),
    ]


def _openrgb_plugin_note(target: DistributionTarget, name: str) -> list[str]:
    release = target.release
    if target.openrgb_plugin:
        return [
            f"**Full on {release.distribution} includes the OpenRGB plugin.** "
            f"{OPENRGB_PLUGIN_RELEASE_BUILD[target.id]} Install `openrgb` from "
            f"{release.distribution} to use it. Every change to the packages is checked "
            f"by starting {name}'s own OpenRGB and confirming it loads the plugin. "
            "The deeper check that drives the plugin through OpenRGB's software "
            "interface runs on the Arch Linux build only.",
        ]
    return [
        f"**Full on {release.distribution} has no OpenRGB plugin.** That is a limit of "
        f"what {name} packages, not of HyperFlux: {OPENRGB_PLUGIN_ABSENT[target.id]} "
        "The HyperFlux plugin is built for plugin interface 5, which OpenRGB 1.0 final "
        "and 1.1 use, and OpenRGB refuses a plugin built for another interface. Doctor "
        "reports Full as ready without it. Arch Linux packages OpenRGB 1.1 and Debian 14 "
        "packages OpenRGB 1.0 final, so Full there includes the plugin. When "
        f"{release.distribution} packages OpenRGB 1.0 final or newer, its Full packages "
        "can include the plugin.",
    ]


def _driver_swap(target: DistributionTarget, driver, full) -> str:
    release = target.release
    if driver.replaces:
        return (
            f"- `{driver.package_name}` replaces the OpenRazer driver package of "
            f"{release.distribution} ({_items(full.conflicts)}) and stands in for it, so "
            "the stock OpenRazer service still installs."
        )
    command = RPM_DRIVER_SWAP[target.id].format(package=full.package_name)
    return (
        f"- `{driver.package_name}` takes the place of OpenRazer's own driver package "
        f"({_items(full.conflicts)}) and stands in for it, so the stock OpenRazer "
        "service still installs. If OpenRazer's driver package is already installed, "
        f"the two conflict; let the package manager remove it: {command}."
    )


def _secure_boot(target_id: str, release: DistributionRelease, module: str) -> list[str]:
    enroll, fallback = SECURE_BOOT_ENROLLMENT[target_id]
    return [
        f"### Secure Boot on {release.distribution}",
        "",
        "If Secure Boot is on, Linux only loads drivers signed by a key it trusts. "
        f"DKMS on {release.distribution} signs the HyperFlux driver with a key made on "
        "your computer, so you need to tell the computer to trust that key once.",
        "",
        "1. Check whether Secure Boot is on:",
        "",
        "   ```sh",
        "   mokutil --sb-state",
        "   ```",
        "",
        "   If it says `SecureBoot disabled`, you can stop here.",
        "",
        "2. Ask to trust the key. Choose a one-time password when asked; you need it "
        "after the restart:",
        "",
        "   ```sh",
        f"   {enroll}",
        "   ```",
        "",
        *(
            [
                "   If that command says there is no key to enroll, run this instead:",
                "",
                "   ```sh",
                f"   {fallback}",
                "   ```",
                "",
            ]
            if fallback
            else []
        ),
        "3. Restart the computer. A blue MokManager screen appears. Choose "
        "**Enroll MOK**, then **Continue**, then **Yes**, type the password, and reboot.",
        "",
        "4. Check that the driver is signed:",
        "",
        "   ```sh",
        f"   modinfo -F signer {module}",
        "   ```",
        "",
        "   It prints the name of the key. An empty line means the driver is not signed.",
    ]


def _debian_format(
    catalog: DistributionCatalog,
    target_id: str,
    split: ArchSplit,
    runtime: LinuxRuntime,
    activation: FirstRunActivation,
) -> list[str]:
    target = catalog.targets[target_id]
    release = target.release
    if release is None or target.package_format != "deb":
        raise ModelError(f"the {target_id} target must be a Debian-format release")
    arch_roles = catalog.targets["arch"].dependency_roles
    from ..distribution_debian import debian_plans

    plans = debian_plans(split, target, runtime, catalog)
    module = runtime.kernel.module_name.replace("-", "_")
    lines = [
        "",
        section_heading(target),
        "",
        *_availability(
            target, ".deb", f"{release.distribution} {release.version} (`{release.codename}`)"
        ),
        "",
        "| Package | Version | Architecture | Depends |",
        "| --- | --- | --- | --- |",
    ]
    for plan in plans:
        lines.append(
            f"| `{plan.package_name}` | `{plan.version}` | `{plan.architecture}` | "
            f"{_items(plan.depends)} |"
        )
    lines.extend(
        [
            "",
            "Dependency roles map to packages on each distribution:",
            "",
            f"| Role | Arch | {release.distribution} |",
            "| --- | --- | --- |",
        ]
    )
    for role, package in target.dependency_roles.items():
        lines.append(f"| `{role}` | `{arch_roles.get(role, '-')}` | `{package}` |")
    lines.extend(_full_edition(target, plans))
    lines.extend(
        [
            "",
            "- Package scripts mirror the Arch hooks: they create the system user and "
            "directories and reload systemd and udev. They never enable, start or stop a "
            "service.",
            f"- `{plans[0].package_name}` and the Full OpenRazer driver package hand their "
            "driver sources to DKMS when they are installed and remove every built copy "
            "when they are removed.",
            f"- Programs must run on the glibc {release.c_runtime_version} that "
            f"{release.distribution} {release.version} ships.",
            *(
                [
                    f"- Full carries an OpenRGB plugin built inside {release.distribution} "
                    f"{release.version}, which needs that release's glibc, so the packages "
                    f"declare `{target.dependency_roles['c-runtime']}`."
                ]
                if target.openrgb_plugin_release_build
                else []
            ),
            "",
            *_secure_boot(target_id, release, module),
            *_removal(
                target_id, release, [plan.package_name for plan in plans], activation, runtime
            ),
        ]
    )
    return lines


def _rpm_format(
    catalog: DistributionCatalog,
    target_id: str,
    split: ArchSplit,
    runtime: LinuxRuntime,
    activation: FirstRunActivation,
) -> list[str]:
    target = catalog.targets[target_id]
    release = target.release
    if release is None or target.package_format != "rpm":
        raise ModelError(f"the {target_id} target must be an RPM release")
    arch_roles = catalog.targets["arch"].dependency_roles
    from ..distribution_rpm import rpm_plans

    plans = rpm_plans(split, target, runtime, catalog)
    module = runtime.kernel.module_name.replace("-", "_")
    name = f"{release.distribution} {release.version}"
    lines = [
        "",
        section_heading(target),
        "",
        *_availability(target, ".rpm", name),
        "",
        "| Package | Version | Architecture | Requires |",
        "| --- | --- | --- | --- |",
    ]
    for plan in plans:
        lines.append(
            f"| `{plan.package_name}` | `{plan.evr}` | `{plan.architecture}` | "
            f"{_items(plan.depends)} |"
        )
    lines.extend(
        [
            "",
            "Dependency roles map to packages on each distribution:",
            "",
            f"| Role | Arch | {release.distribution} |",
            "| --- | --- | --- |",
        ]
    )
    for role, package in target.dependency_roles.items():
        lines.append(f"| `{role}` | `{arch_roles.get(role, '-')}` | `{package}` |")
    lines.extend(_full_edition(target, plans))
    lines.extend(
        [
            "",
            "- Package scripts mirror the Arch hooks: they create the system user and "
            "directories and reload systemd and udev. They never enable, start or stop a "
            "service.",
            f"- `{plans[0].package_name}` and the Full OpenRazer driver package hand their "
            "driver sources to DKMS and build them for every kernel whose development "
            "files are installed. DKMS installs the built "
            "driver when the computer next starts, so installing the package never loads "
            "the driver. Removing the package removes every built copy.",
            f"- Programs need glibc {c_runtime_floor(target)} or newer. CI checks them on "
            f"the glibc {release.c_runtime_version} that {name} ships.",
            "",
        ]
    )
    if target_id in DKMS_SERVICE_DISABLED:
        lines.extend(
            [
                f"### Turn on DKMS at startup on {release.distribution}",
                "",
                f"{release.distribution} ships the DKMS startup service turned off. Turn it "
                "on once, so DKMS installs the HyperFlux driver when the computer starts:",
                "",
                "```sh",
                "sudo systemctl enable dkms.service",
                "```",
                "",
            ]
        )
    lines.extend(_secure_boot(target_id, release, module))
    lines.extend(
        _removal(target_id, release, [plan.package_name for plan in plans], activation, runtime)
    )
    return lines



_EDITION_NAMES = {"linux": "Core", "full": "Full"}


def _sentence_list(values: tuple[str, ...]) -> str:
    names = [f"`{value}`" for value in values]
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else "".join(names)


def _arch_split_packages(split: ArchSplit) -> list[str]:
    core = _sentence_list(split.install_set("linux"))
    full = _sentence_list(split.install_set("full"))
    lines = [
        f"From `{split.first_release}` the Arch `pkgbase={split.pkgbase}` builds "
        f"{len(split.outputs)} packages that never install the same file. Core installs "
        f"{core}. Full installs {full}. Packages for other distributions are tested "
        "separately.",
        "",
        "| Package | Edition | Required packages | Optional packages | Conflicts | Provides | Replaces |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for output in split.outputs:
        optional = ", ".join(
            f"`{item.package}` ({item.purpose})" for item in output.optional_dependencies
        ) or "none"
        lines.append(
            f"| `{output.package_name}` | {_EDITION_NAMES[output.edition]} | "
            f"{_items(output.dependencies)} | {optional} | {_items(output.conflicts)} | "
            f"{_items(output.provides)} | {_items(output.replaces)} |"
        )
    lines.extend(["", "## What Each Package Owns", ""])
    for output in split.outputs:
        lines.append(
            f"- **`{output.package_name}` ({_EDITION_NAMES[output.edition]}):** "
            f"{output.description}."
        )
    return lines


def _arch_two_packages(arch: DistributionTarget) -> list[str]:
    lines = [
        "The Arch `pkgbase=hyperflux-next` builds two user-facing packages that never "
        "install the same file. Packages for other distributions are tested separately.",
        "",
        "| Component | Package | Required packages | Optional packages | Conflicts | Provides | Replaces |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for component in arch.components.values():
        lines.append(
            f"| `{component.id}` | `{component.package_name}` | "
            f"{_items(component.dependencies)} | {_optional_items(component)} | "
            f"{_items(component.conflicts)} | "
            f"{_items(component.provides)} | {_items(component.replaces)} |"
        )
    lines.extend(
        [
            "",
            "## What Each Package Owns",
            "",
            f"- **Core (`{arch.components['linux'].package_name}`):** "
            f"{arch.components['linux'].description}.",
            f"- **Full (`{arch.components['full'].package_name}`):** "
            f"{arch.components['full'].description}.",
        ]
    )
    return lines

def markdown(
    catalog: DistributionCatalog,
    runtime: LinuxRuntime,
    activation: FirstRunActivation,
    split: ArchSplit,
) -> str:
    arch = catalog.targets["arch"]
    lines = [
        "# Distribution Packages",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        "This page lists the HyperFlux packages, what each one depends on, and what each "
        "one replaces, for packagers and anyone choosing between Core and Full.",
        "",
        f"Source digest: `{catalog.source_sha256}`.",
        "",
    ]
    if split.active:
        lines.extend(_arch_split_packages(split))
    else:
        lines.extend(_arch_two_packages(arch))
    lines.extend(
        [
            "- OpenRazer's service and Python client, Polychromatic, and OpenRGB are optional "
            "suggestions, never package payloads. They stay packaged by the distribution.",
            "",
            "## Install And First Run",
            "",
            "- Package hooks create system users/directories and reload systemd and udev registration only.",
            "- Installation does not start, stop, restart, enable, disable, or probe services or hardware.",
            "- Removal does not stop or disable services either. A running bridge keeps "
            "running until the computer restarts, so turn the services off before "
            f"removing the packages: `{activation.disable_command(runtime)}`.",
            "- First run enables the units without starting them, then reboots with the "
            f"receiver connected: `{activation.enable_command(runtime)}`, then "
            f"`{activation.activation_command()}`.",
            "- Live activation without a reboot requires disconnecting the receiver, so it "
            "is only for systems with verified alternate input: "
            f"`{activation.disconnected_command(runtime)}`.",
            "- Bridge restoration ships disabled in this package release.",
            "",
            "## CPU Architectures",
            "",
            ", ".join(
                f"`{source}` maps to `{destination}`"
                for source, destination in arch.architectures.items()
            )
            + ".",
            *(
                line
                for target_id in DEBIAN_FORMAT_TARGET_IDS
                for line in _debian_format(catalog, target_id, split, runtime, activation)
            ),
            *(
                line
                for target_id in RPM_FORMAT_TARGET_IDS
                for line in _rpm_format(catalog, target_id, split, runtime, activation)
            ),
            "",
        ]
    )
    return "\n".join(lines)
