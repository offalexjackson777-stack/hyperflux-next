# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.profiles import (
    _rust_qualification_migrations,
    markdown as profile_markdown,
)
from hfxdev.model import ModelError
from hfxdev.profiles import (
    _runtime_profile_digest,
    _validate_capabilities,
    _validate_candidates,
    _validate_lifecycle_policy_catalog,
    _validate_profiles,
    compiled_catalog,
    composition_fixtures,
    load_profile_inputs,
)
from hfxdev.source_validated import load_source_validated_catalog
from hfxdev.source_validated_catalog import (
    PHYSICALLY_PROVEN,
    SOURCE_QUALIFIED_OPERATIONAL,
    UPSTREAM_BLOCKED_OR_RESEARCH_ONLY,
    public_support_tier,
    public_support_tier_counts,
)


class ProfileCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inputs = load_profile_inputs(ROOT)

    def validation_copies(self) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
        profiles = deepcopy(list(self.inputs.profiles))
        capability_index = _validate_capabilities(deepcopy(self.inputs.capabilities))
        claim_index = {claim["id"]: claim for claim in deepcopy(self.inputs.evidence["claims"])}
        return profiles, capability_index, claim_index

    def lifecycle_policy_index(self) -> dict[str, dict]:
        return _validate_lifecycle_policy_catalog(
            deepcopy(self.inputs.lifecycle_policies)
        )

    def runtime_profile_digest(
        self,
        profile: dict,
        capability_index: dict[str, dict],
        lifecycle_policy_index: dict[str, dict] | None = None,
    ) -> str:
        policies = (
            self.lifecycle_policy_index()
            if lifecycle_policy_index is None
            else lifecycle_policy_index
        )
        return _runtime_profile_digest(
            profile,
            capability_index,
            policies,
        )

    def validate_profiles(
        self,
        profiles: list[dict],
        capability_index: dict[str, dict],
        claim_index: dict[str, dict],
        lifecycle_policy_index: dict[str, dict] | None = None,
    ) -> None:
        policies = (
            self.lifecycle_policy_index()
            if lifecycle_policy_index is None
            else lifecycle_policy_index
        )
        _validate_profiles(
            profiles,
            capability_index,
            claim_index,
            policies,
        )

    def test_canonical_profiles_compile_deterministically(self) -> None:
        first = compiled_catalog(ROOT)
        second = compiled_catalog(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(len(first["profiles"]), 4)
        self.assertTrue(first["composition_policy"]["unknown_children_are_read_only"])
        digests = [profile["runtime_sha256"] for profile in first["profiles"]]
        self.assertEqual(len(digests), len(set(digests)))
        self.assertTrue(all(len(digest) == 64 for digest in digests))

    def test_supported_hardware_renders_three_truthful_support_tiers(self) -> None:
        source_validated = load_source_validated_catalog(ROOT)
        document = profile_markdown(compiled_catalog(ROOT), source_validated)
        self.assertIn("**Physically proven:** 2", document)
        self.assertIn("**Source-qualified operational candidates:** 8", document)
        self.assertIn("**Upstream-blocked or research-only:** 2", document)
        self.assertEqual(document.count("| Razer "), 12)
        self.assertIn("`1532:00ab` / `1532:00aa`", document)
        self.assertIn("`1532:02d2` / `1532:02d4`", document)
        self.assertIn("Upstream support pending for openrazer", document)
        self.assertIn("not a runtime allowlist", document)
        self.assertNotIn("Admitted methods", document)

        self.assertEqual(
            public_support_tier_counts(source_validated["devices"]),
            {
                PHYSICALLY_PROVEN: 2,
                SOURCE_QUALIFIED_OPERATIONAL: 8,
                UPSTREAM_BLOCKED_OR_RESEARCH_ONLY: 2,
            },
        )
        contradictory = deepcopy(
            next(
                device
                for device in source_validated["devices"]
                if device["operational_state"] == "backend-required"
            )
        )
        contradictory["hardware_assurance"] = {
            "state": "hardware-verified",
            "profile_id": "child.fixture",
        }
        with self.assertRaisesRegex(ModelError, "blocked application path"):
            public_support_tier(contradictory)

    def test_receiver_native_exchange_is_declarative_bounded_and_digest_bound(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        native = receiver["transport"]["native_exchange"]
        self.assertEqual(native["protocol_family"], "razer-feature-report-v1")
        self.assertEqual(native["report_bytes"], 90)
        self.assertEqual(native["set_report_index"], 0)
        self.assertEqual(native["response_report_index"], 0)
        self.assertEqual(
            native["report_format"],
            {
                "request_status": {"offset": 0, "value": 0},
                "response_status": {"offset": 0, "minimum": 1, "maximum": 5},
                "protocol_type": {"offset": 4, "value": 0},
                "data_size": {"offset": 5, "maximum": 80},
                "checksum": {
                    "algorithm": "xor8",
                    "start_offset": 2,
                    "end_offset_exclusive": 88,
                    "result_offset": 88,
                },
                "trailer": {"offset": 89, "value": 0},
                "transaction_id": {"offset": 1, "match_mask": 255},
            },
        )
        self.assertEqual(
            native["route_selector"],
            {
                "field": "transaction-id",
                "pointer_lane": 0x1F,
                "keyboard_lane": 0x9F,
                "restore_original_response": True,
            },
        )
        self.assertEqual(native["match_ranges"], [{"offset": 2, "length": 2}, {"offset": 6, "length": 2}])
        before = self.runtime_profile_digest(receiver, capabilities)
        native["response_retry_delay_us"] += 1
        self.validate_profiles(profiles, capabilities, claims)
        self.assertNotEqual(self.runtime_profile_digest(receiver, capabilities), before)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        receiver["transport"]["native_exchange"]["match_ranges"][1] = {
            "offset": 3,
            "length": 5,
        }
        with self.assertRaisesRegex(ModelError, "sorted and disjoint"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        receiver["transport"]["native_exchange"]["report_bytes"] = 89
        with self.assertRaisesRegex(ModelError, "exactly 90 bytes"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        checksum = receiver["transport"]["native_exchange"]["report_format"]["checksum"]
        checksum["result_offset"] = 87
        with self.assertRaisesRegex(ModelError, "checksum range is out of bounds"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        transaction = receiver["transport"]["native_exchange"]["report_format"][
            "transaction_id"
        ]
        transaction["match_mask"] = 256
        with self.assertRaisesRegex(ModelError, "transaction id is out of bounds"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        response_status = receiver["transport"]["native_exchange"]["report_format"][
            "response_status"
        ]
        response_status["minimum"] = 6
        with self.assertRaisesRegex(ModelError, "response status range is reversed"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        selector = receiver["transport"]["native_exchange"]["route_selector"]
        selector["keyboard_lane"] = selector["pointer_lane"]
        with self.assertRaisesRegex(ModelError, "must isolate endpoint lanes"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        selector = receiver["transport"]["native_exchange"]["route_selector"]
        selector["pointer_lane"] = 0
        with self.assertRaisesRegex(ModelError, "pointer_lane is invalid"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        selector = receiver["transport"]["native_exchange"]["route_selector"]
        selector["restore_original_response"] = False
        with self.assertRaisesRegex(ModelError, "must restore the application response"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_runtime_digest_is_profile_local_and_ignores_presentation_only_edits(self) -> None:
        profiles, capabilities, _ = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        mouse_before = self.runtime_profile_digest(mouse, capabilities)
        keyboard_before = self.runtime_profile_digest(keyboard, capabilities)

        mouse["presentation"]["model_key"] = "presentation-only-change"
        self.assertEqual(self.runtime_profile_digest(mouse, capabilities), mouse_before)

        mouse["transport"]["lighting"]["application_index_to_carrier"][0] = 0
        self.assertNotEqual(self.runtime_profile_digest(mouse, capabilities), mouse_before)
        self.assertEqual(self.runtime_profile_digest(keyboard, capabilities), keyboard_before)

    def test_child_presentation_is_exact_typed_and_route_bound(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["surprise"] = "drift"
        with self.assertRaisesRegex(ModelError, "unsupported keys: surprise"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["source_commit"] = "short"
        with self.assertRaisesRegex(ModelError, "40 lowercase hex"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["transport_variant"] = "wired"
        with self.assertRaisesRegex(ModelError, "no matching device route"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_non_child_presentation_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        receiver["presentation"] = deepcopy(
            next(profile for profile in profiles if profile["kind"] == "child")["presentation"]
        )
        with self.assertRaisesRegex(ModelError, "only child profiles"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_mouse_and_keyboard_require_no_sibling(self) -> None:
        for profile in self.inputs.profiles:
            if profile["kind"] == "child":
                self.assertEqual(profile["compatibility"]["required_sibling_kinds"], [])

    def test_exact_combination_key_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        profiles[0]["required_children"] = ["mouse", "keyboard"]
        with self.assertRaisesRegex(ModelError, "exact-combination keys are forbidden"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_child_sibling_dependency_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["required_sibling_kinds"] = ["keyboard"]
        with self.assertRaisesRegex(ModelError, "must not require a sibling"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_compatibility_is_kind_specific_bounded_and_route_checked(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["surprise"] = True
        with self.assertRaisesRegex(ModelError, "unsupported keys: surprise"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["routes"] = ["imaginary-route"]
        with self.assertRaisesRegex(ModelError, "invalid route kind"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_writable_capability_without_physical_evidence_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        claim = claims["claim.child.00cd.complete-led-map"]
        claim["evidence_level"] = "source-reviewed"
        with self.assertRaisesRegex(ModelError, "lacks public physical qualification"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_surface_usb_identity_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        surface = next(profile for profile in profiles if profile["kind"] == "surface")
        surface["identity"]["vendor_id"] = 0x1532
        surface["identity"]["product_id"] = 0x00CF
        with self.assertRaisesRegex(ModelError, "must not invent USB identity"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_incomplete_or_repeated_carrier_map_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["lighting"]["application_index_to_carrier"][1] = 1
        with self.assertRaisesRegex(ModelError, "repeats a receiver carrier"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_passive_decoder_is_lane_bound_disjoint_and_profile_local(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["passive"]["endpoint_lane"] = "keyboard"
        with self.assertRaisesRegex(ModelError, "passive lane contradicts"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        keyboard["transport"]["passive"]["route"]["unavailable"].append(3)
        keyboard["transport"]["passive"]["route"]["unavailable"].sort()
        with self.assertRaisesRegex(ModelError, "route values must be disjoint"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        keyboard["transport"]["passive"]["route"]["indeterminate"].append(3)
        keyboard["transport"]["passive"]["route"]["indeterminate"].sort()
        with self.assertRaisesRegex(ModelError, "route values must be disjoint"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        del mouse["transport"]["passive"]
        with self.assertRaisesRegex(ModelError, "passive telemetry requires a decoder"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_passive_semantics_participate_in_runtime_profile_digest(self) -> None:
        profiles, capabilities, _ = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        before = self.runtime_profile_digest(mouse, capabilities)
        mouse["transport"]["passive"]["contact"]["on_mat"] = [2]
        self.assertNotEqual(self.runtime_profile_digest(mouse, capabilities), before)

    def test_lifecycle_profile_evidence_is_policy_bounded_and_digest_bound(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        before = self.runtime_profile_digest(mouse, capabilities)
        sibling_before = self.runtime_profile_digest(keyboard, capabilities)
        mouse["transport"]["lifecycle"]["accelerator_evidence"] = [
            "battery-report",
            "contact-report",
            "endpoint-activity",
        ]
        self.validate_profiles(profiles, capabilities, claims)
        self.assertNotEqual(self.runtime_profile_digest(mouse, capabilities), before)
        self.assertEqual(self.runtime_profile_digest(keyboard, capabilities), sibling_before)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["lifecycle"]["return_policy_id"] = "missing.policy"
        with self.assertRaisesRegex(ModelError, "unknown return policy"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["passive"]["route"]["unavailable"] = [2]
        with self.assertRaisesRegex(ModelError, "contradicts passive route evidence"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        keyboard["transport"]["lifecycle"]["accelerator_evidence"].append("contact-report")
        keyboard["transport"]["lifecycle"]["accelerator_evidence"].sort()
        with self.assertRaisesRegex(ModelError, "contact return evidence lacks a decoder"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["lifecycle"]["open_evidence"] = ["route-return"]
        mouse["transport"]["lifecycle"]["accelerator_evidence"] = ["route-return"]
        with self.assertRaisesRegex(ModelError, "route-return evidence lacks departure values"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_lifecycle_policy_timing_is_central_bounded_and_authority_backed(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        before_mouse = self.runtime_profile_digest(mouse, capabilities)
        before_keyboard = self.runtime_profile_digest(keyboard, capabilities)

        catalog = deepcopy(self.inputs.lifecycle_policies)
        catalog["policies"][0]["retry_offsets_ms"][-1] = 6_100
        policies = _validate_lifecycle_policy_catalog(catalog)
        self.validate_profiles(profiles, capabilities, claims, policies)
        self.assertNotEqual(
            self.runtime_profile_digest(mouse, capabilities, policies), before_mouse
        )
        self.assertNotEqual(
            self.runtime_profile_digest(keyboard, capabilities, policies), before_keyboard
        )

        metadata_only = deepcopy(self.inputs.lifecycle_policies)
        metadata_only["policies"][0]["timing_authorities"][1]["summary"] += " More detail."
        metadata_policies = _validate_lifecycle_policy_catalog(metadata_only)
        self.assertEqual(
            self.runtime_profile_digest(mouse, capabilities, metadata_policies), before_mouse
        )

        invalid = deepcopy(self.inputs.lifecycle_policies)
        invalid["policies"][0]["retry_offsets_ms"] = [0, 100, 100]
        with self.assertRaisesRegex(ModelError, "retry offsets must be unique"):
            _validate_lifecycle_policy_catalog(invalid)

        invalid = deepcopy(self.inputs.lifecycle_policies)
        invalid["policies"][0]["quiescence_candidate_ms"] = 999
        with self.assertRaisesRegex(ModelError, "quiescence candidate is out of bounds"):
            _validate_lifecycle_policy_catalog(invalid)

        invalid = deepcopy(self.inputs.lifecycle_policies)
        invalid["policies"][0]["observation_jitter_ms"] = 60_000
        with self.assertRaisesRegex(ModelError, "observation jitter tolerance is out of bounds"):
            _validate_lifecycle_policy_catalog(invalid)

        invalid = deepcopy(self.inputs.lifecycle_policies)
        invalid["policies"][0]["timing_authorities"] = [
            invalid["policies"][0]["timing_authorities"][1]
        ]
        with self.assertRaisesRegex(ModelError, "timing values require explicit authorities"):
            _validate_lifecycle_policy_catalog(invalid)

    def test_qualification_migrations_are_exact_and_do_not_change_runtime_identity(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        with_migration = self.runtime_profile_digest(mouse, capabilities)
        del mouse["qualification_migrations"]
        self.assertEqual(self.runtime_profile_digest(mouse, capabilities), with_migration)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["qualification_migrations"][0]["preserve_stage_roles"] = ["identity"]
        with self.assertRaisesRegex(ModelError, "unsupported stage roles: identity"):
            self.validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["qualification_migrations"][0]["from_runtime_sha256"] = (
            self.runtime_profile_digest(mouse, capabilities)
        )
        with self.assertRaisesRegex(ModelError, "already equals the current runtime profile"):
            self.validate_profiles(profiles, capabilities, claims)

    def test_qualification_migration_rust_is_rustfmt_stable(self) -> None:
        migration = {
            "from_runtime_sha256": "a" * 64,
            "preserve_stage_roles": ["lighting", "map"],
        }
        self.assertEqual(
            _rust_qualification_migrations(0, []),
            [
                "const QUALIFICATION_MIGRATIONS_0: &[QualificationMigrationRecord] = &[];",
                "",
            ],
        )
        self.assertEqual(
            _rust_qualification_migrations(1, [migration]),
            [
                "const QUALIFICATION_MIGRATIONS_1: &[QualificationMigrationRecord] =",
                "    &[QualificationMigrationRecord {",
                f'        from_runtime_sha256: "{"a" * 64}",',
                '        preserve_stage_roles: &["lighting", "map"],',
                "    }];",
                "",
            ],
        )
        second = {
            "from_runtime_sha256": "b" * 64,
            "preserve_stage_roles": ["telemetry"],
        }
        self.assertEqual(
            _rust_qualification_migrations(2, [migration, second]),
            [
                "const QUALIFICATION_MIGRATIONS_2: &[QualificationMigrationRecord] = &[",
                "    QualificationMigrationRecord {",
                f'        from_runtime_sha256: "{"a" * 64}",',
                '        preserve_stage_roles: &["lighting", "map"],',
                "    },",
                "    QualificationMigrationRecord {",
                f'        from_runtime_sha256: "{"b" * 64}",',
                '        preserve_stage_roles: &["telemetry"],',
                "    },",
                "];",
                "",
            ],
        )

    def test_candidate_names_never_grant_writes_or_guess_pids(self) -> None:
        snapshots: dict[str, list[dict]] = {}
        for candidate in self.inputs.candidates:
            snapshots.setdefault(candidate["snapshot_id"], []).append(candidate)
            self.assertEqual(candidate["writable_capabilities"], [])
            self.assertNotIn("product_id", candidate)
        self.assertEqual(
            {snapshot: len(candidates) for snapshot, candidates in snapshots.items()},
            {
                "catalog.razer.hyperflux-v2.2026-07-13": 11,
                "catalog.razer.hyperflux-v2.2026-07-21": 12,
            },
        )

    def test_candidate_successor_preserves_historical_entries(self) -> None:
        catalogs = []
        for name in (
            "razer-hyperflux-v2-2026-07-13.json",
            "razer-hyperflux-v2-2026-07-21.json",
        ):
            path = ROOT / "profiles" / "candidates" / name
            value = json.loads(path.read_text(encoding="utf-8"))
            value["_source_path"] = path.relative_to(ROOT).as_posix()
            catalogs.append(value)
        _validate_candidates(deepcopy(catalogs))
        catalogs[1]["candidates"] = catalogs[1]["candidates"][1:]
        with self.assertRaisesRegex(ModelError, "successor silently removes candidates"):
            _validate_candidates(catalogs)

    def test_generated_compositions_cover_independent_and_unknown_children(self) -> None:
        cases = composition_fixtures(ROOT)["cases"]
        identifiers = {case["id"] for case in cases}
        self.assertTrue(any(identifier.endswith(":mouse-only") for identifier in identifiers))
        self.assertTrue(any(identifier.endswith(":keyboard-only") for identifier in identifiers))
        unknown = next(case for case in cases if case["id"].endswith(":unknown-child"))
        self.assertEqual(unknown["expected_unknown_writable_capabilities"], [])


if __name__ == "__main__":
    unittest.main()
