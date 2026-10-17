# SPDX-License-Identifier: GPL-2.0-only

"""Validator selection for owner reports, and parity between the two validators.

The host has no jsonschema, so owner reports fall back to the stdlib subset
checker there. The parity cases run only in the pinned development image
(`./hfx ci verify`, the full-software lane), where both validators exist: on
every owner-report fixture, every schema check the owner-report tests make,
and a sweep of edits to each fixture, both must give the same verdict, and
the subset must never accept what the real validator refuses.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterator
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import owner_reports, schema_validation  # noqa: E402
from hfxdev.model import ModelError, load_json  # noqa: E402
from hfxdev.schema_validation import (  # noqa: E402
    JSONSCHEMA,
    SUBSET,
    SchemaViolation,
    error_path,
    jsonschema_available,
    jsonschema_validator,
    selected_validator,
    validate,
    validate_with_jsonschema,
    validate_with_subset,
)


FIXTURES = ROOT / "apps" / "device-qualification" / "tests" / "fixtures"
REPORT_SCHEMA = ROOT / "apps" / "device-qualification" / "contracts" / "local-qualification-view.schema.json"
POLICY = ROOT / "assurance" / "owner-reports.json"
POLICY_SCHEMA = ROOT / "schemas" / "owner-report-policy.schema.json"
RECORD_SCHEMA = ROOT / "schemas" / "owner-report.schema.json"
UNLISTED_KEY = "unlistedKey7781"
UNLISTED_VALUE = "unlistedValue7781"

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

# How the subset names the keyword it refuses; the parity sweep tolerates a
# refusal only for a construct listed above.
REFUSED_KEYWORD = re.compile(r"unsupported keywords: ([A-Za-z$]+)$")

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


# ---------------------------------------------------------------------------
# Parity cases


def _paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _paths(child, (*path, index))


def _replace(document: Any, path: tuple[Any, ...], change: Callable[[Any, Any], None]) -> Any:
    edited = copy.deepcopy(document)
    parent = edited
    for segment in path[:-1]:
        parent = parent[segment]
    change(parent, path[-1])
    return edited


def _replacements(value: Any) -> list[Any]:
    """A few wrong or edge values for one place in a document."""
    if isinstance(value, bool):
        return [None, 1]
    if isinstance(value, int):
        return [float(value), -1, 2**40, str(value), True]
    if isinstance(value, float):
        return [None, -1.5]
    if isinstance(value, str):
        return [None, "", UNLISTED_VALUE, value + " " * 300, "2026-13-40T99:00:00Z", 0]
    if isinstance(value, list):
        doubled = value + value[:1] if value else [None]
        return [None, [], doubled]
    if isinstance(value, dict):
        return [None, [], {}]
    return [0, "", {}]


def edited_documents(document: Any, seen: set[tuple[Any, ...]]) -> Iterator[tuple[str, Any]]:
    """Single edits to one valid document, once per schema place and value type.

    Array items share their schema, so a place is its path with indexes
    written as `*`; `seen` carries the places already swept across documents
    of the same schema. This keeps the sweep to about a thousand real
    validations, each of which costs about 10 ms on a full report.
    """
    for path, value in _paths(document):
        place = (tuple("*" if isinstance(segment, int) else segment for segment in path), type(value).__name__)
        if place in seen:
            continue
        seen.add(place)
        if path:
            for replacement in _replacements(value):

                def assign(parent: Any, key: Any, new: Any = replacement) -> None:
                    parent[key] = copy.deepcopy(new)

                yield f"{list(path)} = {json.dumps(replacement)[:24]}", _replace(document, path, assign)
            if isinstance(path[-1], str):

                def remove(parent: Any, key: Any) -> None:
                    del parent[key]

                yield f"{list(path)} removed", _replace(document, path, remove)
        if isinstance(value, dict):
            if path:

                def add(parent: Any, key: Any) -> None:
                    parent[key][UNLISTED_KEY] = UNLISTED_VALUE

                yield f"{list(path)} gains a key", _replace(document, path, add)
            else:
                grown = copy.deepcopy(document)
                grown[UNLISTED_KEY] = UNLISTED_VALUE
                yield "root gains a key", grown


def recorded_owner_report_checks() -> list[tuple[str, dict[str, Any], Any]]:
    """Every (schema, value) the owner-report tests hand to a validator.

    Runs tests/test_owner_reports.py with a recorder around both the
    owner-report ingest's validator and the subset validator the tests call
    directly, so a case added there is checked here without a copy.
    """
    import test_owner_reports

    recorded: list[tuple[str, dict[str, Any], Any]] = []

    def recorder(real: Callable[..., None], label: str) -> Callable[..., None]:
        def record(schema: dict[str, Any], value: Any, **options: Any) -> None:
            recorded.append((f"{label} #{len(recorded)}", copy.deepcopy(schema), copy.deepcopy(value)))
            real(schema, value, **options)

        return record

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_owner_reports)
    with mock.patch.object(owner_reports, "validate", recorder(owner_reports.validate, "ingest")):
        with mock.patch.object(test_owner_reports, "validate", recorder(test_owner_reports.validate, "test")):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("tests/test_owner_reports.py must pass before parity can be checked")
    return recorded


@dataclass(frozen=True)
class Verdicts:
    label: str
    subset: str
    message: str
    real_paths: frozenset[str]
    value: Any


@unittest.skipUnless(
    jsonschema_available(),
    "jsonschema is only in the pinned development image; ./hfx ci verify runs the parity cases",
)
class ValidatorParity(unittest.TestCase):
    verdicts: list[Verdicts]
    valid_cases: list[tuple[str, dict[str, Any], Any]]

    @classmethod
    def setUpClass(cls) -> None:
        record = owner_reports.build_owner_record(
            ROOT, _expected_report_text(), issue=42, confirmations=["input", "openrgb-lighting"]
        )
        report_schema = load_json(REPORT_SCHEMA)
        cls.valid_cases = [
            *[(f"fixture {path.name}", report_schema, load_json(path)) for path in sorted(FIXTURES.glob("*.json"))],
            ("policy", load_json(POLICY_SCHEMA), load_json(POLICY)),
            ("record", load_json(RECORD_SCHEMA), record),
        ]
        cases = list(cls.valid_cases) + recorded_owner_report_checks()
        seen: dict[str, set[tuple[Any, ...]]] = {}
        for label, schema, document in cls.valid_cases:
            places = seen.setdefault(schema["$id"], set())
            cases.extend((f"{label}: {name}", schema, edited) for name, edited in edited_documents(document, places))
        validators: dict[int, Any] = {}
        cls.verdicts = []
        for label, schema, value in cases:
            if id(schema) not in validators:
                validators[id(schema)] = (schema, jsonschema_validator(schema, root=ROOT))
            subset, message = _outcome(lambda: validate_with_subset(schema, value))
            real = frozenset(error_path(error) for error in validators[id(schema)][1].iter_errors(value))
            cls.verdicts.append(Verdicts(label, subset, message, real, value))

    def test_owner_report_documents_are_valid_for_both(self) -> None:
        for label, schema, value in self.valid_cases:
            with self.subTest(case=label):
                validate_with_subset(schema, value)
                validate_with_jsonschema(schema, value, root=ROOT)

    def test_subset_never_accepts_what_jsonschema_rejects(self) -> None:
        accepted = [
            f"{item.label}: real validator fails at {sorted(item.real_paths)}"
            for item in self.verdicts
            if item.subset == "valid" and item.real_paths
        ]
        self.assertEqual(accepted, [], f"{len(accepted)} of {len(self.verdicts)} cases")
        for name, (schema, value) in {**UNMODELLED_SCHEMAS, **STRICT_EQUALITY}.items():
            with self.subTest(name=name):
                verdict, _message = _outcome(lambda: validate_with_subset(schema, value))
                self.assertNotEqual(verdict, "valid")

    def test_verdicts_and_failing_paths_match(self) -> None:
        differences = []
        for item in self.verdicts:
            subset_path = item.message.split(": ", 1)[0]
            if item.subset == "unsupported":
                refused = REFUSED_KEYWORD.search(item.message)
                if refused is None or refused.group(1) not in UNMODELLED_SCHEMAS:
                    differences.append(f"{item.label}: subset refuses the schema ({item.message})")
            elif (item.subset == "invalid") != bool(item.real_paths):
                differences.append(f"{item.label}: subset says {item.subset}, real fails at {sorted(item.real_paths)}")
            elif item.subset == "invalid" and subset_path not in item.real_paths:
                differences.append(f"{item.label}: subset fails at {subset_path}, real at {sorted(item.real_paths)}")
        self.assertEqual(differences, [], f"{len(differences)} of {len(self.verdicts)} cases")
        self.assertGreater(len(self.verdicts), 500)

    def test_whole_floats_are_not_integers_for_either(self) -> None:
        schema = {"type": "object", "properties": {"issue": {"type": "integer"}}}
        for value in ({"issue": 7.0}, {"issue": True}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SchemaViolation, r"^\$\.issue: must be integer$"):
                    validate_with_subset(schema, value)
                with self.assertRaisesRegex(SchemaViolation, r"^\$\.issue: must be integer$"):
                    validate_with_jsonschema(schema, value, root=ROOT)

    def test_real_validator_messages_keep_the_privacy_contract(self) -> None:
        checked = 0
        for item in self.verdicts:
            dumped = json.dumps(item.value)
            if not item.real_paths or (UNLISTED_VALUE not in dumped and UNLISTED_KEY not in dumped):
                continue
            checked += 1
            with self.subTest(case=item.label):
                for path in item.real_paths:
                    self.assertTrue(path.startswith("$"), path)
                    self.assertNotIn(UNLISTED_KEY, path)
        self.assertGreater(checked, 0)
        schema = load_json(REPORT_SCHEMA)
        report = load_json(FIXTURES / "rust-qualified-report.json")
        report["companion"][UNLISTED_KEY] = UNLISTED_VALUE
        report["receivers"][0]["model_name"] = UNLISTED_VALUE * 20
        with self.assertRaises(SchemaViolation) as caught:
            validate_with_jsonschema(schema, report, root=ROOT)
        self.assertNotIn(UNLISTED_VALUE, str(caught.exception))
        self.assertNotIn(UNLISTED_KEY, str(caught.exception))

    def test_owner_report_wording_is_the_same_for_both(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["count"],
            "properties": {
                "count": {"type": "integer", "minimum": 1},
                "tags": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z]+$"}},
                "when": {"type": "string", "format": "date-time"},
                "kind": {"oneOf": [{"const": "a"}, {"const": "b"}]},
            },
        }
        cases = [
            {},
            {"count": True},
            {"count": 0},
            {"count": 1, "tags": ["a", "a"]},
            {"count": 1, "tags": [UNLISTED_VALUE]},
            {"count": 1, "when": UNLISTED_VALUE},
            {"count": 1, UNLISTED_KEY: 1},
            {"count": 1, "kind": "c"},
        ]
        for value in cases:
            with self.subTest(value=sorted(value)):
                expected = _outcome(lambda: validate_with_subset(schema, value))
                actual = _outcome(lambda: validate_with_jsonschema(schema, value, root=ROOT))
                self.assertEqual(actual, expected)


def _expected_report_text() -> str:
    import test_owner_reports

    return json.dumps(test_owner_reports.expected_report())


if __name__ == "__main__":
    unittest.main()
