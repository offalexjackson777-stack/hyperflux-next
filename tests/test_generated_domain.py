# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from hyperflux_sdk import BatteryPercent, DeviceKind, GenerationId, ReceiverId


class GeneratedDomainTests(unittest.TestCase):
    def test_numeric_bounds_are_enforced(self) -> None:
        self.assertEqual(BatteryPercent(100).value, 100)
        with self.assertRaises(ValueError):
            BatteryPercent(101)
        with self.assertRaises(ValueError):
            GenerationId(0)

    def test_bool_is_not_accepted_as_integer(self) -> None:
        with self.assertRaises(TypeError):
            BatteryPercent(True)

    def test_identifier_bounds_are_enforced(self) -> None:
        self.assertEqual(ReceiverId("receiver-1").value, "receiver-1")
        with self.assertRaises(ValueError):
            ReceiverId("")

    def test_enum_wire_value_is_stable(self) -> None:
        self.assertEqual(DeviceKind.KEYBOARD.value, "keyboard")

    def test_cross_language_integer_wire_encoding_is_declared(self) -> None:
        self.assertEqual(GenerationId.WIRE_ENCODING, "decimal-string")
        self.assertEqual(BatteryPercent.WIRE_ENCODING, "number")


if __name__ == "__main__":
    unittest.main()
