# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import package_repositories as repositories
from hfxdev import package_repositories_rehearsal as rehearsal
from hfxdev.arch_repository import load_arch_repository_policy
from hfxdev.model import ModelError


ARCH = load_arch_repository_policy(ROOT)
PRIMARY = ARCH.primary_fingerprint
SUBKEY = ARCH.signing_subkey_fingerprint
VERSION = "2.1.0-1"


def synthetic_rpm(signature: bytes = b"OPENPGP-SIGNATURE") -> bytes:
    """A minimal RPM: lead, signature header with RSAHEADER, padding, main header."""
    lead = repositories.RPM_LEAD_MAGIC + b"\0" * 92
    signature_header = (
        repositories.RPM_HEADER_MAGIC
        + b"\0" * 4
        + struct.pack(">II", 1, len(signature))
        + struct.pack(">IIII", 268, 7, 0, len(signature))
        + signature
    )
    padding = b"\0" * (-(len(lead) + len(signature_header)) % 8)
    main_header = (
        repositories.RPM_HEADER_MAGIC
        + b"\0" * 4
        + struct.pack(">II", 1, 4)
        + struct.pack(">IIII", 1000, 6, 0, 1)
        + b"abc\0"
    )
    return lead + signature_header + padding + main_header + b"payload"


def write_packages(base: Path) -> dict[str, Path]:
    directories: dict[str, Path] = {}
    for target_id in ("ubuntu", "debian"):
        directory = base / target_id
        directory.mkdir(parents=True)
        (directory / f"hyperflux-next-dkms_{VERSION}_all.deb").write_bytes(b"dkms " + target_id.encode())
        (directory / f"hyperflux-next-linux_{VERSION}_amd64.deb").write_bytes(b"linux " + target_id.encode())
        (directory / "build.log").write_text("ignored\n")
        directories[target_id] = directory
    for target_id in ("fedora", "opensuse"):
        directory = base / target_id
        directory.mkdir(parents=True)
        (directory / f"hyperflux-next-dkms-{VERSION}.noarch.rpm").write_bytes(synthetic_rpm())
        (directory / f"hyperflux-next-linux-{VERSION}.x86_64.rpm").write_bytes(
            synthetic_rpm(b"OTHER-SIGNATURE")
        )
        directories[target_id] = directory
    return directories


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeTools:
    """Writes the same metadata shape as apt-ftparchive and createrepo_c."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def apt(self, policy, target, directory: Path) -> None:
        self.calls.append(f"apt:{target.id}")
        pool = directory / "pool" / policy.apt_component
        paragraphs = []
        for path in sorted(pool.rglob("*.deb")):
            match = repositories.DEB_FILENAME.fullmatch(path.name)
            assert match is not None
            paragraphs.append(
                "\n".join(
                    (
                        f"Package: {match.group('name')}",
                        f"Version: {match.group('version')}",
                        f"Architecture: {match.group('arch')}",
                        f"Filename: {path.relative_to(directory).as_posix()}",
                        f"Size: {path.stat().st_size}",
                        f"SHA256: {_sha256(path)}",
                    )
                )
            )
        suite = directory / "dists" / target.suite
        binary = suite / policy.apt_component / f"binary-{target.architecture}"
        binary.mkdir(parents=True)
        (binary / "Packages").write_text("\n\n".join(paragraphs) + "\n")
        (binary / "Packages.gz").write_bytes(gzip.compress((binary / "Packages").read_bytes(), mtime=0))
        lines = [
            f"Origin: {policy.label}",
            f"Label: {policy.label}",
            f"Suite: {target.suite}",
            f"Codename: {target.suite}",
            f"Architectures: {target.architecture}",
            f"Components: {policy.apt_component}",
            "SHA256:",
        ]
        for path in sorted(binary.iterdir()):
            lines.append(f" {_sha256(path)} {path.stat().st_size} {path.relative_to(suite).as_posix()}")
        (suite / "Release").write_text("\n".join(lines) + "\n")

    def rpm(self, target, directory: Path) -> None:
        self.calls.append(f"rpm:{target.id}")
        repodata = directory / "repodata"
        if repodata.exists():
            shutil.rmtree(repodata)
        repodata.mkdir()
        packages = []
        for path in sorted((directory / "packages").iterdir()):
            match = repositories.RPM_FILENAME.fullmatch(path.name)
            assert match is not None
            packages.append(
                f'<package type="rpm"><name>{match.group("name")}</name>'
                f'<version epoch="0" ver="{match.group("version")}" rel="{match.group("release")}"/>'
                f'<checksum type="sha256" pkgid="YES">{_sha256(path)}</checksum>'
                f'<location href="packages/{path.name}"/></package>'
            )
        primary = (
            '<metadata xmlns="http://linux.duke.edu/metadata/common" '
            f'packages="{len(packages)}">{"".join(packages)}</metadata>'
        ).encode()
        entries = []
        for kind, payload in (
            ("primary", primary),
            ("filelists", b"<filelists/>"),
            ("other", b"<otherdata/>"),
        ):
            compressed = gzip.compress(payload, mtime=0)
            digest = hashlib.sha256(compressed).hexdigest()
            name = f"{digest}-{kind}.xml.gz"
            (repodata / name).write_bytes(compressed)
            entries.append(
                f'<data type="{kind}"><checksum type="sha256">{digest}</checksum>'
                f'<location href="repodata/{name}"/></data>'
            )
        (repodata / "repomd.xml").write_text(
            '<repomd xmlns="http://linux.duke.edu/metadata/repo">' + "".join(entries) + "</repomd>\n"
        )


def fake_gpg(command: list[str], *, label: str, cwd=None, timeout_seconds: int = 900):
    """Stand in for gpg and rpmsign: deterministic signatures by the policy key."""
    stdout = ""
    if command[0] == "rpmsign":
        for path in command[command.index("--define") + 8 :]:
            with open(path, "ab") as handle:
                handle.write(b"signed")
    elif "--clearsign" in command:
        output = Path(command[command.index("--output") + 1])
        payload = Path(command[-1]).read_text()
        output.write_text(f"-----BEGIN PGP SIGNED MESSAGE-----\n\n{payload}-----BEGIN PGP SIGNATURE-----\n")
    elif "--detach-sign" in command:
        Path(command[command.index("--output") + 1]).write_text("SIGNATURE\n")
    elif "--list-secret-keys" in command or "show-only" in command:
        stdout = f"sec:::::\nfpr:::::::::{PRIMARY}:\nssb:::::\nfpr:::::::::{SUBKEY}:\n"
    elif "--decrypt" in command:
        signed = Path(command[-1]).read_text()
        payload = signed.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----")[0]
        Path(command[command.index("--output") + 1]).write_text(payload)
        stdout = f"[GNUPG:] VALIDSIG {SUBKEY} 2026-10-17 0 0 4 0 1 10 01 {PRIMARY}\n"
    elif "--verify" in command:
        stdout = f"[GNUPG:] VALIDSIG {SUBKEY} 2026-10-17 0 0 4 0 1 10 00 {PRIMARY}\n"
    return subprocess.CompletedProcess(command, 0, stdout, "")


class PackageRepositorySiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hfx-site-test-")
        self.base = Path(self.temporary.name)
        self.packages = write_packages(self.base / "packages")
        self.policy = repositories.load_repository_site_policy(ROOT)
        self.targets = repositories.site_targets(ROOT)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assemble(self, name: str = "unsigned", tools: FakeTools | None = None) -> Path:
        output = self.base / name
        repositories.assemble_site(
            ROOT, packages=self.packages, output=output, tools=tools or FakeTools()
        )
        return output

    def arch_directory(self) -> Path:
        directory = self.base / "arch-signed"
        if directory.exists():
            return directory
        directory.mkdir()
        for package in ARCH.packages:
            (directory / package.filename).write_bytes(package.filename.encode())
            (directory / f"{package.filename}.sig").write_bytes(b"sig")
        (directory / "hyperflux-next.db").write_bytes(b"db")
        (directory / "hyperflux-next.db.sig").write_bytes(b"db-sig")
        return directory

    def sign(self, unsigned: Path, name: str = "signed") -> Path:
        output = self.base / name
        with (
            patch.object(repositories, "_execute", side_effect=fake_gpg),
            patch.object(repositories, "_validate_private_gnupg_home"),
            patch.object(repositories, "verify_arch_repository", return_value={"ok": True}),
        ):
            repositories.sign_site(
                ROOT,
                unsigned,
                output=output,
                identity=repositories.SigningIdentity(self.base / "gnupg", PRIMARY, SUBKEY),
                arch_repository=self.arch_directory(),
                tools=FakeTools(),
            )
        return output

    def verify_signed(self, site: Path):
        with (
            patch.object(repositories, "_execute", side_effect=fake_gpg),
            patch.object(repositories, "verify_arch_repository", return_value={"ok": True}),
        ):
            return repositories.verify_site(ROOT, site)

    def test_policy_is_unpublished_pages_site_on_gh_pages(self) -> None:
        self.assertEqual("unpublished", self.policy.state)
        self.assertFalse(self.policy.published)
        self.assertEqual("gh-pages", self.policy.publication_branch)
        self.assertTrue(self.policy.site_url.endswith(".github.io/hyperflux-next"))
        schema = json.loads((ROOT / "schemas/repository-site.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(json.loads((ROOT / repositories.POLICY_PATH).read_text())))

    def test_policy_rejects_unknown_fields_and_non_pages_urls(self) -> None:
        value = json.loads((ROOT / repositories.POLICY_PATH).read_text())
        for change in (
            {"extra": True},
            {"site_url": "https://example.com/hyperflux"},
            {"state": "live"},
            {"publication_branch": "main"},
        ):
            with self.subTest(change=change):
                root = self.base / f"root-{len(list(self.base.iterdir()))}"
                (root / "packaging").mkdir(parents=True)
                (root / repositories.POLICY_PATH).write_text(json.dumps({**value, **change}))
                with self.assertRaises(ModelError):
                    repositories.load_repository_site_policy(root)

    def test_targets_cover_each_distribution_release_with_pinned_images(self) -> None:
        self.assertEqual(("ubuntu", "debian", "fedora", "opensuse"), tuple(self.targets))
        self.assertEqual(
            {"ubuntu": "noble", "debian": "trixie", "fedora": "44", "opensuse": "tumbleweed"},
            {target_id: target.suite for target_id, target in self.targets.items()},
        )
        for target in self.targets.values():
            self.assertRegex(target.image, r"@sha256:[0-9a-f]{64}$")
            self.assertEqual(("hyperflux-next-dkms", "hyperflux-next-linux"), target.required_packages)
        self.assertEqual("fedora/44/x86_64", self.targets["fedora"].repository_path)
        self.assertEqual("ubuntu/pool/main/h/hyperflux-next", self.targets["ubuntu"].package_directory)

    def test_configuration_files_turn_on_signature_checks(self) -> None:
        url = self.policy.site_url
        sources = repositories.configuration_text(self.policy, self.targets["ubuntu"], url)
        self.assertIn(f"URIs: {url}/ubuntu\n", sources)
        self.assertIn("Suites: noble\n", sources)
        self.assertIn("Signed-By: /etc/apt/keyrings/hyperflux-next.asc\n", sources)
        fedora = repositories.configuration_text(self.policy, self.targets["fedora"], url)
        self.assertIn(f"baseurl={url}/fedora/$releasever/$basearch/\n", fedora)
        opensuse = repositories.configuration_text(self.policy, self.targets["opensuse"], url)
        self.assertIn(f"baseurl={url}/opensuse/tumbleweed/$basearch/\n", opensuse)
        self.assertIn("autorefresh=1\n", opensuse)
        for text in (fedora, opensuse):
            self.assertIn("gpgcheck=1\nrepo_gpgcheck=1\n", text)
            self.assertIn(f"gpgkey={url}/hyperflux-next-repository-key.asc\n", text)

    def test_collect_packages_requires_exact_set_at_one_version(self) -> None:
        ubuntu = self.targets["ubuntu"]
        found = repositories.collect_packages(ubuntu, self.packages["ubuntu"])
        self.assertEqual({"2.1.0-1"}, {package.version for package in found})
        (self.packages["ubuntu"] / "hyperflux-next-extra_2.1.0-1_amd64.deb").write_bytes(b"x")
        with self.assertRaisesRegex(ModelError, "needs exactly"):
            repositories.collect_packages(ubuntu, self.packages["ubuntu"])
        fedora = self.targets["fedora"]
        directory = self.packages["fedora"]
        (directory / f"hyperflux-next-linux-{VERSION}.x86_64.rpm").rename(
            directory / "hyperflux-next-linux-2.1.1-1.x86_64.rpm"
        )
        with self.assertRaisesRegex(ModelError, "different versions"):
            repositories.collect_packages(fedora, directory)
        (directory / "hyperflux-next-linux-2.1.1-1.x86_64.rpm").rename(
            directory / f"hyperflux-next-linux-{VERSION}.aarch64.rpm"
        )
        with self.assertRaisesRegex(ModelError, "wrong architecture"):
            repositories.collect_packages(fedora, directory)

    def test_assembly_builds_the_published_layout_without_any_key(self) -> None:
        tools = FakeTools()
        site = self.assemble(tools=tools)
        self.assertEqual(
            ["apt:ubuntu", "apt:debian", "rpm:fedora", "rpm:opensuse"], tools.calls
        )
        names = {path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()}
        for expected in (
            ".nojekyll",
            "index.html",
            "repository-site.json",
            "hyperflux-next-repository-key.asc",
            "ubuntu/hyperflux-next.sources",
            "ubuntu/dists/noble/Release",
            "debian/dists/trixie/main/binary-amd64/Packages.gz",
            "debian/pool/main/h/hyperflux-next/hyperflux-next-linux_2.1.0-1_amd64.deb",
            "fedora/hyperflux-next.repo",
            "fedora/44/x86_64/repodata/repomd.xml",
            "opensuse/tumbleweed/x86_64/packages/hyperflux-next-dkms-2.1.0-1.noarch.rpm",
        ):
            self.assertIn(expected, names)
        self.assertFalse(any(name.endswith(("InRelease", "Release.gpg", ".asc")) and "/" in name for name in names))
        self.assertFalse((site / "arch").exists())
        page = (site / "index.html").read_text()
        self.assertIn("<h1>Choose your distribution</h1>", page)
        for name in ("Arch Linux", "Ubuntu 24.04", "Debian 13", "Fedora 44", "openSUSE Tumbleweed"):
            self.assertIn(name, page)
        self.assertIn(" ".join(re.findall("....", PRIMARY)), page)
        self.assertEqual(
            (ROOT / ARCH.public_key_path).read_bytes(),
            (site / "hyperflux-next-repository-key.asc").read_bytes(),
        )
        manifest = json.loads((site / "repository-site.json").read_text())
        self.assertFalse(manifest["signed"])
        self.assertIsNone(manifest["signer"])
        report = repositories.verify_site(ROOT, site, require_signatures=False)
        self.assertFalse(report["ubuntu"]["signed"])

    def test_assembly_refuses_missing_target_and_existing_output(self) -> None:
        packages = dict(self.packages)
        packages.pop("opensuse")
        with self.assertRaisesRegex(ModelError, "missing=\\['opensuse'\\]"):
            repositories.assemble_site(ROOT, packages=packages, output=self.base / "x", tools=FakeTools())
        self.assemble("taken")
        with self.assertRaises(ModelError):
            self.assemble("taken")

    def test_assembly_accepts_local_rehearsal_address(self) -> None:
        output = self.base / "local"
        repositories.assemble_site(
            ROOT, packages=self.packages, output=output, site_url="file:///site", tools=FakeTools()
        )
        self.assertIn("URIs: file:///site/debian\n", (output / "debian/hyperflux-next.sources").read_text())
        for bad in ("http://example.com/site", "https://example.com/site/", "file://site"):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                repositories.assemble_site(
                    ROOT, packages=self.packages, output=self.base / "bad", site_url=bad, tools=FakeTools()
                )

    def test_unsigned_verification_catches_tampering(self) -> None:
        site = self.assemble()
        cases = {
            "Packages": site / "ubuntu/dists/noble/main/binary-amd64/Packages",
            "front page": site / "index.html",
            "repo file": site / "fedora/hyperflux-next.repo",
            "package": site / "opensuse/tumbleweed/x86_64/packages/hyperflux-next-dkms-2.1.0-1.noarch.rpm",
        }
        for label, path in cases.items():
            with self.subTest(label=label):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaises(ModelError):
                    repositories.verify_site(ROOT, site, require_signatures=False)
                path.write_bytes(original)
        repositories.verify_site(ROOT, site, require_signatures=False)
        extra = site / "fedora/44/x86_64/repodata/stray.xml.gz"
        extra.write_bytes(b"")
        extra.chmod(0o644)
        with self.assertRaisesRegex(ModelError, "repodata holds files"):
            repositories.verify_site(ROOT, site, require_signatures=False)
        extra.unlink()
        (site / "index.html").chmod(0o600)
        with self.assertRaisesRegex(ModelError, "wrong mode"):
            repositories.verify_site(ROOT, site, require_signatures=False)
        (site / "index.html").chmod(0o644)
        with self.assertRaisesRegex(ModelError, "unsigned"):
            self.verify_signed(site)

    def test_signing_adds_arch_signs_metadata_and_rebuilds_rpm_metadata(self) -> None:
        unsigned = self.assemble()
        # A downloaded CI artifact loses file modes; signing restores them.
        (unsigned / "index.html").chmod(0o600)
        before = (unsigned / "fedora/44/x86_64/repodata/repomd.xml").read_bytes()
        site = self.sign(unsigned)
        for name in (
            "ubuntu/dists/noble/InRelease",
            "ubuntu/dists/noble/Release.gpg",
            "debian/dists/trixie/InRelease",
            "fedora/44/x86_64/repodata/repomd.xml.asc",
            "opensuse/tumbleweed/x86_64/repodata/repomd.xml.asc",
            "arch/hyperflux-next.db.sig",
        ):
            self.assertTrue((site / name).is_file(), name)
        self.assertNotEqual(before, (site / "fedora/44/x86_64/repodata/repomd.xml").read_bytes())
        self.assertTrue(
            (site / "fedora/44/x86_64/packages/hyperflux-next-dkms-2.1.0-1.noarch.rpm").read_bytes().endswith(b"signed")
        )
        manifest = json.loads((site / "repository-site.json").read_text())
        self.assertTrue(manifest["signed"])
        self.assertEqual({"primary_fingerprint": PRIMARY, "signing_subkey_fingerprint": SUBKEY}, manifest["signer"])
        report = self.verify_signed(site)
        self.assertTrue(report["fedora"]["signed"])
        self.assertFalse((unsigned / "arch").exists())

    def test_signing_refuses_a_signed_site_and_a_foreign_key(self) -> None:
        unsigned = self.assemble()
        site = self.sign(unsigned)
        with self.assertRaisesRegex(ModelError, "already signed"):
            self.sign(site, "again")

        def other_key(command, **arguments):
            if "show-only" in command:
                return subprocess.CompletedProcess(command, 0, "fpr:::::::::" + "A" * 40 + ":\n", "")
            return fake_gpg(command, **arguments)

        with (
            patch.object(repositories, "_execute", side_effect=other_key),
            patch.object(repositories, "_validate_private_gnupg_home"),
            self.assertRaisesRegex(ModelError, "not the signing key"),
        ):
            repositories.sign_site(
                ROOT,
                unsigned,
                output=self.base / "foreign",
                identity=repositories.SigningIdentity(self.base / "gnupg", PRIMARY, SUBKEY),
                arch_repository=self.base,
                tools=FakeTools(),
            )
        self.assertFalse((self.base / "foreign").exists())

    def test_signed_verification_rejects_wrong_signer_and_changed_release(self) -> None:
        site = self.sign(self.assemble())

        def wrong_signer(command, **arguments):
            result = fake_gpg(command, **arguments)
            return subprocess.CompletedProcess(command, 0, result.stdout.replace(SUBKEY, "B" * 40), "")

        with (
            patch.object(repositories, "_execute", side_effect=wrong_signer),
            patch.object(repositories, "verify_arch_repository", return_value={}),
            self.assertRaisesRegex(ModelError, "unexpected key"),
        ):
            repositories.verify_site(ROOT, site)
        inrelease = site / "debian/dists/trixie/InRelease"
        inrelease.write_text(inrelease.read_text().replace("Suite: trixie", "Suite: sid"))
        with self.assertRaisesRegex(ModelError, "InRelease does not carry the same Release"):
            self.verify_signed(site)

    def test_signature_status_requires_one_good_signature_by_the_subkey(self) -> None:
        good = f"[GNUPG:] VALIDSIG {SUBKEY} 2026-10-17 0 0 4 0 1 10 00 {PRIMARY}\n"
        repositories._signature_status(good, (PRIMARY, SUBKEY), "x")
        for output in ("", good + good, good + "[GNUPG:] BADSIG 00 x\n", good.replace(PRIMARY, "C" * 40)):
            with self.subTest(output=output), self.assertRaises(ModelError):
                repositories._signature_status(output, (PRIMARY, SUBKEY), "x")

    def test_rpm_header_signature_returns_signature_and_signed_header(self) -> None:
        path = self.base / "package.rpm"
        path.write_bytes(synthetic_rpm(b"SIG"))
        signature, header = repositories.rpm_header_signature(path)
        self.assertEqual(b"SIG", signature)
        self.assertTrue(header.startswith(repositories.RPM_HEADER_MAGIC))
        self.assertTrue(header.endswith(b"abc\0"))
        path.write_bytes(b"not an rpm")
        with self.assertRaisesRegex(ModelError, "not an RPM"):
            repositories.rpm_header_signature(path)
        unsigned = bytearray(synthetic_rpm())
        unsigned[96 + 16 : 96 + 20] = struct.pack(">I", 1004)
        path.write_bytes(bytes(unsigned))
        with self.assertRaisesRegex(ModelError, "no OpenPGP header signature"):
            repositories.rpm_header_signature(path)

    def test_signing_commands_name_the_signing_subkey_only(self) -> None:
        identity = repositories.SigningIdentity(Path("signing-keyring"), PRIMARY, SUBKEY)
        command = repositories.rpmsign_command(identity, [Path("a.rpm")])
        self.assertEqual(["rpmsign", "--addsign"], command[:2])
        self.assertIn(f"_gpg_name {SUBKEY}!", command)
        self.assertIn(f"_openpgp_sign_id {SUBKEY}!", command)
        self.assertEqual("a.rpm", command[-1])
        gpg = repositories._gpg_sign_command(identity, "--clearsign")
        self.assertEqual(f"{SUBKEY}!", gpg[gpg.index("--local-user") + 1])
        self.assertIn("SHA512", gpg)

    def test_metadata_scripts_use_pinned_distribution_tools(self) -> None:
        script = repositories.apt_metadata_script(self.policy, self.targets["debian"])
        self.assertIn("apt-ftparchive --arch amd64 packages pool/main > dists/trixie/main/binary-amd64/Packages", script)
        self.assertIn("APT::FTPArchive::Release::Codename=trixie", script)
        self.assertNotIn("Valid-Until", script)
        self.assertIn("createrepo_c", repositories.rpm_metadata_script())
        with patch.object(repositories.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), patch.dict(
            "os.environ", {"HFX_OCI_ENGINE": "podman"}
        ):
            tools = repositories.ContainerTools()
            command = tools.command(self.targets["fedora"], Path("/tmp/repo"), "true")
        self.assertEqual(["/usr/bin/podman", "run", "--rm"], command[:3])
        self.assertIn(self.targets["fedora"].image, command)
        self.assertIn("createrepo_c", command[-1])
        self.assertNotIn("chown", command[-1])
        with self.assertRaises(ModelError):
            repositories.metadata_tools("host")

    def test_publish_plan_replaces_gh_pages_and_uploads_only_new_arch_assets(self) -> None:
        site = self.base / "site"
        (site / "arch").mkdir(parents=True)
        for path in self.arch_directory().iterdir():
            shutil.copyfile(path, site / "arch" / path.name)
        work = self.base / "work"
        steps = repositories.publish_steps(
            ROOT, site, work=work, remote="git@github.com:o/r.git", present_assets=None
        )
        commands = [step.command for step in steps]
        self.assertEqual(("git", "init", "--quiet", "--initial-branch", "gh-pages"), commands[0])
        self.assertEqual(("git", "push", "--force", "git@github.com:o/r.git", "HEAD:refs/heads/gh-pages"), commands[3])
        self.assertTrue(all(step.cwd == work for step in steps[:4]))
        upload, metadata = commands[4], commands[5]
        self.assertNotIn("--clobber", upload)
        self.assertIn("--clobber", metadata)
        self.assertIn(str(site / "arch" / ARCH.packages[0].filename), upload)
        self.assertIn(str(site / "arch" / "hyperflux-next.db"), metadata)
        present = {
            path.name: _sha256(path) for path in (site / "arch").iterdir() if ".pkg.tar.zst" in path.name
        }
        steps = repositories.publish_steps(ROOT, site, work=work, remote="r", present_assets=present)
        self.assertEqual(5, len(steps))
        self.assertIn("--clobber", steps[-1].command)
        present[ARCH.packages[0].filename] = "0" * 64
        with self.assertRaisesRegex(ModelError, "different bytes"):
            repositories.publish_steps(ROOT, site, work=work, remote="r", present_assets=present)

    def test_publish_dry_run_prints_commands_and_runs_nothing(self) -> None:
        site = self.base / "site"
        (site / "arch").mkdir(parents=True)
        for path in self.arch_directory().iterdir():
            shutil.copyfile(path, site / "arch" / path.name)
        lines: list[str] = []
        calls: list[list[str]] = []

        def record(command, **arguments):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "git@github.com:o/r.git\n", "")

        with patch.object(repositories, "verify_site") as verify, patch.object(
            repositories, "_execute", side_effect=record
        ):
            repositories.publish_site(ROOT, site, execute=False, emit=lines.append)
        verify.assert_called_once()
        self.assertEqual([["git", "-C", str(ROOT), "remote", "get-url", "origin"]], calls)
        text = "\n".join(lines)
        self.assertIn("git push --force git@github.com:o/r.git HEAD:refs/heads/gh-pages", text)
        self.assertIn("gh release upload arch-repository", text)
        self.assertIn("Dry run", text)

    def test_arch_guides_switch_to_pages_only_once_published(self) -> None:
        self.assertEqual(ARCH, repositories.arch_policy_for_guides(ARCH, self.policy))
        published = repositories.RepositorySitePolicy(**{**self.policy.__dict__, "state": "published"})
        self.assertEqual(
            f"{self.policy.site_url}/arch",
            repositories.arch_policy_for_guides(ARCH, published).server_url,
        )


class RepositoryRehearsalTests(unittest.TestCase):
    def test_runs_cover_every_client_and_a_refusal_for_each(self) -> None:
        runs = rehearsal.consumption_runs(ROOT, Path("/s"), Path("/k.asc"))
        names = [run.name for run in runs]
        for client in ("ubuntu-apt", "debian-apt", "fedora-dnf", "opensuse-zypper", "arch-pacman"):
            self.assertIn(f"{client}-install", names)
            self.assertTrue(any(name.startswith(f"{client}-refuses") for name in names), client)
        images = repositories.site_targets(ROOT)
        for run in runs:
            if run.name.startswith("arch"):
                self.assertEqual(rehearsal.DEVELOPMENT_IMAGE, run.image)
            else:
                self.assertEqual(images[run.name.split("-")[0]].image, run.image)
            self.assertIn(("/s", "/site", "ro"), run.mounts)

    def test_client_scripts_trust_only_the_named_key(self) -> None:
        apt = rehearsal.apt_script("debian", "/keys/k.asc", "hyperflux-next", expect_success=True)
        self.assertIn("install -m 0644 /keys/k.asc /etc/apt/keyrings/hyperflux-next.asc", apt)
        self.assertIn("apt-get install -y -q --no-install-recommends hyperflux-next-linux", apt)
        refusal = rehearsal.apt_script("debian", "/keys/o.asc", "hyperflux-next", expect_success=False)
        self.assertIn("Dir::Etc::SourceParts=/tmp/parts", refusal)
        self.assertIn("NO_PUBKEY", refusal)
        dnf = rehearsal.dnf_script("/keys/o.asc", "hyperflux-next", expect_success=False)
        self.assertIn("gpgkey=file:///keys/o.asc", dnf)
        self.assertIn("repomd.xml GPG signature verification error", dnf)
        self.assertIn("if rpm -q hyperflux-next-linux; then exit 1; fi", dnf)
        zypper = rehearsal.zypper_script("/keys/k.asc", "hyperflux-next", expect_success=True)
        self.assertIn("zypper --non-interactive install --no-recommends hyperflux-next-linux", zypper)
        self.assertNotIn("gpg-auto-import-keys", zypper)
        pacman = rehearsal.pacman_script("[x]\nServer = file:///site/arch\n", PRIMARY, expect_success=False)
        self.assertNotIn("lsign-key", pacman)
        self.assertIn("Server = file:///site/arch", pacman)

    def test_throwaway_key_lives_in_a_container_tmpfs(self) -> None:
        script = rehearsal.signing_script()
        self.assertIn("export GNUPGHOME=/keys/home", script)
        self.assertIn("--quick-generate-key 'Throwaway Rehearsal <throwaway@invalid>' rsa3072 cert 1d", script)
        self.assertNotIn("--export-secret", script)
        run = rehearsal.RehearsalRun("sign", "image", script, True, (), ("/keys:rw,mode=0700",))
        command = rehearsal.container_command("podman", run)
        self.assertEqual(["--tmpfs", "/keys:rw,mode=0700"], command[command.index("--tmpfs") : command.index("--tmpfs") + 2])


if __name__ == "__main__":
    unittest.main()
