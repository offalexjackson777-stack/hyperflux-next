# SPDX-License-Identifier: GPL-2.0-only

"""Validator selection for owner reports.

The host has no jsonschema, so owner reports fall back to the stdlib subset
checker there; the pinned development image uses the real validator. The
subset must refuse every construct it does not model rather than skip it.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Callable
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import owner_reports, schema_validation  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402
from hfxdev.schema_validation import (  # noqa: E402
    JSONSCHEMA,
    SUBSET,
    SchemaViolation,
    jsonschema_available,
    selected_validator,
    validate,
    validate_with_subset,
)


# Constructs the subset checker does not model. It must refuse each one
# (ModelError for the schema, or SchemaViolation for the value) and never
# accept the value, whatever the real validator would say.
UNMODELLED_SCHEMAS: dict[str, tuple[dict[str, Any], Any]] = {
    "patternProperties": ({"type": "object", "patternProperties": {"^a": {"type": "string"}}}, {"a": 1}),
    "allOf": ({"allOf": [{"type": "integer"}]}, "x"),
    "anyOf": ({"anyOf": [{"type": "integer"}]}, "x"),
    "not": ({"not": {"type": "string"}}, "x"),
    "if": ({"if": {"type": "string"}, "then": {"minLength": 2}}, "x"),
    "prefixItems": ({"type": "array", "prefixItems": [{"type": "integer"}]}, ["x"]),
    "contains": ({"type": "array", "contains": {"type": "integer"}}, ["x"]),
    "minProperties": ({"type": "object", "minProperties": 1}, {}),
    "propertyNames": ({"type": "object", "propertyNames": {"maxLength": 1}}, {"ab": 1}),
    "exclusiveMinimum": ({"type": "integer", "exclusiveMinimum": 1}, 1),
    "multipleOf": ({"type": "integer", "multipleOf": 2}, 3),
    "dependentRequired": ({"type": "object", "dependentRequired": {"a": ["b"]}}, {"a": 1}),
    "unevaluatedProperties": ({"type": "object", "unevaluatedProperties": False}, {"a": 1}),
    "boolean items schema": ({"type": "array", "items": False}, [1]),
    "boolean property schema": ({"type": "object", "properties": {"a": False}}, {"a": 1}),
    "boolean oneOf branch": ({"oneOf": [False, {"type": "string"}, True]}, "x"),
    "nested unknown keyword in items": ({"type": "array", "items": {"not": {"type": "integer"}}}, [1]),
    "remote reference": ({"$ref": "https://hyperflux.dev/schemas/owner-report-v1.json"}, {}),
    "unknown format": ({"type": "string", "format": "email"}, "x"),
}

# Values the subset must refuse although Python's own == calls them equal.
STRICT_EQUALITY: dict[str, tuple[dict[str, Any], Any]] = {
    "true is not 1 inside an object": ({"const": {"a": 1}}, {"a": True}),
    "false is not 0 inside an array": ({"enum": [[0]]}, [False]),
    "nested 1 and 1.0 are the same item": ({"type": "array", "uniqueItems": True}, [[1], [1.0]]),
}


def _outcome(check: Callable[[], None]) -> tuple[str, str]:
    """valid, invalid or unsupported, with the message."""
    try:
        check()
    except SchemaViolation as error:
        return "invalid", str(error)
    except ModelError as error:
        return "unsupported", str(error)
    return "valid", ""


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


class SubsetRefusesWhatItDoesNotModel(unittest.TestCase):
    """The host half of the rule: the subset never accepts by skipping a rule."""

    def test_unmodelled_constructs_are_refused(self) -> None:
        for name, (schema, value) in UNMODELLED_SCHEMAS.items():
            with self.subTest(name=name):
                verdict, _message = _outcome(lambda: validate_with_subset(schema, value))
                self.assertNotEqual(verdict, "valid")

    def test_json_equality_keeps_booleans_apart_at_every_depth(self) -> None:
        for name, (schema, value) in STRICT_EQUALITY.items():
            with self.subTest(name=name):
                with self.assertRaises(SchemaViolation):
                    validate_with_subset(schema, value)
        validate_with_subset({"const": {"a": [1, 2.0]}}, {"a": [1.0, 2]})
        validate_with_subset({"type": "array", "uniqueItems": True}, [[1], [True]])


if __name__ == "__main__":
    unittest.main()
