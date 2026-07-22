# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.readme import markdown as readme_markdown
from hfxdev.atlas import load_repository_atlas
from hfxdev.governance import load_github_governance
from hfxdev.licensing import (
    expression_for_path,
    load_licensing_policy,
    verify_licensing_policy,
)
from hfxdev.local_companion import load_local_companion
from hfxdev.model import ModelError, load_json
from hfxdev.public_readiness import public_readiness
from hfxdev.repository_docs import repository_link_issues


class RepositoryExperienceContracts(unittest.TestCase):
    def test_readme_is_the_generated_github_front_door(self) -> None:
        readiness = public_readiness(ROOT)
        self.assertEqual(
            load_json(ROOT / "generated" / "public-readiness.json"), readiness
        )
        readme = readme_markdown(load_github_governance(ROOT), readiness)
        self.assertEqual((ROOT / "README.md").read_text(encoding="utf-8"), readme)
        for section in ("publication", "evidence"):
            self.assertIn(readiness[section]["summary"], readme)
        self.assertIn(readiness["software"]["summary"], readme)
        self.assertIn("apps/device-qualification/README.md", readme)
        self.assertIn("Inspect an installed candidate", readme)
        self.assertIn("hardware-changing runners remain explicitly unavailable", readme)
        self.assertNotIn("github.io", readme)
        self.assertNotIn("Documentation portal", readme)

    def test_all_repository_markdown_links_and_anchors_resolve(self) -> None:
        issues = repository_link_issues(ROOT)
        self.assertEqual(issues, (), "\n".join(str(issue) for issue in issues))

    def test_repository_link_contract_rejects_missing_files_and_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "# Home\n\n[Good](guide.md#working)\n"
                "[Missing file](absent.md)\n[Missing anchor](guide.md#absent)\n",
                encoding="utf-8",
            )
            (root / "guide.md").write_text("# Working\n", encoding="utf-8")
            issues = repository_link_issues(root)
        self.assertEqual(
            {issue.reason for issue in issues},
            {"local target does not exist", "Markdown anchor does not exist"},
        )

    def test_folder_front_doors_are_concise_generated_projections(self) -> None:
        atlas = load_repository_atlas(ROOT)
        for node in atlas.nodes:
            path = ROOT / node.path / "README.md"
            with self.subTest(node=node.id):
                text = path.read_text(encoding="utf-8")
                self.assertLessEqual(len(text.splitlines()), 60)
                self.assertIn(node.purpose, text)
                self.assertTrue(
                    "## Start Here" in text or "## Browse By Need" in text
                )
                self.assertIn("## Scope", text)
                self.assertIn("## Verification", text)

    def test_visible_top_level_collections_have_generated_front_doors(self) -> None:
        expected = {
            ".devcontainer",
            "LICENSES",
            "apps",
            "generated",
            "sdk",
            "uapi",
        }
        for directory in expected:
            with self.subTest(directory=directory):
                readme = ROOT / directory / "README.md"
                self.assertTrue(readme.is_file())
                text = readme.read_text(encoding="utf-8")
                self.assertIn("structural collection", text)
                self.assertIn("## Safe Changes", text)
        self.assertFalse(
            (ROOT / ".github" / "README.md").exists(),
            ".github/README.md overrides the root README on GitHub's repository landing page",
        )

    def test_obsolete_pages_portal_is_absent(self) -> None:
        obsolete = (
            ".github/workflows/pages.yml",
            ".github/workflows/documentation.yml",
            ".github/workflows/repository-experience.yml",
            "docs/portal.json",
            "schemas/documentation-portal.schema.json",
            "tools/hfxdev/portal.py",
        )
        for relative in obsolete:
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())

    def test_local_companion_is_loopback_read_only_and_privacy_safe(self) -> None:
        contract = load_local_companion(ROOT)
        self.assertEqual(contract["base_url"], "http://127.0.0.1:47427")
        self.assertTrue(contract["snapshot"]["read_only"])
        self.assertEqual(
            contract["snapshot"]["evidence_states"],
            ["active", "sleeping", "disconnected", "unknown"],
        )
        self.assertEqual(contract["write_capabilities"]["default_state"], "disabled")
        self.assertLessEqual(contract["write_capabilities"]["maximum_ttl_seconds"], 300)
        self.assertTrue(all(value is False for value in contract["privacy"].values()))

    def test_local_companion_rejects_relaxed_write_or_privacy_boundaries(self) -> None:
        canonical = load_json(ROOT / "runtime" / "local-companion.json")
        mutations = (
            ("persistent writes", lambda value: value["write_capabilities"].update(maximum_ttl_seconds=301)),
            ("direct USB", lambda value: value["privacy"].update(direct_usb_access=True)),
            ("non-loopback", lambda value: value.update(base_url="http://0.0.0.0:47427")),
        )
        for label, mutate in mutations:
            with self.subTest(boundary=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "runtime").mkdir()
                value = copy.deepcopy(canonical)
                mutate(value)
                (root / "runtime" / "local-companion.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_local_companion(root)

    def test_license_policy_covers_spdx_sources_and_one_root_license(self) -> None:
        result = verify_licensing_policy(ROOT)
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["checked_spdx_files"], 100)
        self.assertEqual(result["unknown_license_files"], 0)
        self.assertEqual(
            sorted(path.name for path in ROOT.iterdir() if path.name.startswith("LICENSE")),
            ["LICENSE", "LICENSES"],
        )
        policy = load_licensing_policy(ROOT)
        self.assertEqual(
            expression_for_path(policy, "driver/kernel/uapi/hyperflux_next.h"),
            "GPL-2.0 WITH Linux-syscall-note",
        )
        self.assertEqual(expression_for_path(policy, "README.md"), "GPL-2.0-only")

    def test_ambiguous_root_license_document_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "governance").mkdir()
            shutil.copy2(ROOT / "governance" / "licensing.json", root / "governance")
            shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
            shutil.copytree(ROOT / "LICENSES", root / "LICENSES")
            (root / "LICENSE-DECISION.md").write_text("ambiguous\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "ambiguous root license"):
                verify_licensing_policy(root)


if __name__ == "__main__":
    unittest.main()
