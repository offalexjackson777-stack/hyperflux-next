# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.portal import _render_mermaid, build_portal, load_portal_config, verify_portal


class DocumentationPortalTests(unittest.TestCase):
    def test_portal_has_three_distinct_audiences_and_unique_sources(self) -> None:
        config = load_portal_config(ROOT)
        self.assertEqual(
            [audience.id for audience in config.audiences],
            ["users", "developers", "maintainers"],
        )
        self.assertEqual(len(config.pages), 21)
        self.assertEqual(len({page.url for page in config.pages}), len(config.pages))
        self.assertEqual(len({page.source for page in config.pages}), len(config.pages))
        self.assertEqual(config.publication_state, "local-artifact-only")

    def test_portal_build_is_deterministic_offline_and_accessible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first_result = build_portal(ROOT, first)
            second_result = build_portal(ROOT, second)
            first_manifest = json.loads(first_result.manifest.read_text(encoding="utf-8"))
            second_manifest = json.loads(second_result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(first_manifest, second_manifest)
            self.assertFalse(first_manifest["publication_authorized"])
            self.assertFalse(first_manifest["external_runtime_dependencies"])
            self.assertEqual(first_result.pages, 22)
            self.assertEqual(verify_portal(ROOT, first)["source_tree_sha256"], first_manifest["source_tree_sha256"])
            index = (first / "index.html").read_text(encoding="utf-8")
            self.assertIn('id="main-content"', index)
            self.assertIn('class="skip-link"', index)
            self.assertIn('class="mobile-nav"', index)
            self.assertIn("system-map.svg", index)
            self.assertNotIn("https://fonts", index)
            self.assertEqual(index.count("</html>"), 1)
            architecture = (first / "developers" / "architecture.html").read_text(
                encoding="utf-8"
            )
            self.assertIn('class="compiled-diagram"', architecture)
            self.assertNotIn('class="language-mermaid"', architecture)
            shadow = (first / "maintainers" / "migration-shadow.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("Profile selection", shadow)
            self.assertIn("Hardware writes: forbidden", shadow)

    def test_unsupported_mermaid_fails_closed(self) -> None:
        with self.assertRaisesRegex(ModelError, "unsupported Mermaid diagram type"):
            _render_mermaid("journey\n  title Not supported")

    def test_tampered_portal_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            build_portal(ROOT, site)
            index = site / "index.html"
            index.write_text(index.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "file inventory"):
                verify_portal(ROOT, site)

    def test_nonempty_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            site.mkdir()
            (site / "keep.txt").write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "must be empty"):
                build_portal(ROOT, site)
            self.assertEqual((site / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_source_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "docs", root / "docs")
            value = json.loads((root / "docs" / "portal.json").read_text(encoding="utf-8"))
            value["audiences"][0]["pages"][0]["source"] = "../outside.md"
            (root / "docs" / "portal.json").write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "safe repository Markdown path"):
                load_portal_config(root)


if __name__ == "__main__":
    unittest.main()
