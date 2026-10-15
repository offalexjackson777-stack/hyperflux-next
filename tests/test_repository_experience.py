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
    REVIEW_PAGES,
    generated_markers,
    load_document_catalog,
    load_documentation_review,
    long_line_issues,
    markdown_structure_issues,
    protected_prose,
    replace_generated_blocks,
    stale_claim_issues,
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
        readme_blocks = (
            "release-summary",
            "compatibility-summary",
            "recommended-package",
        )
        self.assertEqual(record.generated_block_ids, readme_blocks)
        self.assertEqual(generated_markers(readme), readme_blocks)
        rendered_views = rendered_files(ROOT)
        rendered = rendered_views[ROOT / "README.md"]
        self.assertEqual(protected_prose(rendered), protected_prose(readme))
        # DOC-003: landing pages carry one short sentence. Release Status names
        # the current release and its notes in plain words, without the
        # repository's internal channel wording.
        repository = load_arch_repository_policy(ROOT)
        release_status = rendered_views[ROOT / "docs/project/release-status.md"]
        flat_status = " ".join(release_status.replace("\n> ", "\n").split())
        version = repository.product_release_tag.removeprefix("v")
        self.assertIn(f"HyperFlux Next {version} is the current release.", flat_status)
        self.assertIn(repository.product_release_url, flat_status)
        self.assertNotIn(readiness["publication"]["summary"], " ".join(rendered.split()))
        self.assertNotIn("machine channel", rendered)
        self.assertNotIn("machine channel", release_status)
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
        # The front door answers three questions in order: does my device
        # work, how do I install it, and where do I go next.
        headings = (
            "# HyperFlux Next",
            "## Does My Device Work?",
            "## Install",
            "## Next Steps",
        )
        positions = [readme.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(
            "Your Razer keyboard and mouse on a HyperFlux receiver, working in "
            "OpenRazer, Polychromatic, and OpenRGB on Linux.",
            " ".join(readme.split()),
        )
        source_validated = load_json(
            ROOT / "integrations/native-route/source-validated.json"
        )
        compatibility = readme[
            readme.index("## Does My Device Work?") : readme.index("## Install")
        ]
        for device in source_validated["devices"]:
            self.assertIn(device["model_name"], " ".join(compatibility.split()))
        self.assertIn("**Tested on real hardware.**", compatibility)
        self.assertIn(
            "**Expected to work.** Nobody has tested these 8 devices",
            " ".join(compatibility.split()),
        )
        # Four numbered install steps, the first linking to the one-time setup.
        install = readme[readme.index("## Install") : readme.index("## Next Steps")]
        for step in ("\n1. **", "\n2. **", "\n3. **", "\n4. **"):
            self.assertEqual(install.count(step), 1, step)
        self.assertNotIn("\n5. ", install)
        # Product jargon belongs on the engineering pages, not the front door.
        for jargon in (
            "capability-based routing",
            "Fails closed",
            "fail closed",
            "boundary",
            "source-qualified",
            "fixture",
            "allowlist",
            "restoration ships disabled",
        ):
            self.assertNotIn(jargon, readme)
        self.assertLessEqual(len(re.findall(r"!\[[^]]+\]\([^)]*\)", readme)), 3)
        # GH-004: the status badge covers the complete graph, not the Fast lane.
        self.assertIn("actions/workflows/full-verification.yml/badge.svg", readme)
        self.assertNotIn("actions/workflows/verification.yml/badge.svg", readme)
        self.assertEqual(readme.count("> [!IMPORTANT]"), 0)
        # DOC-002: the README shows the first-run sequence, not the key setup.
        for command in (
            "sudo systemctl enable hyperflux-next-bridge.service",
            "sudo reboot",
            "hyperfluxctl doctor",
            "HyperFlux Next is ready.",
        ):
            self.assertIn(command, readme)
        self.assertNotIn("pacman-key", readme)
        self.assertIn(
            "docs/user/installation.md#1-add-the-hyperflux-repository-one-time-only",
            readme,
        )
        # The exact package descriptions stay on the installation guide and
        # the journey; the README describes Full and Core in plain words.
        distribution = load_distribution_catalog(ROOT).targets["arch"]
        product_pages = (
            (ROOT / "docs/user/installation.md").read_text(),
            (ROOT / "docs/project/journey.md").read_text(),
        )
        for component in distribution.components.values():
            expected = f"{component.description}."
            for page in product_pages:
                self.assertIn(expected, " ".join(page.split()))
        self.assertIn("sudo pacman -Syu hyperflux-next-full", readme)
        self.assertIn("sudo pacman -Syu hyperflux-next-linux", readme)
        self.assertIn("signed project-operated Arch repository", readme)
        self.assertIn("not an official Arch Linux repository", readme)
        self.assertIn(
            load_arch_repository_policy(ROOT).product_release_url, readme
        )
        installation_guide = (ROOT / "docs/user/installation.md").read_text(
            encoding="utf-8"
        )
        installation_record = catalog.by_path["docs/user/installation.md"]
        self.assertEqual(installation_record.mode, "hybrid")
        self.assertEqual(
            installation_record.generated_block_ids,
            ("repository-setup", "package-fallback"),
        )
        self.assertEqual(
            protected_prose(rendered_views[ROOT / "docs/user/installation.md"]),
            protected_prose(installation_guide),
        )
        # The manual route uses the same key, fingerprint, and Pacman lines as
        # the checked setup command.
        for manual in (
            "gpg --show-keys --with-subkey-fingerprints",
            f"sudo pacman-key --add {repository.public_key_asset}",
            f"sudo pacman-key --lsign-key {repository.primary_fingerprint}",
            repository.signing_subkey_fingerprint,
            f"SigLevel = {repository.signature_level}",
            f"Server = {repository.server_url}",
        ):
            self.assertIn(manual, installation_guide)
        self.assertLess(
            installation_guide.index("<summary>Show the setup command</summary>"),
            installation_guide.index("bash <<'HFX_REPOSITORY_SETUP'"),
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

    def test_every_tracked_markdown_file_is_cataloged_or_classified(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        tracked_paths = sorted(path for path in tracked.split("\0") if path)
        catalog = load_document_catalog(ROOT)
        self.assertEqual(sorted(catalog.covered_paths), tracked_paths)

    def test_private_process_wording_fails_the_markdown_lint(self) -> None:
        canonical = load_document_catalog(ROOT).by_path["README.md"]
        base = "# HyperFlux Next\n\n## Good\n\nText.\n"
        for sentence in (
            "Packages ship only when separately authorized.",
            "Writes need explicit authorization first.",
            "Retiring a gate needs separate approval.",
            "This is a software-only mission.",
        ):
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "README.md").write_text(base + "\n" + sentence + "\n", encoding="utf-8")
                catalog = DocumentCatalog(
                    documents=(replace(canonical, canonical_sources=()),),
                    classified_markdown=(),
                )
                reasons = {issue.reason for issue in markdown_structure_issues(root, catalog)}
                self.assertIn("public prose contains private process terminology", reasons)

    def test_public_markdown_repeats_no_claim_the_code_contradicts(self) -> None:
        # The facts each stale-claim pattern relies on must still hold.
        verify = subprocess.run(
            [str(ROOT / "hfx"), "verify", "--changed-from", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verify.returncode, 2)
        self.assertIn("--fast --full --all is required", verify.stderr)
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for line in workflow.read_text(encoding="utf-8").splitlines():
                if "cron:" in line:
                    fields = line.split("cron:", 1)[1].strip().strip("'\"").split()
                    self.assertFalse(
                        fields[2] == "*" and fields[4] == "*",
                        f"{workflow.name} runs daily",
                    )
        packaging = "\n".join(
            (ROOT / "packaging" / name).read_text(encoding="utf-8")
            for name in ("install.json", "distributions.json")
        )
        self.assertNotIn("device-qualification", packaging)
        self.assertTrue((ROOT / "assurance" / "generated" / "hyperflux-next.spdx.json").is_file())
        self.assertFalse(
            [
                path
                for path in repository_files(ROOT)
                if "cyclonedx" in path.name.casefold() or ".cdx." in path.name.casefold()
            ]
        )

        self.assertEqual(stale_claim_issues(ROOT), ())

        canonical = load_document_catalog(ROOT).by_path["README.md"]
        base = "# HyperFlux Next\n\nText.\n"
        for sentence in (
            "Run `./hfx verify --changed-from main` while iterating.",
            "Nightly builds publish test packages.",
            "Historical benchmarks show the trend.",
            "Open the installed qualification console.",
            "Each release ships a CycloneDX SBOM.",
        ):
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "README.md").write_text(base + sentence + "\n", encoding="utf-8")
                catalog = DocumentCatalog(documents=(canonical,), classified_markdown=())
                self.assertEqual(len(stale_claim_issues(root, catalog)), 1)

    def test_hand_written_prose_lines_stay_readable_in_source(self) -> None:
        counts: dict[str, int] = {}
        for issue in long_line_issues(ROOT):
            counts[issue.path] = counts.get(issue.path, 0) + 1
        for path, count in counts.items():
            self.assertEqual(count, 0, path)

        canonical = load_document_catalog(ROOT).by_path["README.md"]
        long_words = " ".join(["word"] * 30)
        allowed = "\n".join(
            [
                "# HyperFlux Next",
                "",
                "```sh",
                long_words,
                "```",
                "",
                f"| {long_words} |",
                "",
                f"- [{long_words}](https://example.invalid/{'a' * 40}).",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = DocumentCatalog(documents=(canonical,), classified_markdown=())
            (root / "README.md").write_text(allowed, encoding="utf-8")
            self.assertEqual(long_line_issues(root, catalog), ())
            (root / "README.md").write_text(allowed + long_words + "\n", encoding="utf-8")
            self.assertEqual(
                [issue.reason for issue in long_line_issues(root, catalog)],
                ["prose line exceeds 100 characters"],
            )

    def test_critical_pages_have_a_manual_review_entry(self) -> None:
        review = load_documentation_review(ROOT)
        self.assertEqual(tuple(page.path for page in review.pages), REVIEW_PAGES)
        self.assertTrue(
            any("private" in check for check in review.common_checks),
            "the review must check for private process wording",
        )
        source = load_json(ROOT / "assurance" / "documentation-review.json")
        atlas = load_repository_atlas(ROOT)
        owned = {path for node in atlas.nodes for path in node.canonical_files}
        self.assertIn("assurance/documentation-review.json", owned)

        def drop_page(value: dict) -> None:
            value["pages"].pop()

        def missing_authority(value: dict) -> None:
            value["pages"][0]["authorities"] = ["missing/authority.json"]

        def generated_page(value: dict) -> None:
            value["pages"][0]["path"] = "docs/generated/verification.md"

        def no_checks(value: dict) -> None:
            value["pages"][0]["checks"] = []

        for mutate in (drop_page, missing_authority, generated_page, no_checks):
            with self.subTest(mutation=mutate.__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for page in source["pages"]:
                    for authority in page["authorities"]:
                        (root / authority).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(ROOT / authority, root / authority)
                value = copy.deepcopy(source)
                (root / "assurance" / "documentation-review.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                load_documentation_review(root, load_document_catalog(ROOT))
                mutate(value)
                (root / "assurance" / "documentation-review.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_documentation_review(root, load_document_catalog(ROOT))

    def test_document_render_audit_binds_sources_visual_review_and_fast_gate(self) -> None:
        audit = load_documentation_render_audit(ROOT)
        policy = load_documentation_render_policy(ROOT)
        self.assertEqual(audit.source_revision, "aca3bbc796080a465403ed2050361e3ff76cbb05")
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

    def test_design_book_is_a_short_index_of_readable_chapters(self) -> None:
        # The Design Book is an index plus one page per chapter (ARCH-001).
        index = (ROOT / "docs/architecture/design-book.md").read_text(encoding="utf-8")
        chapters = sorted((ROOT / "docs/architecture/design-book").glob("*.md"))
        self.assertEqual(len(chapters), 12)
        self.assertLessEqual(len(index.splitlines()), 120)
        self.assertNotRegex(index, r"(?m)^#{2,3} \d+\. ")
        self.assertEqual(len(re.findall(r"(?m)^## Chapter [IVX]+: ", index)), 12)
        subjects: list[int] = []
        diagrams = 0
        for chapter in chapters:
            text = chapter.read_text(encoding="utf-8")
            relative = chapter.relative_to(ROOT / "docs/architecture").as_posix()
            with self.subTest(chapter=chapter.name):
                self.assertIn(f"]({relative}#", index)
                self.assertIn("](../design-book.md)", text)
                self.assertNotIn("\n\n\n", text)
                for number, line in enumerate(text.splitlines(), 1):
                    if line.startswith("#"):
                        self.assertEqual(text.splitlines()[number - 2 : number - 1] or [""], [""])
                    if len(line) > 100:
                        self.assertRegex(line, r"^\s*(- |Next: |Previous: )?\[[^\]]+\]\([^)]+\)[.,;:]?$")
            subjects.extend(int(value) for value in re.findall(r"(?m)^## (\d+)\. ", text))
            diagrams += text.count("```mermaid")
        self.assertEqual(subjects, list(range(1, 68)))
        self.assertGreaterEqual(diagrams, 3)
        # The contributor change order lives in the verification guide.
        guide = (ROOT / "docs/developer/verification.md").read_text(encoding="utf-8")
        self.assertIn("## Safe Change Order\n", guide)
        self.assertIn("```mermaid", guide)
        self.assertIn("verification.md#safe-change-order", chapters[-1].read_text(encoding="utf-8"))

    def test_architecture_and_assurance_prose_lines_stay_readable_in_source(self) -> None:
        # Architecture and assurance prose wraps instead of running past 100
        # columns; only fenced code, tables, and link-only lines may (ARCH-002).
        link_only = re.compile(r"^\s*(- |\d+\. |Next: |Previous: )?\[[^\]]+\]\([^)]+\)[.,;:]?$")
        pages = sorted(
            [
                *(ROOT / "docs/architecture").rglob("*.md"),
                *(ROOT / "docs/assurance").rglob("*.md"),
            ]
        )
        self.assertGreaterEqual(len(pages), 20)
        for page in pages:
            relative = page.relative_to(ROOT).as_posix()
            fenced = False
            for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                if fenced or line.lstrip().startswith("|") or len(line) <= 100:
                    continue
                with self.subTest(page=relative, line=number):
                    self.assertRegex(line, link_only)

    def test_architecture_prose_states_technical_entry_criteria_not_task_control(self) -> None:
        # Public architecture describes design and technical entry criteria,
        # not private approval steps, while protocol authorization terms stay
        # (ARCH-003).
        private_voice = re.compile(
            r"separately authori[sz]ed|explicit(?:ly)? authori[sz]|separate(?: retirement)? approval"
            r"|\bmission\b|publication authori[sz]ation|publication decision|future authorized release"
            r"|nightly builds|release authorization|does not authorize a product release",
            re.IGNORECASE,
        )
        sources = sorted(
            [
                *(ROOT / "docs/architecture").rglob("*.md"),
                *(ROOT / "docs/assurance").rglob("*.md"),
                ROOT / "docs/developer/verification.md",
                ROOT / "docs/generated/design-coverage.md",
                ROOT / "docs/generated/supply-chain.md",
                ROOT / "architecture/repository-atlas.json",
            ]
        )
        for source in sources:
            text = " ".join(source.read_text(encoding="utf-8").split())
            with self.subTest(source=source.relative_to(ROOT).as_posix()):
                self.assertEqual(private_voice.findall(text), [])
        bridge = " ".join((ROOT / "docs/architecture/bridge-core.md").read_text(encoding="utf-8").split())
        self.assertIn("authorization epoch", bridge)
        self.assertIn("a token does not replace local socket authorization", bridge)
        release = " ".join(
            (ROOT / "docs/architecture/design-book/12-release-boundaries.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("candidate-bound physical evidence", release)
        self.assertIn("packages signed into the project-operated Arch repository", release)

    def test_documentation_decision_is_a_concise_public_adr(self) -> None:
        # The documentation ADR states context, decision, consequences, and
        # scope without delivery-process justification (ARCH-004).
        decisions = ROOT / "docs/architecture/decisions"
        self.assertFalse((decisions / "github-native-documentation.md").exists())
        path = decisions / "repository-documentation-and-local-hardware-authority.md"
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Repository Documentation And Local Hardware Authority\n"))
        headings = re.findall(r"(?m)^## (.+)$", text)
        self.assertEqual(headings[:4], ["Context", "Decision", "Consequences", "Scope"])
        self.assertLessEqual(len(text.splitlines()), 60)
        flat = " ".join(text.split())
        for phrase in ("authorize a release", "package-matched", "installed loopback console", "Pages portal"):
            self.assertNotIn(phrase, flat)
        self.assertIn("source-only, loopback-only console", flat)
        catalog = load_json(ROOT / "docs/catalog.json")
        entry = next(item for item in catalog["documents"] if item["path"] == path.relative_to(ROOT).as_posix())
        self.assertEqual(entry["title"], "Repository Documentation And Local Hardware Authority")

    def test_performance_claims_point_to_checks_or_retained_measurements(self) -> None:
        # Software budgets name their executable checks, physical budgets say
        # nothing is retained, and no trend analysis is claimed (ARCH-007).
        from hfxdev.performance import load_performance_budgets

        metrics = load_performance_budgets(ROOT)
        software = [metric for metric in metrics if metric.status == "enforced-software"]
        physical = [metric for metric in metrics if metric.status != "enforced-software"]
        self.assertEqual({metric.status for metric in physical}, {"unmeasured-physical"})
        self.assertFalse([metric.id for metric in physical if metric.release_blocking])
        authority = load_json(ROOT / "assurance/performance-budgets.json")
        blocking = next(
            item for item in authority["metrics"] if item["measurement_kind"] == "physical"
        )
        blocking["release_blocking"] = True
        with (
            patch("hfxdev.performance.load_json", return_value=authority),
            self.assertRaisesRegex(ModelError, "without a retained measurement"),
        ):
            load_performance_budgets(ROOT)
        words = {10: "Ten", 5: "Five"}
        chapter = (ROOT / "docs/architecture/design-book/10-updates-packaging-and-operations.md").read_text(
            encoding="utf-8"
        )
        section = " ".join(chapter.split("## 57. ", 1)[1].split("\n---\n", 1)[0].split())
        self.assertIn(f"{words[len(software)]} software budgets are checked by executable contracts.", section)
        self.assertIn(f"{words[len(physical)]} physical budgets", section)
        self.assertIn("`assurance-contracts`", section)
        self.assertIn("`package-contracts`", section)
        self.assertIn("No measurement is retained in the repository yet", section)
        self.assertIn("stores no benchmark history", section)
        for overclaim in ("Rainbow Wave", "Historical benchmark trends", "distinguish noise"):
            self.assertNotIn(overclaim, section)
        repository = (ROOT / "tools/hfxdev/verify_repository.py").read_text(encoding="utf-8")
        packaging = (ROOT / "tools/hfxdev/verify_packaging.py").read_text(encoding="utf-8")
        self.assertIn("verify_static_performance_budgets(root, metrics)", repository)
        self.assertIn("verify_package_performance_budgets(", packaging.split("def _run_package_contracts", 1)[1])
        page = (ROOT / "docs/generated/performance-budgets.md").read_text(encoding="utf-8")
        self.assertIn("No measurement history is stored", page)

    def test_design_book_claims_match_verification_and_application_boundaries(self) -> None:
        # The runner rejects hardware nodes and reruns every selected node, and
        # effects, persistence, and settings stay upstream (AUTH-008, AUTH-009).
        book = " ".join(
            " ".join(path.read_text(encoding="utf-8").split())
            for path in sorted((ROOT / "docs/architecture/design-book").glob("*.md"))
        )
        for overclaim in (
            "official effect engine",
            "exactly as they are for direct devices",
            "future model behavior",
            "hardware stages",
            "reuses its verified artifact",
            "invalidated its cache",
            "hardware generations where applicable",
            "test planner compares",
        ):
            with self.subTest(overclaim=overclaim):
                self.assertNotIn(overclaim, book)
        for statement in (
            "one software-verification entry point",
            "refuses any node that needs hardware",
            "does not reuse earlier results",
            "not an artifact cache",
            "optional OpenRGB Effects Plugin",
            "HyperFlux does not bundle, recreate, or claim those effects",
            "only where the upstream application supports it for that exact model",
            "user/limitations.md",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, book)
        # Supply-chain and dependency subjects match the repository (ARCH-001).
        for overclaim in (
            "CycloneDX",
            "Packages produce SPDX",
            "Future releases can use signed tags",
            "Renovate",
            "Each update reports API compatibility",
        ):
            with self.subTest(overclaim=overclaim):
                self.assertNotIn(overclaim, book)
        self.assertIn("Arch packages and the package repository database are signed", book)
        self.assertIn("Tags and commits are not signed", book)
        runner = (ROOT / "tools/hfxdev/verification_run.py").read_text(encoding="utf-8")
        # Reusing results by resume policy would make the "does not reuse" claim stale.
        self.assertNotIn("resume_policy", runner)
        self.assertIn("software verification selected hardware-authorized nodes", runner)

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

    def test_ripple_is_documented_as_offered_but_not_reactive(self) -> None:
        # Stock OpenRazer advertises Ripple per keyboard model, so pages must
        # not claim HyperFlux hides it (REL-007).
        pages = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").rglob("*.md"))
        found = [
            str(page.relative_to(ROOT))
            for page in pages
            if "not advertised over hyperflux"
            in " ".join(page.read_text(encoding="utf-8").lower().split())
        ]
        self.assertEqual(found, [])
        for path in ("docs/user/applications.md", "docs/user/limitations.md"):
            page = " ".join((ROOT / path).read_text(encoding="utf-8").split())
            self.assertIn("Polychromatic offers Ripple", page, path)
            self.assertIn("no reactive lighting", page, path)

    def test_upstream_application_limitations_are_documented(self) -> None:
        # Firmware shortcuts, the OpenRGB variant label, and the Polychromatic
        # receiver entry come from stock behaviour (REL-006, REL-008, REL-018).
        limitations = " ".join(
            (ROOT / "docs/user/limitations.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("HyperFlux passes that reply through unchanged", limitations)
        self.assertIn("lighting inside the keyboard firmware", limitations)
        self.assertIn("unrecognised `1532:00CF` device", limitations)
        self.assertIn("never makes up a value", limitations)

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

    def test_folder_front_doors_lead_with_guides_and_leave_the_graph_to_the_atlas(self) -> None:
        # Folder READMEs open with purpose and useful pages; dependency graphs
        # stay in the maintainer Repository Atlas (ARCH-005, ARCH-006).
        atlas = load_repository_atlas(ROOT)
        for node in atlas.nodes:
            if f"{node.path}/README.md" not in node.generated_files:
                continue
            text = (ROOT / node.path / "README.md").read_text(encoding="utf-8")
            with self.subTest(node=node.id):
                headings = re.findall(r"(?m)^## (.+)$", text)
                self.assertIn(headings[0], {"Start Here", "Browse By Need"})
                self.assertNotIn("Used by", text)
                self.assertNotIn("Review direct consumers", text)
                self.assertLessEqual(max(len(line) for line in text.splitlines()), 300)
                self.assertIn("docs/generated/repository-atlas.md)", text)
        architecture = (ROOT / "architecture/README.md").read_text(encoding="utf-8")
        start = architecture.split("## Start Here", 1)[1].split("\n## ", 1)[0]
        for target in ("developer/architecture.md", "architecture/design-book.md", "developer/verification.md"):
            self.assertIn(target, start)
        self.assertIn("## Decision Records", architecture)
        self.assertIn("## Limitations", architecture)
        self.assertNotRegex(start, r"\[`docs/")
        assurance = (ROOT / "assurance/README.md").read_text(encoding="utf-8")
        start = assurance.split("## Start Here", 1)[1].split("\n## ", 1)[0]
        for target in ("developer/verification.md", "generated/design-coverage.md", "assurance/simulator.md"):
            self.assertIn(target, start)
        self.assertIn("## Limitations", assurance)
        page = (ROOT / "docs/generated/repository-atlas.md").read_text(encoding="utf-8")
        self.assertIn("Maintainer reference for repository ownership.", page)
        self.assertIn("## Dependencies", page)
        self.assertIn("| Subsystem | Depends on | Used by |", page)
        catalog = load_json(ROOT / "docs/catalog.json")
        entry = next(item for item in catalog["documents"] if item["path"] == "docs/generated/repository-atlas.md")
        self.assertEqual(entry["audience"], "maintainers")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        workflow = contributing.split("## Change Workflow", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("repository-atlas.md", workflow)
        self.assertIn("./hfx verify --fast --changed-from", workflow)

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
            ("installed claim", lambda value: value.update(delivery_state="installed")),
            ("missing delivery", lambda value: value.pop("delivery_state")),
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

    def test_qualification_console_and_companion_stay_out_of_packages(self) -> None:
        contract = load_local_companion(ROOT)
        self.assertEqual(contract["delivery_state"], "source-only")
        manifest = load_json(ROOT / "packaging" / "install.json")
        sources = [payload["source"] for payload in manifest["payloads"]]
        destinations = [
            entry["destination"] for entry in manifest["builds"] + manifest["payloads"]
        ]
        targets = [build.get("target") for build in manifest["builds"]]
        packages = [build.get("package") for build in manifest["builds"]]
        for source in sources:
            self.assertFalse(source.startswith("apps/device-qualification"), source)
        for destination in destinations:
            self.assertNotIn("qualification", destination)
        self.assertNotIn("hyperflux-next-device-qualification", targets)
        self.assertNotIn("hfx-device-qualification", packages)
        atlas = load_json(ROOT / "architecture" / "repository-atlas.json")
        packaging = next(node for node in atlas["nodes"] if node["id"] == "packaging")
        self.assertNotIn("device-qualification-console", packaging["depends_on"])

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


def _page(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class UserDocumentationContracts(unittest.TestCase):
    """Keep the guides ordinary users follow truthful and actionable (#45)."""

    def test_security_policy_names_the_published_release(self) -> None:
        policy = load_arch_repository_policy(ROOT)
        version = policy.product_release_tag.removeprefix("v")
        self.assertIn(
            f"Latest 2.0.x release (currently {version})", _page("SECURITY.md")
        )

    def test_support_sends_users_to_the_installation_guide(self) -> None:
        support = _page("SUPPORT.md")
        self.assertIn("(docs/user/installation.md)", support)
        self.assertIsNone(re.search(r"\bunreleased\b", support, re.IGNORECASE))


    def test_installation_asks_for_one_edition_and_finishes_with_doctor(self) -> None:
        installation = _page("docs/user/installation.md")
        self.assertIn("Choose one.", installation)
        self.assertNotIn("Install Core and Full together", installation)
        self.assertEqual(installation.count("sudo pacman -U"), 1)
        fallback = installation.index("## If You Cannot Add The Repository")
        self.assertGreater(installation.index("sudo pacman -U"), fallback)
        for command in (
            "sudo systemctl enable hyperflux-next-bridge.service",
            "sudo reboot",
            "hyperfluxctl doctor",
        ):
            self.assertLess(installation.index(command), fallback, command)

    def test_first_run_follows_the_install_order_in_plain_words(self) -> None:
        first_run = _page("docs/user/first-run.md")
        order = [
            "installation guide",
            "sudo systemctl enable",
            "sudo reboot",
            "hyperfluxctl status",
            "hyperfluxctl doctor",
            "Open OpenRGB",
            "Open Polychromatic",
        ]
        positions = [first_run.index(step) for step in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("HyperFlux Next is ready.", first_run)
        for word in ("lease", "authority", "reviewed local artifact", "boundary"):
            self.assertNotIn(word, first_run)

    def test_troubleshooting_gives_user_actions_for_a_missing_device(self) -> None:
        troubleshooting = _page("docs/user/troubleshooting.md")
        section = troubleshooting.split("## A Device Is Missing From An Application", 1)[1]
        section = section.split("\n## ", 1)[0]
        for action in ("Wake the device", "Close any other", "Reopen", "hyperfluxctl doctor"):
            self.assertIn(action, section)
        self.assertIn("hyperfluxctl support-bundle --preview", section)
        for word in ("non-writable", "lease", "unqualified", "boundary"):
            self.assertNotIn(word, troubleshooting)
        self.assertIn("> [!WARNING]", troubleshooting)

    def test_known_limitations_page_is_linked_from_user_pages(self) -> None:
        limitations = _page("docs/user/limitations.md")
        for topic in ("Ripple", "Quartz", "Fn", "1532:00CF", "F5", "brightness", "Arch Linux"):
            self.assertIn(topic, limitations)
        for path in (
            "docs/user/applications.md",
            "docs/user/troubleshooting.md",
        ):
            self.assertIn("(limitations.md)", _page(path), path)
        self.assertIn("(../user/limitations.md)", _page("docs/project/journey.md"))

    def test_hardware_page_names_the_tested_devices_from_the_generated_reference(self) -> None:
        hardware = _page("docs/user/hardware.md")
        reference = _page("docs/generated/supported-hardware.md")
        proven = reference.split("## Tested On Real Hardware", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in proven.splitlines() if line.startswith("| Razer")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            self.assertIn(f"**{cells[0]}**", hardware)
            for identity in re.findall(r"`(1532:[0-9a-f]{4})`", cells[2]):
                self.assertIn(identity, hardware)
        self.assertIn("not the only devices", hardware)
        self.assertIn("(limitations.md)", hardware)
        for word in ("authority", "non-writable", "unqualified", "route"):
            self.assertNotIn(word, hardware)

    def test_applications_page_says_who_owns_the_stock_applications(self) -> None:
        applications = _page("docs/user/applications.md")
        self.assertIn("does not install, patch, or own them", applications)
        self.assertIn("OpenRazer is the background service", applications)
        self.assertIn("(limitations.md)", applications)
        for word in ("qualified", "distribution application", "transports"):
            self.assertNotIn(word, applications)

    def test_documentation_index_lists_limitations_without_process_words(self) -> None:
        index = _page("docs/README.md")
        self.assertIn("(user/limitations.md)", index)
        for word in ("authorize", "canonical", "evidence"):
            self.assertNotIn(word, index)

    def test_roadmap_separates_shipped_next_and_later(self) -> None:
        roadmap = _page("docs/project/roadmap.md")
        headings = ["## Shipped", "## Next", "## Later"]
        positions = [roadmap.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        shipped = roadmap[positions[0] : positions[1]]
        self.assertIn("**2.0.0:**", shipped)
        self.assertIn("**2.0.1:**", shipped)
        self.assertNotIn("AUR", shipped)
        later = roadmap[positions[1] :]
        self.assertIn("not published yet", later)
        for command in ("yay ", "paru ", "makepkg", "git clone"):
            self.assertNotIn(command, roadmap)

    def test_contributing_starts_with_the_change_workflow(self) -> None:
        contributing = _page("CONTRIBUTING.md")
        first_section = contributing.split("\n## ", 2)[1]
        self.assertTrue(first_section.startswith("Change Workflow"))
        self.assertIn(
            "https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml",
            contributing,
        )
        self.assertNotIn("(.github/ISSUE_TEMPLATE/", contributing)
        self.assertNotIn("authoriz", contributing)

    def test_updates_page_gives_exact_update_rollback_and_removal_commands(self) -> None:
        page = _page("docs/user/updates-removal.md")
        for command in (
            "sudo pacman -Syu",
            "hyperfluxctl doctor",
            "sudo pacman -U",
            "sudo pacman -Rs hyperflux-next-full\n",
            "sudo pacman -S openrazer-driver-dkms",
            "sudo systemctl disable --now hyperflux-next-bridge.service",
            "sudo pacman -Rs hyperflux-next-full hyperflux-next-linux",
            "bridge.json.pacsave",
            "sudo pacman-key --delete 8BF8A266E756BD1C24C930918B3F3850007159E8",
        ):
            self.assertIn(command, page)
        disable = page.index("sudo systemctl disable --now")
        self.assertLess(disable, page.index("sudo pacman -Rs hyperflux-next-full hyperflux-next-linux"))
        self.assertIn("are not removed", page)
        for word in ("qualification", "rollback inputs", "restoration"):
            self.assertNotIn(word, page)

    def test_journey_source_map_is_a_readable_list(self) -> None:
        journey = _page("docs/project/journey.md")
        source_map = journey.split("### Technical source map", 1)[1].split("\n### ", 1)[0]
        self.assertNotIn("| Era |", source_map)
        self.assertEqual(source_map.count("\n- **"), 6)
        for line in source_map.splitlines():
            self.assertLessEqual(len(line), 100, line)

if __name__ == "__main__":
    unittest.main()
