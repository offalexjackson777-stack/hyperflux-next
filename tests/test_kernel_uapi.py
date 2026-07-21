# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.hfxdev.kernel_uapi import MAX_IOCTL_STRUCT_BYTES, load_kernel_uapi
from tools.hfxdev.model import ModelError, load_json


ROOT = Path(__file__).resolve().parents[1]


class KernelUapiTests(unittest.TestCase):
    def mutated_root(self, value: dict) -> tempfile.TemporaryDirectory[str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "uapi").mkdir()
        (root / "uapi" / "kernel-uapi.json").write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        return temporary

    def test_canonical_layout_is_deterministic_and_bounded(self) -> None:
        first = load_kernel_uapi(ROOT)
        second = load_kernel_uapi(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(
            {item.name: item.size for item in first.structs},
            {
                "info": 40,
                "begin_session": 128,
                "end_session": 32,
                "frame": 112,
                "submit": 1_872,
                "transaction_result": 104,
                "observation": 40,
                "read_observations": 1_328,
            },
        )
        self.assertTrue(all(item.size < MAX_IOCTL_STRUCT_BYTES for item in first.structs))

    def test_boundary_has_no_pointer_or_product_presentation_fields(self) -> None:
        catalog = load_kernel_uapi(ROOT)
        forbidden_types = {"pointer", "usize", "isize", "string", "path"}
        forbidden_field_fragments = {"mouse", "keyboard", "layout", "effect", "application"}
        for structure in catalog.structs:
            for field in structure.fields:
                self.assertNotIn(field.type_name, forbidden_types)
                self.assertTrue(
                    all(fragment not in field.name for fragment in forbidden_field_fragments),
                    field.name,
                )

    def test_unknown_catalog_keys_fail_closed(self) -> None:
        value = deepcopy(load_json(ROOT / "uapi" / "kernel-uapi.json"))
        value["surprise"] = True
        with self.mutated_root(value) as root:
            with self.assertRaisesRegex(ModelError, "unknown surprise"):
                load_kernel_uapi(Path(root))

    def test_forward_declared_and_oversized_structures_are_rejected(self) -> None:
        value = deepcopy(load_json(ROOT / "uapi" / "kernel-uapi.json"))
        value["structs"][0]["fields"][0]["type"] = "future_type"
        with self.mutated_root(value) as root:
            with self.assertRaisesRegex(ModelError, "unknown or forward-declared"):
                load_kernel_uapi(Path(root))

        value = deepcopy(load_json(ROOT / "uapi" / "kernel-uapi.json"))
        value["limits"]["max_frames"] = 4096
        with self.mutated_root(value) as root:
            with self.assertRaisesRegex(ModelError, "exceeds the Linux ioctl size field"):
                load_kernel_uapi(Path(root))

    def test_duplicate_ioctl_numbers_and_wide_enum_values_are_rejected(self) -> None:
        value = deepcopy(load_json(ROOT / "uapi" / "kernel-uapi.json"))
        value["ioctls"][1]["number"] = value["ioctls"][0]["number"]
        with self.mutated_root(value) as root:
            with self.assertRaisesRegex(ModelError, "duplicate kernel UAPI ioctl number"):
                load_kernel_uapi(Path(root))

        value = deepcopy(load_json(ROOT / "uapi" / "kernel-uapi.json"))
        value["enums"][0]["values"][0]["value"] = 1 << 32
        with self.mutated_root(value) as root:
            with self.assertRaisesRegex(ModelError, "integer <= 4294967295"):
                load_kernel_uapi(Path(root))


if __name__ == "__main__":
    unittest.main()
