# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.model import load_json


class LinuxRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_json(ROOT / "runtime" / "linux.json")

    def test_runtime_authority_is_canonical_and_machine_independent(self) -> None:
        self.assertEqual(self.runtime["schema"], "hyperflux-linux-runtime-v1")
        self.assertEqual(set(self.runtime), {"$schema", "schema", "bridge", "kernel"})
        bridge = self.runtime["bridge"]
        self.assertEqual(
            set(bridge),
            {
                "service_account",
                "runtime_directory",
                "socket_name",
                "state_directory",
                "configuration_directory",
            },
        )
        self.assertRegex(bridge["service_account"], r"^[a-z_][a-z0-9_-]{0,31}$")
        self.assertRegex(bridge["socket_name"], r"^[a-z0-9][a-z0-9_.-]{0,63}\.sock$")
        self.assertEqual(PurePosixPath(bridge["runtime_directory"]).parts[:2], ("/", "run"))
        self.assertEqual(PurePosixPath(bridge["state_directory"]).parts[:3], ("/", "var", "lib"))
        self.assertEqual(PurePosixPath(bridge["configuration_directory"]).parts[:2], ("/", "etc"))
        for value in bridge.values():
            self.assertNotIn("/home/", value)

    def test_kernel_names_are_bounded_and_not_product_presentation(self) -> None:
        kernel = self.runtime["kernel"]
        self.assertEqual(set(kernel), {"module_name", "device_prefix"})
        for value in kernel.values():
            self.assertRegex(value, r"^[a-z][a-z0-9_-]{0,63}$")
            self.assertIsNone(re.search(r"razer|mouse|keyboard|hard|cloth", value))

    def test_socket_path_is_derived_once(self) -> None:
        bridge = self.runtime["bridge"]
        socket_path = PurePosixPath(bridge["runtime_directory"]) / bridge["socket_name"]
        self.assertEqual(str(socket_path), "/run/hyperflux-next/bridge.sock")
        self.assertLess(len(str(socket_path).encode()), 108)


if __name__ == "__main__":
    unittest.main()
