# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.arch_repository import (
    build_arch_repository,
    fingerprint_receipt,
    load_arch_repository_policy,
    repository_configuration,
)
from hfxdev.model import ModelError


class ArchRepositoryTests(unittest.TestCase):
    def test_policy_binds_exact_two_package_release_and_third_party_channel(
        self,
    ) -> None:
        policy = load_arch_repository_policy(ROOT)
        self.assertEqual(policy.name, "hyperflux-next")
        self.assertFalse(policy.official_arch_repository)
        self.assertEqual(policy.release_tag, "v2.0.0")
        self.assertEqual(
            {package.name for package in policy.packages},
            {"hyperflux-next-linux", "hyperflux-next-full"},
        )
        self.assertEqual({package.version for package in policy.packages}, {"2.0.0-1"})
        self.assertTrue(policy.server_url.startswith("https://"))
        self.assertIn("/releases/latest/download", policy.server_url)

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
            "releases/latest/download\n",
        )
        self.assertNotIn("Never", configuration)
        self.assertNotIn("TrustAll", configuration)
        self.assertNotIn("file://", configuration)

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


if __name__ == "__main__":
    unittest.main()
