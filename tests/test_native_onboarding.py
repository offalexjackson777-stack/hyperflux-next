# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hfxdev.native_onboarding as onboarding
import hfxdev.private_artifact as private_artifact
from hfxdev.knowledge import load_knowledge_inputs
from hfxdev.model import ModelError, sha256_file
from hfxdev.native_onboarding import (
    build_onboarding_draft,
    build_promotion_assessment,
    build_promotion_from_files,
    load_route_observation,
    load_onboarding_draft,
    validate_onboarding_draft,
    write_onboarding_draft,
)
from hfxdev.native_probe import load_native_probe_catalog
from hfxdev.private_artifact import write_new_private_json


REVISION = "1" * 40
CREATED_AT = "2026-07-25T00:00:01Z"


def observation(
    product_id: int,
    *,
    device_kind: str = "mouse",
    protocol_family: str = "razer-feature-report-v1",
    report_bytes: int = 90,
) -> dict:
    return {
        "$schema": "https://hyperflux.dev/schemas/native-route-observation-v1.json",
        "schema": "hyperflux-native-route-observation-v1",
        "observed_at": "2026-07-25T00:00:00Z",
        "source": "bridge-native-route-v7",
        "receiver_generation": 7,
        "vendor_id": 0x1532,
        "product_id": product_id,
        "device_kind": device_kind,
        "route_kind": "hyperflux-wireless",
        "route_state": "available",
        "access": "candidate",
        "protocol_family": protocol_family,
        "report_bytes": report_bytes,
        "privacy": {
            "serials_included": False,
            "route_tokens_included": False,
            "host_paths_included": False,
            "raw_reports_included": False,
        },
    }


def passed_parity(profile_id: str, source_revision: str) -> dict:
    catalog = load_native_probe_catalog(ROOT)
    device = next(
        value for value in catalog["devices"] if value["profile_id"] == profile_id
    )
    observations = []
    for probe in device["probes"]:
        request = probe["request"]
        observations.append(
            {
                "method": probe["method"],
                "origin": "receiver-route",
                "comparison": probe["comparison"],
                "request": {
                    "sha256": request["normalized_sha256"],
                    "command_class": request["command_class"],
                    "command_id": request["command_id"],
                    "data_size": request["data_size"],
                    "upstream_signature_matched": True,
                    "complete_capture": True,
                    "reconstructed_from_upstream": False,
                },
                "response": {
                    "status": 2,
                    "structural_match": True,
                    "complete_capture": True,
                    "summary": {},
                },
                "receiver_comparison": {
                    "attempts": 1,
                    "unchanged_request": True,
                    "standard_response": True,
                    "semantic_parity": True,
                },
            }
        )
    required = [probe["method"] for probe in device["probes"]]
    return {
        "$schema": "https://hyperflux.dev/schemas/native-transport-parity-evidence-v1.json",
        "schema": "hyperflux-native-transport-parity-evidence-v1",
        "created_at": CREATED_AT,
        "source_revision": source_revision,
        "profile_id": profile_id,
        "upstream_commit": catalog["upstream"]["commit"],
        "catalog_sha256": sha256_file(
            ROOT / "integrations" / "openrazer" / "native-probe-catalog.json"
        ),
        "safety": {
            "read_only": True,
            "settings_changed": False,
            "serials_persisted": False,
            "raw_reports_persisted": False,
            "private_paths_persisted": False,
            "network_upload": False,
            "file_mode": "0600",
        },
        "observations": observations,
        "coverage": {
            "required": required,
            "passed": required,
            "missing": [],
            "failed": [],
        },
        "conclusion": "passed",
    }


class NativeOnboardingTests(unittest.TestCase):
    def draft(self, value: dict) -> dict:
        return build_onboarding_draft(
            ROOT,
            value,
            created_at=CREATED_AT,
            source_revision=REVISION,
        )

    def test_existing_device_is_exact_but_remains_non_writable(self) -> None:
        draft = self.draft(observation(0x00CD))
        self.assertEqual(draft["assessment"]["state"], "candidate")
        self.assertEqual(draft["assessment"]["topology_state"], "agreement")
        self.assertEqual(
            draft["assessment"]["existing_profile_id"],
            "child.razer.basilisk-v3-pro-35k.00cd",
        )
        self.assertFalse(draft["assessment"]["writable"])
        self.assertFalse(
            draft["candidate_profile"]["native_control"]["write_authority"]
        )

    def test_second_same_protocol_device_onboards_without_engine_or_map_changes(self) -> None:
        value = load_route_observation(
            ROOT
            / "tests"
            / "fixtures"
            / "native-onboarding"
            / "viper-ultimate-007b-observation.json"
        )
        draft = self.draft(value)
        self.assertEqual(draft["assessment"]["state"], "candidate")
        self.assertIsNone(draft["assessment"]["existing_profile_id"])
        self.assertEqual(
            draft["candidate_profile"]["profile_id"],
            "child.razer.viper-ultimate.007b",
        )
        self.assertEqual(
            draft["candidate_profile"]["topology"]["state"], "agreement"
        )
        self.assertIsNone(
            draft["candidate_profile"]["topology"]["hyperflux_carrier_map"]
        )
        self.assertFalse(draft["promotion_contract"]["engine_changes_allowed"])
        self.assertIn(
            "crates/hfx-bridge",
            draft["promotion_contract"]["immutable_engine_domains"],
        )

    def test_upstream_topology_disagreement_is_explicit_and_blocking(self) -> None:
        draft = self.draft(observation(0x00D7))
        self.assertEqual(draft["assessment"]["state"], "conflicted")
        self.assertEqual(draft["assessment"]["topology_state"], "conflict")
        self.assertIn(
            "upstream-topology-disagreement", draft["assessment"]["reason_codes"]
        )
        self.assertEqual(draft["promotion_contract"]["decision"], "blocked")

    def test_different_report_format_stops_as_backend_required(self) -> None:
        draft = self.draft(
            observation(0x007B, protocol_family="different-family", report_bytes=64)
        )
        self.assertEqual(draft["assessment"]["state"], "backend-required")
        self.assertFalse(draft["assessment"]["same_protocol"])
        self.assertEqual(
            draft["promotion_contract"]["decision"], "backend-required"
        )

    def test_unknown_and_ambiguous_records_never_become_writable(self) -> None:
        unknown = self.draft(observation(0x7FFF))
        self.assertEqual(unknown["assessment"]["state"], "unknown")

        inputs = deepcopy(load_knowledge_inputs(ROOT))
        duplicate = deepcopy(inputs.source_catalogs["openrazer"]["records"][0])
        source = next(
            record
            for record in inputs.source_catalogs["openrazer"]["records"]
            if record["usb_identity"] == {"vendor_id": 0x1532, "product_id": 0x007B}
        )
        duplicate = deepcopy(source)
        duplicate["record_id"] = "openrazer:SyntheticDuplicate"
        duplicate["source_device_key"] = "SyntheticDuplicate"
        inputs.source_catalogs["openrazer"]["records"].append(duplicate)
        with patch.object(onboarding, "load_knowledge_inputs", return_value=inputs):
            ambiguous = self.draft(observation(0x007B))
        self.assertEqual(ambiguous["assessment"]["state"], "ambiguous")
        self.assertFalse(ambiguous["assessment"]["writable"])

    def test_private_draft_is_mode_0600_non_overwriting_and_path_safe(self) -> None:
        draft = self.draft(observation(0x007B))
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            destination = write_onboarding_draft(draft, cache_root=cache)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            serialized = destination.read_text(encoding="utf-8")
            self.assertNotIn("/home/", serialized)
            self.assertNotIn("route_token", serialized)
            with self.assertRaisesRegex(ModelError, "already exists"):
                write_onboarding_draft(draft, cache_root=cache)

    def test_private_writer_never_replaces_a_racing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            destination = cache / "race.json"
            original_link = os.link

            def racing_link(source, target, *, follow_symlinks=True):
                Path(target).write_text("concurrent winner\n", encoding="utf-8")
                return original_link(
                    source, target, follow_symlinks=follow_symlinks
                )

            with patch.object(
                private_artifact.os, "link", side_effect=racing_link
            ):
                with self.assertRaisesRegex(ModelError, "already exists"):
                    write_new_private_json(cache, "race.json", {"safe": True})
            self.assertEqual(
                destination.read_text(encoding="utf-8"), "concurrent winner\n"
            )
            self.assertEqual(list(cache.glob(".*.tmp")), [])

    def test_observation_timestamp_and_protocol_family_are_canonical(self) -> None:
        noncanonical = observation(0x007B)
        noncanonical["observed_at"] = "2026-7-25T00:00:00Z"
        with self.assertRaisesRegex(ModelError, "canonical UTC"):
            self.draft(noncanonical)

        impossible = observation(0x007B)
        impossible["observed_at"] = "2026-02-30T00:00:00Z"
        with self.assertRaisesRegex(ModelError, "invalid timestamp"):
            self.draft(impossible)

        oversized = observation(0x007B, protocol_family="x" * 65)
        with self.assertRaisesRegex(ModelError, "protocol family is invalid"):
            self.draft(oversized)

        oversized_utf8 = observation(0x007B, protocol_family="é" * 33)
        with self.assertRaisesRegex(ModelError, "protocol family is invalid"):
            self.draft(oversized_utf8)

    def test_input_loaders_reject_symlinks_and_oversized_documents(self) -> None:
        fixture = (
            ROOT
            / "tests"
            / "fixtures"
            / "native-onboarding"
            / "viper-ultimate-007b-observation.json"
        )
        with TemporaryDirectory() as directory:
            base = Path(directory)
            linked = base / "observation.json"
            linked.symlink_to(fixture)
            with self.assertRaisesRegex(ModelError, "symbolic, or unreadable"):
                load_route_observation(linked)

            oversized = base / "oversized.json"
            oversized.write_bytes(b"{" + b" " * onboarding.MAX_INPUT_BYTES + b"}")
            with self.assertRaisesRegex(ModelError, "exceeds"):
                load_route_observation(oversized)

            draft = self.draft(observation(0x00CD))
            draft_path = base / "draft.json"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            self.assertEqual(load_onboarding_draft(draft_path), draft)
            parity_path = base / "parity.json"
            parity_path.write_bytes(
                b"{" + b" " * onboarding.MAX_INPUT_BYTES + b"}"
            )
            with self.assertRaisesRegex(ModelError, "native parity evidence exceeds"):
                build_promotion_from_files(
                    ROOT,
                    draft_path,
                    parity_path,
                    created_at="2026-07-25T00:00:02Z",
                    source_revision=REVISION,
                )

    def test_tampering_cannot_grant_write_authority(self) -> None:
        draft = self.draft(observation(0x007B))
        draft["candidate_profile"]["native_control"]["write_authority"] = True
        with self.assertRaisesRegex(ModelError, "cannot be writable"):
            validate_onboarding_draft(draft)

    def test_promotion_rederives_the_complete_draft_from_canonical_sources(self) -> None:
        original = self.draft(observation(0x00CD))
        parity = passed_parity(
            "child.razer.basilisk-v3-pro-35k.00cd", REVISION
        )
        mutations = (
            lambda draft: draft["source_bindings"]["openrazer"].__setitem__(
                "commit", "2" * 40
            ),
            lambda draft: draft["candidate_profile"]["identity"].__setitem__(
                "model_name", "Edited model"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                draft = deepcopy(original)
                mutate(draft)
                with self.assertRaisesRegex(
                    ModelError, "differs from canonical sources"
                ):
                    build_promotion_assessment(
                        ROOT,
                        draft,
                        parity,
                        created_at="2026-07-25T00:00:02Z",
                        source_revision=REVISION,
                    )

    def test_promotion_requires_exact_full_parity_and_never_grants_automatically(self) -> None:
        draft = self.draft(observation(0x00CD))
        parity = passed_parity(
            "child.razer.basilisk-v3-pro-35k.00cd", REVISION
        )
        assessment = build_promotion_assessment(
            ROOT,
            draft,
            parity,
            created_at="2026-07-25T00:00:02Z",
            source_revision=REVISION,
        )
        self.assertEqual(
            assessment["state"], "ready-for-reviewed-profile-promotion"
        )
        self.assertFalse(
            assessment["change_contract"]["automatic_write_authority_granted"]
        )

        parity["coverage"]["passed"] = parity["coverage"]["passed"][:-1]
        parity["coverage"]["missing"] = [parity["coverage"]["required"][-1]]
        parity["observations"] = parity["observations"][:-1]
        parity["conclusion"] = "incomplete"
        blocked = build_promotion_assessment(
            ROOT,
            draft,
            parity,
            created_at="2026-07-25T00:00:03Z",
            source_revision=REVISION,
        )
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("native-parity-not-passed", blocked["reason_codes"])


if __name__ == "__main__":
    unittest.main()
