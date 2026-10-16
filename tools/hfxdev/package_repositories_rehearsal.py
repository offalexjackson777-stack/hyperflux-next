# SPDX-License-Identifier: GPL-2.0-only
"""Prove the repository site end to end without the real signing key.

The rehearsal assembles the site with ``file:///site`` as its address, signs
it inside the pinned Fedora image with a throwaway key that lives only in a
tmpfs of that container, and then installs HyperFlux from the signed site in
every pinned distribution image: apt on Ubuntu and Debian, dnf on Fedora,
zypper on openSUSE, and pacman in the development image. Each client is also
shown to refuse the site when it trusts a different key. The throwaway key's
public half is kept in the output directory as evidence; its secret half never
leaves the container.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path

from .arch_repository import load_arch_repository_policy, repository_configuration
from .package_repositories import (
    ARCH_DIRECTORY,
    ContainerTools,
    _execute,
    assemble_site,
    load_repository_site_policy,
    site_targets,
)
from .model import ModelError


REHEARSAL_SITE_URL = "file:///site"
DEVELOPMENT_IMAGE = "localhost/hyperflux-next-dev:ci"
INSTALL_PACKAGE = "hyperflux-next-linux"
THROWAWAY_KEY = "throwaway-repository-key.asc"


@dataclass(frozen=True)
class RehearsalRun:
    name: str
    image: str
    script: str
    expect_success: bool
    mounts: tuple[tuple[str, str, str], ...]
    tmpfs: tuple[str, ...] = ()


def signing_script() -> str:
    """Generate a throwaway key in a tmpfs and sign /work/unsigned with it."""
    return r"""set -euo pipefail
umask 077
dnf install -y -q --setopt=install_weak_deps=False \
  gnupg2 rpm-sign createrepo_c python3 bsdtar >/dev/null
mkdir -p -m 0700 /run/user/0
export GNUPGHOME=/keys/home
mkdir -m 0700 "$GNUPGHOME"
gpg --batch --quiet --passphrase '' \
  --quick-generate-key 'Throwaway Rehearsal <throwaway@invalid>' rsa3072 cert 1d
primary="$(gpg --batch --with-colons --list-keys | awk -F: '/^fpr:/ {print $10; exit}')"
gpg --batch --quiet --passphrase '' --quick-add-key "$primary" rsa3072 sign 1d
subkey="$(gpg --batch --with-colons --list-keys "$primary" | awk -F: '/^fpr:/ {print $10}' | sed -n 2p)"
umask 022
gpg --batch --armor --export "$primary" > /work/throwaway-repository-key.asc
cp -a /work/unsigned /work/unsigned-throwaway
cp /work/throwaway-repository-key.asc /work/unsigned-throwaway/hyperflux-next-repository-key.asc
export PYTHONDONTWRITEBYTECODE=1
python3 - "$primary" "$subkey" <<'PYTHON'
import json
import sys
from pathlib import Path

sys.path.insert(0, "/src/tools")
from hfxdev.package_repositories import LocalTools, SigningIdentity, sign_site, verify_site

root = Path("/src")
work = Path("/work")
identity = SigningIdentity(Path("/keys/home"), sys.argv[1], sys.argv[2])
sign_site(
    root,
    work / "unsigned-throwaway",
    output=work / "signed",
    identity=identity,
    arch_repository=Path("/arch"),
    tools=LocalTools(),
    trusted_key=work / "throwaway-repository-key.asc",
)
report = verify_site(
    root,
    work / "signed",
    trusted_key=work / "throwaway-repository-key.asc",
    signer=(sys.argv[1], sys.argv[2]),
)
(work / "signed-verification.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
(work / "throwaway-fingerprints.json").write_text(
    json.dumps({"primary": sys.argv[1], "signing_subkey": sys.argv[2]}, indent=2) + "\n"
)
PYTHON
rpm -q gnupg2 rpm-sign createrepo_c
"""


def apt_script(target_id: str, key: str, repository_id: str, *, expect_success: bool) -> str:
    if expect_success:
        body = (
            "apt-get update -q\n"
            f"apt-get install -y -q --no-install-recommends {INSTALL_PACKAGE}\n"
            f"dpkg-query -W -f='${{Package}} ${{Version}} ${{Status}}\\n' {INSTALL_PACKAGE} hyperflux-next-dkms\n"
            f"apt-cache policy {INSTALL_PACKAGE}\n"
        )
    else:
        # Only this one source, so the base archive cannot hide the failure.
        body = (
            "mkdir /tmp/parts\n"
            f"cp /etc/apt/sources.list.d/{repository_id}.sources /tmp/parts/\n"
            "if apt-get update -q -o Dir::Etc::SourceList=/dev/null "
            "-o Dir::Etc::SourceParts=/tmp/parts >/tmp/update.log 2>&1; "
            "then cat /tmp/update.log; exit 1; fi\n"
            "cat /tmp/update.log\n"
            "grep -E 'NO_PUBKEY|not signed|BADSIG' /tmp/update.log\n"
            f"! apt-cache show {INSTALL_PACKAGE} >/dev/null 2>&1\n"
        )
    return (
        "set -euo pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "echo 'Acquire::Retries \"5\";' > /etc/apt/apt.conf.d/80-retries\n"
        "printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod 0755 /usr/sbin/policy-rc.d\n"
        "install -d -m 0755 /etc/apt/keyrings\n"
        f"install -m 0644 {key} /etc/apt/keyrings/{repository_id}.asc\n"
        f"install -m 0644 /site/{target_id}/{repository_id}.sources /etc/apt/sources.list.d/{repository_id}.sources\n"
        f"{body}"
    )


def dnf_script(key: str, repository_id: str, *, expect_success: bool) -> str:
    repo = f"/etc/yum.repos.d/{repository_id}.repo"
    if expect_success:
        body = (
            f"dnf install -y -q --setopt=install_weak_deps=False {INSTALL_PACKAGE}\n"
            f"rpm -q {INSTALL_PACKAGE} hyperflux-next-dkms\n"
            "rpm -K /site/fedora/*/*/packages/*.rpm\n"
        )
    else:
        body = (
            f"sed -i 's|^gpgkey=.*|gpgkey=file://{key}|' {repo}\n"
            # dnf5 reports a bad repomd.xml signature, drops the repository and
            # still exits 0 from makecache, so the proof is that install fails.
            f"if dnf install -y --repo {repository_id} --setopt=install_weak_deps=False "
            f"{INSTALL_PACKAGE} >/tmp/dnf.log 2>&1; then cat /tmp/dnf.log; exit 1; fi\n"
            "cat /tmp/dnf.log\n"
            "grep -E 'repomd.xml GPG signature verification error' /tmp/dnf.log\n"
            f"if rpm -q {INSTALL_PACKAGE}; then exit 1; fi\n"
        )
    return (
        "set -euo pipefail\n"
        f"rpm --import {key}\n"
        f"install -m 0644 /site/fedora/{repository_id}.repo {repo}\n"
        f"{body}"
    )


def zypper_script(key: str, repository_id: str, *, expect_success: bool) -> str:
    if expect_success:
        body = (
            f"zypper --non-interactive refresh --repo {repository_id}\n"
            # The image's Tumbleweed update repository can be empty, and an
            # install refreshes every repository, so leave only the ones needed.
            "zypper --non-interactive modifyrepo --disable repo-update || true\n"
            f"zypper --non-interactive install --no-recommends {INSTALL_PACKAGE}\n"
            f"rpm -q {INSTALL_PACKAGE} hyperflux-next-dkms\n"
        )
    else:
        body = (
            f"if zypper --non-interactive refresh --repo {repository_id} >/tmp/zypper.log 2>&1; then cat /tmp/zypper.log; exit 1; fi\n"
            "cat /tmp/zypper.log\n"
            "grep -iE 'signature|gpg|key' /tmp/zypper.log\n"
        )
    edit = (
        ""
        if expect_success
        else f"sed -i 's|^gpgkey=.*|gpgkey=file://{key}|' /tmp/{repository_id}.repo\n"
    )
    return (
        "set -euo pipefail\n"
        f"rpm --import {key}\n"
        f"cp /site/opensuse/{repository_id}.repo /tmp/{repository_id}.repo\n"
        f"{edit}"
        f"zypper --non-interactive addrepo /tmp/{repository_id}.repo\n"
        f"{body}"
    )


def pacman_script(configuration: str, primary: str, *, expect_success: bool) -> str:
    trust = (
        "pacman-key --add /src/packaging/keys/hyperflux-next-arch-repository-key.asc\n"
        f"pacman-key --lsign-key {primary}\n"
        if expect_success
        else ""
    )
    if expect_success:
        body = (
            f"pacman -Sy --noconfirm --needed {INSTALL_PACKAGE}\n"
            f"pacman -Q {INSTALL_PACKAGE}\n"
            f"pacman -Si {INSTALL_PACKAGE} | grep -E '^(Repository|Version)'\n"
        )
    else:
        body = (
            "if pacman -Sy --noconfirm >/tmp/pacman.log 2>&1; then cat /tmp/pacman.log; exit 1; fi\n"
            "cat /tmp/pacman.log\n"
            "grep -iE 'signature|unknown trust|invalid or corrupted' /tmp/pacman.log\n"
        )
    return (
        "set -euo pipefail\n"
        "pacman-key --init >/dev/null 2>&1\n"
        "pacman-key --populate archlinux >/dev/null 2>&1\n"
        f"{trust}"
        f"cat >> /etc/pacman.conf <<'PACMAN'\n\n{configuration}PACMAN\n"
        f"{body}"
    )


def consumption_runs(root: Path, signed: Path, throwaway_key: Path) -> list[RehearsalRun]:
    policy = load_repository_site_policy(root)
    arch = load_arch_repository_policy(root)
    targets = site_targets(root)
    real_key = root / arch.public_key_path
    site_mount = (str(signed), "/site", "ro")
    good = (str(throwaway_key), "/keys/throwaway.asc", "ro")
    wrong = (str(real_key), "/keys/other.asc", "ro")
    runs: list[RehearsalRun] = []
    for target_id in ("ubuntu", "debian"):
        image = targets[target_id].image
        runs.append(
            RehearsalRun(
                f"{target_id}-apt-install",
                image,
                apt_script(target_id, "/keys/throwaway.asc", policy.repository_id, expect_success=True),
                True,
                (site_mount, good),
            )
        )
        runs.append(
            RehearsalRun(
                f"{target_id}-apt-refuses-other-key",
                image,
                apt_script(target_id, "/keys/other.asc", policy.repository_id, expect_success=False),
                True,
                (site_mount, wrong),
            )
        )
    fedora = targets["fedora"].image
    runs.append(
        RehearsalRun(
            "fedora-dnf-install",
            fedora,
            dnf_script("/keys/throwaway.asc", policy.repository_id, expect_success=True),
            True,
            (site_mount, good),
        )
    )
    runs.append(
        RehearsalRun(
            "fedora-dnf-refuses-other-key",
            fedora,
            dnf_script("/keys/other.asc", policy.repository_id, expect_success=False),
            True,
            (site_mount, wrong),
        )
    )
    opensuse = targets["opensuse"].image
    runs.append(
        RehearsalRun(
            "opensuse-zypper-install",
            opensuse,
            zypper_script("/keys/throwaway.asc", policy.repository_id, expect_success=True),
            True,
            (site_mount, good),
        )
    )
    runs.append(
        RehearsalRun(
            "opensuse-zypper-refuses-other-key",
            opensuse,
            zypper_script("/keys/other.asc", policy.repository_id, expect_success=False),
            True,
            (site_mount, wrong),
        )
    )
    configuration = repository_configuration(
        replace(arch, server_url=f"{REHEARSAL_SITE_URL}/{ARCH_DIRECTORY}")
    )
    source = (str(root), "/src", "ro")
    runs.append(
        RehearsalRun(
            "arch-pacman-install",
            DEVELOPMENT_IMAGE,
            pacman_script(configuration, arch.primary_fingerprint, expect_success=True),
            True,
            (site_mount, source),
        )
    )
    runs.append(
        RehearsalRun(
            "arch-pacman-refuses-untrusted-key",
            DEVELOPMENT_IMAGE,
            pacman_script(configuration, arch.primary_fingerprint, expect_success=False),
            True,
            (site_mount, source),
        )
    )
    return runs


def container_command(engine: str, run: RehearsalRun) -> list[str]:
    command = [engine, "run", "--rm", "--user", "0"]
    for source, destination, mode in run.mounts:
        command.extend(("--volume", f"{source}:{destination}:{mode}"))
    for mount in run.tmpfs:
        command.extend(("--tmpfs", mount))
    command.extend((run.image, "/bin/bash", "-c", run.script))
    return command


def _run_logged(engine: str, run: RehearsalRun, logs: Path) -> dict[str, object]:
    log = logs / f"{run.name}.log"
    try:
        result = _execute(container_command(engine, run), label=run.name, timeout_seconds=1800)
    except ModelError as error:
        log.write_text(f"$ {run.name}\n{error}\n", encoding="utf-8")
        raise
    log.write_text(
        f"$ {run.name} ({run.image})\n--- script\n{run.script}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}",
        encoding="utf-8",
    )
    return {"name": run.name, "image": run.image, "passed": True, "log": log.name}


def rehearse(
    root: Path,
    *,
    packages: dict[str, Path],
    arch_repository: Path,
    output: Path,
) -> Path:
    if output.exists():
        raise ModelError("rehearsal output must be a new directory")
    tools = ContainerTools()
    engine = tools.engine
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    assemble_site(
        root,
        packages=packages,
        output=output / "unsigned",
        site_url=REHEARSAL_SITE_URL,
        tools=tools,
    )
    signing = RehearsalRun(
        "sign-with-throwaway-key",
        site_targets(root)["fedora"].image,
        signing_script(),
        True,
        (
            (str(root), "/src", "ro"),
            (str(output), "/work", "rw"),
            (str(arch_repository), "/arch", "ro"),
        ),
        ("/keys:rw,mode=0700",),
    )
    results = [_run_logged(engine, signing, logs)]
    signed = output / "signed"
    throwaway_key = output / THROWAWAY_KEY
    for run in consumption_runs(root, signed, throwaway_key):
        results.append(_run_logged(engine, run, logs))
    receipt = output / "rehearsal-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "hyperflux-repository-rehearsal-v1",
                "site_url": REHEARSAL_SITE_URL,
                "throwaway_key": json.loads((output / "throwaway-fingerprints.json").read_text()),
                "runs": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return receipt

