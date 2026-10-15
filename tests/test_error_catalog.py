# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from hfxdev.atlas import load_repository_atlas
from hfxdev.errors import MAX_ERRORS, MAX_SAFE_DETAIL_FIELDS, load_error_catalog
from hfxdev.generators.errors import cpp_catalog, markdown, python_catalog, rust_catalog
from hfxdev.model import ModelError
from hyperflux_sdk.generated.error_catalog import (
    ERRORS,
    ERRORS_BY_CODE,
    REMEDIATIONS,
    ErrorCode,
    RetryPolicy,
    SideEffectCertaintyPolicy,
    validate_safe_details,
)


class ErrorCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads((ROOT / "errors" / "catalog.json").read_text(encoding="utf-8"))

    def assert_catalog_rejected(self, value: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "errors").mkdir()
            (root / "schemas").mkdir()
            (root / "errors" / "catalog.json").write_text(
                json.dumps(value, indent=2) + "\n",
                encoding="utf-8",
            )
            (root / "schemas" / "domain-catalog.json").write_text(
                (ROOT / "schemas" / "domain-catalog.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(ModelError):
                load_error_catalog(root)

    def test_catalog_and_all_generated_outputs_are_deterministic(self) -> None:
        first = load_error_catalog(ROOT)
        second = load_error_catalog(ROOT)
        self.assertEqual(first, second)
        outputs = {
            ROOT / "crates" / "hfx-errors" / "src" / "generated.rs": rust_catalog(first),
            ROOT / "sdk" / "cpp" / "include" / "hyperflux" / "generated" / "error_catalog.hpp": cpp_catalog(first),
            ROOT / "sdk" / "python" / "hyperflux_sdk" / "generated" / "error_catalog.py": python_catalog(first),
            ROOT / "docs" / "generated" / "error-catalog.md": markdown(
                first, load_repository_atlas(ROOT)
            ),
        }
        for path, expected in outputs.items():
            with self.subTest(path=path):
                self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_atlas_consumers_match_source_that_emits_catalog_codes(self) -> None:
        catalog = load_error_catalog(ROOT)
        atlas = load_repository_atlas(ROOT)
        codes = re.compile(
            "|".join(re.escape(item.code) for item in catalog.errors)
            + r"|generated[./]error_catalog|hfx_errors"
        )
        excluded = {"errors", "rust-workspace", "tests"}
        paths = sorted((node.path, node.id) for node in atlas.nodes)
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        ).stdout.decode("utf-8").split("\0")
        emitting: set[str] = set()
        for relative in tracked:
            if not relative.endswith((".rs", ".py", ".cpp", ".hpp", ".h", ".c", ".sh")):
                continue
            if "/tests/" in f"/{relative}" or "/generated/" in relative:
                continue
            owners = [node for path, node in paths if relative.startswith(f"{path}/")]
            if not owners:
                continue
            owner = max(owners, key=lambda node: len(atlas.by_id[node].path))
            if owner in excluded:
                continue
            if codes.search((ROOT / relative).read_text(encoding="utf-8")):
                emitting.add(owner)
        self.assertEqual(set(atlas.used_by["errors"]), emitting)
        page = (ROOT / "docs" / "generated" / "error-catalog.md").read_text(encoding="utf-8")
        for identifier in atlas.used_by["errors"]:
            with self.subTest(consumer=identifier):
                self.assertIn(f"- {atlas.by_id[identifier].title} (`", page)

    def test_catalog_is_bounded_cross_referenced_and_privacy_safe(self) -> None:
        catalog = load_error_catalog(ROOT)
        self.assertLessEqual(len(catalog.errors), MAX_ERRORS)
        remediation_ids = {item.identifier for item in catalog.remediations}
        for item in catalog.errors:
            with self.subTest(code=item.code):
                self.assertIn(item.remediation_id, remediation_ids)
                self.assertLessEqual(len(item.safe_detail_fields), MAX_SAFE_DETAIL_FIELDS)
                self.assertIn(item.privacy, {"public", "public-summary"})
                self.assertEqual(
                    item.docs_path,
                    f"docs/generated/error-catalog.md#{item.code.lower()}",
                )
                for field in item.safe_detail_fields:
                    self.assertIn(field.privacy, {"public", "public-summary"})
        for item in self.raw["errors"]:
            for field in item["safe_detail_fields"]:
                if field["type"] == "u64-decimal":
                    self.assertIsInstance(field["maximum_value"], str)

    def test_retry_cannot_replay_an_uncertain_hardware_operation(self) -> None:
        for item in ERRORS:
            if item.side_effect_certainty_policy in {
                SideEffectCertaintyPolicy.POSSIBLE,
                SideEffectCertaintyPolicy.PARTIAL,
            }:
                self.assertIs(item.retry_policy, RetryPolicy.OUTCOME_LOOKUP_ONLY)
            if item.retry_policy is RetryPolicy.BOUNDED_BACKOFF:
                self.assertIs(
                    item.side_effect_certainty_policy,
                    SideEffectCertaintyPolicy.MUST_BE_NONE,
                )

        unsafe = deepcopy(self.raw)
        transport = next(item for item in unsafe["errors"] if item["code"] == "HFX-TRANSPORT-002")
        transport["retry_policy"] = "bounded-backoff"
        self.assert_catalog_rejected(unsafe)

    def test_unknown_keys_unbounded_fields_and_private_details_are_rejected(self) -> None:
        unknown = deepcopy(self.raw)
        unknown["errors"][0]["legacy_message"] = "not authoritative"
        self.assert_catalog_rejected(unknown)

        unbounded = deepcopy(self.raw)
        request_id = next(
            field
            for field in unbounded["errors"][0]["safe_detail_fields"]
            if field["name"] == "request_id"
        )
        request_id["maximum_length"] = None
        self.assert_catalog_rejected(unbounded)

        private = deepcopy(self.raw)
        private["errors"][0]["safe_detail_fields"][0]["privacy"] = "private"
        self.assert_catalog_rejected(private)

    def test_generated_python_lookup_and_safe_detail_validation(self) -> None:
        self.assertEqual(len(ERRORS_BY_CODE), len(ERRORS))
        self.assertTrue(REMEDIATIONS)
        validate_safe_details(
            ErrorCode.HFX_GENERATION_001,
            {
                "active_generation": "18446744073709551615",
                "requested_generation": "1",
            },
        )
        with self.assertRaises(ValueError):
            validate_safe_details(
                ErrorCode.HFX_GENERATION_001,
                {"active_generation": "01", "requested_generation": "1"},
            )
        with self.assertRaises(ValueError):
            validate_safe_details(
                ErrorCode.HFX_TRANSPORT_002,
                {"transaction_id": "transaction-1", "raw_payload": "forbidden"},
            )

    def test_lifecycle_replacements_must_be_declared_and_non_self_referential(self) -> None:
        invalid = deepcopy(self.raw)
        entry = invalid["errors"][0]
        entry["lifecycle"] = {
            "state": "deprecated",
            "introduced_in": "0.0.0-dev.1",
            "deprecated_in": "0.1.0",
            "replacement_code": entry["code"],
        }
        self.assert_catalog_rejected(invalid)


if __name__ == "__main__":
    unittest.main()
