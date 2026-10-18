# SPDX-License-Identifier: GPL-2.0-only
"""Generated install steps for the apt and rpm-md repositories on the Pages site.

Every address, file name, and fingerprint comes from the repository site
policy, the distribution catalog, and the Arch key policy, so the guides
describe exactly the files ``./hfx repositories assemble`` writes.
"""

from __future__ import annotations

import textwrap

from ..arch_repository import ArchRepositoryPolicy
from ..package_repositories import (
    APT_KEYRING_DIRECTORY,
    GUIDES,
    RepositorySitePolicy,
    SiteTarget,
    configuration_text,
)
from .distributions import PRERELEASE_NOTE


FULL_PACKAGE = "hyperflux-next-full"
CORE_PACKAGE = "hyperflux-next-linux"


def block_id(target_id: str) -> str:
    return f"{target_id}-repository-setup"


def _grouped(fingerprint: str) -> str:
    return " ".join(fingerprint[index : index + 4] for index in range(0, len(fingerprint), 4))


def _not_yet(site: RepositorySitePolicy, target: SiteTarget, maturity: str) -> str:
    if site.published and maturity == "published":
        lines = [
            f"The {target.name} packages are built and tested automatically, but not yet "
            "confirmed on real hardware.",
        ]
        if target.id in PRERELEASE_NOTE:
            lines.append(PRERELEASE_NOTE[target.id])
        wrapped = textwrap.wrap(" ".join(lines), width=74, break_long_words=False)
        return "> [!NOTE]\n" + "\n".join(f"> {line}" for line in wrapped) + "\n\n"
    return f"""> [!NOTE]
> The {target.name} packages are not published yet. These steps show how
> installing will work once they are. Until then the repository address does
> not answer, and the key download in step 1 stops with an error.

"""


def _fingerprint_check(arch: ArchRepositoryPolicy, key_file: str, remove: str) -> str:
    return f"""   ```bash
   gpg --show-keys --with-subkey-fingerprints {key_file}
   ```

   You should see exactly these two fingerprints. If either one is different,
   stop here and delete the file with `{remove}`.

   - Main key: `{arch.primary_fingerprint}`
   - Signing subkey: `{arch.signing_subkey_fingerprint}`"""


def _apt_steps(site: RepositorySitePolicy, arch: ArchRepositoryPolicy, target: SiteTarget) -> str:
    key_file = f"{APT_KEYRING_DIRECTORY}/{site.repository_id}.asc"
    sources = f"/etc/apt/sources.list.d/{site.repository_id}.sources"
    contents = configuration_text(site, target, site.site_url).rstrip()
    return f"""1. **Download the HyperFlux key.** The key proves that the packages really
   come from HyperFlux. If `curl` is missing, install it first with
   `sudo apt install curl`.

   ```bash
   sudo install -d -m 0755 {APT_KEYRING_DIRECTORY}
   sudo curl --proto '=https' --tlsv1.2 --fail --location \\
     --output {key_file} -- \\
     {site.site_url}/{site.public_key_asset}
   ```

2. **Check the key.**

{_fingerprint_check(arch, key_file, f"sudo rm {key_file}")}

3. **Add the repository.** This saves one small file that tells apt where
   HyperFlux is and that only this key may sign it.

   ```bash
   sudo curl --proto '=https' --tlsv1.2 --fail --location \\
     --output {sources} -- \\
     {site.site_url}/{target.configuration_path(site)}
   ```

   The file holds these lines:

   ```text
{_indent(contents)}
   ```

4. **Install HyperFlux Next.** This installs Full:

   ```bash
   sudo apt update
   sudo apt install {FULL_PACKAGE}
   ```

   To install Core instead, run `sudo apt install {CORE_PACKAGE}`.

   apt lists what it is about to install and asks
   `Do you want to continue? [Y/n]`. Press Enter. If {target.distribution}'s own
   `openrazer-driver-dkms` package is installed, apt removes it. Full brings
   its own copy of that driver, so OpenRazer keeps working."""


def _rpm_steps(site: RepositorySitePolicy, arch: ArchRepositoryPolicy, target: SiteTarget) -> str:
    key_file = site.public_key_asset
    contents = configuration_text(site, target, site.site_url).rstrip()
    repo_url = f"{site.site_url}/{target.configuration_path(site)}"
    if target.id == "fedora":
        add = f"""   ```bash
   sudo curl --proto '=https' --tlsv1.2 --fail --location \\
     --output /etc/yum.repos.d/{site.repository_id}.repo -- \\
     {repo_url}
   ```"""
        install = f"""   ```bash
   sudo dnf install {FULL_PACKAGE}
   ```

   To install Core instead, run `sudo dnf install {CORE_PACKAGE}`.

   dnf lists what it is about to install and asks `Is this ok [y/N]`. Type `y`
   and press Enter. If OpenRazer's own `openrazer-kernel-modules-dkms` package
   is installed, dnf stops because the two conflict. Then run
   `sudo dnf install --allowerasing {FULL_PACKAGE}`, which removes it. Full
   brings its own copy of that driver, so OpenRazer keeps working."""
        tool = "dnf"
    else:
        add = f"""   ```bash
   sudo zypper addrepo {repo_url}
   sudo zypper refresh
   ```"""
        install = f"""   ```bash
   sudo zypper install {FULL_PACKAGE}
   ```

   To install Core instead, run `sudo zypper install {CORE_PACKAGE}`.

   zypper lists what it is about to install and asks `Continue? [y/n/v/...? shows all options] (y)`.
   Press Enter. If OpenRazer's own `openrazer-kernel-modules-dkms` package is
   installed, zypper reports a conflict and offers solutions. Choose the one
   that deinstalls `openrazer-kernel-modules-dkms`. Full brings its own copy
   of that driver, so OpenRazer keeps working."""
        tool = "zypper"
    return f"""1. **Download the HyperFlux key.** The key proves that the packages really
   come from HyperFlux.

   ```bash
   curl --proto '=https' --tlsv1.2 --fail --location --remote-name -- \\
     {site.site_url}/{site.public_key_asset}
   ```

2. **Check the key, then trust it.**

{_fingerprint_check(arch, key_file, f"rm {key_file}")}

   If both match, give the key to the package manager:

   ```bash
   sudo rpm --import {key_file}
   ```

3. **Add the repository.** This tells {tool} where HyperFlux is and to check
   the signature on every package and on the package list.

{add}

   The repository file holds these lines:

   ```ini
{_indent(contents)}
   ```

4. **Install HyperFlux Next.** This installs Full:

{install}"""


def _indent(text: str) -> str:
    return "\n".join(f"   {line}" if line else "" for line in text.splitlines())


def repository_setup(
    site: RepositorySitePolicy,
    arch: ArchRepositoryPolicy,
    target: SiteTarget,
    maturity: str,
) -> str:
    """Add the key, add the repository, install: the generated part of one guide."""
    steps = _apt_steps(site, arch, target) if target.package_format == "deb" else _rpm_steps(site, arch, target)
    return f"""{_not_yet(site, target, maturity)}You do steps 1 to 3 once. The key's fingerprint is
`{_grouped(arch.primary_fingerprint)}`.

{steps}

This is a signed repository run by the HyperFlux project, not an official
{target.distribution} repository."""


def install_table(
    site: RepositorySitePolicy,
    targets: dict[str, SiteTarget],
    maturities: dict[str, str],
) -> str:
    """The README's list of guides, one row per distribution."""

    def state(target_id: str) -> str:
        if target_id == "arch":
            return "Ready"
        if site.published and maturities[target_id] == "published":
            return "Ready"
        return "Not published yet"

    def listed(names: list[str]) -> str:
        if len(names) < 3:
            return " and ".join(names)
        return f"{', '.join(names[:-1])}, and {names[-1]}"

    names = {"arch": "Arch Linux", **{t.id: t.distribution for t in targets.values()}}
    ready = [names[t] for t in names if state(t) == "Ready"]
    waiting = [names[t] for t in names if state(t) != "Ready"]
    availability = (
        f"Packages are published for {listed(ready)}. The {listed(waiting)} "
        "packages are built and tested in CI but not published yet; their guides "
        "show how installing will work."
        if waiting
        else "Packages are published for every distribution in the table."
    )
    untested = [names[t] for t in names if t != "arch" and state(t) == "Ready"]
    if untested:
        availability += (
            f" The {listed(untested)} packages are built and tested automatically, "
            "but not yet confirmed on real hardware."
        )

    rows = [
        f"| Arch Linux | rolling | [Install on Arch Linux]({GUIDES['arch']}) | {state('arch')} |",
        *(
            f"| {target.distribution} | {target.release} | "
            f"[Install on {target.distribution}]({GUIDES[target.id]}) | {state(target.id)} |"
            for target in targets.values()
        ),
    ]
    return "\n".join(
        (
            "Pick your Linux distribution and follow its guide. Each guide adds the",
            "signed HyperFlux repository, installs HyperFlux Next, and checks that it",
            "works.",
            "",
            "| Distribution | Release | Guide | Packages |",
            "| --- | --- | --- | --- |",
            *rows,
            "",
            *textwrap.wrap(availability, width=78, break_long_words=False, break_on_hyphens=False),
        )
    )
