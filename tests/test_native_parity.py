# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import ModelError
from hfxdev.native_parity import (
    build_direct_semantic_reference,
    build_offline_parity_evidence,
    canonical_request,
    parse_usbmon_capture,
    validate_parity_evidence,
    write_parity_evidence,
)
from hfxdev.native_direct_reference import parse_tshark_usbmon_fields
from hfxdev.native_probe import load_native_probe_catalog


REVISION = "1" * 40


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


def _capture(request: bytes, response: bytes, *, device: int) -> str:
    identifier = f"{device:03d}"
    return "\n".join(
        (
            f"ffff000000000001 1000 S Co:3:{identifier}:0 s 21 09 0300 0000 005a 90 = {_words(request)}",
            f"ffff000000000001 1001 C Co:3:{identifier}:0 0 90 >",
            f"ffff000000000002 1002 S Ci:3:{identifier}:0 s a1 01 0300 0000 005a 90 <",
            f"ffff000000000002 1003 C Ci:3:{identifier}:0 0 90 = {_words(response)}",
            "",
        )
    )


def _tshark_row(
    timestamp: str,
    urb_id: str,
    event: str,
    *,
    request_type: str = "",
    request_id: str = "",
    value: str = "",
    interface: str = "",
    setup_length: str = "",
    payload: bytes = b"",
) -> str:
    fields = [
        timestamp,
        urb_id,
        event,
        "2",
        "3",
        "12",
        "0",
        request_type,
        request_id,
        value,
        interface,
        setup_length,
        str(len(payload)) if payload else "0",
        ":".join(f"{byte:02x}" for byte in payload),
        "",
        "",
        "",
    ]
    return "\t".join(fields)


def _probe(profile_id: str, method: str) -> dict:
    catalog = load_native_probe_catalog(ROOT)
    profile = next(value for value in catalog["devices"] if value["profile_id"] == profile_id)
    return next(value for value in profile["probes"] if value["method"] == method)


class NativeParityTests(unittest.TestCase):
    mouse_profile = "child.razer.basilisk-v3-pro-35k.00cd"
    keyboard_profile = "child.razer.deathstalker-v2-pro-tkl.0296"

    def _mouse_firmware_evidence(self) -> dict:
        probe = _probe(self.mouse_profile, "get_firmware")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((2, 51)))
        receiver_response = _response(receiver_request, bytes((2, 51)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        return evidence

    def test_complete_source_bound_firmware_exchange_passes_that_method(self) -> None:
        evidence = self._mouse_firmware_evidence()
        self.assertIn("get_firmware", evidence["coverage"]["passed"])
        self.assertEqual(evidence["conclusion"], "incomplete")
        routed = next(
            value
            for value in evidence["observations"]
            if value["method"] == "get_firmware" and value["origin"] == "receiver-route"
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
                "response_restored_for_application": None,
                "authority": "captured-usbmon",
            },
        )

    def test_full_tshark_fields_preserve_complete_direct_reports(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        rows = "\n".join(
            (
                _tshark_row(
                    "1.000001",
                    "0x1",
                    "S",
                    request_type="0x21",
                    request_id="9",
                    value="0x0300",
                    interface="0",
                    setup_length="90",
                    payload=request,
                ),
                _tshark_row(
                    "1.000002",
                    "0x2",
                    "S",
                    request_type="0xa1",
                    request_id="1",
                    value="0x0300",
                    interface="0",
                    setup_length="90",
                ),
                _tshark_row("1.000003", "0x2", "C", payload=response),
            )
        )
        exchanges = parse_tshark_usbmon_fields(
            rows,
            target_bus=3,
            target_device=12,
        )
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].request, request)
        self.assertEqual(exchanges[0].response, response)
        self.assertEqual(exchanges[0].timestamp_us, 1_000_001)

        reference = build_direct_semantic_reference(
            ROOT,
            profile_id=self.mouse_profile,
            exchanges=exchanges,
            created_at="2026-07-26T00:00:00Z",
            source_revision=REVISION,
        )
        direct = next(
            item
            for item in reference.evidence["observations"]
            if item["method"] == "get_firmware"
        )
        self.assertTrue(direct["request"]["complete_capture"])
        self.assertEqual(direct["response"]["summary"]["minor"], 51)

    def test_tshark_fields_reject_incomplete_or_conflicting_raw_material(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        request = canonical_request(probe, 0x1F)
        incomplete = _tshark_row(
            "1.0",
            "0x1",
            "S",
            request_type="0x21",
            request_id="9",
            value="0x0300",
            interface="0",
            setup_length="90",
            payload=request[:-1],
        )
        with self.assertRaisesRegex(ModelError, "request is not complete"):
            parse_tshark_usbmon_fields(incomplete, target_bus=3, target_device=12)

        fields = _tshark_row("1.0", "0x1", "C", payload=request).split("\t")
        fields[14] = (bytes((request[0] ^ 1,)) + request[1:]).hex()
        with self.assertRaisesRegex(ModelError, "conflicting complete reports"):
            parse_tshark_usbmon_fields(
                "\t".join(fields), target_bus=3, target_device=12
            )

    def test_route_translation_remains_optional_for_v1_evidence(self) -> None:
        legacy = deepcopy(self._mouse_firmware_evidence())
        for observation in legacy["observations"]:
            observation.pop("route_translation", None)
        validate_parity_evidence(legacy)

    def test_route_translation_rejects_applied_mismatch(self) -> None:
        forged = deepcopy(self._mouse_firmware_evidence())
        routed = next(
            value for value in forged["observations"] if value["origin"] == "receiver-route"
        )
        routed["route_translation"]["applied"] = True
        with self.assertRaisesRegex(ModelError, "invalid route translation"):
            validate_parity_evidence(forged)

    def test_route_translation_rejects_false_response_restoration(self) -> None:
        forged = deepcopy(self._mouse_firmware_evidence())
        routed = next(
            value for value in forged["observations"] if value["origin"] == "receiver-route"
        )
        routed["route_translation"]["response_selector"] = 0x9F
        routed["route_translation"]["response_restored_for_application"] = True
        with self.assertRaisesRegex(ModelError, "invalid route translation"):
            validate_parity_evidence(forged)

    def test_matching_terminal_failure_is_transport_parity_offline(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((0, 0)), status=3)
        receiver_response = _response(receiver_request, bytes((0, 0)), status=3)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        self.assertIn("get_firmware", evidence["coverage"]["passed"])
        routed = next(
            value
            for value in evidence["observations"]
            if value["method"] == "get_firmware" and value["origin"] == "receiver-route"
        )
        self.assertTrue(routed["receiver_comparison"]["standard_response"])
        self.assertTrue(routed["receiver_comparison"]["semantic_parity"])

    def test_matching_terminal_domain_status_does_not_compare_undefined_payloads(self) -> None:
        probe = _probe(self.mouse_profile, "get_battery")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((0, 128)), status=3)
        receiver_response = _response(receiver_request, bytes((0, 144)), status=3)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        self.assertIn("get_battery", evidence["coverage"]["passed"])

    def test_complete_request_rewrite_requires_a_backend(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        direct_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((2, 51)))
        receiver_request = bytearray(canonical_request(probe, 0x1F))
        receiver_request[89] = 1
        receiver_response = _response(bytes(receiver_request), bytes((2, 51)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(bytes(receiver_request), receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        self.assertEqual(evidence["conclusion"], "backend-required")
        self.assertIn("get_firmware", evidence["coverage"]["failed"])

    def test_serial_is_compared_in_memory_but_never_persisted(self) -> None:
        probe = _probe(self.mouse_profile, "get_serial")
        serial = b"PRIVATE-SERIAL-1234567"
        self.assertEqual(len(serial), 22)
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, serial)
        receiver_response = _response(receiver_request, serial)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
            destination = write_parity_evidence(evidence, cache_root=root / "cache")
            rendered = destination.read_text()
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertIn("get_serial", evidence["coverage"]["passed"])
        self.assertNotIn(serial.decode(), rendered)
        self.assertNotIn(hashlib.sha256(serial).hexdigest(), rendered)
        summaries = [
            value["response"]["summary"]
            for value in evidence["observations"]
            if value["method"] == "get_serial"
        ]
        self.assertEqual(
            summaries,
            [
                {"kind": "redacted-serial-equality", "redacted": True, "available": True},
                {"kind": "redacted-serial-equality", "redacted": True, "available": True},
            ],
        )

    def test_busy_retries_are_bounded_and_only_the_terminal_result_counts(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((2, 51)))
        busy_response = _response(receiver_request, bytes((0, 0)), status=1)
        success_response = _response(receiver_request, bytes((2, 51)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(
                _capture(receiver_request, busy_response, device=11)
                + _capture(receiver_request, success_response, device=11)
            )
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        routed = next(
            value for value in evidence["observations"] if value["origin"] == "receiver-route"
        )
        self.assertEqual(routed["receiver_comparison"]["attempts"], 2)
        self.assertIn("get_firmware", evidence["coverage"]["passed"])

    def test_dynamic_telemetry_requires_valid_values_not_identical_snapshots(self) -> None:
        probe = _probe(self.mouse_profile, "get_battery")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((0, 128)))
        receiver_response = _response(receiver_request, bytes((0, 144)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=12))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        routed = next(
            value for value in evidence["observations"] if value["origin"] == "receiver-route"
        )
        self.assertEqual(routed["comparison"], "domain-valid")
        self.assertTrue(routed["receiver_comparison"]["semantic_parity"])
        self.assertIn("get_battery", evidence["coverage"]["passed"])

    def test_keyboard_layout_matches_the_byte_exposed_by_openrazer(self) -> None:
        probe = _probe(self.keyboard_profile, "get_keyboard_layout")
        direct_request = canonical_request(probe, 0xFF)
        receiver_request = canonical_request(probe, 0x9F)
        direct_response = _response(direct_request, bytes((6, 0x80)))
        receiver_response = _response(receiver_request, bytes((6, 0x00)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            receiver = root / "receiver.txt"
            direct.write_text(_capture(direct_request, direct_response, device=13))
            receiver.write_text(_capture(receiver_request, receiver_response, device=11))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.keyboard_profile,
                direct_capture=direct,
                receiver_capture=receiver,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
        routed = next(
            value for value in evidence["observations"] if value["origin"] == "receiver-route"
        )
        self.assertEqual(routed["response"]["summary"]["layout_code"], 6)
        self.assertTrue(routed["receiver_comparison"]["semantic_parity"])
        self.assertEqual(
            routed["route_translation"],
            {
                "field": "transaction-id",
                "application_selector": 0xFF,
                "receiver_selector": 0x9F,
                "applied": True,
                "response_selector": 0x9F,
                "response_restored_for_application": None,
                "authority": "captured-usbmon",
            },
        )

    def test_parser_rejects_unmatched_or_nonstandard_reports(self) -> None:
        with TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.txt"
            capture.write_text(
                "ffff 1 S Co:3:011:0 s 21 09 0300 0000 005a 90 = 0000000000000000\n"
            )
            with self.assertRaisesRegex(ModelError, "unmatched"):
                parse_usbmon_capture(capture)

    def test_request_marker_and_out_of_range_status_are_not_standard_responses(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        direct_request = canonical_request(probe, 0x1F)
        receiver_request = canonical_request(probe, 0x1F)
        direct_response = _response(direct_request, bytes((2, 51)))
        for status in (0, 6):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                direct = root / "direct.txt"
                receiver = root / "receiver.txt"
                direct.write_text(_capture(direct_request, direct_response, device=12))
                receiver.write_text(
                    _capture(
                        receiver_request,
                        _response(receiver_request, bytes((2, 51)), status=status),
                        device=11,
                    )
                )
                evidence = build_offline_parity_evidence(
                    ROOT,
                    profile_id=self.mouse_profile,
                    direct_capture=direct,
                    receiver_capture=receiver,
                    created_at="2026-07-25T00:00:00Z",
                    source_revision=REVISION,
                )
            self.assertIn("get_firmware", evidence["coverage"]["failed"])
            self.assertEqual(evidence["conclusion"], "backend-required")

    def test_writer_rederives_claims_and_rejects_symlink_cache_roots(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.txt"
            direct.write_text(_capture(request, response, device=12))
            evidence = build_offline_parity_evidence(
                ROOT,
                profile_id=self.mouse_profile,
                direct_capture=direct,
                created_at="2026-07-25T00:00:00Z",
                source_revision=REVISION,
            )
            forged = deepcopy(evidence)
            forged["conclusion"] = "passed"
            with self.assertRaisesRegex(ModelError, "conclusion differs"):
                write_parity_evidence(forged, cache_root=root / "cache")

            real = root / "real-cache"
            real.mkdir()
            linked = root / "linked-cache"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ModelError, "symbolic link"):
                write_parity_evidence(evidence, cache_root=linked)


if __name__ == "__main__":
    unittest.main()
