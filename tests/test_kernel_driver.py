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

    def test_composite_activity_uses_kernel_report_metadata(self) -> None:
        source = (ROOT / "driver/kernel/hyperflux-next-core.c").read_text(encoding="utf-8")
        start = source.index("static int hfx_raw_event")
        end = source.index("static int hfx_probe", start)
        callback = source[start:end]
        self.assertIn("report->id", callback)
        self.assertNotIn("data[0]", callback)


if __name__ == "__main__":
    unittest.main()
