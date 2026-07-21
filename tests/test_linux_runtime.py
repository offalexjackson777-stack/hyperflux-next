# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import shutil
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.linux_runtime import (
    activation_service,
    confirmation_service,
    default_bridge_configuration,
    systemd_service,
    sysusers,
    tmpfiles,
    udev_rules,
)
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, load_json


class LinuxRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = load_json(ROOT / "runtime" / "linux.json")
        cls.runtime = load_linux_runtime(ROOT)

    def test_runtime_authority_is_canonical_and_machine_independent(self) -> None:
        self.assertEqual(self.value["schema"], "hyperflux-linux-runtime-v1")
        self.assertEqual(
            set(self.value),
            {"$schema", "schema", "product", "bridge", "kernel", "operations"},
        )
        bridge = self.runtime.bridge
        self.assertRegex(bridge.service_account, r"^[a-z_][a-z0-9_-]{0,31}$")
        self.assertRegex(bridge.socket_name, r"^[a-z0-9][a-z0-9_.-]{0,63}$")
        self.assertEqual(PurePosixPath(bridge.runtime_directory).parts[:2], ("/", "run"))
        self.assertEqual(PurePosixPath(bridge.state_directory).parts[:3], ("/", "var", "lib"))
        self.assertEqual(
            PurePosixPath(bridge.configuration_directory).parts[:2], ("/", "etc")
        )
        for section in self.value.values():
            if isinstance(section, dict):
                for item in section.values():
                    self.assertNotIn("/home/", str(item))

    def test_kernel_names_are_bounded_and_not_product_presentation(self) -> None:
        kernel = self.runtime.kernel
        for value in (kernel.module_name, kernel.dkms_name, kernel.device_prefix):
            self.assertRegex(value, r"^[a-z][a-z0-9_-]{0,63}$")
            self.assertIsNone(re.search(r"razer|mouse|keyboard|hard|cloth", value))
        self.assertEqual(
            kernel.source_directory,
            f"/usr/src/{kernel.dkms_name}-{self.runtime.product.version}",
        )

    def test_derived_runtime_paths_are_bounded_and_unique(self) -> None:
        bridge = self.runtime.bridge
        self.assertEqual(bridge.socket_path, "/run/hyperflux-next/bridge.sock")
        self.assertEqual(bridge.socket_lock_path, "/run/hyperflux-next/bridge.lock")
        self.assertEqual(bridge.state_file_path, "/var/lib/hyperflux-next/bridge-state.json")
        self.assertEqual(
            bridge.identity_secret_file_path,
            "/var/lib/hyperflux-next/receiver-identity.key",
        )
        self.assertEqual(
            bridge.configuration_file_path, "/etc/hyperflux-next/bridge.json"
        )
        self.assertEqual(
            self.runtime.update_state_path,
            "/var/lib/hyperflux-next/package-update.json",
        )
        self.assertEqual(
            self.runtime.operations.python_module_directory,
            "/usr/lib/hyperflux-next/python",
        )
        self.assertLess(len(bridge.socket_path.encode()), 108)
        self.assertNotEqual(bridge.kernel_access_group, bridge.client_group)
        self.assertGreater(
            bridge.restoration.max_pending_claims,
            bridge.restoration.max_pending_triggers,
        )
        self.assertGreater(
            bridge.restoration.authority_window_ms,
            bridge.restoration.lease_duration_ms,
        )

    def test_generated_linux_policy_is_hardened_and_non_activating(self) -> None:
        service = systemd_service(self.runtime)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertIn("CapabilityBoundingSet=\n", service)
        self.assertIn(f"Group={self.runtime.bridge.client_group}", service)
        self.assertIn(
            f"SupplementaryGroups={self.runtime.bridge.kernel_access_group}", service
        )
        self.assertNotIn("modprobe", service)
        self.assertNotIn("ExecStartPre", service)
        self.assertIn(
            f"Requires={self.runtime.operations.activation_service_unit}", service
        )
        self.assertIn(
            f"Wants={self.runtime.operations.confirmation_service_unit}", service
        )

        prepare = activation_service(self.runtime)
        confirm = confirmation_service(self.runtime)
        self.assertIn(" prepare-start", prepare)
        self.assertIn(" confirm-start", confirm)
        for unit in (prepare, confirm):
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn(self.runtime.bridge.configuration_directory, unit)
            self.assertIn(self.runtime.bridge.state_directory, unit)
            self.assertNotIn("modprobe", unit)

        rules = udev_rules(self.runtime)
        self.assertIn('MODE="0660"', rules)
        self.assertNotIn('MODE="0666"', rules)
        self.assertNotIn("SYSTEMD_WANTS", rules)

        users = sysusers(self.runtime)
        self.assertIn(f"g {self.runtime.bridge.client_group} - -", users)
        self.assertIn(
            f"m {self.runtime.bridge.service_account} {self.runtime.bridge.client_group}",
            users,
        )
        directories = tmpfiles(self.runtime)
        self.assertIn(
            f"d {self.runtime.bridge.state_directory} 0700", directories
        )

    def test_default_configuration_is_read_only_and_restoration_off(self) -> None:
        config = default_bridge_configuration(self.runtime)
        self.assertIn('"mode": "read-only"', config)
        self.assertIn('"enabled": false', config)
        self.assertIn('"mode": "0660"', config)

    def test_semantic_loader_rejects_cross_file_drift(self) -> None:
        cases = []

        same_groups = copy.deepcopy(self.value)
        same_groups["bridge"]["client_group"] = same_groups["bridge"]["kernel_access_group"]
        cases.append(same_groups)

        stale_source = copy.deepcopy(self.value)
        stale_source["kernel"]["source_directory"] = "/usr/src/hid-hyperflux-next-9.9.9"
        cases.append(stale_source)

        noncanonical_path = copy.deepcopy(self.value)
        noncanonical_path["bridge"]["executable_path"] = "/opt/hyperflux-next/bin/bridge"
        cases.append(noncanonical_path)

        unsafe_bound = copy.deepcopy(self.value)
        unsafe_bound["operations"]["max_support_bundle_bytes"] = 1_000_000_000
        cases.append(unsafe_bound)

        undersized_history = copy.deepcopy(self.value)
        undersized_history["bridge"]["limits"]["lease_history_capacity"] = 1
        cases.append(undersized_history)

        undersized_commands = copy.deepcopy(self.value)
        undersized_commands["bridge"]["limits"]["command_queue_capacity"] = 1
        cases.append(undersized_commands)

        undersized_restore_claims = copy.deepcopy(self.value)
        undersized_restore_claims["bridge"]["restoration"]["max_pending_claims"] = 1
        cases.append(undersized_restore_claims)

        expired_restore_authority = copy.deepcopy(self.value)
        expired_restore_authority["bridge"]["restoration"]["authority_window_ms"] = 1_000
        expired_restore_authority["bridge"]["restoration"]["lease_duration_ms"] = 1_000
        cases.append(expired_restore_authority)

        for index, value in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "runtime").mkdir()
                shutil.copytree(ROOT / "uapi", root / "uapi")
                (root / "runtime" / "linux.json").write_text(
                    __import__("json").dumps(value), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_linux_runtime(root)

    def test_actor_latency_bound_covers_one_maximum_kernel_dispatch(self) -> None:
        uapi = load_json(ROOT / "uapi" / "kernel-uapi.json")
        maximum_dispatch_ms = (
            uapi["limits"]["max_frames"]
            * self.runtime.kernel.control_transfer_timeout_ms
            + (uapi["limits"]["max_transaction_delay_us"] + 999) // 1_000
        )
        self.assertEqual(self.runtime.bridge.limits.dispatches_per_tick, 1)
        self.assertEqual(self.runtime.bridge.restoration.claims_per_tick, 1)
        self.assertGreaterEqual(
            self.runtime.bridge.timing.actor_response_timeout_ms,
            maximum_dispatch_ms + 1_000,
        )


if __name__ == "__main__":
    unittest.main()
