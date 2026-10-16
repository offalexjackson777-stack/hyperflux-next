# SPDX-License-Identifier: GPL-2.0-only

"""Validator selection for owner reports.

The host has no jsonschema, so owner reports fall back to the stdlib subset
checker there; the pinned development image uses the real validator.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import owner_reports, schema_validation  # noqa: E402
from hfxdev.schema_validation import (  # noqa: E402
    JSONSCHEMA,
    SUBSET,
    SchemaViolation,
    jsonschema_available,
    selected_validator,
    validate,
)


class SelectionTests(unittest.TestCase):
    def test_host_without_jsonschema_uses_the_subset(self) -> None:
        with mock.patch.object(schema_validation, "jsonschema_available", return_value=False):
            self.assertEqual(selected_validator(), SUBSET)
            with mock.patch.object(schema_validation, "validate_with_jsonschema") as real:
                with self.assertRaisesRegex(SchemaViolation, r"^\$: must be integer$"):
                    validate({"type": "integer"}, "x")
            real.assert_not_called()

    def test_image_with_jsonschema_uses_the_real_validator(self) -> None:
        with mock.patch.object(schema_validation, "jsonschema_available", return_value=True):
            self.assertEqual(selected_validator(), JSONSCHEMA)
            with mock.patch.object(schema_validation, "validate_with_jsonschema") as real:
                validate({"type": "integer"}, 1, root=ROOT)
            real.assert_called_once_with({"type": "integer"}, 1, root=ROOT)

    def test_missing_module_means_the_subset(self) -> None:
        found = schema_validation.importlib.util.find_spec

        def without_referencing(name: str, *args: Any) -> Any:
            return None if name == "referencing" else found(name, *args)

        with mock.patch.object(schema_validation.importlib.util, "find_spec", without_referencing):
            self.assertFalse(jsonschema_available())

    def test_owner_reports_use_the_selected_validator(self) -> None:
        self.assertIs(owner_reports.validate, schema_validation.validate)
        self.assertIs(owner_reports.SchemaViolation, SchemaViolation)

    def test_the_host_never_needs_jsonschema_for_owner_reports(self) -> None:
        with mock.patch.object(schema_validation, "jsonschema_available", return_value=False):
            owner_reports.load_owner_report_policy(ROOT)
            owner_reports.load_owner_reports(ROOT)


if __name__ == "__main__":
    unittest.main()
