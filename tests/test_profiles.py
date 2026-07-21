# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.profiles import (
    _runtime_profile_digest,
    _validate_capabilities,
    _validate_profiles,
    compiled_catalog,
    composition_fixtures,
    load_profile_inputs,
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

    def test_canonical_profiles_compile_deterministically(self) -> None:
        first = compiled_catalog(ROOT)
        second = compiled_catalog(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(len(first["profiles"]), 4)
        self.assertTrue(first["composition_policy"]["unknown_children_are_read_only"])
        digests = [profile["runtime_sha256"] for profile in first["profiles"]]
        self.assertEqual(len(digests), len(set(digests)))
        self.assertTrue(all(len(digest) == 64 for digest in digests))

    def test_runtime_digest_is_profile_local_and_ignores_presentation_only_edits(self) -> None:
        profiles, capabilities, _ = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        keyboard = next(profile for profile in profiles if profile["device_kind"] == "keyboard")
        mouse_before = _runtime_profile_digest(mouse, capabilities)
        keyboard_before = _runtime_profile_digest(keyboard, capabilities)

        mouse["presentation"]["model_key"] = "presentation-only-change"
        self.assertEqual(_runtime_profile_digest(mouse, capabilities), mouse_before)

        mouse["transport"]["lighting"]["application_index_to_carrier"][0] = 0
        self.assertNotEqual(_runtime_profile_digest(mouse, capabilities), mouse_before)
        self.assertEqual(_runtime_profile_digest(keyboard, capabilities), keyboard_before)

    def test_child_presentation_is_exact_typed_and_route_bound(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["surprise"] = "drift"
        with self.assertRaisesRegex(ModelError, "unsupported keys: surprise"):
            _validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["source_commit"] = "short"
        with self.assertRaisesRegex(ModelError, "40 lowercase hex"):
            _validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["presentation"]["transport_variant"] = "wired"
        with self.assertRaisesRegex(ModelError, "no matching device route"):
            _validate_profiles(profiles, capabilities, claims)

    def test_non_child_presentation_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        receiver = next(profile for profile in profiles if profile["kind"] == "receiver")
        receiver["presentation"] = deepcopy(
            next(profile for profile in profiles if profile["kind"] == "child")["presentation"]
        )
        with self.assertRaisesRegex(ModelError, "only child profiles"):
            _validate_profiles(profiles, capabilities, claims)

    def test_mouse_and_keyboard_require_no_sibling(self) -> None:
        for profile in self.inputs.profiles:
            if profile["kind"] == "child":
                self.assertEqual(profile["compatibility"]["required_sibling_kinds"], [])

    def test_exact_combination_key_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        profiles[0]["required_children"] = ["mouse", "keyboard"]
        with self.assertRaisesRegex(ModelError, "exact-combination keys are forbidden"):
            _validate_profiles(profiles, capabilities, claims)

    def test_child_sibling_dependency_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["required_sibling_kinds"] = ["keyboard"]
        with self.assertRaisesRegex(ModelError, "must not require a sibling"):
            _validate_profiles(profiles, capabilities, claims)

    def test_compatibility_is_kind_specific_bounded_and_route_checked(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["surprise"] = True
        with self.assertRaisesRegex(ModelError, "unsupported keys: surprise"):
            _validate_profiles(profiles, capabilities, claims)

        profiles, capabilities, claims = self.validation_copies()
        child = next(profile for profile in profiles if profile["kind"] == "child")
        child["compatibility"]["routes"] = ["imaginary-route"]
        with self.assertRaisesRegex(ModelError, "invalid route kind"):
            _validate_profiles(profiles, capabilities, claims)

    def test_writable_capability_without_physical_evidence_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        claim = claims["claim.child.00cd.complete-led-map"]
        claim["evidence_level"] = "source-reviewed"
        with self.assertRaisesRegex(ModelError, "lacks public physical qualification"):
            _validate_profiles(profiles, capabilities, claims)

    def test_surface_usb_identity_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        surface = next(profile for profile in profiles if profile["kind"] == "surface")
        surface["identity"]["vendor_id"] = 0x1532
        surface["identity"]["product_id"] = 0x00CF
        with self.assertRaisesRegex(ModelError, "must not invent USB identity"):
            _validate_profiles(profiles, capabilities, claims)

    def test_incomplete_or_repeated_carrier_map_is_rejected(self) -> None:
        profiles, capabilities, claims = self.validation_copies()
        mouse = next(profile for profile in profiles if profile["device_kind"] == "mouse")
        mouse["transport"]["lighting"]["application_index_to_carrier"][1] = 1
        with self.assertRaisesRegex(ModelError, "repeats a receiver carrier"):
            _validate_profiles(profiles, capabilities, claims)

    def test_candidate_names_never_grant_writes_or_guess_pids(self) -> None:
        self.assertEqual(len(self.inputs.candidates), 11)
        for candidate in self.inputs.candidates:
            self.assertEqual(candidate["writable_capabilities"], [])
            self.assertNotIn("product_id", candidate)

    def test_generated_compositions_cover_independent_and_unknown_children(self) -> None:
        cases = composition_fixtures(ROOT)["cases"]
        identifiers = {case["id"] for case in cases}
        self.assertTrue(any(identifier.endswith(":mouse-only") for identifier in identifiers))
        self.assertTrue(any(identifier.endswith(":keyboard-only") for identifier in identifiers))
        unknown = next(case for case in cases if case["id"].endswith(":unknown-child"))
        self.assertEqual(unknown["expected_unknown_writable_capabilities"], [])


if __name__ == "__main__":
    unittest.main()
