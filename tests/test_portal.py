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
from hfxdev.portal import (
    MAX_HTML_BYTES,
    MAX_JAVASCRIPT_BYTES,
    MAX_PORTAL_BYTES,
    MAX_SEARCH_INDEX_BYTES,
    MAX_STYLESHEET_BYTES,
    _render_mermaid,
    build_portal,
    load_portal_config,
    verify_portal,
)


class DocumentationPortalTests(unittest.TestCase):
    def test_portal_has_three_distinct_audiences_and_unique_sources(self) -> None:
        config = load_portal_config(ROOT)
        self.assertEqual(
            [audience.id for audience in config.audiences],
            ["users", "developers", "maintainers"],
        )
        self.assertEqual(len(config.pages), 23)
        self.assertEqual(len({page.url for page in config.pages}), len(config.pages))
        self.assertEqual(len({page.source for page in config.pages}), len(config.pages))
        self.assertEqual(
            {page.kind for page in config.pages},
            {"guide", "concept", "reference", "book", "ledger"},
        )
        self.assertEqual(
            next(page.kind for page in config.pages if page.id == "protocol"),
            "reference",
        )
        self.assertEqual(
            next(page.kind for page in config.pages if page.id == "design-book"),
            "book",
        )
        self.assertEqual(
            next(page.kind for page in config.pages if page.id == "coverage"),
            "ledger",
        )
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
            self.assertEqual(
                first_manifest["source_publication_state"],
                "public-pages-pre-release",
            )
            self.assertFalse(first_manifest["product_publication_authorized"])
            self.assertFalse(first_manifest["external_runtime_dependencies"])
            self.assertEqual(first_result.pages, 37)
            self.assertEqual(verify_portal(ROOT, first)["source_tree_sha256"], first_manifest["source_tree_sha256"])
            inventory = first_manifest["files"]
            self.assertLessEqual(sum(item["size"] for item in inventory), MAX_PORTAL_BYTES)
            self.assertLessEqual(
                max(item["size"] for item in inventory if item["path"].endswith(".html")),
                MAX_HTML_BYTES,
            )
            self.assertTrue(
                all(
                    item["size"] <= MAX_JAVASCRIPT_BYTES
                    for item in inventory
                    if item["path"].endswith(".js")
                )
            )
            sizes = {item["path"]: item["size"] for item in inventory}
            self.assertLessEqual(sizes["assets/site.css"], MAX_STYLESHEET_BYTES)
            self.assertLessEqual(
                sizes["assets/search-index.json"], MAX_SEARCH_INDEX_BYTES
            )
            index = (first / "index.html").read_text(encoding="utf-8")
            self.assertIn('id="main-content"', index)
            self.assertIn('class="skip-link"', index)
            self.assertIn('class="mobile-nav"', index)
            self.assertIn('class="primary-nav"', index)
            self.assertIn('class="theme-cycle"', index)
            self.assertIn('data-search-index="assets/search-index.json"', index)
            self.assertIn("system-map.svg", index)
            self.assertIn("Explore tested hardware", index)
            self.assertIn("One direction of responsibility", index)
            self.assertIn("whole-product profiles marked fully qualified", index)
            self.assertIn("Capability-scoped route evidence", index)
            self.assertNotIn("https://fonts", index)
            self.assertNotIn('id="portal-search-data"', index)
            self.assertEqual(index.count("</html>"), 1)
            self.assertTrue((first / "assets" / "search-index.json").is_file())
            self.assertTrue((first / "assets" / "social-preview.png").is_file())
            search_records = json.loads(
                (first / "assets" / "search-index.json").read_text(encoding="utf-8")
            )
            self.assertGreater(len(search_records), 100)
            self.assertTrue(
                all(
                    set(record) == {"title", "audience", "summary", "url", "search"}
                    for record in search_records
                )
            )
            portal_script = (first / "assets" / "portal.js").read_text(encoding="utf-8")
            self.assertIn("fetch(document.body.dataset.searchIndex", portal_script)
            self.assertIn("globalThis.HyperFluxPortal", portal_script)
            self.assertNotIn("fetch('http", portal_script)
            self.assertNotIn('fetch("http', portal_script)
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
            design_book = (first / "developers" / "design-book.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("twelve-chapter book", design_book)
            self.assertIn("<strong>12</strong><span>chapters</span>", design_book)
            chapters = sorted((first / "developers" / "design-book").glob("*.html"))
            self.assertEqual(len(chapters), 12)
            self.assertIn('id="section-1"', chapters[0].read_text(encoding="utf-8"))
            protocol = (first / "developers" / "protocol.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("Generated API reference", protocol)
            self.assertEqual(protocol.count("data-reference-entry"), 49)
            self.assertEqual(protocol.count('id="reference-detail"'), 1)
            reference_script = (first / "assets" / "reference.js").read_text(
                encoding="utf-8"
            )
            self.assertIn("HyperFluxPortal.preferredVisible", reference_script)
            coverage = (first / "maintainers" / "coverage.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("Implementation ledger", coverage)
            self.assertEqual(coverage.count("data-coverage-entry"), 67)
            self.assertEqual(coverage.count('id="coverage-detail"'), 1)
            coverage_script = (first / "assets" / "coverage.js").read_text(
                encoding="utf-8"
            )
            self.assertIn("HyperFluxPortal.preferredVisible", coverage_script)
            device_lab = (first / "devices" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Hardware evidence catalog", device_lab)
            self.assertIn("Evidence catalog, not a control panel", device_lab)
            self.assertIn("Tested through HyperFlux", device_lab)
            self.assertIn("Evidence heatmap", device_lab)
            self.assertIn('id="device-filter"', device_lab)
            self.assertEqual(device_lab.count('data-compare-id="'), 12)
            self.assertEqual(device_lab.count("data-device-row"), 12)
            self.assertEqual(device_lab.count('data-support="route-qualified"'), 2)
            self.assertEqual(device_lab.count("lab-detail-header"), 1)
            device_script = (first / "assets" / "device-lab.js").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("fetch(", device_script)
            self.assertNotIn("XMLHttpRequest", device_script)
            self.assertIn("HyperFluxPortal.preferredVisible", device_script)
            atlas = (first / "atlas" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Generated architecture map", atlas)
            self.assertIn("One architecture record", atlas)
            self.assertIn("Responsibility boundary", atlas)
            self.assertIn('id="atlas-filter"', atlas)
            self.assertEqual(atlas.count("data-atlas-node"), 31)
            self.assertEqual(atlas.count("data-atlas-selected-record"), 1)
            self.assertNotIn("data-atlas-row", atlas)
            self.assertNotIn("data-atlas-detail", atlas)
            atlas_script = (first / "assets" / "atlas.js").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("fetch(", atlas_script)
            self.assertNotIn("XMLHttpRequest", atlas_script)
            self.assertIn("HyperFluxPortal.preferredVisible", atlas_script)
            state = (first / "state" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Release gates", state)
            self.assertIn("Migration decisions", state)
            self.assertIn("Verification timing budgets", state)
            self.assertIn("Performance boundaries", state)
            self.assertIn("Software readiness is not hardware evidence", state)
            self.assertIn("gates ready in software", state)
            self.assertIn("estimated serial verification time", state)
            self.assertNotIn('id="repository-state-data"', state)
            self.assertEqual(state.count("data-gate"), 10)
            self.assertEqual(state.count("data-migration"), 13)
            self.assertEqual(state.count("data-verification"), 30)
            state_script = (first / "assets" / "repository-state.js").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("fetch(", state_script)
            self.assertNotIn("XMLHttpRequest", state_script)

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
