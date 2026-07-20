# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.protocol import cpp_types, python_types, rust_types
from hfxdev.protocol import RESERVED_FIELD_NAMES, load_protocol_catalog


class ProtocolCompilerTests(unittest.TestCase):
    def test_catalog_is_versioned_and_generated_deterministically(self) -> None:
        first = load_protocol_catalog(ROOT)
        second = load_protocol_catalog(ROOT)
        self.assertEqual(first, second)
        self.assertLessEqual(first.minimum_version, first.maximum_version)
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
        records = {record.name for record in catalog.records}
        for method in catalog.methods:
            self.assertIn(method.request, records)
            self.assertIn(method.response, records)

    def test_resource_keys_are_device_scoped_and_not_mouse_keyboard_specific(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        resource = next(record for record in catalog.records if record.name == "ResourceKey")
        self.assertEqual([field.name for field in resource.fields], ["device_id", "kind"])
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


if __name__ == "__main__":
    unittest.main()
