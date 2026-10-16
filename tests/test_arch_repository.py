# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.arch_repository import (
    RepositoryPackage,
    _publish_atomic_directory,
    _payload_asset_names,
    _repository_manifest,
    build_arch_repository,
    fingerprint_receipt,
    load_arch_repository_policy,
    load_arch_repository_retention,
    repository_configuration,
    repository_package_set,
    repository_setup_script,
    retained_asset_names,
    verify_retained_packages,
)
from hfxdev.arch_repository_public import verify_public_arch_repository
from hfxdev.generators.installation import package_fallback
from hfxdev.model import ModelError


class ArchRepositoryTests(unittest.TestCase):
    def test_policy_binds_exact_release_package_set_and_third_party_channel(
        self,
    ) -> None:
        policy = load_arch_repository_policy(ROOT)
        self.assertEqual(policy.name, "hyperflux-next")
        self.assertFalse(policy.official_arch_repository)
        self.assertEqual(policy.release_tag, "arch-repository")
        (version,) = {package.version for package in policy.packages}
        self.assertEqual(policy.product_release_tag, f"v{version.split('-')[0]}")
        self.assertEqual(policy.channel_purpose, "machine-managed-arch-repository")
        self.assertTrue(policy.github_release_prerelease)
        self.assertFalse(policy.human_facing_product_release)
        self.assertEqual(
            {package.name for package in policy.packages},
            set(repository_package_set(ROOT, version)),
        )
        self.assertEqual(
            {package.filename for package in policy.packages},
            {
                f"{package.name}-{version}-x86_64.pkg.tar.zst"
                for package in policy.packages
            },
        )
        self.assertTrue(policy.server_url.startswith("https://"))
        self.assertIn("/releases/download/arch-repository", policy.server_url)
        self.assertNotIn("/releases/latest/download", policy.server_url)

    def test_release_package_set_follows_the_split_first_release(self) -> None:
        self.assertEqual(
            {
                "hyperflux-next-linux": (),
                "hyperflux-next-full": ("hyperflux-next-linux",),
            },
            repository_package_set(ROOT, "2.0.2-1"),
        )
        self.assertEqual(
            {
                "hyperflux-next-dkms": (),
                "hyperflux-next-linux": ("hyperflux-next-dkms",),
                "hyperflux-next-openrazer-driver-dkms": ("hyperflux-next-dkms",),
                "hyperflux-next-full": (
                    "hyperflux-next-linux",
                    "hyperflux-next-openrazer-driver-dkms",
                ),
            },
            repository_package_set(ROOT, "2.1.0-1"),
        )

    def test_policy_rejects_a_release_tag_or_filename_that_differs_from_packages(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "assurance" / "arch-repository.json").read_text(encoding="utf-8")
        )
        key = ROOT / "packaging" / "keys" / "hyperflux-next-arch-repository-key.asc"

        def mutated_tag(value: dict) -> None:
            value["repository"]["product_release_tag"] = "v0.0.1"
            value["repository"]["product_release_url"] = (
                "https://github.com/offalexjackson777-stack/hyperflux-next/"
                "releases/tag/v0.0.1"
            )

        def mutated_url(value: dict) -> None:
            value["repository"]["product_release_url"] += "x"

        def mutated_filename(value: dict) -> None:
            value["packages"][0]["filename"] = "hyperflux-next-linux.pkg.tar.zst"

        def mutated_version(value: dict) -> None:
            value["packages"][0]["version"] = "9.9.9-1"

        cases = {
            "product release differs": mutated_tag,
            "identity differs": mutated_url,
            "filename is not canonical": mutated_filename,
            "versions must match": mutated_version,
        }
        for message, mutate in cases.items():
            with self.subTest(message), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                value = json.loads(json.dumps(source))
                mutate(value)
                (root / "assurance").mkdir()
                (root / "assurance" / "arch-repository.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                (root / "packaging" / "keys").mkdir(parents=True)
                (root / "packaging" / "keys" / key.name).write_bytes(key.read_bytes())
                with self.assertRaisesRegex(ModelError, message):
                    load_arch_repository_policy(root)

    def test_policy_rejects_a_package_set_other_than_the_release_set(self) -> None:
        source = json.loads(
            (ROOT / "assurance" / "arch-repository.json").read_text(encoding="utf-8")
        )
        missing = json.loads(json.dumps(source))
        missing["packages"] = missing["packages"][1:]
        with patch("hfxdev.arch_repository.load_json", return_value=missing):
            with self.assertRaisesRegex(ModelError, "not the exact .* package set"):
                load_arch_repository_policy(ROOT)

    def test_direct_install_names_every_package_of_a_split_release(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        version = "2.1.0-1"
        names = tuple(repository_package_set(ROOT, version))
        split = dataclasses.replace(
            policy,
            product_release_tag="v2.1.0",
            product_release_url=policy.product_release_url.replace(
                policy.product_release_tag, "v2.1.0"
            ),
            packages=tuple(
                RepositoryPackage(
                    name, version, f"{name}-{version}-x86_64.pkg.tar.zst", "0" * 64
                )
                for name in names
            ),
        )
        text = package_fallback(split)
        script = text.split("```bash\n", 1)[1].split("```", 1)[0]
        for package in split.packages:
            self.assertEqual(script.count(package.filename), 3, package.name)
        self.assertIn(f'test "$(wc -l < SHA256SUMS-packages)" -eq {len(names)}', script)
        self.assertIn("installs them in one step", text)
        syntax = subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_configuration_requires_trusted_package_and_database_signatures(
        self,
    ) -> None:
        policy = load_arch_repository_policy(ROOT)
        configuration = repository_configuration(policy)
        self.assertEqual(
            configuration,
            "[hyperflux-next]\n"
            "SigLevel = Required DatabaseRequired TrustedOnly\n"
            "Server = https://github.com/offalexjackson777-stack/hyperflux-next/"
            "releases/download/arch-repository\n",
        )
        self.assertNotIn("Never", configuration)
        self.assertNotIn("TrustAll", configuration)
        self.assertNotIn("file://", configuration)

    def test_displayed_setup_is_one_fail_closed_idempotent_block(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        script = repository_setup_script(policy).rstrip()
        self.assertTrue(
            script.startswith(
                "bash <<'HFX_REPOSITORY_SETUP'\nset -euo pipefail\n"
            )
        )
        self.assertTrue(script.endswith("\nHFX_REPOSITORY_SETUP"))
        self.assertNotEqual(script.splitlines()[0], "set -euo pipefail")
        self.assertEqual(script.count("HFX_REPOSITORY_SETUP"), 2)
        self.assertIn(policy.primary_fingerprint, script)
        self.assertIn(policy.signing_subkey_fingerprint, script)
        self.assertIn(policy.server_url, script)
        self.assertIn("SigLevel = Required DatabaseRequired TrustedOnly", script)
        self.assertIn("include_count", script)
        self.assertIn("section_count", script)
        self.assertIn("[[ \"$include_count\" -eq 0 ]]", script)
        self.assertNotIn("SigLevel = Never", script)
        self.assertNotIn("TrustAll", script)
        self.assertNotIn("curl |", script)
        self.assertNotIn(f"{policy.server_url}/hyperflux-next.conf", script)
        self.assertLess(script.index("fingerprint mismatch"), script.index("sudo"))
        fenced = f"```bash\n{script}\n```"
        # The README links to the guide; only the guide shows the setup block.
        for path, blocks in (
            (ROOT / "README.md", 0),
            (ROOT / "docs" / "user" / "installation.md", 1),
        ):
            document = path.read_text(encoding="utf-8")
            self.assertEqual(document.count(fenced), blocks, path)
            self.assertEqual(
                document.count("sudo pacman -Syu hyperflux-next-full"), 1, path
            )
            self.assertEqual(
                document.count("sudo pacman -Syu hyperflux-next-linux"), 1, path
            )

    def test_fingerprint_receipt_is_full_and_truthfully_third_party(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        receipt = fingerprint_receipt(policy)
        self.assertIn(
            "8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8", receipt
        )
        self.assertIn("not an official Arch Linux repository", receipt)
        self.assertNotIn("PRIVATE KEY", receipt)

    def test_tracked_public_key_matches_both_canonical_fingerprints(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--with-colons",
                "--import-options",
                "show-only",
                "--import",
                str(ROOT / policy.public_key_path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        fingerprints = {
            line.split(":")[9]
            for line in result.stdout.splitlines()
            if line.startswith("fpr:")
        }
        self.assertIn(policy.primary_fingerprint, fingerprints)
        self.assertIn(policy.signing_subkey_fingerprint, fingerprints)

    def test_manifest_binds_the_complete_policy_and_excludes_configuration_asset(
        self,
    ) -> None:
        policy = load_arch_repository_policy(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            for name in _payload_asset_names(policy):
                (repository / name).write_bytes(name.encode("ascii"))
            manifest = _repository_manifest(ROOT, repository, policy)
        authority = json.loads(
            (ROOT / "assurance" / "arch-repository.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["policy"], authority)
        self.assertEqual(
            manifest["policy"]["packages"], authority["packages"]
        )
        self.assertNotIn(
            "hyperflux-next.conf",
            {item["name"] for item in manifest["published_assets"]},
        )

    def test_builder_rejects_nonfrozen_bytes_before_creating_output(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            packages = {}
            for package in policy.packages:
                path = workspace / package.filename
                path.write_bytes(b"not the frozen package")
                packages[package.name] = path
            gnupg_home = workspace / "gnupg"
            gnupg_home.mkdir(mode=0o700)
            output = workspace / "repository"
            with self.assertRaisesRegex(ModelError, "package bytes differ"):
                build_arch_repository(
                    ROOT,
                    packages=tuple(packages.values()),
                    gnupg_home=gnupg_home,
                    key_fingerprint=policy.primary_fingerprint,
                    output=output,
                )
            self.assertFalse(output.exists())

    def test_builder_rejects_nonprivate_signing_home_before_output(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            packages = {}
            for package in policy.packages:
                path = workspace / package.filename
                path.write_bytes(b"not inspected because the key home is unsafe")
                packages[package.name] = path
            gnupg_home = workspace / "gnupg"
            gnupg_home.mkdir(mode=0o755)
            output = workspace / "repository"
            with self.assertRaisesRegex(ModelError, "mode 0700"):
                build_arch_repository(
                    ROOT,
                    packages=tuple(packages.values()),
                    gnupg_home=gnupg_home,
                    key_fingerprint=policy.primary_fingerprint,
                    output=output,
                )
            self.assertFalse(output.exists())

    def test_atomic_publisher_removes_failed_candidate_and_publishes_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            output = workspace / "repository"

            def fail(candidate: Path) -> None:
                candidate.mkdir()
                (candidate / "partial").write_text("partial", encoding="ascii")
                raise ModelError("injected verification failure")

            with self.assertRaisesRegex(ModelError, "injected verification failure"):
                _publish_atomic_directory(output, fail)
            self.assertFalse(output.exists())
            self.assertEqual(list(workspace.glob(".repository.tmp-*")), [])

            def succeed(candidate: Path) -> None:
                candidate.mkdir()
                (candidate / "complete").write_text("complete", encoding="ascii")

            _publish_atomic_directory(output, succeed)
            self.assertEqual(
                (output / "complete").read_text(encoding="ascii"), "complete"
            )


    def test_retention_keeps_exactly_one_earlier_release_package_set(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        retained = load_arch_repository_retention(ROOT, policy)
        self.assertTrue(retained)
        (version,) = {package.version for package in retained}
        current = {package.filename for package in policy.packages}
        self.assertEqual(
            sorted(package.name for package in retained),
            sorted(repository_package_set(ROOT, version)),
        )
        self.assertFalse(current & {package.filename for package in retained})
        self.assertEqual(
            retained_asset_names(retained),
            tuple(
                sorted(
                    name
                    for package in retained
                    for name in (package.filename, f"{package.filename}.sig")
                )
            ),
        )
        self.assertFalse(
            set(retained_asset_names(retained)) & set(_payload_asset_names(policy))
        )

    def test_retention_rejects_anything_but_one_earlier_release_set(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        source = json.loads(
            (ROOT / "assurance" / "arch-repository-retention.json").read_text(
                encoding="utf-8"
            )
        )

        def mixed_versions(value: dict) -> None:
            value["packages"][0]["version"] = "2.0.1-1"
            value["packages"][0]["filename"] = (
                f"{value['packages'][0]['name']}-2.0.1-1-x86_64.pkg.tar.zst"
            )

        def current_release(value: dict) -> None:
            value["packages"] = [
                dataclasses.asdict(package) for package in policy.packages
            ]

        def noncanonical_filename(value: dict) -> None:
            value["packages"][0]["filename"] = "hyperflux-next-linux.pkg.tar.zst"

        def partial_set(value: dict) -> None:
            value["packages"] = value["packages"][:1]

        def unknown_field(value: dict) -> None:
            value["packages"][0]["signature"] = "x"

        cases = {
            "exactly one release": mixed_versions,
            "not an earlier release": current_release,
            "filename is not canonical": noncanonical_filename,
            "not the exact .* package set": partial_set,
            "missing or unknown fields": unknown_field,
        }
        for message, mutate in cases.items():
            with self.subTest(message):
                value = json.loads(json.dumps(source))
                mutate(value)
                with patch("hfxdev.arch_repository.load_json", return_value=value):
                    with self.assertRaisesRegex(ModelError, message):
                        load_arch_repository_retention(ROOT, policy)
        empty = json.loads(json.dumps(source))
        empty["packages"] = []
        with patch("hfxdev.arch_repository.load_json", return_value=empty):
            self.assertEqual((), load_arch_repository_retention(ROOT, policy))

    def test_retained_packages_must_match_inventory_and_pinned_bytes(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        retained = load_arch_repository_retention(ROOT, policy)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in retained_asset_names(retained):
                (directory / name).write_bytes(b"not the pinned package")
            (directory / "hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst").write_bytes(
                b"stale"
            )
            with self.assertRaisesRegex(ModelError, r"extra=\['hyperflux-next-full-2\.0\.0"):
                verify_retained_packages(
                    directory,
                    policy=policy,
                    retained=retained,
                    public_key=ROOT / policy.public_key_path,
                )
            (directory / "hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst").unlink()
            with self.assertRaisesRegex(ModelError, "retained package changed"):
                verify_retained_packages(
                    directory,
                    policy=policy,
                    retained=retained,
                    public_key=ROOT / policy.public_key_path,
                )

    def test_public_verifier_names_assets_outside_the_retention_rule(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        retained = load_arch_repository_retention(ROOT, policy)
        stale = "hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst"
        published = [
            *_payload_asset_names(policy),
            policy.manifest,
            policy.checksums,
            *retained_asset_names(retained),
            stale,
        ]
        release = {
            "tag_name": policy.release_tag,
            "html_url": policy.release_url,
            "draft": False,
            "prerelease": True,
            "id": 1,
            "assets": [
                {"name": name, "browser_download_url": f"{policy.server_url}/{name}"}
                for name in published
            ],
        }

        def fetch(url: str, destination: Path) -> str:
            destination.write_text(json.dumps(release), encoding="utf-8")
            return url

        with (
            patch("hfxdev.arch_repository_public._require_tools"),
            patch(
                "hfxdev.arch_repository_public._run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
            patch("hfxdev.arch_repository_public._fetch", side_effect=fetch),
            tempfile.TemporaryDirectory() as temporary,
        ):
            with self.assertRaisesRegex(
                ModelError, rf"inventory differs \(missing=\[\], extra=\['{stale}'\]\)"
            ):
                verify_public_arch_repository(ROOT, Path(temporary) / "receipt.json")

if __name__ == "__main__":
    unittest.main()
