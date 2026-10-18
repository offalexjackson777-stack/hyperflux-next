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


    def test_child_battery_publication_never_queries_the_device(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-battery.c").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "hid_hw_raw_request",
            "hid_hw_request",
            "hid_hw_output_report",
            "usb_control_msg",
            "usb_interrupt_msg",
            "hfx_transport",
            "schedule_delayed_work",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("POWER_SUPPLY_SCOPE_DEVICE", source)
        self.assertIn('value->strval = "Razer";', source)
        self.assertIn("power_supply_register(&parent->dev", source)
        self.assertNotIn("power_supply_register(&lane->hdev->dev", source)
        report_start = source.index("long hfx_battery_report(")
        report_end = source.index("\n}\n", report_start)
        report = source[report_start:report_end]
        self.assertIn("if (!context->writer)", report)
        self.assertIn("receiver->writer_context == context", report)
        self.assertIn("-ESTALE", report)
        self.assertIn(
            "lane->owner = context;\n\t\tpower_supply_changed(lane->psy);", report
        )

    def test_child_battery_is_parented_beside_no_input(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-battery.c").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "receiver->battery_parent ? receiver->battery_parent : lane->hdev",
            source,
        )
        attach_start = source.index("void hfx_battery_attach_interface(")
        attach = source[attach_start : source.index("\n}\n", attach_start)]
        self.assertIn("!(hdev->claimed & HID_CLAIMED_INPUT)", attach)
        self.assertIn(
            "interface_number < receiver->battery_parent_interface", attach
        )
        self.assertIn("hfx_battery_move_all_locked(receiver);", attach)
        detach_start = source.index("void hfx_battery_detach_interface(")
        detach = source[detach_start : source.index("\n}\n", detach_start)]
        self.assertLess(
            detach.index("receiver->battery_parent = NULL;"),
            detach.index("hfx_battery_move_all_locked(receiver);"),
        )
        move_start = source.index("static void hfx_battery_move_locked(")
        move = source[move_start : source.index("\n}\n", move_start)]
        self.assertNotIn("lane->owner = NULL", move)
        self.assertIn("hfx_battery_publish_locked(receiver, lane)", move)
        core = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            core.index("hid_hw_start(hdev, HID_CONNECT_DEFAULT)"),
            core.index("hfx_battery_attach_interface(interface->receiver"),
        )

    def test_child_battery_is_torn_down_on_every_exit(self) -> None:
        core = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(encoding="utf-8")
        session = (ROOT / "driver/kernel/hyperflux-next-session.c").read_text(
            encoding="utf-8"
        )
        put_start = core.index("static void hfx_receiver_put")
        put = core[put_start : core.index("\n}\n", put_start)]
        self.assertLess(
            put.index("hfx_battery_teardown(receiver)"), put.index("misc_deregister")
        )
        remove_start = core.index("static void hfx_remove")
        remove = core[remove_start : core.index("\n}\n", remove_start)]
        self.assertLess(
            remove.index("hfx_battery_detach_interface"), remove.index("hid_hw_stop")
        )
        release_start = session.index("static int hfx_session_release_file")
        release = session[release_start : session.index("\n}\n", release_start)]
        self.assertLess(
            release.index("hfx_battery_release_owner(receiver, context)"),
            release.index("kref_put"),
        )
        self.assertIn(
            "case HFX_UAPI_IOCTL_REPORT_CHILD_BATTERY:\n"
            "\t\treturn hfx_battery_report(receiver, context, user_arg);",
            session,
        )

if __name__ == "__main__":
    unittest.main()
