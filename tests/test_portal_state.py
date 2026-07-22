# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.portal_state import REPOSITORY_STATE_SCRIPT, render_repository_state


class RepositoryStateTests(unittest.TestCase):
    def test_state_page_is_a_canonical_read_only_projection(self) -> None:
        page = render_repository_state(ROOT)
        self.assertEqual(page.content.count("data-gate"), 10)
        self.assertEqual(page.content.count("data-migration"), 13)
        self.assertEqual(page.content.count("data-verification"), 30)
        self.assertIn("Product unreleased", page.content)
        self.assertIn('data-state="publication-locked"', page.content)
        self.assertIn("budgets rather than observed run times", page.content)

        marker = '<script id="repository-state-data" type="application/json">'
        payload = page.content.split(marker, 1)[1].split("</script>", 1)[0]
        records = json.loads(payload)
        self.assertEqual(len(records["gates"]), 10)
        self.assertEqual(len(records["migration"]), 13)
        self.assertEqual(len(records["verification"]), 30)
        self.assertTrue(all(item["expected"] > 0 for item in records["verification"]))
        self.assertTrue(all(item["timeout"] >= item["expected"] for item in records["verification"]))

    def test_state_interactions_are_local_and_accessible(self) -> None:
        page = render_repository_state(ROOT)
        self.assertIn('role="tablist"', page.content)
        self.assertEqual(page.content.count('role="tabpanel"'), 4)
        self.assertIn('aria-live="polite"', page.content)
        self.assertEqual(REPOSITORY_STATE_SCRIPT.count("const panels ="), 1)
        self.assertNotIn("fetch(", REPOSITORY_STATE_SCRIPT)
        self.assertNotIn("XMLHttpRequest", REPOSITORY_STATE_SCRIPT)
        self.assertNotIn("WebSocket", REPOSITORY_STATE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
