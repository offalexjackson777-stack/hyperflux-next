# SPDX-License-Identifier: GPL-2.0-only
"""The one package repository site for every distribution.

A single GitHub Pages site carries five repositories under one front page:

- ``arch/``: the signed Arch repository, copied byte for byte from the
  directory ``./hfx arch-repository build`` produces. The same directory still
  goes to the ``arch-repository`` release, which stays a mirror.
- ``ubuntu/`` and ``debian/``: apt repositories, one suite per release
  (``dists/<codename>``) and one ``pool/``.
- ``fedora/`` and ``opensuse/``: rpm-md repositories with ``repodata/`` and a
  ``.repo`` file that turns on both package and metadata signature checks.

The work is split in three so the signing key never leaves the maintainer's
machine. ``assemble`` builds the unsigned site from release packages in the
pinned distribution images (``apt-ftparchive``, ``createrepo_c``); CI can run
it. ``sign`` runs on the maintainer's machine: it adds the Arch repository,
signs the apt ``Release`` files, signs the RPM packages, rebuilds the rpm-md
metadata over the signed packages (signing changes their bytes), and signs
``repomd.xml``. ``verify`` checks every hash and signature, and ``publish``
pushes the site to ``gh-pages`` and refreshes the Arch release mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import gzip
from html import escape
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
from typing import Any, Callable, Protocol
import xml.etree.ElementTree as ElementTree

from .arch_repository import (
    ArchRepositoryPolicy,
    _publish_atomic_directory,
    _validate_private_gnupg_home,
    load_arch_repository_policy,
    verify_arch_repository,
)
from .distributions import (
    DEBIAN_FORMAT_TARGET_IDS,
    RPM_FORMAT_TARGET_IDS,
    load_distribution_catalog,
)
from .governance import load_github_governance
from .model import ModelError, load_json, sha256_file


POLICY_PATH = "packaging/repository-site.json"
SITE_MANIFEST = "repository-site.json"
SITE_MANIFEST_SCHEMA = "hyperflux-repository-site-manifest-v1"
FRONT_PAGE = "index.html"
NO_JEKYLL = ".nojekyll"
ARCH_DIRECTORY = "arch"
SITE_TARGET_IDS = (*DEBIAN_FORMAT_TARGET_IDS, *RPM_FORMAT_TARGET_IDS)
DKMS_PACKAGE = "hyperflux-next-dkms"
STATES = ("unpublished", "published")
FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
SITE_URL = re.compile(
    r"^(?:https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*"
    r"|http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*"
    r"|file://(?:/[A-Za-z0-9._~-]+)+)$"
)
PACKAGE_NAME = r"[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*"
DEB_FILENAME = re.compile(
    rf"^(?P<name>{PACKAGE_NAME})_(?P<version>[0-9][A-Za-z0-9.+~-]*)_(?P<arch>[a-z0-9]+)\.deb$"
)
RPM_FILENAME = re.compile(
    rf"^(?P<name>{PACKAGE_NAME})-(?P<version>[0-9][A-Za-z0-9.+~^_]*)"
    r"-(?P<release>[0-9][A-Za-z0-9.+~^_]*)\.(?P<arch>[a-z0-9_]+)\.rpm$"
)
GUIDES = {
    "arch": "docs/user/installation.md",
    "ubuntu": "docs/user/install-ubuntu.md",
    "debian": "docs/user/install-debian.md",
    "fedora": "docs/user/install-fedora.md",
    "opensuse": "docs/user/install-opensuse.md",
}
REPOMD_NAMESPACE = "{http://linux.duke.edu/metadata/repo}"
COMMON_NAMESPACE = "{http://linux.duke.edu/metadata/common}"
RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
RPM_HEADER_MAGIC = b"\x8e\xad\xe8\x01"
RPM_SIGNATURE_TAGS = (268, 267)  # RSAHEADER, DSAHEADER: OpenPGP over the header.
APT_KEYRING_DIRECTORY = "/etc/apt/keyrings"


@dataclass(frozen=True)
class RepositorySitePolicy:
    state: str
    site_url: str
    publication_branch: str
    repository_id: str
    label: str
    public_key_asset: str
    apt_component: str

    @property
    def published(self) -> bool:
        return self.state == "published"


@dataclass(frozen=True)
class SiteTarget:
    """One distribution release the site carries a repository for."""

    id: str
    package_format: str
    distribution: str
    release: str
    suite: str
    architecture: str
    image: str
    required_packages: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.distribution} {self.release}"

    @property
    def repository_path(self) -> str:
        """Where the repository's metadata lives, relative to the site root."""
        if self.package_format == "deb":
            return self.id
        return f"{self.id}/{self.suite}/{self.architecture}"

    @property
    def package_directory(self) -> str:
        if self.package_format == "deb":
            return f"{self.id}/pool/main/h/hyperflux-next"
        return f"{self.repository_path}/packages"

    def configuration_path(self, policy: RepositorySitePolicy) -> str:
        suffix = "sources" if self.package_format == "deb" else "repo"
        return f"{self.id}/{policy.repository_id}.{suffix}"

    @property
    def package_architectures(self) -> frozenset[str]:
        neutral = "all" if self.package_format == "deb" else "noarch"
        return frozenset({self.architecture, neutral})


@dataclass(frozen=True)
class SitePackage:
    filename: str
    name: str
    version: str
    architecture: str


@dataclass(frozen=True)
class SigningIdentity:
    gnupg_home: Path
    primary_fingerprint: str
    signing_subkey_fingerprint: str


def _execute(
    command: list[str],
    *,
    label: str,
    cwd: Path | None = None,
    timeout_seconds: int = 900,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"{label} could not run: {error}") from error
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise ModelError(f"{label} failed with exit status {result.returncode}: {details}")
    return result


def load_repository_site_policy(root: Path) -> RepositorySitePolicy:
    value = load_json(root / POLICY_PATH)
    expected = {
        "$schema",
        "schema",
        "state",
        "site_url",
        "publication_branch",
        "repository_id",
        "label",
        "public_key_asset",
        "apt_component",
    }
    if set(value) != expected:
        raise ModelError("repository site policy has missing or unknown fields")
    if value["$schema"] != "../schemas/repository-site.schema.json":
        raise ModelError("repository site policy names the wrong schema")
    if value["schema"] != "hyperflux-repository-site-v1":
        raise ModelError("repository site policy has an unsupported schema")
    if value["state"] not in STATES:
        raise ModelError("repository site state must be unpublished or published")
    site_url = value["site_url"]
    if not isinstance(site_url, str) or not re.fullmatch(
        r"https://[a-z0-9.-]+\.github\.io/[A-Za-z0-9._-]+", site_url
    ):
        raise ModelError("repository site URL must be one GitHub Pages project URL")
    if value["publication_branch"] != "gh-pages":
        raise ModelError("repository site publishes from the gh-pages branch")
    if not isinstance(value["repository_id"], str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{1,63}", value["repository_id"]
    ):
        raise ModelError("repository site id is invalid")
    if not isinstance(value["label"], str) or not 0 < len(value["label"]) <= 64:
        raise ModelError("repository site label is invalid")
    if not isinstance(value["public_key_asset"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,95}\.asc", value["public_key_asset"]
    ):
        raise ModelError("repository site public key asset is invalid")
    if value["apt_component"] != "main":
        raise ModelError("repository site apt component must be main")
    return RepositorySitePolicy(
        state=value["state"],
        site_url=site_url,
        publication_branch=value["publication_branch"],
        repository_id=value["repository_id"],
        label=value["label"],
        public_key_asset=value["public_key_asset"],
        apt_component=value["apt_component"],
    )


def site_targets(root: Path) -> dict[str, SiteTarget]:
    catalog = load_distribution_catalog(root)
    images = dict(load_github_governance(root).distribution_package_images)
    targets: dict[str, SiteTarget] = {}
    for target_id in SITE_TARGET_IDS:
        target = catalog.targets[target_id]
        release = target.release
        if release is None:
            raise ModelError(f"distribution {target_id} names no release")
        architectures = sorted(set(target.architectures.values()))
        if len(architectures) != 1:
            raise ModelError(f"distribution {target_id} must name one architecture")
        suite = release.codename if target.package_format == "deb" else release.version.lower()
        if not suite:
            raise ModelError(f"distribution {target_id} names no suite")
        if target_id not in images:
            raise ModelError(f"distribution {target_id} has no pinned image")
        required = tuple(
            sorted(
                {DKMS_PACKAGE}
                | {component.package_name for component in target.components.values()}
            )
        )
        targets[target_id] = SiteTarget(
            id=target_id,
            package_format=target.package_format,
            distribution=release.distribution,
            release=release.version,
            suite=suite,
            architecture=architectures[0],
            image=images[target_id],
            required_packages=required,
        )
    return targets


def _site_url(value: str) -> str:
    if not SITE_URL.fullmatch(value):
        raise ModelError(
            "site URL must be https://, file:///, or a local http:// address without a trailing slash"
        )
    return value


def _homepage(root: Path) -> str:
    return load_distribution_catalog(root).homepage


def _repository_slug(root: Path) -> str:
    homepage = _homepage(root)
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", homepage)
    if match is None:
        raise ModelError("distribution homepage is not a GitHub repository")
    return match.group(1)


def guide_url(root: Path, target_id: str) -> str:
    return f"{_homepage(root)}/blob/main/{GUIDES[target_id]}"


# --- Configuration files the guides install -------------------------------


def apt_sources(policy: RepositorySitePolicy, target: SiteTarget, site_url: str) -> str:
    """The deb822 source file a user copies to /etc/apt/sources.list.d/."""
    return "".join(
        (
            "Types: deb\n",
            f"URIs: {site_url}/{target.id}\n",
            f"Suites: {target.suite}\n",
            f"Components: {policy.apt_component}\n",
            f"Architectures: {target.architecture}\n",
            f"Signed-By: {APT_KEYRING_DIRECTORY}/{policy.repository_id}.asc\n",
        )
    )


def rpm_repository_file(
    policy: RepositorySitePolicy, target: SiteTarget, site_url: str
) -> str:
    """The .repo file dnf and zypper read; both signature checks are on."""
    if target.id == "fedora":
        base = f"{site_url}/{target.id}/$releasever/$basearch/"
    else:
        base = f"{site_url}/{target.id}/{target.suite}/$basearch/"
    lines = [
        f"[{policy.repository_id}]",
        f"name={policy.label} for {target.distribution}",
        f"baseurl={base}",
        "type=rpm-md",
        "enabled=1",
    ]
    if target.id == "opensuse":
        lines.append("autorefresh=1")
    lines.extend(
        (
            "gpgcheck=1",
            "repo_gpgcheck=1",
            f"gpgkey={site_url}/{policy.public_key_asset}",
        )
    )
    return "\n".join(lines) + "\n"


def configuration_text(
    policy: RepositorySitePolicy, target: SiteTarget, site_url: str
) -> str:
    if target.package_format == "deb":
        return apt_sources(policy, target, site_url)
    return rpm_repository_file(policy, target, site_url)


def front_page(
    root: Path,
    policy: RepositorySitePolicy,
    arch: ArchRepositoryPolicy,
    targets: dict[str, SiteTarget],
) -> str:
    rows = [
        (
            "Arch Linux",
            ARCH_DIRECTORY,
            guide_url(root, "arch"),
        ),
        *(
            (target.name, target.id, guide_url(root, target.id))
            for target in targets.values()
        ),
    ]
    grouped = " ".join(
        arch.primary_fingerprint[index : index + 4]
        for index in range(0, len(arch.primary_fingerprint), 4)
    )
    items = "\n".join(
        f'      <li><a href="{escape(guide)}">{escape(name)}</a>'
        f' <span class="folder">(<a href="{escape(folder)}/">{escape(folder)}/</a>)</span></li>'
        for name, folder, guide in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(policy.label)} packages</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, sans-serif; line-height: 1.5; margin: 0 auto;
           max-width: 40rem; padding: 2rem 1rem; }}
    li {{ margin: 0.5rem 0; }}
    .folder {{ font-size: 0.9em; opacity: 0.75; }}
    code {{ overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main>
    <h1>Choose your distribution</h1>
    <p>These are the {escape(policy.label)} package repositories. Each link opens
    the install guide for that distribution.</p>
    <ul>
{items}
    </ul>
    <p>Every repository is signed with one key,
    <a href="{escape(policy.public_key_asset)}">{escape(policy.public_key_asset)}</a>.
    Its fingerprint is <code>{escape(grouped)}</code>.</p>
    <p>These are not official repositories of any distribution.
    <a href="{escape(_homepage(root))}">Project home</a>.</p>
  </main>
</body>
</html>
"""


# --- Metadata tools ---------------------------------------------------------


class MetadataTools(Protocol):
    def apt(self, policy: RepositorySitePolicy, target: SiteTarget, directory: Path) -> None: ...

    def rpm(self, target: SiteTarget, directory: Path) -> None: ...


def apt_metadata_script(policy: RepositorySitePolicy, target: SiteTarget) -> str:
    """apt-ftparchive commands run inside the distribution's repository root."""
    component = policy.apt_component
    suite = target.suite
    binary = f"dists/{suite}/{component}/binary-{target.architecture}"
    fields = (
        ("Origin", policy.label),
        ("Label", policy.label),
        ("Suite", suite),
        ("Codename", suite),
        ("Architectures", target.architecture),
        ("Components", component),
        ("Description", f"{policy.label} packages for {target.name}"),
    )
    options = " ".join(
        f"-o {shlex.quote(f'APT::FTPArchive::Release::{key}={value}')}" for key, value in fields
    )
    return "\n".join(
        (
            "set -eu",
            f"mkdir -p {binary}",
            f"apt-ftparchive --arch {target.architecture} packages pool/{component} > {binary}/Packages",
            f"gzip -9n < {binary}/Packages > {binary}/Packages.gz",
            'release="$(mktemp)"',
            f'apt-ftparchive {options} release dists/{suite} > "$release"',
            f'cat "$release" > dists/{suite}/Release',
            'rm -f -- "$release"',
        )
    )


def rpm_metadata_script() -> str:
    """createrepo_c over the packages/ folder; gzip keeps every client able to read it."""
    return "\n".join(
        (
            "set -eu",
            "createrepo_c --quiet --no-database --general-compress-type gz .",
        )
    )


TOOL_INSTALL = {
    "ubuntu": (
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -qq\n"
        "apt-get install -y -qq --no-install-recommends apt-utils >/dev/null"
    ),
    "debian": (
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -qq\n"
        "apt-get install -y -qq --no-install-recommends apt-utils >/dev/null"
    ),
    "fedora": "dnf install -y -q --setopt=install_weak_deps=False createrepo_c >/dev/null",
    "opensuse": "zypper --non-interactive --quiet install --no-recommends createrepo_c >/dev/null",
}


class LocalTools:
    """Run apt-ftparchive and createrepo_c from PATH (inside a prepared container)."""

    def apt(self, policy: RepositorySitePolicy, target: SiteTarget, directory: Path) -> None:
        _execute(
            ["bash", "-c", apt_metadata_script(policy, target)],
            label=f"create {target.name} apt metadata",
            cwd=directory,
        )

    def rpm(self, target: SiteTarget, directory: Path) -> None:
        _execute(
            ["bash", "-c", rpm_metadata_script()],
            label=f"create {target.name} rpm-md metadata",
            cwd=directory,
        )


class ContainerTools:
    """Run the metadata tools in each target's pinned distribution image."""

    def __init__(self, engine: str | None = None) -> None:
        from .ci import _engine

        self.engine = _engine(engine)

    def command(self, target: SiteTarget, directory: Path, script: str) -> list[str]:
        owner = ""
        if Path(self.engine).name == "docker":
            # Docker runs as real root; give the files back to the caller.
            owner = f"\nchown -R {os.getuid()}:{os.getgid()} /repository"
        body = f"{TOOL_INSTALL[target.id]}\ncd /repository\n{script}{owner}\n"
        return [
            self.engine,
            "run",
            "--rm",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{directory}:/repository:rw",
            "--workdir",
            "/repository",
            target.image,
            "/bin/bash",
            "-c",
            body,
        ]

    def apt(self, policy: RepositorySitePolicy, target: SiteTarget, directory: Path) -> None:
        _execute(
            self.command(target, directory, apt_metadata_script(policy, target)),
            label=f"create {target.name} apt metadata in its pinned image",
        )

    def rpm(self, target: SiteTarget, directory: Path) -> None:
        _execute(
            self.command(target, directory, rpm_metadata_script()),
            label=f"create {target.name} rpm-md metadata in its pinned image",
        )


def metadata_tools(choice: str) -> MetadataTools:
    if choice == "container":
        return ContainerTools()
    if choice == "local":
        return LocalTools()
    raise ModelError("metadata tools must be container or local")


# --- Packages -----------------------------------------------------------------


def collect_packages(target: SiteTarget, directory: Path) -> tuple[SitePackage, ...]:
    """The exact release packages of one target, all at one version."""
    if not directory.is_dir() or directory.is_symlink():
        raise ModelError(f"{target.name} package directory is missing: {directory}")
    pattern = DEB_FILENAME if target.package_format == "deb" else RPM_FILENAME
    suffix = f".{target.package_format}"
    packages: list[SitePackage] = []
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(suffix):
            continue
        if not path.is_file() or path.is_symlink():
            raise ModelError(f"{target.name} package is not a regular file: {path.name}")
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ModelError(f"{target.name} package name is not recognized: {path.name}")
        version = match.group("version")
        if target.package_format == "rpm":
            version = f"{version}-{match.group('release')}"
        if match.group("arch") not in target.package_architectures:
            raise ModelError(f"{target.name} package has the wrong architecture: {path.name}")
        packages.append(
            SitePackage(
                filename=path.name,
                name=match.group("name"),
                version=version,
                architecture=match.group("arch"),
            )
        )
    names = sorted(package.name for package in packages)
    if names != list(target.required_packages):
        raise ModelError(
            f"{target.name} needs exactly {', '.join(target.required_packages)}; found {', '.join(names) or 'none'}"
        )
    if len({package.version for package in packages}) != 1:
        raise ModelError(f"{target.name} packages carry different versions")
    return tuple(packages)


def _copy_public(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def _write_public(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)


def _manifest_packages(site: Path, target: SiteTarget) -> list[dict[str, Any]]:
    directory = site / target.package_directory
    return [
        {
            "filename": path.name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(directory.iterdir())
    ]


def _write_manifest(site: Path, value: dict[str, Any]) -> None:
    _write_public(site / SITE_MANIFEST, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _load_manifest(site: Path) -> dict[str, Any]:
    value = load_json(site / SITE_MANIFEST)
    expected = {"schema", "site_url", "signed", "signer", "targets"}
    if set(value) != expected or value["schema"] != SITE_MANIFEST_SCHEMA:
        raise ModelError("site manifest is missing, unknown, or has unknown fields")
    if not isinstance(value["signed"], bool):
        raise ModelError("site manifest signed flag must be true or false")
    _site_url(value["site_url"])
    if set(value["targets"]) != set(SITE_TARGET_IDS):
        raise ModelError("site manifest must list every distribution repository")
    return value


def _normalize_tree(site: Path) -> None:
    """GitHub Pages serves whatever is there; keep every file public and plain."""
    for path in sorted(site.rglob("*")):
        if path.is_symlink():
            raise ModelError(f"site contains a symbolic link: {path.relative_to(site)}")
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
        else:
            raise ModelError(f"site contains an unsupported file: {path.relative_to(site)}")
    site.chmod(0o755)


# --- Assemble -----------------------------------------------------------------


def assemble_site(
    root: Path,
    *,
    packages: dict[str, Path],
    output: Path,
    site_url: str | None = None,
    tools: MetadataTools | None = None,
) -> Path:
    """Build the unsigned site from release packages; no key is involved."""
    policy = load_repository_site_policy(root)
    arch = load_arch_repository_policy(root)
    targets = site_targets(root)
    if set(packages) != set(targets):
        missing = sorted(set(targets) - set(packages))
        extra = sorted(set(packages) - set(targets))
        raise ModelError(
            f"assembly needs one package directory per repository (missing={missing}, extra={extra})"
        )
    collected = {
        target_id: collect_packages(targets[target_id], packages[target_id])
        for target_id in targets
    }
    url = _site_url(site_url or policy.site_url)
    selected = tools if tools is not None else ContainerTools()

    def build(temporary: Path) -> None:
        temporary.mkdir(mode=0o755)
        _write_public(temporary / NO_JEKYLL, "")
        _write_public(temporary / FRONT_PAGE, front_page(root, policy, arch, targets))
        _copy_public(root / arch.public_key_path, temporary / policy.public_key_asset)
        for target_id, target in targets.items():
            for package in collected[target_id]:
                _copy_public(
                    packages[target_id] / package.filename,
                    temporary / target.package_directory / package.filename,
                )
            _write_public(
                temporary / target.configuration_path(policy),
                configuration_text(policy, target, url),
            )
            repository = temporary / target.repository_path
            if target.package_format == "deb":
                selected.apt(policy, target, repository)
            else:
                selected.rpm(target, repository)
        _normalize_tree(temporary)
        _write_manifest(
            temporary,
            {
                "schema": SITE_MANIFEST_SCHEMA,
                "site_url": url,
                "signed": False,
                "signer": None,
                "targets": {
                    target_id: {
                        "name": target.name,
                        "suite": target.suite,
                        "architecture": target.architecture,
                        "version": collected[target_id][0].version,
                        "packages": _manifest_packages(temporary, target),
                    }
                    for target_id, target in targets.items()
                },
            },
        )
        verify_site(root, temporary, require_signatures=False)

    _publish_atomic_directory(output, build)
    return output / SITE_MANIFEST


# --- Signing ------------------------------------------------------------------


def _gpg_sign_command(identity: SigningIdentity, *arguments: str) -> list[str]:
    return [
        "gpg",
        "--batch",
        "--yes",
        "--homedir",
        str(identity.gnupg_home),
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        "",
        "--local-user",
        f"{identity.signing_subkey_fingerprint}!",
        "--digest-algo",
        "SHA512",
        *arguments,
    ]


def rpmsign_command(identity: SigningIdentity, packages: list[Path]) -> list[str]:
    extra = "--batch --pinentry-mode loopback --passphrase ''"
    subkey = f"{identity.signing_subkey_fingerprint}!"
    return [
        "rpmsign",
        "--addsign",
        "--define",
        f"_gpg_path {identity.gnupg_home}",
        "--define",
        f"_gpg_name {subkey}",
        "--define",
        f"_openpgp_sign_id {subkey}",
        "--define",
        "_gpg_sign_cmd_extra_args " + extra,
        *(str(package) for package in packages),
    ]


def _key_fingerprints(key: Path) -> tuple[str, ...]:
    with tempfile.TemporaryDirectory(prefix="hfx-site-key-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        listing = _execute(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(home),
                "--with-colons",
                "--import-options",
                "show-only",
                "--import",
                str(key),
            ],
            label="inspect repository public key",
        ).stdout
    return tuple(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))


def _require_secret_key(identity: SigningIdentity) -> None:
    listing = _execute(
        [
            "gpg",
            "--batch",
            "--homedir",
            str(identity.gnupg_home),
            "--with-colons",
            "--list-secret-keys",
            identity.primary_fingerprint,
        ],
        label="inspect repository signing key",
    ).stdout
    fingerprints = {
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    }
    if {identity.primary_fingerprint, identity.signing_subkey_fingerprint} - fingerprints:
        raise ModelError("signing home lacks the repository key and its signing subkey")


def sign_site(
    root: Path,
    site: Path,
    *,
    output: Path,
    identity: SigningIdentity,
    arch_repository: Path,
    tools: MetadataTools | None = None,
    trusted_key: Path | None = None,
) -> Path:
    """Sign an assembled site into a new directory, then verify the result."""
    for fingerprint in (identity.primary_fingerprint, identity.signing_subkey_fingerprint):
        if not FINGERPRINT.fullmatch(fingerprint):
            raise ModelError("signing fingerprints must be 40 upper-case hex digits")
    policy = load_repository_site_policy(root)
    targets = site_targets(root)
    manifest = _load_manifest(site)
    if manifest["signed"]:
        raise ModelError("site is already signed; sign the unsigned assembly instead")
    verify_site(root, site, require_signatures=False)
    _validate_private_gnupg_home(identity.gnupg_home)
    _require_secret_key(identity)
    published_key = site / policy.public_key_asset
    if {identity.primary_fingerprint, identity.signing_subkey_fingerprint} - set(
        _key_fingerprints(published_key)
    ):
        raise ModelError("the site's public key is not the signing key")
    verify_arch_repository(root, arch_repository)
    selected = tools if tools is not None else ContainerTools()

    def build(temporary: Path) -> None:
        shutil.copytree(site, temporary, symlinks=True)
        if (temporary / ARCH_DIRECTORY).exists():
            raise ModelError("unsigned site must not carry an Arch folder")
        (temporary / ARCH_DIRECTORY).mkdir(mode=0o755)
        for path in sorted(arch_repository.iterdir()):
            _copy_public(path, temporary / ARCH_DIRECTORY / path.name)
        for target in targets.values():
            if target.package_format == "deb":
                suite = temporary / target.id / "dists" / target.suite
                release = suite / "Release"
                _execute(
                    _gpg_sign_command(
                        identity, "--clearsign", "--output", str(suite / "InRelease"), str(release)
                    ),
                    label=f"clear-sign {target.name} Release",
                )
                _execute(
                    _gpg_sign_command(
                        identity,
                        "--armor",
                        "--detach-sign",
                        "--output",
                        str(suite / "Release.gpg"),
                        str(release),
                    ),
                    label=f"sign {target.name} Release",
                )
            else:
                directory = temporary / target.package_directory
                _execute(
                    rpmsign_command(identity, sorted(directory.glob("*.rpm"))),
                    label=f"sign {target.name} packages",
                )
                repository = temporary / target.repository_path
                selected.rpm(target, repository)
                repomd = repository / "repodata" / "repomd.xml"
                _execute(
                    _gpg_sign_command(
                        identity,
                        "--armor",
                        "--detach-sign",
                        "--output",
                        str(repomd.with_name("repomd.xml.asc")),
                        str(repomd),
                    ),
                    label=f"sign {target.name} repomd.xml",
                )
        _normalize_tree(temporary)
        for target_id, target in targets.items():
            manifest["targets"][target_id]["packages"] = _manifest_packages(temporary, target)
        manifest["signed"] = True
        manifest["signer"] = {
            "primary_fingerprint": identity.primary_fingerprint,
            "signing_subkey_fingerprint": identity.signing_subkey_fingerprint,
        }
        _write_manifest(temporary, manifest)
        _validate_private_gnupg_home(identity.gnupg_home)

    _publish_atomic_directory(output, build)
    verify_site(
        root,
        output,
        trusted_key=trusted_key,
        signer=(identity.primary_fingerprint, identity.signing_subkey_fingerprint),
    )
    return output / SITE_MANIFEST


# --- Verify -------------------------------------------------------------------


def _signature_status(output: str, signer: tuple[str, str], label: str) -> None:
    valid = [line.split() for line in output.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    bad = [
        line
        for line in output.splitlines()
        if line.startswith(("[GNUPG:] BADSIG", "[GNUPG:] ERRSIG", "[GNUPG:] EXPKEYSIG", "[GNUPG:] REVKEYSIG"))
    ]
    if bad or len(valid) != 1:
        raise ModelError(f"{label} is not signed by exactly one good signature")
    fields = valid[0]
    if fields[2] != signer[1] or fields[-1] != signer[0]:
        raise ModelError(f"{label} is signed by an unexpected key")


class _Verifier:
    """A throwaway keyring that holds only the trusted public key."""

    def __init__(self, home: Path, key: Path, signer: tuple[str, str]) -> None:
        self.home = home
        self.signer = signer
        _execute(
            ["gpg", "--batch", "--homedir", str(home), "--import", str(key)],
            label="import trusted repository key",
        )

    def detached(self, signature: Path, payload: Path, label: str) -> None:
        result = _execute(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(self.home),
                "--status-fd",
                "1",
                "--verify",
                str(signature),
                str(payload),
            ],
            label=f"verify {label}",
        )
        _signature_status(result.stdout, self.signer, label)

    def clearsigned(self, signed: Path, label: str) -> bytes:
        plain = self.home / "clearsigned-payload"
        if plain.exists():
            plain.unlink()
        result = _execute(
            [
                "gpg",
                "--batch",
                "--homedir",
                str(self.home),
                "--status-fd",
                "1",
                "--output",
                str(plain),
                "--decrypt",
                str(signed),
            ],
            label=f"verify {label}",
        )
        _signature_status(result.stdout, self.signer, label)
        return plain.read_bytes()


def _release_fields(text: str) -> tuple[dict[str, str], dict[str, tuple[str, int]]]:
    fields: dict[str, str] = {}
    hashes: dict[str, tuple[str, int]] = {}
    section: str | None = None
    for line in text.splitlines():
        if line.startswith(" "):
            if section == "SHA256":
                parts = line.split()
                if len(parts) != 3:
                    raise ModelError("Release has a malformed SHA256 line")
                hashes[parts[2]] = (parts[0], int(parts[1]))
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ModelError("Release has a malformed line")
        section = key
        fields[key] = value.strip()
    return fields, hashes


def _control_paragraphs(text: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    for block in text.strip().split("\n\n"):
        fields: dict[str, str] = {}
        key = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")):
                fields[key] += "\n" + line
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if fields:
            paragraphs.append(fields)
    return paragraphs


def _verify_apt(
    site: Path,
    policy: RepositorySitePolicy,
    target: SiteTarget,
    manifest: dict[str, Any],
    verifier: _Verifier | None,
) -> dict[str, Any]:
    root = site / target.id
    expected_top = {"dists", "pool", Path(target.configuration_path(policy)).name}
    if {path.name for path in root.iterdir()} != expected_top:
        raise ModelError(f"{target.name} repository has unexpected top-level entries")
    suite = root / "dists" / target.suite
    if [path.name for path in (root / "dists").iterdir()] != [target.suite]:
        raise ModelError(f"{target.name} repository must carry exactly the {target.suite} suite")
    release = suite / "Release"
    fields, hashes = _release_fields(release.read_text(encoding="utf-8"))
    expected_fields = {
        "Suite": target.suite,
        "Codename": target.suite,
        "Architectures": target.architecture,
        "Components": policy.apt_component,
        "Origin": policy.label,
        "Label": policy.label,
    }
    for key, value in expected_fields.items():
        if fields.get(key) != value:
            raise ModelError(f"{target.name} Release field {key} differs")
    signatures = {"InRelease", "Release.gpg"}
    present = {path.name for path in suite.iterdir() if path.is_file()} & signatures
    if verifier is None:
        if present:
            raise ModelError(f"unsigned {target.name} repository already carries signatures")
    else:
        if present != signatures:
            raise ModelError(f"{target.name} repository lacks InRelease or Release.gpg")
        verifier.detached(suite / "Release.gpg", release, f"{target.name} Release.gpg")
        plain = verifier.clearsigned(suite / "InRelease", f"{target.name} InRelease")
        if plain.rstrip(b"\n") != release.read_bytes().rstrip(b"\n"):
            raise ModelError(f"{target.name} InRelease does not carry the same Release")
    indexed = {
        path.relative_to(suite).as_posix()
        for path in suite.rglob("*")
        if path.is_file() and path.name not in {"Release", *signatures}
    }
    if set(hashes) != indexed:
        raise ModelError(f"{target.name} Release does not list exactly its index files")
    for name, (digest, size) in hashes.items():
        path = suite / name
        if sha256_file(path) != digest or path.stat().st_size != size:
            raise ModelError(f"{target.name} index file differs from Release: {name}")
    binary = suite / policy.apt_component / f"binary-{target.architecture}"
    packages_text = (binary / "Packages").read_bytes()
    if gzip.decompress((binary / "Packages.gz").read_bytes()) != packages_text:
        raise ModelError(f"{target.name} Packages.gz differs from Packages")
    paragraphs = _control_paragraphs(packages_text.decode("utf-8"))
    pool = site / target.package_directory
    pool_files = {f"{target.package_directory.split('/', 1)[1]}/{path.name}" for path in pool.iterdir()}
    listed = {paragraph.get("Filename", "") for paragraph in paragraphs}
    if listed != pool_files or len(paragraphs) != len(pool_files):
        raise ModelError(f"{target.name} Packages does not list exactly the pool")
    for paragraph in paragraphs:
        path = root / paragraph["Filename"]
        if sha256_file(path) != paragraph.get("SHA256") or str(path.stat().st_size) != paragraph.get("Size"):
            raise ModelError(f"{target.name} package differs from Packages: {path.name}")
    names = sorted(paragraph["Package"] for paragraph in paragraphs)
    if names != list(target.required_packages):
        raise ModelError(f"{target.name} Packages lists the wrong packages")
    versions = {paragraph["Version"] for paragraph in paragraphs}
    if versions != {manifest["targets"][target.id]["version"]}:
        raise ModelError(f"{target.name} Packages version differs from the site manifest")
    return {"suite": target.suite, "packages": names, "signed": verifier is not None}


def rpm_header_signature(path: Path) -> tuple[bytes, bytes]:
    """Return the OpenPGP header signature and the header bytes it covers."""
    data = path.read_bytes()
    if data[:4] != RPM_LEAD_MAGIC:
        raise ModelError(f"{path.name} is not an RPM package")

    def header(offset: int) -> tuple[list[tuple[int, int, int, int]], bytes, int]:
        if data[offset : offset + 4] != RPM_HEADER_MAGIC:
            raise ModelError(f"{path.name} has a malformed header")
        count, size = struct.unpack(">II", data[offset + 8 : offset + 16])
        index_end = offset + 16 + count * 16
        end = index_end + size
        if end > len(data):
            raise ModelError(f"{path.name} has a truncated header")
        entries = [
            struct.unpack(">IIII", data[offset + 16 + 16 * item : offset + 32 + 16 * item])
            for item in range(count)
        ]
        return entries, data[index_end:end], end

    entries, store, end = header(96)
    signature = b""
    for tag in RPM_SIGNATURE_TAGS:
        for entry_tag, entry_type, entry_offset, entry_count in entries:
            if entry_tag == tag and entry_type == 7:
                signature = store[entry_offset : entry_offset + entry_count]
                break
        if signature:
            break
    if not signature:
        raise ModelError(f"{path.name} carries no OpenPGP header signature")
    start = end + (-end % 8)
    _, _, main_end = header(start)
    return signature, data[start:main_end]


def _verify_rpm(
    site: Path,
    policy: RepositorySitePolicy,
    target: SiteTarget,
    manifest: dict[str, Any],
    verifier: _Verifier | None,
) -> dict[str, Any]:
    top = site / target.id
    expected_top = {target.suite, Path(target.configuration_path(policy)).name}
    if {path.name for path in top.iterdir()} != expected_top:
        raise ModelError(f"{target.name} repository has unexpected top-level entries")
    if [path.name for path in (top / target.suite).iterdir()] != [target.architecture]:
        raise ModelError(f"{target.name} repository must carry exactly {target.architecture}")
    repository = site / target.repository_path
    if {path.name for path in repository.iterdir()} != {"packages", "repodata"}:
        raise ModelError(f"{target.name} repository must hold only packages/ and repodata/")
    repodata = repository / "repodata"
    repomd = repodata / "repomd.xml"
    signature = repodata / "repomd.xml.asc"
    if verifier is None:
        if signature.exists():
            raise ModelError(f"unsigned {target.name} repository already carries repomd.xml.asc")
    else:
        if not signature.is_file():
            raise ModelError(f"{target.name} repository lacks repomd.xml.asc")
        verifier.detached(signature, repomd, f"{target.name} repomd.xml")
    document = ElementTree.parse(repomd).getroot()
    locations: dict[str, Path] = {}
    for data in document.findall(f"{REPOMD_NAMESPACE}data"):
        location = data.find(f"{REPOMD_NAMESPACE}location")
        checksum = data.find(f"{REPOMD_NAMESPACE}checksum")
        if location is None or checksum is None or checksum.get("type") != "sha256":
            raise ModelError(f"{target.name} repomd.xml entry lacks a sha256 location")
        path = repository / location.get("href", "")
        if path.parent != repodata or sha256_file(path) != (checksum.text or "").strip():
            raise ModelError(f"{target.name} metadata file differs from repomd.xml: {path.name}")
        locations[data.get("type", "")] = path
    if {"primary", "filelists", "other"} - set(locations):
        raise ModelError(f"{target.name} repomd.xml lacks primary, filelists, or other")
    listed_files = {path.name for path in locations.values()} | {"repomd.xml"}
    if verifier is not None:
        listed_files.add("repomd.xml.asc")
    if {path.name for path in repodata.iterdir()} != listed_files:
        raise ModelError(f"{target.name} repodata holds files repomd.xml does not list")
    primary = ElementTree.fromstring(gzip.decompress(locations["primary"].read_bytes()))
    packages = repository / "packages"
    expected = {f"packages/{path.name}" for path in packages.iterdir()}
    found: dict[str, tuple[str, str]] = {}
    for package in primary.findall(f"{COMMON_NAMESPACE}package"):
        name = package.findtext(f"{COMMON_NAMESPACE}name") or ""
        location = package.find(f"{COMMON_NAMESPACE}location")
        checksum = package.find(f"{COMMON_NAMESPACE}checksum")
        version = package.find(f"{COMMON_NAMESPACE}version")
        if location is None or checksum is None or version is None or checksum.get("type") != "sha256":
            raise ModelError(f"{target.name} primary metadata entry is incomplete")
        href = location.get("href", "")
        found[href] = (name, f"{version.get('ver')}-{version.get('rel')}")
        if sha256_file(repository / href) != (checksum.text or "").strip():
            raise ModelError(f"{target.name} package differs from primary metadata: {href}")
    if set(found) != expected:
        raise ModelError(f"{target.name} primary metadata does not list exactly packages/")
    names = sorted(name for name, _ in found.values())
    if names != list(target.required_packages):
        raise ModelError(f"{target.name} primary metadata lists the wrong packages")
    if {version for _, version in found.values()} != {manifest["targets"][target.id]["version"]}:
        raise ModelError(f"{target.name} package version differs from the site manifest")
    if verifier is not None:
        for path in sorted(packages.iterdir()):
            header_signature, header = rpm_header_signature(path)
            signature_file = verifier.home / "rpm-header.sig"
            header_file = verifier.home / "rpm-header.bin"
            signature_file.write_bytes(header_signature)
            header_file.write_bytes(header)
            verifier.detached(signature_file, header_file, f"{target.name} {path.name}")
    return {"suite": target.suite, "packages": names, "signed": verifier is not None}


def verify_site(
    root: Path,
    site: Path,
    *,
    require_signatures: bool = True,
    trusted_key: Path | None = None,
    signer: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Check the whole site; with signatures, check them against the trusted key."""
    policy = load_repository_site_policy(root)
    arch = load_arch_repository_policy(root)
    targets = site_targets(root)
    if not site.is_dir() or site.is_symlink():
        raise ModelError("site must be one regular directory")
    manifest = _load_manifest(site)
    if manifest["signed"] != require_signatures:
        state = "signed" if manifest["signed"] else "unsigned"
        raise ModelError(f"site manifest says the site is {state}")
    for path in site.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ModelError(f"site holds a link or special file: {path.relative_to(site)}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != (0o755 if path.is_dir() else 0o644):
            raise ModelError(f"site entry has the wrong mode: {path.relative_to(site)}")
    expected_top = {NO_JEKYLL, FRONT_PAGE, SITE_MANIFEST, policy.public_key_asset, *targets}
    if require_signatures:
        expected_top.add(ARCH_DIRECTORY)
    if {path.name for path in site.iterdir()} != expected_top:
        raise ModelError("site top level differs from the published layout")
    if (site / FRONT_PAGE).read_text(encoding="utf-8") != front_page(root, policy, arch, targets):
        raise ModelError("front page differs from the generated page")
    for target in targets.values():
        expected = configuration_text(policy, target, manifest["site_url"])
        if (site / target.configuration_path(policy)).read_text(encoding="utf-8") != expected:
            raise ModelError(f"{target.name} configuration file differs from the generated file")
        listed = manifest["targets"][target.id]["packages"]
        if listed != _manifest_packages(site, target):
            raise ModelError(f"{target.name} packages differ from the site manifest")
    key = trusted_key if trusted_key is not None else root / arch.public_key_path
    if (site / policy.public_key_asset).read_bytes() != key.read_bytes():
        raise ModelError("site public key differs from the trusted key")
    expected_signer = signer or (arch.primary_fingerprint, arch.signing_subkey_fingerprint)
    report: dict[str, Any] = {"site_url": manifest["site_url"], "signed": require_signatures}
    with tempfile.TemporaryDirectory(prefix="hfx-site-verify-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        verifier = _Verifier(home, key, expected_signer) if require_signatures else None
        if require_signatures:
            recorded = manifest["signer"]
            if recorded != {
                "primary_fingerprint": expected_signer[0],
                "signing_subkey_fingerprint": expected_signer[1],
            }:
                raise ModelError("site manifest names a different signer")
            report["arch"] = verify_arch_repository(root, site / ARCH_DIRECTORY)
        elif manifest["signer"] is not None:
            raise ModelError("unsigned site manifest must not name a signer")
        for target in targets.values():
            check = _verify_apt if target.package_format == "deb" else _verify_rpm
            report[target.id] = check(site, policy, target, manifest, verifier)
    return report


# --- Publish ------------------------------------------------------------------


@dataclass(frozen=True)
class PublishStep:
    description: str
    command: tuple[str, ...]
    cwd: Path | None = None


def _arch_mirror_files(arch: ArchRepositoryPolicy, repository: Path) -> tuple[list[Path], list[Path]]:
    """Packages and their signatures first; then the metadata that points at them."""
    packages: list[Path] = []
    for package in arch.packages:
        packages.extend(
            (repository / package.filename, repository / f"{package.filename}.sig")
        )
    names = {path.name for path in packages}
    metadata = sorted(path for path in repository.iterdir() if path.name not in names)
    return packages, metadata


def publish_steps(
    root: Path,
    site: Path,
    *,
    work: Path,
    remote: str,
    present_assets: dict[str, str] | None,
) -> list[PublishStep]:
    """The exact commands that publish a verified site.

    ``present_assets`` maps the names already on the Arch release to their
    sha256. Packages already there with the same bytes are not uploaded again;
    a package with different bytes stops publication. ``None`` (a dry run)
    plans to upload every package.
    """
    policy = load_repository_site_policy(root)
    arch = load_arch_repository_policy(root)
    slug = _repository_slug(root)
    packages, metadata = _arch_mirror_files(arch, site / ARCH_DIRECTORY)
    uploads: list[Path] = []
    for path in packages:
        digest = None if present_assets is None else present_assets.get(path.name)
        if digest is None:
            uploads.append(path)
        elif digest != sha256_file(path):
            raise ModelError(f"the Arch release already holds different bytes for {path.name}")
    steps = [
        PublishStep("Start an empty gh-pages history", ("git", "init", "--quiet", "--initial-branch", policy.publication_branch), work),
        PublishStep("Stage the whole site", ("git", "add", "--all"), work),
        PublishStep(
            "Record the site in one commit",
            ("git", "commit", "--quiet", "--message", f"Publish the {policy.label} package repositories"),
            work,
        ),
        PublishStep(
            "Replace the gh-pages branch with this one commit",
            ("git", "push", "--force", remote, f"HEAD:refs/heads/{policy.publication_branch}"),
            work,
        ),
    ]
    if uploads:
        steps.append(
            PublishStep(
                "Upload new Arch packages and signatures to the mirror release",
                ("gh", "release", "upload", arch.release_tag, "--repo", slug, *(str(path) for path in uploads)),
            )
        )
    steps.append(
        PublishStep(
            "Replace the Arch metadata on the mirror release",
            ("gh", "release", "upload", arch.release_tag, "--repo", slug, "--clobber", *(str(path) for path in metadata)),
        )
    )
    return steps


def release_asset_digests(root: Path) -> dict[str, str]:
    arch = load_arch_repository_policy(root)
    listing = _execute(
        [
            "gh",
            "release",
            "view",
            arch.release_tag,
            "--repo",
            _repository_slug(root),
            "--json",
            "assets",
            "--jq",
            '.assets[] | "\\(.digest // "") \\(.name)"',
        ],
        label="list the Arch mirror release assets",
    ).stdout
    digests: dict[str, str] = {}
    for line in listing.splitlines():
        digest, _, name = line.partition(" ")
        digests[name] = digest.removeprefix("sha256:")
    return digests


def publish_site(
    root: Path,
    site: Path,
    *,
    execute: bool,
    remote: str | None = None,
    emit: Callable[[str], None] = print,
) -> list[PublishStep]:
    """Verify, then print (default) or run the publication commands."""
    verify_site(root, site)
    if remote is None:
        remote = _execute(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            label="read the origin remote",
        ).stdout.strip()
    parent = Path(tempfile.mkdtemp(prefix="hfx-pages-"))
    work = parent / "site"
    present = release_asset_digests(root) if execute else None
    steps = publish_steps(root, site, work=work, remote=remote, present_assets=present)
    emit(f"Copy {site} to {work}")
    for step in steps:
        where = f"(in {step.cwd}) " if step.cwd else ""
        emit(f"{step.description}:\n  {where}{shlex.join(step.command)}")
    if not execute:
        shutil.rmtree(parent)
        emit("Dry run: nothing was pushed or uploaded. Add --execute to publish.")
        return steps
    shutil.copytree(site, work, symlinks=True)
    for step in steps:
        _execute(list(step.command), label=step.description, cwd=step.cwd)
    published = release_asset_digests(root)
    for path in sorted((site / ARCH_DIRECTORY).iterdir()):
        if published.get(path.name) != sha256_file(path):
            raise ModelError(f"the Arch mirror does not match the site: {path.name}")
    shutil.rmtree(parent)
    emit("Published: gh-pages replaced and the Arch mirror matches the site.")
    return steps


# --- Documentation ------------------------------------------------------------


def documented_site_url(policy: RepositorySitePolicy) -> str:
    return policy.site_url


def arch_policy_for_guides(
    arch: ArchRepositoryPolicy, site: RepositorySitePolicy
) -> ArchRepositoryPolicy:
    """New installs use the Pages site once it is live; until then, the release."""
    if not site.published:
        return arch
    return replace(arch, server_url=f"{site.site_url}/{ARCH_DIRECTORY}")
