# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.protocol import cpp_types, python_types, rust_types
from hfxdev.protocol import (
    RESERVED_FIELD_NAMES,
    load_protocol_catalog,
    load_protocol_registry,
)


class ProtocolCompilerTests(unittest.TestCase):
    def test_catalog_is_versioned_and_generated_deterministically(self) -> None:
        registry = load_protocol_registry(ROOT)
        self.assertEqual(registry.current_version, 2)
        self.assertEqual([item.version for item in registry.versions], [1, 2])
        self.assertEqual(
            registry.versions[0].source_sha256,
            "ed3df1f1627ca8a836f509ead3ab7dc7a4cbc6116f6da4accb2cc53e7500859f",
        )
        self.assertTrue(
            all(len(item.source_sha256) == 64 for item in registry.versions)
        )
        v1_transaction = next(
            record
            for record in registry.versions[0].catalog.records
            if record.name == "TransactionRequest"
        )
        v2_transaction = next(
            record
            for record in registry.versions[1].catalog.records
            if record.name == "TransactionRequest"
        )
        self.assertNotIn(
            "device_profiles", [field.name for field in v1_transaction.fields]
        )
        self.assertIn(
            "device_profiles", [field.name for field in v2_transaction.fields]
        )
        first = load_protocol_catalog(ROOT)
        second = load_protocol_catalog(ROOT)
        self.assertEqual(first, second)
        self.assertLessEqual(first.minimum_version, first.maximum_version)
        self.assertEqual(first.max_message_bytes, 1_048_576)
        self.assertEqual(first.max_json_depth, 128)
        self.assertEqual(rust_types(first), rust_types(second))
        self.assertEqual(cpp_types(first), cpp_types(second))
        self.assertEqual(python_types(first), python_types(second))

    def test_all_collections_and_protocol_methods_are_bounded(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        for record in catalog.records:
            for field in record.fields:
                with self.subTest(record=record.name, field=field.name):
                    if field.many:
                        self.assertIsNotNone(field.max_items)
                        self.assertLessEqual(field.max_items, 4096)
        protocol_types = {record.name for record in catalog.records}
        protocol_types.update(union.name for union in catalog.unions)
        for method in catalog.methods:
            self.assertIn(method.request, protocol_types)
            self.assertIn(method.response, protocol_types)

    def test_resource_keys_are_device_scoped_and_not_mouse_keyboard_specific(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        resource = next(record for record in catalog.records if record.name == "ResourceKey")
        self.assertEqual(
            [field.name for field in resource.fields],
            ["receiver_id", "generation_id", "device_id", "kind"],
        )
        domain = (ROOT / "schemas" / "domain-catalog.json").read_text(encoding="utf-8")
        self.assertNotIn('"mouse-lighting"', domain)
        self.assertNotIn('"keyboard-lighting"', domain)

    def test_no_generated_method_is_an_executable_command(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        for method in catalog.methods:
            with self.subTest(method=method.name):
                self.assertNotIn(" ", method.name)
                self.assertNotIn("/", method.name)
                self.assertNotIn(";", method.name)

    def test_field_names_are_portable_across_generated_languages(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        for record in catalog.records:
            for field in record.fields:
                with self.subTest(record=record.name, field=field.name):
                    self.assertNotIn(field.name, RESERVED_FIELD_NAMES)

    def test_outcomes_use_tagged_unions_instead_of_optional_field_combinations(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        unions = {union.name: union for union in catalog.unions}
        self.assertEqual(
            [variant.wire for variant in unions["LeaseResult"].variants],
            ["granted", "conflict", "rejected"],
        )
        self.assertEqual(
            [variant.wire for variant in unions["TransactionResult"].variants],
            ["progress", "terminal", "unavailable"],
        )


if __name__ == "__main__":
    unittest.main()
