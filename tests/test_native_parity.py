# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import stat
import struct
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
from hfxdev.native_direct_reference import parse_pcap_usbmon_capture
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


def _usbmon_frame(
    urb_id: int,
    event: str,
    *,
    device: int = 12,
    setup: tuple[int, int, int, int, int] | None = None,
    data: bytes = b"",
    status: int = 0,
) -> bytes:
    frame = bytearray(64)
    frame[0:8] = urb_id.to_bytes(8, byteorder=sys.byteorder)
    frame[8] = ord(event)
    frame[9] = 2
    frame[11] = device
    frame[12:14] = (3).to_bytes(2, byteorder=sys.byteorder)
    frame[14] = 0 if setup is not None else 1
    frame[15] = 0 if data else 1
    frame[28:32] = status.to_bytes(4, byteorder=sys.byteorder, signed=True)
    frame[32:36] = (setup[4] if setup else len(data)).to_bytes(
        4, byteorder=sys.byteorder
    )
    frame[36:40] = len(data).to_bytes(4, byteorder=sys.byteorder)
    if setup is not None:
        request_type, request_id, value, interface, length = setup
        frame[40] = request_type
        frame[41] = request_id
        frame[42:44] = value.to_bytes(2, byteorder="little")
        frame[44:46] = interface.to_bytes(2, byteorder="little")
        frame[46:48] = length.to_bytes(2, byteorder="little")
    return bytes(frame) + data


def _pcap_capture(
    request: bytes,
    response: bytes,
    *,
    device: int = 12,
    response_status: int = 0,
    linktype: int = 220,
) -> bytes:
    frames = (
        _usbmon_frame(
            1,
            "S",
            device=device,
            setup=(0x21, 0x09, 0x0300, 3, 90),
            data=request,
        ),
        _usbmon_frame(1, "C", device=device),
        _usbmon_frame(
            2,
            "S",
            device=device,
            setup=(0xA1, 0x01, 0x0300, 3, 90),
        ),
        _usbmon_frame(2, "C", device=device, data=response, status=response_status),
    )
    capture = bytearray(
        struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, linktype)
    )
    for fraction, frame in enumerate(frames, start=1):
        capture.extend(struct.pack("<IIII", 1, fraction, len(frame), len(frame)))
        capture.extend(frame)
    return bytes(capture)


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

    def test_linux_usbmon_pcap_preserves_complete_direct_reports(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        exchanges = parse_pcap_usbmon_capture(
            _pcap_capture(request, response),
            target_bus=3,
            target_device=12,
        )
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].request, request)
        self.assertEqual(exchanges[0].response, response)
        self.assertEqual(exchanges[0].timestamp_us, 1_000_001)
        self.assertEqual(
            parse_pcap_usbmon_capture(
                _pcap_capture(request, response),
                target_bus=3,
                target_device=11,
            ),
            (),
        )

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

    def test_linux_usbmon_pcap_rejects_incomplete_or_failed_material(self) -> None:
        probe = _probe(self.mouse_profile, "get_firmware")
        request = canonical_request(probe, 0x1F)
        response = _response(request, bytes((2, 51)))
        with self.assertRaisesRegex(ModelError, "request is not complete"):
            parse_pcap_usbmon_capture(
                _pcap_capture(request[:-1], response),
                target_bus=3,
                target_device=12,
            )

        with self.assertRaisesRegex(ModelError, "completed with an error"):
            parse_pcap_usbmon_capture(
                _pcap_capture(request, response, response_status=-32),
                target_bus=3,
                target_device=12,
            )

        with self.assertRaisesRegex(ModelError, "not Linux usbmon"):
            parse_pcap_usbmon_capture(
                _pcap_capture(request, response, linktype=1),
                target_bus=3,
                target_device=12,
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
