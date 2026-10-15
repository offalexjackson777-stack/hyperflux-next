# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class KernelDriverContracts(unittest.TestCase):
    def test_pairing_inventory_is_one_read_only_private_operation(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(encoding="utf-8")
        start = source.index("static void hfx_pairing_inventory_bootstrap")
        end = source.index("static int hfx_raw_event", start)
        bootstrap = source[start:end]

        self.assertEqual(bootstrap.count("hid_hw_raw_request"), 1)
        self.assertIn("HID_FEATURE_REPORT", bootstrap)
        self.assertIn("HID_REQ_GET_REPORT", bootstrap)
        self.assertIn("memzero_explicit", bootstrap)
        for forbidden in (
            "HID_REQ_SET_REPORT",
            "usb_control_msg",
            "msleep",
            "schedule_delayed_work",
        ):
            self.assertNotIn(forbidden, bootstrap)

    def test_pairing_inventory_parser_does_not_bind_owner_child_ids(self) -> None:
        source = (
            ROOT / "driver/kernel/hyperflux-next-pairing-inventory.h"
        ).read_text(encoding="utf-8")
        self.assertNotIn("0x00cd", source.lower())
        self.assertNotIn("0x0296", source.lower())

    def test_pairing_inventory_publishes_valid_slots_independently(self) -> None:
        source = (
            ROOT / "driver/kernel/hyperflux-next-observation.c"
        ).read_text(encoding="utf-8")
        start = source.index("bool hfx_observe_pairing_inventory")
        end = source.index("bool hfx_observation_identity_conflicted", start)
        observer = source[start:end]

        self.assertIn("inventory->pointer_state", observer)
        self.assertIn("inventory->keyboard_state", observer)
        self.assertEqual(observer.count("hfx_observe_product_id_locked("), 2)
        self.assertNotIn("if (pointer_conflict || keyboard_conflict)", observer)

        bootstrap = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(
            encoding="utf-8"
        )
        self.assertIn("interface->receiver, &inventory", bootstrap)

    def test_composite_activity_uses_kernel_report_metadata(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(encoding="utf-8")
        start = source.index("static int hfx_raw_event")
        end = source.index("static int hfx_probe", start)
        callback = source[start:end]
        self.assertIn("report->id", callback)
        self.assertNotIn("data[0]", callback)

    def test_native_exchange_transport_is_child_model_agnostic(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        ).lower()
        for forbidden in (
            "0x00cd",
            "0x0296",
            "basilisk",
            "deathstalker",
        ):
            self.assertNotIn(forbidden, source)

    def test_native_exchange_sends_once_then_retries_only_get(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        )
        start = source.index("static int hfx_native_record_dispatch")
        end = source.index("static int hfx_native_reserve", start)
        dispatch = source[start:end]
        loop_start = dispatch.index("for (attempt")
        response_loop = dispatch[loop_start:]

        self.assertEqual(dispatch.count("hfx_native_usb_set("), 1)
        self.assertNotIn("hfx_native_usb_set(", response_loop)
        self.assertEqual(response_loop.count("hfx_native_usb_get("), 1)
        self.assertIn("native_max_response_attempts", response_loop)
        self.assertIn("native_response_retry_delay_us", response_loop)

    def test_native_exchange_uses_declarative_receiver_profile(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        )
        for profile_field in (
            "profile->native_report_bytes",
            "profile->native_set_report_index",
            "profile->native_response_report_index",
            "profile->native_initial_response_delay_us",
            "profile->native_response_retry_delay_us",
            "profile->native_max_response_attempts",
            "profile->native_request_status_offset",
            "profile->native_response_status_offset",
            "profile->native_response_status_minimum",
            "profile->native_response_status_maximum",
            "profile->native_protocol_type_offset",
            "profile->native_data_size_offset",
            "profile->native_checksum_start_offset",
            "profile->native_checksum_end_offset",
            "profile->native_checksum_result_offset",
            "profile->native_trailer_offset",
            "profile->native_transaction_id_offset",
            "profile->native_transaction_id_match_mask",
            "profile->native_route_selector_kind",
            "profile->native_pointer_route_selector",
            "profile->native_keyboard_route_selector",
            "profile->native_restore_original_response",
            "profile->native_match_ranges",
        ):
            self.assertIn(profile_field, source)

        table = (
            ROOT / "driver/kernel/generated/hyperflux_receiver_profiles.inc"
        ).read_text(encoding="utf-8")
        self.assertIn("HFX_NATIVE_EXCHANGE_RAZER_FEATURE_REPORT_V1", table)
        self.assertIn("HFX_NATIVE_CHECKSUM_XOR8", table)
        self.assertIn("HFX_NATIVE_ROUTE_SELECTOR_TRANSACTION_ID", table)
        self.assertIn(".native_match_ranges", table)

    def test_native_exchange_validates_request_before_set_and_response_before_match(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        )
        submit_start = source.index("static int hfx_native_submit_validate")
        submit_end = source.index("static bool hfx_native_key_matches", submit_start)
        submit = source[submit_start:submit_end]
        self.assertIn("hfx_native_request_report_valid", submit)

        match_start = source.index("static bool hfx_native_response_matches")
        match_end = source.index("static int hfx_native_usb_set", match_start)
        matcher = source[match_start:match_end]
        self.assertIn("hfx_native_response_report_valid", matcher)
        self.assertIn("native_transaction_id_match_mask", matcher)

    def test_native_exchange_keeps_polling_past_another_transactions_reply(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        )
        match_start = source.index("static bool hfx_native_response_matches")
        match_end = source.index("static int hfx_native_usb_set", match_start)
        matcher = source[match_start:match_end]
        self.assertRegex(
            matcher,
            r"request\[profile->native_transaction_id_offset\] \^\s*"
            r"response\[profile->native_transaction_id_offset\];",
        )
        self.assertRegex(
            matcher,
            r"if \(transaction_difference &\s*"
            r"profile->native_transaction_id_match_mask\)\s*return false;",
        )

        start = source.index("static int hfx_native_record_dispatch")
        end = source.index("static int hfx_native_reserve", start)
        response_loop = source[start:end][source[start:end].index("for (attempt"):]
        matched = response_loop.index("hfx_native_response_matches(")
        self.assertLess(matched, response_loop.index("hfx_native_restore_response("))
        self.assertIn("ret = -ENOMSG;", response_loop[matched:])

        table = (
            ROOT / "driver/kernel/generated/hyperflux_receiver_profiles.inc"
        ).read_text(encoding="utf-8")
        self.assertIn(".native_transaction_id_match_mask = 255,", table)
        self.assertNotIn(".native_transaction_id_match_mask = 0,", table)

    def test_native_route_selector_is_endpoint_declarative_and_response_repaired(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-native.c").read_text(
            encoding="utf-8"
        )
        route_start = source.index("static int hfx_native_route_selector")
        route_end = source.index("static int hfx_native_submit_validate", route_start)
        routing = source[route_start:route_end]
        self.assertIn("HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE", routing)
        self.assertIn("HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE", routing)
        self.assertIn("hfx_native_checksum_update", routing)
        self.assertIn("hfx_native_restore_response", routing)
        self.assertIn("response[offset] != receiver_selector", routing)
        for forbidden in ("0x1f", "0x9f", "0x00cd", "0x0296"):
            self.assertNotIn(forbidden, routing.lower())


if __name__ == "__main__":
    unittest.main()
