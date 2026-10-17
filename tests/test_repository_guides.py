# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import package_repositories as repositories
from hfxdev.arch_repository import load_arch_repository_policy
from hfxdev.generators import repository_guides as guides


ARCH = load_arch_repository_policy(ROOT)


class RepositoryGuideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = repositories.load_repository_site_policy(ROOT)
        self.targets = repositories.site_targets(ROOT)
        self.live = replace(self.site, state="published")

    def test_unpublished_guides_say_so_before_the_steps(self) -> None:
        for target in self.targets.values():
            text = guides.repository_setup(self.site, ARCH, target, "ci-only")
            self.assertTrue(text.startswith("> [!NOTE]"), target.id)
            self.assertIn(f"The {target.name} packages are not published yet.", text)
        live = guides.repository_setup(self.live, ARCH, self.targets["fedora"], "ci-only")
        self.assertIn("not published yet", live)
        ready = guides.repository_setup(self.live, ARCH, self.targets["fedora"], "published")
        self.assertNotIn("not published yet", ready)

    def test_every_guide_checks_both_fingerprints_before_trusting_the_key(self) -> None:
        for target in self.targets.values():
            text = guides.repository_setup(self.site, ARCH, target, "ci-only")
            check = text.index("gpg --show-keys --with-subkey-fingerprints")
            self.assertIn(f"Main key: `{ARCH.primary_fingerprint}`", text)
            self.assertIn(f"Signing subkey: `{ARCH.signing_subkey_fingerprint}`", text)
            self.assertIn(f"{self.site.site_url}/{self.site.public_key_asset}", text)
            install = text.index("hyperflux-next-linux")
            self.assertLess(check, install, target.id)

    def test_apt_guides_pin_the_key_to_the_repository(self) -> None:
        for target_id in ("ubuntu", "debian"):
            target = self.targets[target_id]
            text = guides.repository_setup(self.site, ARCH, target, "ci-only")
            self.assertIn("--output /etc/apt/keyrings/hyperflux-next.asc", text)
            self.assertIn("--output /etc/apt/sources.list.d/hyperflux-next.sources", text)
            self.assertIn(f"   Suites: {target.suite}", text)
            self.assertIn("   Signed-By: /etc/apt/keyrings/hyperflux-next.asc", text)
            self.assertIn("sudo apt update\n   sudo apt install hyperflux-next-linux", text)
            self.assertNotIn("rpm --import", text)
            for line in repositories.configuration_text(self.site, target, self.site.site_url).splitlines():
                self.assertIn(f"   {line}".rstrip(), text)

    def test_rpm_guides_import_the_key_and_check_repository_signatures(self) -> None:
        fedora = guides.repository_setup(self.site, ARCH, self.targets["fedora"], "ci-only")
        self.assertIn("sudo rpm --import hyperflux-next-repository-key.asc", fedora)
        self.assertIn("--output /etc/yum.repos.d/hyperflux-next.repo", fedora)
        self.assertIn("sudo dnf install hyperflux-next-linux", fedora)
        opensuse = guides.repository_setup(self.site, ARCH, self.targets["opensuse"], "ci-only")
        self.assertIn(
            f"sudo zypper addrepo {self.site.site_url}/opensuse/hyperflux-next.repo", opensuse
        )
        self.assertIn("sudo zypper install hyperflux-next-linux", opensuse)
        for text in (fedora, opensuse):
            self.assertIn("   gpgcheck=1", text)
            self.assertIn("   repo_gpgcheck=1", text)
            self.assertLess(text.index("rpm --import"), text.index("hyperflux-next.repo"))

    def test_install_table_lists_every_guide(self) -> None:
        maturities = {target_id: "ci-only" for target_id in self.targets}
        table = guides.install_table(self.site, self.targets, maturities)
        self.assertIn("| Arch Linux | rolling | [Install on Arch Linux](docs/user/installation.md) | Ready |", table)
        for target in self.targets.values():
            self.assertIn(
                f"| {target.distribution} | {target.release} | "
                f"[Install on {target.distribution}]({repositories.GUIDES[target.id]}) | Not published yet |",
                table,
            )
        ready = guides.install_table(self.live, self.targets, {**maturities, "fedora": "published"})
        self.assertIn("[Install on Fedora](docs/user/install-fedora.md) | Ready |", ready)
        self.assertIn("[Install on Debian](docs/user/install-debian.md) | Not published yet |", ready)
        flat = " ".join(table.split())
        self.assertIn(
            "Packages are published for Arch Linux. The Ubuntu, Debian, Fedora, and "
            "openSUSE packages are built and tested in CI but not published yet;",
            flat,
        )
        self.assertNotIn("quick steps", flat)
        flat_ready = " ".join(ready.split())
        self.assertIn("Packages are published for Arch Linux and Fedora.", flat_ready)
        all_ready = guides.install_table(
            self.live, self.targets, {target_id: "published" for target_id in self.targets}
        )
        self.assertIn(
            "Packages are published for every distribution in the table.",
            " ".join(all_ready.split()),
        )

    def test_catalog_registers_each_guide_with_its_block(self) -> None:
        catalog = {entry["path"]: entry for entry in json.loads((ROOT / "docs/catalog.json").read_text())["documents"]}
        for target_id in self.targets:
            entry = catalog[repositories.GUIDES[target_id]]
            self.assertEqual("hybrid", entry["mode"])
            self.assertEqual([guides.block_id(target_id)], entry["generated_block_ids"])
            self.assertIn("packaging/repository-site.json", entry["canonical_sources"])
        self.assertIn("install-table", catalog["README.md"]["generated_block_ids"])


if __name__ == "__main__":
    unittest.main()
