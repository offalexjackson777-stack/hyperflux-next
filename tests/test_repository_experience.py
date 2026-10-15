# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.arch_repository import load_arch_repository_policy
from hfxdev.atlas import load_repository_atlas
from hfxdev.documentation import (
    DocumentCatalog,
    GENERATED_BLOCK_IDS,
    generated_markers,
    load_document_catalog,
    markdown_structure_issues,
    protected_prose,
    replace_generated_blocks,
)
from hfxdev.documentation_preview import (
    load_documentation_render_audit,
    load_documentation_render_policy,
    validate_documentation_render_audit,
)
from hfxdev.distributions import load_distribution_catalog
from hfxdev.licensing import (
    expression_for_path,
    load_licensing_policy,
    verify_licensing_policy,
)
from hfxdev.local_companion import load_local_companion
from hfxdev.model import ModelError, load_json, repository_files
from hfxdev.openrazer_native_route import load_native_deployment
from hfxdev.public_readiness import public_readiness
from hfxdev.render import rendered_files
from hfxdev.repository_docs import markdown_files, repository_link_issues


class RepositoryExperienceContracts(unittest.TestCase):
    def test_readme_is_a_human_owned_github_front_door(self) -> None:
        readiness = public_readiness(ROOT)
        self.assertEqual(
            load_json(ROOT / "generated" / "public-readiness.json"), readiness
        )
        deployment = load_native_deployment(ROOT)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        catalog = load_document_catalog(ROOT)
        record = catalog.by_path["README.md"]
        self.assertEqual(record.mode, "hybrid")
        self.assertEqual(record.generated_block_ids, GENERATED_BLOCK_IDS)
        self.assertEqual(generated_markers(readme), GENERATED_BLOCK_IDS)
        rendered = rendered_files(ROOT)[ROOT / "README.md"]
        self.assertEqual(protected_prose(rendered), protected_prose(readme))
        self.assertIn(readiness["publication"]["summary"], rendered)
        self.assertNotIn(readiness["software"]["summary"], readme)
        self.assertNotIn("release gates", readme)
        self.assertEqual(readiness["hardware"]["physically_proven"], 2)
        self.assertEqual(readiness["hardware"]["source_qualified_operational"], 8)
        self.assertEqual(
            readiness["hardware"]["upstream_blocked_or_research_only"], 2
        )
        self.assertIn(deployment.compatibility.verified_upstream_version, readme)
        self.assertNotIn("Choose A Path", readme)
        self.assertNotIn("```mermaid", readme)
        self.assertNotIn("## Repository Map", readme)
        self.assertNotIn("## Verify A Change", readme)
        headings = (
            "# HyperFlux Next",
            "## What It Does",
            "## Packages",
            "## Hardware And Applications",
            "## Start Here",
        )
        positions = [readme.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(readme.count("\n- **"), 3)
        self.assertLessEqual(len(re.findall(r"!\[[^]]+\]\([^)]*\)", readme)), 3)
        self.assertEqual(readme.count("> [!IMPORTANT]"), 1)
        distribution = load_distribution_catalog(ROOT).targets["arch"]
        product_pages = (
            readme,
            (ROOT / "docs/user/installation.md").read_text(),
            (ROOT / "docs/project/journey.md").read_text(),
        )
        for component in distribution.components.values():
            expected = f"{component.description}."
            for page in product_pages:
                self.assertIn(expected, " ".join(page.split()))
        self.assertIn("sudo pacman -Syu hyperflux-next-full", readme)
        self.assertIn("sudo pacman -Syu hyperflux-next-linux", readme)
        self.assertIn("third-party", readme)
        self.assertIn("not an official Arch Linux repository", readme)
        self.assertIn(
            load_arch_repository_policy(ROOT).product_release_url, readme
        )
        installation_guide = (ROOT / "docs/user/installation.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("sudo pacman -Syu hyperflux-next-full", installation_guide)
        self.assertIn("sudo pacman -Syu hyperflux-next-linux", installation_guide)
        self.assertIn("pacman -U", installation_guide)
        self.assertIn(
            "8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8",
            installation_guide,
        )
        self.assertNotIn("SigLevel = Never", installation_guide)
        self.assertNotIn("releases/latest/download", installation_guide)
        self.assertNotIn("./hyperflux-next.conf", installation_guide)
        self.assertIn("releases/download/arch-repository", installation_guide)
        self.assertIn("SHA256SUMS-packages", installation_guide)
        installation = (ROOT / "docs/generated/installation.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(deployment.driver.source_directory, installation)
        self.assertIn(deployment.relay.service_unit, installation)
        self.assertIn("openrazer-driver-dkms", installation)
        native_deployment = (
            ROOT / "docs/generated/openrazer-native-deployment.md"
        ).read_text(encoding="utf-8")
        self.assertIn(deployment.upstream_commit, native_deployment)
        self.assertNotIn("github.io", readme)
        self.assertNotIn("Documentation portal", readme)

    def test_document_catalog_covers_every_document_and_declares_real_owners(self) -> None:
        catalog = load_document_catalog(ROOT)
        owners = set(load_repository_atlas(ROOT).by_id)
        self.assertEqual(len(catalog.covered_paths), len(set(catalog.covered_paths)))
        self.assertEqual(
            {record.classification for record in catalog.classified_markdown},
            {
                "evidence-record",
                "generated-front-door",
                "repository-template",
                "vendored-component-note",
            },
        )
        for document in catalog.documents:
            with self.subTest(path=document.path):
                self.assertIn(document.owner, owners)
                self.assertTrue(document.purpose)
                self.assertTrue(document.canonical_sources)
                lines = (ROOT / document.path).read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines.count(f"# {document.title}"), 1)

    def test_generated_blocks_are_fail_closed_and_preserve_reviewed_prose(self) -> None:
        source = (
            "# Example\n\nBefore.\n"
            "<!-- HFX-GENERATED-BEGIN: release-status -->\nold\n"
            "<!-- HFX-GENERATED-END: release-status -->\nAfter.\n"
        )
        updated = replace_generated_blocks(
            source,
            ("release-status",),
            {"release-status": lambda: "new"},
        )
        self.assertEqual(protected_prose(source), protected_prose(updated))
        self.assertIn("\nnew\n", updated)
        invalid = (
            source.replace(
                "<!-- HFX-GENERATED-END: release-status -->",
                "<!-- HFX-GENERATED-END: compatibility-summary -->",
            ),
            source.replace("Before.\n", "Before.\n<!-- HFX-GENERATED-BEGIN: release-status -->\n"),
            source + "<!-- HFX-GENERATED-BEGIN: invented -->\n",
            source + source,
        )
        for mutation in invalid:
            with self.subTest(sha=hashlib.sha256(mutation.encode()).hexdigest()[:12]):
                with self.assertRaises(ModelError):
                    generated_markers(mutation)

    def test_human_document_experience_is_bounded_and_privacy_safe(self) -> None:
        catalog = load_document_catalog(ROOT)
        private_patterns = (
            re.compile(r"/(?:home|tmp)/shini(?:/|\b)"),
            re.compile(r"\b019[0-9a-f]{5}-[0-9a-f-]{20,}\b", re.IGNORECASE),
            re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
            re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
        )
        image = re.compile(r"!\[([^]]*)\]\([^)]*\)")
        for document in catalog.documents:
            if document.mode == "generated":
                continue
            text = (ROOT / document.path).read_text(encoding="utf-8")
            with self.subTest(path=document.path):
                self.assertLessEqual(text.count("> [!"), 2)
                self.assertLessEqual(text.count("| ---"), 4)
                for alt in image.findall(text):
                    self.assertTrue(alt.strip())
                for pattern in private_patterns:
                    self.assertIsNone(pattern.search(text))
        root = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("HyperFlux Next 2.0", root)
        self.assertIn("hyperflux-next-full", root)

    def test_document_render_policy_covers_every_reviewed_page_and_required_preview(self) -> None:
        catalog = load_document_catalog(ROOT)
        policy = load_documentation_render_policy(ROOT)
        self.assertEqual(policy.viewports, ((375, 812), (768, 1024), (1280, 900)))
        self.assertEqual(policy.themes, ("light", "dark"))
        self.assertEqual(
            policy.render_paths,
            tuple(
                document.path
                for document in catalog.documents
            ),
        )
        self.assertEqual(policy.check_count, len(policy.render_paths) * 6)
        self.assertEqual(policy.screenshot_count, 108)
        self.assertEqual(policy.capture_mode, "full-page")
        self.assertEqual(policy.max_page_height, 32768)
        self.assertIn("README.md", policy.screenshot_documents)
        self.assertIn("docs/project/journey.md", policy.screenshot_documents)
        self.assertIn("docs/generated/distributions.md", policy.screenshot_documents)
        self.assertIn("docs/generated/supported-hardware.md", policy.screenshot_documents)

        renderer = (ROOT / "tools" / "documentation_preview.cjs").read_text(
            encoding="utf-8"
        )
        for contract in (
            "page-horizontal-overflow",
            "element-outside-viewport",
            "clipped-text",
            "broken-image",
            "missing-image-alt",
            "table-column-bound",
            "blocked_requests",
            "max_screenshot_bytes",
            "github-compatible-gfm-v2",
            'fullPage: true',
        ):
            self.assertIn(contract, renderer)

    def test_document_render_policy_rejects_missing_theme_or_unreviewed_screenshot(self) -> None:
        canonical = load_json(ROOT / "docs" / "render-preview.json")
        mutations = (
            lambda value: value.update(themes=["light"]),
            lambda value: value["screenshot_documents"].__setitem__(
                0, "docs/generated/architecture.md"
            ),
            lambda value: value["limits"].update(max_screenshots=107),
            lambda value: value["capture"].update(mode="viewport"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "docs").mkdir()
                value = copy.deepcopy(canonical)
                mutate(value)
                (root / "docs" / "render-preview.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                with patch(
                    "hfxdev.documentation_preview.load_document_catalog",
                    return_value=load_document_catalog(ROOT),
                ), self.assertRaises(ModelError):
                    load_documentation_render_policy(root)

    def test_markdown_structure_lint_covers_public_and_generated_documents(self) -> None:
        self.assertEqual(markdown_structure_issues(ROOT), ())
        canonical = load_document_catalog(ROOT).by_path["README.md"]
        base = "# HyperFlux Next\n\n## Good\n\nText.\n"
        mutations = {
            "heading hierarchy skips a level": base + "\n#### Jump\n",
            "fenced block is missing a language": base + "\n```\nbody\n```\n",
            "fenced block is not closed": base + "\n```sh\necho ok\n",
            "raw HTML tag iframe is not allowed": base + "\n<iframe></iframe>\n",
            "raw HTML comment is not an approved generated marker": (
                base + "\n<!-- hidden note -->\n"
            ),
            "Markdown table exceeds eight columns": (
                base + "\n| a | b | c | d | e | f | g | h | i |\n"
            ),
            "public prose contains private process terminology": base + "\nPhase 8 notes.\n",
        }
        for reason, source in mutations.items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "README.md").write_text(source, encoding="utf-8")
                catalog = DocumentCatalog(
                    documents=(replace(canonical, canonical_sources=()),),
                    classified_markdown=(),
                )
                reasons = {issue.reason for issue in markdown_structure_issues(root, catalog)}
                self.assertIn(reason, reasons)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(base, encoding="utf-8")
            (root / "COPY.md").write_text(base, encoding="utf-8")
            duplicate = replace(canonical, path="COPY.md", canonical_sources=())
            catalog = DocumentCatalog(
                documents=(replace(canonical, canonical_sources=()), duplicate),
                classified_markdown=(),
            )
            reasons = {issue.reason for issue in markdown_structure_issues(root, catalog)}
            self.assertIn("document title duplicates README.md", reasons)

    def test_document_render_audit_binds_sources_visual_review_and_fast_gate(self) -> None:
        audit = load_documentation_render_audit(ROOT)
        policy = load_documentation_render_policy(ROOT)
        self.assertEqual(audit.source_revision, "d6f8081679669e5958bda5db485fed98b7e369a0")
        self.assertEqual(audit.check_count, policy.check_count)
        self.assertEqual(audit.screenshot_count, policy.screenshot_count)
        self.assertEqual(audit.capture_mode, "full-page")
        self.assertEqual(audit.fast_run_id, "hfxv-591106dafead36b83b77")
        self.assertEqual(audit.full_run_id, "hfxv-9d9fe8246a443a155051")
        self.assertEqual(
            audit.full_source_revision,
            "446c2c064444e98b0f89b5ef2ee7db5e8ce546a0",
        )

        canonical = load_json(ROOT / "assurance" / "documentation-audit.json")
        mutations = (
            lambda value: value["render"].update(issue_count=1),
            lambda value: value["verification"].update(
                release_gate_changes=[]
            ),
            lambda value: value["visual_review"]["reviewed_screenshots"].__setitem__(
                0,
                value["visual_review"]["reviewed_screenshots"][1],
            ),
            lambda value: value["visual_review"]["reviewed_screenshots"][0].update(
                sha256="0" * 64
            ),
        )
        for mutate in mutations:
            value = copy.deepcopy(canonical)
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ModelError):
                validate_documentation_render_audit(ROOT, value)
        with patch(
            "hfxdev.documentation_preview.rendered_documents_sha256",
            return_value="0" * 64,
        ), self.assertRaisesRegex(ModelError, "rendered_documents_sha256 is stale"):
            validate_documentation_render_audit(ROOT, canonical)

    def test_all_repository_markdown_links_and_anchors_resolve(self) -> None:
        issues = repository_link_issues(ROOT)
        self.assertEqual(issues, (), "\n".join(str(issue) for issue in issues))

    def test_public_pages_do_not_contradict_the_published_release(self) -> None:
        # A release is published; no public page may claim otherwise (DOC-001).
        stale = (
            "is unreleased",
            "no supported product version",
            "no supported installation",
            "public pre-release source",
            "have not been published yet",
        )
        pages = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").rglob("*.md"))
        found = [
            f"{page.relative_to(ROOT)}: {phrase!r}"
            for page in pages
            for phrase in stale
            if phrase in page.read_text(encoding="utf-8").lower()
        ]
        self.assertEqual(found, [], "\n".join(found))

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
            if f"{node.path}/README.md" not in node.generated_files:
                continue
            path = ROOT / node.path / "README.md"
            with self.subTest(node=node.id):
                text = path.read_text(encoding="utf-8")
                self.assertLessEqual(len(text.splitlines()), 64)
                self.assertIn(node.purpose, text)
                self.assertTrue(
                    "## Start Here" in text or "## Browse By Need" in text
                )
                self.assertIn("## Boundaries", text)
                self.assertIn("## Verify", text)

    def test_visible_top_level_collections_have_generated_front_doors(self) -> None:
        expected = {
            ".devcontainer",
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
                self.assertNotIn("structural collection", text)
                self.assertIn("## Change Workflow", text)
        self.assertFalse(
            (ROOT / ".github" / "README.md").exists(),
            ".github/README.md overrides the root README on GitHub's repository landing page",
        )
        self.assertFalse(
            (ROOT / "LICENSES" / "README.md").exists(),
            "a second license-like README confuses GitHub license detection",
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

    def test_license_policy_prunes_generated_roots_before_descent(self) -> None:
        source = ROOT / "tools" / "hfxdev" / "licensing.py"

        def guarded_walk(root: Path, *, topdown: bool, followlinks: bool):
            directories = ["build", "target", "tools"]
            yield str(root), directories, []
            generated = {"build", "target"}.intersection(directories)
            if generated:
                raise AssertionError(f"generated roots were traversed: {generated}")
            yield str(source.parent), [], [source.name]

        with (
            patch("hfxdev.model._git_repository_files", return_value=None),
            patch("hfxdev.model.os.walk", guarded_walk),
        ):
            result = verify_licensing_policy(ROOT)

        self.assertEqual(result["checked_spdx_files"], 1)

    @staticmethod
    def _license_fixture(root: Path) -> None:
        (root / "governance").mkdir()
        shutil.copy2(ROOT / "governance" / "licensing.json", root / "governance")
        shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
        shutil.copytree(ROOT / "LICENSES", root / "LICENSES")

    @staticmethod
    def _nested_worktree(root: Path) -> Path:
        # A mismatching vendored header inside a nested checkout copy, as a
        # Claude Code worktree at .claude/worktrees/<name>/ would carry it.
        nested = root / ".claude" / "worktrees" / "stray"
        header = nested / "sdk" / "cpp" / "vendor" / "include" / "nlohmann" / "json.hpp"
        header.parent.mkdir(parents=True)
        header.write_text("// SPDX-License-Identifier: MIT\n", encoding="utf-8")
        (nested / "README.md").write_text("# Stray\n", encoding="utf-8")
        return header

    @unittest.skipUnless(shutil.which("git"), "git is required for the git-backed scan")
    def test_license_scan_skips_nested_directories_git_ignores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._license_fixture(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (root / ".git" / "info" / "exclude").write_text(
                ".claude/worktrees/\n", encoding="utf-8"
            )
            header = self._nested_worktree(root)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")

            verify_licensing_policy(root)
            self.assertNotIn(header, repository_files(root))
            self.assertEqual(markdown_files(root), (root.resolve() / "README.md",))

            # Untracked files git does not ignore still count.
            source = root / "tools" / "real.py"
            source.parent.mkdir()
            source.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "tools/real.py"):
                verify_licensing_policy(root)

    def test_license_scan_fallback_skips_nested_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._license_fixture(root)
            # A linked worktree mounted without its common git directory, as in
            # the verification container: git refuses, so the walk stands in.
            (root / ".git").write_text("gitdir: /nonexistent/worktrees/root\n", encoding="utf-8")
            header = self._nested_worktree(root)
            (header.parents[4] / ".git").write_text(
                "gitdir: /nonexistent/worktrees/stray\n", encoding="utf-8"
            )

            verify_licensing_policy(root)
            self.assertNotIn(header, repository_files(root))

            source = root / "tools" / "real.py"
            source.parent.mkdir()
            source.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            self.assertIn(source, repository_files(root))
            with self.assertRaisesRegex(ModelError, "tools/real.py"):
                verify_licensing_policy(root)

    def test_license_scan_ignores_untracked_build_and_environment_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "governance").mkdir()
            shutil.copy2(ROOT / "governance" / "licensing.json", root / "governance")
            shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
            shutil.copytree(ROOT / "LICENSES", root / "LICENSES")
            for stray in (
                ".venv/lib/site.py",
                "integrations/x/build/lib/module.py",
                "tools/__pycache__/cached.py",
            ):
                path = root / stray
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            verify_licensing_policy(root)

            source = root / "tools" / "real.py"
            source.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "license policy mismatch"):
                verify_licensing_policy(root)

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
