# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.security import parse_rust_audit_result
from hfxdev.supply_chain import load_dependency_inventory


class RustDependencyAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = load_dependency_inventory(ROOT)

    def result(self) -> dict[str, object]:
        return {
            "database": {"advisory-count": 1178},
            "lockfile": {"dependency-count": 45},
            "vulnerabilities": {"count": 0, "found": False, "list": []},
            "warnings": {"unmaintained": [], "unsound": [], "yanked": []},
        }

    def test_clean_pinned_audit_is_accepted(self) -> None:
        result = parse_rust_audit_result(json.dumps(self.result()), self.inventory)
        self.assertEqual(result.advisory_count, 1178)
        self.assertEqual(result.dependency_count, 45)
        self.assertEqual(result.vulnerability_count, 0)
        self.assertEqual(result.warning_count, 0)

    def test_vulnerability_warning_and_inventory_drift_fail_closed(self) -> None:
        cases = (
            ("database", "advisory-count", 1177, "database count drifted"),
            ("lockfile", "dependency-count", 44, "lockfile count drifted"),
            ("vulnerabilities", "count", 1, "details are inconsistent"),
        )
        for section, field, replacement, message in cases:
            with self.subTest(section=section, field=field):
                value = self.result()
                value[section][field] = replacement  # type: ignore[index]
                with self.assertRaisesRegex(ModelError, message):
                    parse_rust_audit_result(json.dumps(value), self.inventory)

        warning = self.result()
        warning["warnings"]["unsound"] = [{"advisory": "fixture"}]  # type: ignore[index]
        with self.assertRaisesRegex(ModelError, "unreviewed warning"):
            parse_rust_audit_result(json.dumps(warning), self.inventory)

        vulnerable = self.result()
        vulnerable["vulnerabilities"]["count"] = 1  # type: ignore[index]
        vulnerable["vulnerabilities"]["list"] = [  # type: ignore[index]
            {"advisory": "fixture"}
        ]
        with self.assertRaisesRegex(ModelError, "found a vulnerability"):
            parse_rust_audit_result(json.dumps(vulnerable), self.inventory)

    def test_malformed_or_unbounded_shapes_fail_closed(self) -> None:
        for value in ("[]", "{}", "not-json"):
            with self.subTest(value=value), self.assertRaises(ModelError):
                parse_rust_audit_result(value, self.inventory)


if __name__ == "__main__":
    unittest.main()
