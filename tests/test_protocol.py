# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.protocol import cpp_types, python_types, rust_types
from hfxdev.model import ModelError
from hfxdev.protocol import (
    RESERVED_FIELD_NAMES,
    load_protocol_catalog,
    load_protocol_registry,
)


class ProtocolCompilerTests(unittest.TestCase):
    def test_catalog_is_versioned_and_generated_deterministically(self) -> None:
        registry = load_protocol_registry(ROOT)
        self.assertEqual(registry.current_version, 7)
        self.assertEqual([item.version for item in registry.versions], [1, 2, 3, 4, 5, 6, 7])
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
        v3_transaction = next(
            record
            for record in registry.versions[2].catalog.records
            if record.name == "TransactionRequest"
        )
        v4_snapshot = next(
            record
            for record in registry.versions[3].catalog.records
            if record.name == "ReceiverSnapshot"
        )
        v5 = registry.versions[4].catalog
        v6 = registry.versions[5].catalog
        v7 = registry.versions[6].catalog
        self.assertNotIn(
            "device_profiles", [field.name for field in v1_transaction.fields]
        )
        self.assertIn(
            "device_profiles", [field.name for field in v2_transaction.fields]
        )
        self.assertNotIn(
            "stable_intents", [field.name for field in v2_transaction.fields]
        )
        self.assertIn(
            "stable_intents", [field.name for field in v3_transaction.fields]
        )
        self.assertNotIn(
            "atomic-transactions", registry.versions[0].served_features
        )
        self.assertIn(
            "profile-bound-transactions", registry.versions[1].served_features
        )
        self.assertNotIn(
            "semantic-stable-lighting", registry.versions[1].served_features
        )
        self.assertIn(
            "semantic-stable-lighting", registry.versions[2].served_features
        )
        self.assertIn(
            "snapshot-profile-bindings", registry.versions[3].served_features
        )
        self.assertIn("profile_digest", [field.name for field in v4_snapshot.fields])
        self.assertIn("integration-view-projection", registry.versions[4].served_features)
        self.assertIn("ControllerOwnership", [union.name for union in v5.unions])
        self.assertIn(
            "return-lifecycle-projection", registry.versions[5].served_features
        )
        self.assertIn("ReturnEpoch", [record.name for record in v6.records])
        v6_endpoint = next(
            record for record in v6.records if record.name == "EndpointSnapshot"
        )
        self.assertIn("return_epoch", [field.name for field in v6_endpoint.fields])
        self.assertIn("native-receiver-tunnel", registry.versions[6].served_features)
        self.assertEqual(
            [
                method.name
                for method in v7.methods
                if method.required_feature == "native-receiver-tunnel"
            ],
            ["native-enumerate", "native-transact", "native-watch"],
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

    def test_generated_declarations_follow_record_and_union_dependencies(self) -> None:
        catalog = load_protocol_catalog(ROOT)
        rust = rust_types(catalog)
        cpp = cpp_types(catalog)
        python = python_types(catalog)
        self.assertIn(
            "Self::Snapshot(envelope)\n"
            "            | Self::IntegrationView(envelope)\n"
            "            | Self::Diagnostics(envelope)\n"
            "            | Self::NativeEnumerate(envelope) => &envelope.request_id,",
            rust,
        )
        self.assertLess(
            cpp.index("using ControllerOwnership ="),
            cpp.index("struct ControllerView"),
        )
        self.assertLess(
            python.index("ControllerOwnership: TypeAlias ="),
            python.index("class ControllerView:"),
        )

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



class RetiredProtocolRegistryTests(unittest.TestCase):
    """A retired version number must never come back with another catalog."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        shutil.copytree(ROOT / "protocol", self.root / "protocol")
        (self.root / "schemas").mkdir()
        shutil.copy2(
            ROOT / "schemas" / "domain-catalog.json",
            self.root / "schemas" / "domain-catalog.json",
        )
        self.registry = json.loads(
            (ROOT / "protocol" / "registry.json").read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _load(self, registry: dict[str, Any]):
        (self.root / "protocol" / "registry.json").write_text(
            json.dumps(registry, indent=2) + "\n", encoding="utf-8"
        )
        return load_protocol_registry(self.root)

    def _retire_oldest(self) -> tuple[dict[str, Any], dict[str, Any]]:
        registry = copy.deepcopy(self.registry)
        oldest = registry["versions"][0]
        self.assertNotEqual(oldest["version"], registry["current_version"])
        registry["versions"] = registry["versions"][1:]
        entry = {
            "version": oldest["version"],
            "retired_in": "9.9.0",
            "sha256": oldest["sha256"],
        }
        registry["retired"] = sorted(
            [*registry.get("retired", []), entry], key=lambda item: item["version"]
        )
        return registry, entry

    def test_retired_versions_load_and_stay_out_of_the_served_set(self) -> None:
        registry, entry = self._retire_oldest()
        loaded = self._load(registry)
        retired = {item.version: item for item in loaded.retired}
        self.assertEqual(retired[entry["version"]].retired_in, "9.9.0")
        self.assertEqual(retired[entry["version"]].source_sha256, entry["sha256"])
        self.assertNotIn(
            entry["version"], [item.version for item in loaded.versions]
        )

    def test_retired_version_cannot_be_served_again(self) -> None:
        registry, entry = self._retire_oldest()
        current = next(
            item
            for item in registry["versions"]
            if item["version"] == registry["current_version"]
        )
        registry["retired"] = sorted(
            [
                *registry["retired"],
                {
                    "version": current["version"],
                    "retired_in": "9.9.0",
                    "sha256": current["sha256"],
                },
            ],
            key=lambda item: item["version"],
        )
        with self.assertRaisesRegex(ModelError, "cannot be served again"):
            self._load(registry)

    def test_retired_entries_are_unique_sorted_and_exact(self) -> None:
        registry, entry = self._retire_oldest()
        duplicate = copy.deepcopy(registry)
        duplicate["retired"].append(dict(entry))
        with self.assertRaisesRegex(ModelError, "protocol retired version"):
            self._load(duplicate)

        unsorted = copy.deepcopy(registry)
        unsorted["retired"].append(
            {"version": 65_535, "retired_in": "9.9.0", "sha256": entry["sha256"]}
        )
        unsorted["retired"].reverse()
        with self.assertRaisesRegex(ModelError, "must be sorted"):
            self._load(unsorted)

        bad_release = copy.deepcopy(registry)
        bad_release["retired"][-1]["retired_in"] = "2.2"
        with self.assertRaisesRegex(ModelError, "release version"):
            self._load(bad_release)

        bad_digest = copy.deepcopy(registry)
        bad_digest["retired"][-1]["sha256"] = "0" * 63
        with self.assertRaisesRegex(ModelError, "invalid source digest"):
            self._load(bad_digest)

        unknown_field = copy.deepcopy(registry)
        unknown_field["retired"][-1]["catalog"] = "v1/catalog.json"
        with self.assertRaisesRegex(ModelError, "unknown fields catalog"):
            self._load(unknown_field)


if __name__ == "__main__":
    unittest.main()
