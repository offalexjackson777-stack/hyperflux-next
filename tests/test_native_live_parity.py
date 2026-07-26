# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
import hfxdev.native_live_parity as native_live
from hfxdev.native_live_parity import _feature_only_has_no_input_nodes
from hfxdev.native_parity import (
    build_live_parity_evidence,
    build_offline_parity_evidence,
    canonical_request,
)
from hfxdev.native_probe import load_native_probe_catalog


PROFILE = "child.razer.basilisk-v3-pro-35k.00cd"
REVISION = "a" * 40
PACKAGE_SHA256 = "b" * 64


def _crc(report: bytes | bytearray) -> int:
    value = 0
    for byte in report[2:88]:
        value ^= byte
    return value


def _response(request: bytes, arguments: bytes, *, status: int = 2) -> bytes:
    report = bytearray(90)
    report[0] = status
    report[1] = request[1]
    report[2:4] = request[2:4]
    report[5] = len(arguments)
    report[6:8] = request[6:8]
    report[8 : 8 + len(arguments)] = arguments
    report[88] = _crc(report)
    return bytes(report)


def _words(value: bytes) -> str:
    return " ".join(value[index : index + 4].hex() for index in range(0, len(value), 4))


def _capture(request: bytes, response: bytes) -> str:
    return "\n".join(
        (
            f"ffff000000000001 1000 S Co:3:012:0 s 21 09 0300 0000 005a 90 = {_words(request)}",
            "ffff000000000001 1001 C Co:3:012:0 0 90 >",
            "ffff000000000002 1002 S Ci:3:012:0 s a1 01 0300 0000 005a 90 <",
            f"ffff000000000002 1003 C Ci:3:012:0 0 90 = {_words(response)}",
            "",
        )
    )


def _probe(method: str) -> dict:
    catalog = load_native_probe_catalog(ROOT)
    profile = next(item for item in catalog["devices"] if item["profile_id"] == PROFILE)
    return next(item for item in profile["probes"] if item["method"] == method)


def _raw_record(method: str, request: bytes, response: bytes) -> dict:
    return {
        "sequence": 1,
        "method": method,
        "attempt": 1,
        "request_hex": request.hex(),
        "certainty": "matched-response",
        "transport_errno": 0,
        "response_hex": response.hex(),
    }


def _raw(records: list[dict]) -> dict:
    return {
        "schema": "hyperflux-native-qualification-raw-v1",
        "source_revision": REVISION,
        "package_sha256": PACKAGE_SHA256,
        "profile_id": PROFILE,
        "receiver_generation": 7,
        "read_only": True,
        "completed": False,
        "records": records,
    }


class NativeLiveParityTests(unittest.TestCase):
    def _direct_method_evidence(
        self,
        method: str,
        arguments: bytes,
        *,
        status: int = 2,
        complete_capture: bool = True,
    ) -> dict:
        probe = _probe(method)
        request = canonical_request(probe, 0x1F)
        response = _response(request, arguments, status=status)
        with TemporaryDirectory() as directory:
            direct = Path(directory) / "direct.txt"
            direct.write_text(_capture(request, response), encoding="utf-8")
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=PROFILE,
                direct_capture=direct,
                created_at="2026-07-26T00:00:00Z",
                source_revision=REVISION,
            )
        observation = next(
            item
            for item in evidence["observations"]
            if item["method"] == method
            and item["origin"] == "direct-semantic-reference"
        )
        observation["response"]["complete_capture"] = complete_capture
        return evidence

    def _direct_firmware_evidence(self) -> dict:
        return self._direct_method_evidence("get_firmware", bytes((2, 51)))

    def test_ephemeral_exact_report_becomes_privacy_safe_parity(self) -> None:
        probe = _probe("get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_firmware_evidence(),
            raw_qualification=_raw([_raw_record("get_firmware", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            created_at="2026-07-26T00:01:00Z",
            source_revision=REVISION,
        )
        self.assertIn("get_firmware", evidence["coverage"]["passed"])
        self.assertTrue(evidence["safety"]["read_only"])
        self.assertFalse(evidence["safety"]["raw_reports_persisted"])
        serialized = json.dumps(evidence)
        self.assertNotIn(request.hex(), serialized)
        self.assertNotIn(response.hex(), serialized)

    def test_source_bound_semantic_reference_does_not_require_raw_direct_report(self) -> None:
        probe = _probe("get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_method_evidence(
                "get_firmware", bytes((2, 51)), complete_capture=False
            ),
            raw_qualification=_raw([_raw_record("get_firmware", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        self.assertIn("get_firmware", evidence["coverage"]["passed"])

    def test_device_mode_follows_upstream_fixed_argument_reads(self) -> None:
        probe = _probe("get_device_mode")
        request = canonical_request(probe, 0x1F)
        response = bytearray(_response(request, bytes((4,))))
        response[9] = 7
        response[88] = _crc(response)
        with TemporaryDirectory() as directory:
            direct = Path(directory) / "direct.txt"
            direct.write_text(_capture(request, bytes(response)), encoding="utf-8")
            direct_evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=PROFILE,
                direct_capture=direct,
                created_at="2026-07-26T00:00:00Z",
                source_revision=REVISION,
            )
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=direct_evidence,
            raw_qualification=_raw(
                [_raw_record("get_device_mode", request, bytes(response))]
            ),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        self.assertIn("get_device_mode", evidence["coverage"]["passed"])
        summaries = [
            item["response"]["summary"]
            for item in evidence["observations"]
            if item["method"] == "get_device_mode"
        ]
        self.assertEqual(
            summaries,
            [
                {"kind": "device-mode", "mode": 4, "parameter": 7, "available": True},
                {"kind": "device-mode", "mode": 4, "parameter": 7, "available": True},
            ],
        )

    def test_matching_terminal_failure_is_preserved_as_transport_parity(self) -> None:
        probe = _probe("get_brightness")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((0,)), status=3)
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_method_evidence(
                "get_brightness", bytes((0,)), status=3, complete_capture=False
            ),
            raw_qualification=_raw([_raw_record("get_brightness", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        routed = next(
            item
            for item in evidence["observations"]
            if item["method"] == "get_brightness"
            and item["origin"] == "receiver-route"
        )
        self.assertEqual(
            routed["receiver_comparison"],
            {
                "attempts": 1,
                "unchanged_request": True,
                "standard_response": True,
                "semantic_parity": True,
            },
        )
        self.assertEqual(
            routed["route_translation"],
            {
                "field": "transaction-id",
                "application_selector": 0x1F,
                "receiver_selector": 0x1F,
                "applied": False,
                "response_selector": 0x1F,
                "response_restored_for_application": True,
                "authority": "receiver-profile",
            },
        )
        self.assertIn("get_brightness", evidence["coverage"]["passed"])
        self.assertNotEqual(evidence["conclusion"], "backend-required")

    def test_terminal_status_mismatch_is_semantic_failure_not_backend_rewrite(self) -> None:
        probe = _probe("get_brightness")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((0,)), status=5)
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_method_evidence(
                "get_brightness", bytes((0,)), status=3, complete_capture=False
            ),
            raw_qualification=_raw([_raw_record("get_brightness", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        self.assertIn("get_brightness", evidence["coverage"]["failed"])
        self.assertEqual(evidence["conclusion"], "incomplete")

    def test_matching_terminal_status_with_different_exact_summary_fails_semantics(self) -> None:
        probe = _probe("get_brightness")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((1,)), status=3)
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_method_evidence(
                "get_brightness", bytes((0,)), status=3, complete_capture=False
            ),
            raw_qualification=_raw([_raw_record("get_brightness", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        self.assertIn("get_brightness", evidence["coverage"]["failed"])
        self.assertEqual(evidence["conclusion"], "incomplete")

    def test_final_busy_response_remains_incomplete(self) -> None:
        probe = _probe("get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((0, 0)), status=1)
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_firmware_evidence(),
            raw_qualification=_raw([_raw_record("get_firmware", request, response)]),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        routed = next(
            item
            for item in evidence["observations"]
            if item["method"] == "get_firmware"
            and item["origin"] == "receiver-route"
        )
        self.assertIsNone(routed["receiver_comparison"]["standard_response"])
        self.assertIsNone(routed["receiver_comparison"]["semantic_parity"])
        self.assertIn("get_firmware", evidence["coverage"]["missing"])
        self.assertEqual(evidence["conclusion"], "incomplete")

    def test_malformed_receiver_response_requires_a_backend(self) -> None:
        probe = _probe("get_firmware")
        request = canonical_request(probe, 0x1F)
        response = bytearray(_response(request, bytes((2, 51))))
        response[1] ^= 1
        response[88] = _crc(response)
        evidence = build_live_parity_evidence(
            ROOT,
            profile_id=PROFILE,
            direct_evidence=self._direct_firmware_evidence(),
            raw_qualification=_raw(
                [_raw_record("get_firmware", request, bytes(response))]
            ),
            package_sha256=PACKAGE_SHA256,
            receiver_generation=7,
            source_revision=REVISION,
        )
        self.assertIn("get_firmware", evidence["coverage"]["failed"])
        self.assertEqual(evidence["conclusion"], "backend-required")

    def test_unclassified_sent_request_and_rewritten_read_fail_closed(self) -> None:
        probe = _probe("get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        unknown = _raw_record("get_firmware", request, response)
        unknown["method"] = None
        with self.assertRaises(ModelError):
            build_live_parity_evidence(
                ROOT,
                profile_id=PROFILE,
                direct_evidence=self._direct_firmware_evidence(),
                raw_qualification=_raw([unknown]),
                package_sha256=PACKAGE_SHA256,
                receiver_generation=7,
                source_revision=REVISION,
            )
        changed = bytearray(request)
        changed[8] ^= 1
        changed[88] = _crc(changed)
        with self.assertRaises(ModelError):
            build_live_parity_evidence(
                ROOT,
                profile_id=PROFILE,
                direct_evidence=self._direct_firmware_evidence(),
                raw_qualification=_raw(
                    [_raw_record("get_firmware", bytes(changed), response)]
                ),
                package_sha256=PACKAGE_SHA256,
                receiver_generation=7,
                source_revision=REVISION,
            )

    def test_feature_only_topology_rejects_any_input_collection(self) -> None:
        with TemporaryDirectory() as directory:
            endpoint = Path(directory) / "endpoint"
            endpoint.mkdir()
            self.assertTrue(_feature_only_has_no_input_nodes(endpoint))
            (endpoint / "input0").mkdir()
            self.assertFalse(_feature_only_has_no_input_nodes(endpoint))

    def test_ephemeral_raw_capture_is_deleted_even_when_supervision_fails(self) -> None:
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.json"
            with patch.object(native_live, "_safe_raw_path", return_value=raw):
                with self.assertRaisesRegex(RuntimeError, "supervisor failed"):
                    with native_live._ephemeral_raw_path() as path:
                        path.write_text("private", encoding="utf-8")
                        raise RuntimeError("supervisor failed")
            self.assertFalse(raw.exists())

    def test_service_isolation_rejects_an_active_production_service(self) -> None:
        inactive = Mock(returncode=3)
        with patch.object(native_live.subprocess, "run", return_value=inactive) as run:
            native_live._require_isolated_services()
        self.assertEqual(run.call_count, len(native_live.INACTIVE_SERVICES))

        active = Mock(returncode=0)
        with patch.object(native_live.subprocess, "run", return_value=active):
            with self.assertRaisesRegex(ModelError, "must be stopped"):
                native_live._require_isolated_services()

    def test_installed_qualifier_must_match_the_exact_candidate_package(self) -> None:
        with TemporaryDirectory() as directory:
            package = Path(directory) / "candidate.pkg.tar.zst"
            qualifier = Path(directory) / "qualifier"
            package.write_bytes(b"candidate package")
            qualifier.write_bytes(b"source-bound qualifier")
            package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
            qualifier_digest = hashlib.sha256(qualifier.read_bytes()).hexdigest()
            with patch.object(native_live, "QUALIFIER_PATH", qualifier), patch.object(
                native_live, "_package_member_digest", return_value=qualifier_digest
            ):
                native_live._verify_candidate_package(package, package_digest)
                qualifier.write_bytes(b"different installed qualifier")
                with self.assertRaisesRegex(ModelError, "differs from the candidate package"):
                    native_live._verify_candidate_package(package, package_digest)

    def test_only_cataloged_attributes_are_triggered(self) -> None:
        profile = {
            "probes": [
                {"method": "get_firmware", "driver": {"attribute": "firmware_version"}},
                {"method": "get_battery", "driver": {"attribute": "battery"}},
            ]
        }
        process = Mock()
        process.poll.return_value = None
        completed = Mock(returncode=0, stderr=b"")
        with TemporaryDirectory() as directory, patch.object(
            native_live.shutil, "which", return_value="/usr/bin/cat"
        ), patch.object(native_live.subprocess, "run", return_value=completed) as run:
            entry = Path(directory)
            result = native_live._trigger_probe_attributes(entry, profile, 5, process)
        self.assertEqual(result, {"get_firmware": True, "get_battery": True})
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/usr/bin/cat", str(entry / "firmware_version")],
                ["/usr/bin/cat", str(entry / "battery")],
            ],
        )

    def test_qualifier_command_binds_every_approved_material(self) -> None:
        raw = Path("/dev/shm/hyperflux-next-native-parity-test.json")
        command = native_live._qualifier_command(
            profile_id=PROFILE,
            expected_generation=7,
            revision=REVISION,
            package_sha256=PACKAGE_SHA256,
            raw_path=raw,
            timeout_seconds=90,
            approval_text=native_live.READ_ONLY_APPROVAL,
        )
        self.assertEqual(command[0], str(native_live.QUALIFIER_PATH))
        for value in (PROFILE, "7", REVISION, PACKAGE_SHA256, str(raw), "90"):
            self.assertIn(value, command)
        self.assertEqual(command[-1], native_live.READ_ONLY_APPROVAL)


if __name__ == "__main__":
    unittest.main()
