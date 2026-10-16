# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from ..arch_split import ArchSplit
from ..distributions import DEBIAN_FORMAT_TARGET_IDS, DistributionCatalog, DistributionComponent
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
}


def _debian_format(
    catalog: DistributionCatalog,
    target_id: str,
    split: ArchSplit,
    runtime: LinuxRuntime,
) -> list[str]:
    target = catalog.targets[target_id]
    release = target.release
    if release is None or target.published:
        raise ModelError(f"the {target_id} target must be an unpublished release")
    enroll, fallback = SECURE_BOOT_ENROLLMENT[target_id]
    arch_roles = catalog.targets["arch"].dependency_roles
    from ..distribution_debian import debian_plans

    plans = debian_plans(split, target, runtime, catalog)
    module = runtime.kernel.module_name.replace("-", "_")
    lines = [
        "",
        f"## {release.distribution} {release.version} (CI-built, not published yet)",
        "",
        f"HyperFlux builds `.deb` packages for {release.distribution} {release.version} "
        f"(`{release.codename}`) every week in CI. They are not published, so there is "
        "nothing to install from yet. The packages carry only the Core edition; there "
        f"is no Full edition for {release.distribution}.",
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
    lines.extend(
        [
            "",
            "- Package scripts mirror the Arch hooks: they create the system user and "
            "directories and reload systemd and udev. They never enable or start a service.",
            f"- `{plans[0].package_name}` hands the driver source to DKMS when it is "
            "installed and removes every built copy when it is removed.",
            f"- Programs must run on the glibc {release.c_runtime_version} that "
            f"{release.distribution} {release.version} ships.",
            "",
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
            "- Stock applications are optional suggestions rather than package payloads.",
            "- Stock OpenRazer daemon/client, Polychromatic, and OpenRGB remain distribution-owned applications.",
            "",
            "## Install And First Run",
            "",
            "- Package hooks create system users/directories and reload systemd and udev registration only.",
            "- Installation does not start, stop, restart, enable, disable, or probe services or hardware.",
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
                for line in _debian_format(catalog, target_id, split, runtime)
            ),
            "",
        ]
    )
    return "\n".join(lines)
