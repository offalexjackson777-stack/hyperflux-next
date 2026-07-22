# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

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
        self.assertIn("Publication decision required", page.content)
        self.assertIn("Timing values are planning budgets", page.content)
        self.assertIn("Ready in software", page.content)
        self.assertIn("Awaiting hardware evidence", page.content)
        self.assertNotIn('id="repository-state-data"', page.content)
        self.assertEqual(len(page.search_records), 53)

    def test_state_interactions_are_local_and_accessible(self) -> None:
        page = render_repository_state(ROOT)
        self.assertIn('role="tablist"', page.content)
        self.assertEqual(page.content.count('role="tabpanel"'), 4)
        self.assertIn('aria-live="polite"', page.content)
        self.assertEqual(REPOSITORY_STATE_SCRIPT.count("const panels ="), 1)
        self.assertIn("selectFromHash", REPOSITORY_STATE_SCRIPT)
        self.assertNotIn("fetch(", REPOSITORY_STATE_SCRIPT)
        self.assertNotIn("XMLHttpRequest", REPOSITORY_STATE_SCRIPT)
        self.assertNotIn("WebSocket", REPOSITORY_STATE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
