# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.arch_repository import (
    _publish_atomic_directory,
    _payload_asset_names,
    _repository_manifest,
    build_arch_repository,
    fingerprint_receipt,
    load_arch_repository_policy,
    repository_configuration,
    repository_setup_script,
)
from hfxdev.model import ModelError


class ArchRepositoryTests(unittest.TestCase):
    def test_policy_binds_exact_two_package_release_and_third_party_channel(
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
            {"hyperflux-next-linux", "hyperflux-next-full"},
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
                    core_package=packages["hyperflux-next-linux"],
                    full_package=packages["hyperflux-next-full"],
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
                    core_package=packages["hyperflux-next-linux"],
                    full_package=packages["hyperflux-next-full"],
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


if __name__ == "__main__":
    unittest.main()
