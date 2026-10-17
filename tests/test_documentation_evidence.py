# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import documentation_evidence as evidence
from hfxdev.documentation_preview import (
    AUDIT_PATH,
    RENDER_CONTRACT,
    RENDER_INPUT_FILES,
    SCREENSHOT_MANIFEST_PATH,
    _screenshot_files,
    load_documentation_render_audit,
    load_documentation_render_policy,
)
from hfxdev.model import ModelError, load_json


REVISION = "1" * 40


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_tree(destination: Path) -> None:
    """Copy just the files the render evidence reads into a scratch repository."""
    catalog = load_json(ROOT / "docs" / "catalog.json")
    files = {document["path"] for document in catalog["documents"]}
    for document in catalog["documents"]:
        files.update(document["canonical_sources"])
    files.update(item["path"] for item in catalog["classified_markdown"])
    files.update(RENDER_INPUT_FILES.values())
    files.update(
        {
            AUDIT_PATH,
            SCREENSHOT_MANIFEST_PATH,
            "architecture/constitution.json",
        }
    )
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


def _write_render(root: Path, render: Path, *, salt: str = "a") -> None:
    """Write a passing render of `root` whose screenshots are distinct placeholder bytes."""
    policy = load_documentation_render_policy(root)
    render.mkdir()
    (render / "screenshots").mkdir()
    screenshots = []
    for file, (document, width, height, theme) in sorted(_screenshot_files(policy).items()):
        image = render / file
        image.write_bytes(f"{salt}:{file}".encode())
        screenshots.append(
            {
                "path": document,
                "width": width,
                "height": height,
                "theme": theme,
                "file": file,
                "sha256": _sha(image),
                "bytes": image.stat().st_size,
            }
        )
    result = {
        "schema": evidence.RESULT_SCHEMA,
        "render_contract": RENDER_CONTRACT,
        "status": "passed",
        "policy_sha256": _sha(root / RENDER_INPUT_FILES["policy_sha256"]),
        "catalog_sha256": _sha(root / RENDER_INPUT_FILES["catalog_sha256"]),
        "renderer_sha256": _sha(root / RENDER_INPUT_FILES["renderer_sha256"]),
        "environment": {
            "node": "v26.4.0",
            "playwright": "1.62.0",
            "marked": "17.0.5",
            "chromium": "150.0.7871.128",
        },
        "counts": {
            "documents": len(policy.render_paths),
            "checks": policy.check_count,
            "screenshots": policy.screenshot_count,
            "issues": 0,
        },
        "capture": {"mode": policy.capture_mode, "max_page_height": policy.max_page_height},
        "network": {"blocked_requests": 0, "placeholder_images": 4},
        "documents": [
            {
                "path": path,
                "sha256": _sha(root / path),
                "bytes": (root / path).stat().st_size,
            }
            for path in policy.render_paths
        ],
        "checks": [
            {
                "path": path,
                "width": width,
                "height": height,
                "theme": theme,
                "issues": [],
                "metrics": {},
            }
            for path in policy.render_paths
            for width, height in policy.viewports
            for theme in policy.themes
        ],
        "screenshots": screenshots,
    }
    (render / "result.json").write_text(json.dumps(result), encoding="utf-8")


def _rewrite_result(render: Path, change) -> None:
    result = json.loads((render / "result.json").read_text(encoding="utf-8"))
    change(result)
    (render / "result.json").write_text(json.dumps(result), encoding="utf-8")


class DocumentationEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name)
        self.root = base / "repository"
        self.root.mkdir()
        _copy_tree(self.root)
        self.render = base / "render"
        _write_render(self.root, self.render)

    def test_check_render_accepts_a_clean_render_of_this_tree(self) -> None:
        checked = evidence.check_render(self.root, self.render)
        self.assertEqual(len(checked.screenshots), 108)
        self.assertEqual(checked.result_sha256, _sha(self.render / "result.json"))

    def test_check_render_rejects_issues_network_and_other_trees(self) -> None:
        first = load_documentation_render_policy(self.root).render_paths[0]

        def layout_issue(result: dict) -> None:
            result["checks"][3]["issues"] = ["horizontal-overflow"]

        def counted_issue(result: dict) -> None:
            result["counts"]["issues"] = 1

        def outbound_request(result: dict) -> None:
            result["network"]["blocked_requests"] = 1

        def failed(result: dict) -> None:
            result["status"] = "failed"

        def missing_check(result: dict) -> None:
            result["checks"][-1] = result["checks"][0]

        def other_document(result: dict) -> None:
            result["documents"][0]["sha256"] = "0" * 64

        def other_renderer(result: dict) -> None:
            result["renderer_sha256"] = "0" * 64

        def missing_screenshot(result: dict) -> None:
            result["screenshots"].pop()

        def bad_runtime(result: dict) -> None:
            result["environment"]["chromium"] = "150 beta"

        for mutate in (
            layout_issue,
            counted_issue,
            outbound_request,
            failed,
            missing_check,
            other_document,
            other_renderer,
            missing_screenshot,
            bad_runtime,
        ):
            with self.subTest(mutation=mutate.__name__):
                render = Path(self._temporary.name) / f"render-{mutate.__name__}"
                _write_render(self.root, render)
                _rewrite_result(render, mutate)
                with self.assertRaises(ModelError):
                    evidence.check_render(self.root, render)

        with self.subTest(mutation="edited document"):
            (self.root / first).write_text(
                (self.root / first).read_text(encoding="utf-8") + "\nEdited.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ModelError, "another version"):
                evidence.check_render(self.root, self.render)

    def test_check_render_rejects_changed_or_linked_screenshots(self) -> None:
        file = sorted(_screenshot_files(load_documentation_render_policy(self.root)))[0]
        (self.render / file).write_bytes(b"different")
        with self.assertRaisesRegex(ModelError, "does not match result.json"):
            evidence.check_render(self.root, self.render)
        (self.render / file).unlink()
        (self.render / file).symlink_to(self.render / "result.json")
        with self.assertRaisesRegex(ModelError, "not a regular file"):
            evidence.check_render(self.root, self.render)

    def test_bind_keeps_review_until_each_changed_screenshot_is_confirmed(self) -> None:
        before = load_json(self.root / AUDIT_PATH)["visual_review"]
        outcome = evidence.bind(self.root, self.render, REVISION)
        self.assertEqual(len(outcome.changed_screenshots), 108)
        self.assertEqual(len(outcome.pending_review), 18)
        self.assertEqual(load_json(self.root / AUDIT_PATH)["visual_review"], before)
        manifest = load_json(self.root / SCREENSHOT_MANIFEST_PATH)
        self.assertEqual(manifest["source_revision"], REVISION)
        self.assertEqual(manifest["render_result_sha256"], _sha(self.render / "result.json"))
        self.assertEqual(
            [record["file"] for record in manifest["screenshots"]],
            sorted(record["file"] for record in manifest["screenshots"]),
        )
        with self.assertRaisesRegex(ModelError, "./hfx docs review"):
            load_documentation_render_audit(self.root)

        # Listing pending screenshots never marks them.
        self.assertEqual(evidence.review(self.root, self.render), outcome.pending_review)
        self.assertEqual(load_json(self.root / AUDIT_PATH)["visual_review"], before)

        first, *rest = outcome.pending_review
        self.assertEqual(evidence.review(self.root, self.render, (first,)), tuple(rest))
        with self.assertRaisesRegex(ModelError, "only changed, unreviewed"):
            evidence.review(self.root, self.render, (first,))
        with self.assertRaisesRegex(ModelError, "only changed, unreviewed"):
            evidence.review(self.root, self.render, ("screenshots/README--375--light.png",))
        with self.assertRaisesRegex(ModelError, "named only once"):
            evidence.review(self.root, self.render, (rest[0], rest[0]))
        self.assertEqual(evidence.review(self.root, self.render, tuple(rest)), ())

        audit = load_documentation_render_audit(self.root, release_evidence=True)
        self.assertEqual(audit.source_revision, REVISION)

    def test_review_requires_the_bound_render_and_its_exact_screenshots(self) -> None:
        outcome = evidence.bind(self.root, self.render, REVISION)
        other = Path(self._temporary.name) / "other-render"
        _write_render(self.root, other, salt="b")
        with self.assertRaisesRegex(ModelError, "not the one bound"):
            evidence.review(self.root, other, outcome.pending_review[:1])
        (self.render / outcome.pending_review[0]).write_bytes(b"swapped after bind")
        with self.assertRaisesRegex(ModelError, "differs from the screenshot manifest"):
            evidence.review(self.root, self.render, outcome.pending_review[:1])

    def test_unchanged_screenshots_need_no_new_review(self) -> None:
        outcome = evidence.bind(self.root, self.render, REVISION)
        evidence.review(self.root, self.render, outcome.pending_review)
        again = Path(self._temporary.name) / "same-pixels"
        _write_render(self.root, again)
        outcome = evidence.bind(self.root, again, "2" * 40)
        self.assertEqual(outcome.changed_screenshots, ())
        self.assertEqual(outcome.pending_review, ())
        load_documentation_render_audit(self.root, release_evidence=True)

    def test_structure_mode_ignores_a_stale_record_and_release_mode_rejects_it(self) -> None:
        outcome = evidence.bind(self.root, self.render, REVISION)
        evidence.review(self.root, self.render, outcome.pending_review)
        first = load_documentation_render_policy(self.root).render_paths[0]
        (self.root / first).write_text(
            (self.root / first).read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8"
        )
        load_documentation_render_audit(self.root)
        with self.assertRaisesRegex(ModelError, "rendered_documents_sha256 is stale"):
            load_documentation_render_audit(self.root, release_evidence=True)

    def test_render_selection_fails_closed(self) -> None:
        for base in (None, "", "0" * 40):
            with self.subTest(base=base):
                self.assertTrue(evidence.render_selection(ROOT, base).needed)
        with patch.object(
            evidence, "git_changed_paths", side_effect=ModelError("unknown revision")
        ):
            selection = evidence.render_selection(ROOT, "f" * 40)
        self.assertTrue(selection.needed)
        self.assertEqual(selection.reason, "base revision is unavailable")
        with patch.object(
            evidence, "git_changed_paths", return_value=("a" * 40, ("src/lib.rs",))
        ):
            self.assertFalse(evidence.render_selection(ROOT, "a" * 40).needed)
        for path in (
            "README.md",
            "docs/render-preview.json",
            "tools/documentation_preview.cjs",
            "toolchains/documentation-render/package-lock.json",
            ".devcontainer/Containerfile",
        ):
            with self.subTest(path=path), patch.object(
                evidence, "git_changed_paths", return_value=("a" * 40, (path,))
            ):
                selection = evidence.render_selection(ROOT, "a" * 40)
                self.assertTrue(selection.needed)
                self.assertEqual(selection.changed_inputs, (path,))

    def test_skipped_render_writes_a_summary_without_running_node(self) -> None:
        output = Path(self._temporary.name) / "skipped"
        with patch.object(
            evidence, "git_changed_paths", return_value=("a" * 40, ("src/lib.rs",))
        ), patch.object(evidence.shutil, "which") as which:
            self.assertEqual(evidence.run_render(ROOT, output, changed_from="a" * 40), 0)
        which.assert_not_called()
        self.assertFalse((output / "render").exists())
        self.assertFalse(load_json(output / "selection.json")["needed"])
        self.assertIn("Skipped", (output / "summary.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ModelError, "must not exist"):
            evidence.run_render(ROOT, output, changed_from="a" * 40)

    def test_missing_renderer_packages_fail_the_render(self) -> None:
        output = Path(self._temporary.name) / "no-packages"
        with self.assertRaisesRegex(ModelError, "documentation render failed"):
            evidence.run_render(
                ROOT,
                output,
                node_modules=Path(self._temporary.name) / "absent",
            )
        self.assertIn("FAILED", (output / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
