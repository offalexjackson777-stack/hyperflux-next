# SPDX-License-Identifier: GPL-2.0-only

"""The report-only JSON Schema instance validation node.

The validator lives only in the pinned development image, so these tests run
on the host without it: they use fake error objects and a fake validator, and
the end-to-end case runs only where jsonschema is importable.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterator
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev import schema_instances  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402
from hfxdev.schema_instances import (  # noqa: E402
    MISSING_VALIDATOR_MESSAGE,
    REPORT_PATH,
    build_report,
    is_date,
    is_date_time,
    is_uri,
    jsonschema_validator_factory,
    violation_record,
    write_report,
)
from hfxdev.testgraph import load_test_catalog  # noqa: E402
from hfxdev.verify import RUNNERS  # noqa: E402


JSONSCHEMA_AVAILABLE = (
    importlib.util.find_spec("jsonschema") is not None
    and importlib.util.find_spec("referencing") is not None
)
PRIVATE_KEY = "owner-home-serial-0xDEADBEEF"
PRIVATE_VALUE = "private text the report must never repeat"


@dataclass
class FakeError:
    validator: Any
    absolute_path: list[Any]
    absolute_schema_path: list[Any]
    message: str = f"{PRIVATE_VALUE!r} is not valid; unexpected {PRIVATE_KEY!r}"
    instance: Any = PRIVATE_VALUE
    validator_value: Any = field(default_factory=lambda: [PRIVATE_VALUE])


class FakeValidator:
    def __init__(self, errors: list[FakeError]) -> None:
        self.errors = errors

    def iter_errors(self, _instance: Any) -> Iterator[FakeError]:
        yield from self.errors


def _write(root: Path, relative: str, value: Any) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://hyperflux.dev/schemas/example-v1.json",
    "type": "object",
    "required": ["$schema", "name"],
    "additionalProperties": False,
    "properties": {
        "$schema": {"const": "../schemas/example.schema.json"},
        "name": {"type": "string"},
        "when": {"type": "string", "format": "date-time"},
        "devices": {"type": "object", "additionalProperties": {"type": "integer"}},
    },
}


class ViolationPrivacyTests(unittest.TestCase):
    def test_record_holds_path_keyword_and_schema_path_only(self) -> None:
        error = FakeError(
            validator="type",
            absolute_path=["devices", PRIVATE_KEY, 3],
            absolute_schema_path=[
                "properties",
                "devices",
                "additionalProperties",
                "items",
                "type",
            ],
        )
        record = violation_record(error)
        self.assertEqual(set(record), {"path", "keyword", "schema_path"})
        self.assertEqual(record["path"], "/devices/*/3")
        self.assertEqual(record["keyword"], "type")
        self.assertEqual(
            record["schema_path"], "/properties/devices/additionalProperties/items/type"
        )
        rendered = json.dumps(record)
        self.assertNotIn(PRIVATE_KEY, rendered)
        self.assertNotIn(PRIVATE_VALUE, rendered)

    def test_unknown_key_errors_never_name_the_key(self) -> None:
        error = FakeError(
            validator="additionalProperties",
            absolute_path=[],
            absolute_schema_path=["additionalProperties"],
        )
        record = violation_record(error)
        self.assertEqual(record, {"path": "", "keyword": "additionalProperties", "schema_path": "/additionalProperties"})
        self.assertNotIn(PRIVATE_KEY, json.dumps(record))

    def test_keys_reached_through_ref_keep_their_declared_names(self) -> None:
        error = FakeError(
            validator="minLength",
            absolute_path=["records", 0, "title"],
            absolute_schema_path=[
                "properties",
                "records",
                "items",
                "$ref",
                "properties",
                "title",
                "minLength",
            ],
        )
        self.assertEqual(violation_record(error)["path"], "/records/0/title")

    def test_a_property_named_properties_does_not_leak_the_next_segment(self) -> None:
        error = FakeError(
            validator="required",
            absolute_path=["properties", PRIVATE_KEY],
            absolute_schema_path=["properties", "properties", "additionalProperties", "required"],
        )
        self.assertEqual(violation_record(error)["path"], "/properties/*")

    def test_pointer_segments_are_escaped(self) -> None:
        error = FakeError(
            validator="type",
            absolute_path=["a/b~c"],
            absolute_schema_path=["properties", "a/b~c", "type"],
        )
        record = violation_record(error)
        self.assertEqual(record["path"], "/a~1b~0c")
        self.assertEqual(record["schema_path"], "/properties/a~1b~0c/type")

    def test_non_string_keyword_is_not_repeated(self) -> None:
        error = FakeError(validator=object(), absolute_path=[], absolute_schema_path=[])
        self.assertEqual(violation_record(error)["keyword"], "unknown")


class FormatCheckTests(unittest.TestCase):
    def test_date(self) -> None:
        self.assertTrue(is_date("2026-07-21"))
        self.assertFalse(is_date("2026-02-30"))
        self.assertFalse(is_date("2026-7-21"))
        self.assertTrue(is_date(7), "formats apply to strings only")

    def test_date_time(self) -> None:
        self.assertTrue(is_date_time("2026-07-21T10:11:12Z"))
        self.assertTrue(is_date_time("2026-07-21T10:11:12.123456+02:00"))
        self.assertFalse(is_date_time("2026-07-21 10:11:12Z"))
        self.assertFalse(is_date_time("2026-07-21T10:11:12"))
        self.assertFalse(is_date_time("2026-13-21T10:11:12Z"))

    def test_uri(self) -> None:
        self.assertTrue(is_uri("https://archive.archlinux.org/repos/2026/07/21/$repo/os/$arch"))
        self.assertTrue(is_uri("urn:example:a%20b"))
        self.assertFalse(is_uri("relative/path"))
        self.assertFalse(is_uri("https://example.org/with space"))
        self.assertFalse(is_uri("https://example.org/%zz"))


class ReportTests(unittest.TestCase):
    def test_report_counts_and_never_raises_on_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [
                _write(root, "schemas/example.schema.json", SCHEMA),
                _write(root, "data/clean.json", {"$schema": "../schemas/example.schema.json", "name": "a"}),
                _write(root, "data/bad.json", {"$schema": "../schemas/example.schema.json", "name": 1}),
                _write(root, "data/missing.json", {"$schema": "../schemas/absent.schema.json"}),
                _write(root, "data/plain.json", {"no": "schema"}),
            ]
            bad_error = FakeError(
                validator="type",
                absolute_path=["name"],
                absolute_schema_path=["properties", "name", "type"],
            )

            def validator_for(schema: dict[str, Any]) -> FakeValidator:
                return FakeValidator([])

            def failing_for(schema: dict[str, Any]) -> FakeValidator:
                return FakeValidator([bad_error] if schema.get("$id") else [])

            calls: list[str] = []

            def recording(schema: dict[str, Any]) -> FakeValidator:
                calls.append(schema.get("$id") or schema.get("$ref"))
                return failing_for(schema)

            report = build_report(root, recording, validator_version="test", files=files)
            summary = report["summary"]
            self.assertEqual(report["mode"], "report-only")
            self.assertEqual(summary["documents"], 4)
            self.assertEqual(summary["unresolved"], 1)
            self.assertEqual(summary["with_violations"], 2)
            self.assertEqual(summary["clean"], 1, "the schema itself checks against the meta-schema")
            self.assertIn("https://json-schema.org/draft/2020-12/schema", calls)
            statuses = {entry["path"]: entry["status"] for entry in report["documents"]}
            self.assertNotIn("data/plain.json", statuses)
            self.assertEqual(statuses["data/missing.json"], "unresolved")
            self.assertEqual(statuses["schemas/example.schema.json"], "clean")

            clean = build_report(root, validator_for, validator_version="test", files=files)
            self.assertEqual(clean["summary"]["with_violations"], 0)

    def test_installed_schema_paths_resolve_to_repository_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [
                _write(root, "schemas/example.schema.json", SCHEMA),
                _write(
                    root,
                    "packaging/generated/example.json",
                    {"$schema": "/usr/share/hyperflux-next/schemas/example.schema.json", "name": "a"},
                ),
                _write(root, "data/by-id.json", {"$schema": SCHEMA["$id"], "name": "a"}),
            ]
            report = build_report(root, lambda _schema: FakeValidator([]), validator_version="test", files=files)
            by_path = {entry["path"]: entry for entry in report["documents"]}
            self.assertEqual(by_path["packaging/generated/example.json"]["schema_id"], SCHEMA["$id"])
            self.assertEqual(by_path["data/by-id.json"]["schema_id"], SCHEMA["$id"])

    def test_a_broken_schema_is_recorded_by_exception_type_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [
                _write(root, "schemas/example.schema.json", SCHEMA),
                _write(root, "data/a.json", {"$schema": "../schemas/example.schema.json"}),
            ]

            def broken(schema: dict[str, Any]) -> FakeValidator:
                if schema.get("$id"):
                    raise ValueError(PRIVATE_VALUE)
                return FakeValidator([])

            report = build_report(root, broken, validator_version="test", files=files)
            entry = next(item for item in report["documents"] if item["path"] == "data/a.json")
            self.assertEqual(entry["status"], "validator-error")
            self.assertEqual(entry["error"], "ValueError")
            self.assertNotIn(PRIVATE_VALUE, json.dumps(report))

    def test_report_is_written_below_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = write_report(root, {"schema": "x", "summary": {}})
            self.assertEqual(destination, root / REPORT_PATH)
            self.assertEqual(REPORT_PATH.parts[0], "build")
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["schema"], "x")


class MissingValidatorTests(unittest.TestCase):
    def test_missing_module_points_to_the_pinned_image(self) -> None:
        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] in {"jsonschema", "referencing", "jsonschema_specifications"}:
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", refuse):
            with self.assertRaises(ModelError) as raised:
                jsonschema_validator_factory(ROOT, [])
        self.assertEqual(str(raised.exception), MISSING_VALIDATOR_MESSAGE)
        self.assertIn("./hfx ci verify", MISSING_VALIDATOR_MESSAGE)


class TestGraphPlacementTests(unittest.TestCase):
    def test_node_runs_in_the_full_container_lane_only(self) -> None:
        node = load_test_catalog(ROOT).by_id["schema-instances"]
        self.assertEqual(node.lanes, ("full-software",))
        self.assertNotIn("fast", node.lanes)
        self.assertEqual(node.hardware_requirement, "none")
        self.assertFalse(node.writes_hardware)
        self.assertIn("schema-contracts", node.dependencies)
        self.assertIn(node.runner, RUNNERS)

    def test_runner_reports_without_failing(self) -> None:
        report = {"summary": {"with_violations": 3}}
        with mock.patch.object(
            schema_instances, "run_schema_instance_report", return_value=report
        ) as run:
            node = load_test_catalog(ROOT).by_id["schema-instances"]
            self.assertIsNone(RUNNERS[node.runner](ROOT, node))
        run.assert_called_once_with(ROOT)


@unittest.skipUnless(
    JSONSCHEMA_AVAILABLE,
    "jsonschema is only in the pinned development image; ./hfx ci verify runs this case",
)
class RealValidatorTests(unittest.TestCase):
    def test_draft_2020_12_registry_and_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [
                _write(root, "schemas/example.schema.json", SCHEMA),
                _write(root, "data/clean.json", {"$schema": "../schemas/example.schema.json", "name": "a"}),
                _write(
                    root,
                    "data/bad.json",
                    {
                        "$schema": "../schemas/example.schema.json",
                        "name": PRIVATE_VALUE,
                        "when": "yesterday",
                        "devices": {PRIVATE_KEY: "not a number"},
                        PRIVATE_KEY: True,
                    },
                ),
            ]
            validator_for, version = jsonschema_validator_factory(root, files)
            report = build_report(root, validator_for, validator_version=version, files=files)
            by_path = {entry["path"]: entry for entry in report["documents"]}
            self.assertEqual(by_path["schemas/example.schema.json"]["status"], "clean")
            self.assertEqual(by_path["data/clean.json"]["status"], "clean")
            keywords = sorted(item["keyword"] for item in by_path["data/bad.json"]["violations"])
            self.assertEqual(keywords, ["additionalProperties", "format", "type"])
            rendered = json.dumps(report)
            self.assertNotIn(PRIVATE_KEY, rendered)
            self.assertNotIn(PRIVATE_VALUE, rendered)
            self.assertNotIn("yesterday", rendered)

    def test_closed_branch_schemas_accept_their_own_keys_only(self) -> None:
        # An object schema that closes itself with additionalProperties must
        # name its keys beside that keyword; keys named only inside oneOf
        # branches count as unknown. The source-validated catalog is the
        # regression case.
        catalog_path = ROOT / "integrations/native-route/source-validated.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        schema = json.loads(
            (ROOT / "schemas/source-validated-device-catalog.schema.json").read_text(encoding="utf-8")
        )
        validator_for, _version = jsonschema_validator_factory(ROOT)
        validator = validator_for(schema)
        self.assertEqual(list(validator.iter_errors(catalog)), [])
        unavailable = next(
            index
            for index, device in enumerate(catalog["devices"])
            if device["integrations"]["openrazer"]["state"] != "source-validated"
        )
        for location in ("hardware_assurance", "openrazer"):
            mutated = json.loads(json.dumps(catalog))
            device = mutated["devices"][unavailable]
            target = device["hardware_assurance"] if location == "hardware_assurance" else device["integrations"]["openrazer"]
            target[PRIVATE_KEY] = True
            with self.subTest(location=location):
                self.assertNotEqual(list(validator.iter_errors(mutated)), [])


if __name__ == "__main__":
    unittest.main()
