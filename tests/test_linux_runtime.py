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
    default_bridge_configuration,
    desktop_bridge_proxy_service,
    desktop_bridge_socket,
    dkms_configuration,
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
            {
                "$schema",
                "schema",
                "product",
                "bridge",
                "desktop_access",
                "kernel",
                "operations",
            },
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
            f"/usr/src/{kernel.dkms_name}-{kernel.dkms_version}",
        )
        self.assertEqual(kernel.dkms_version, "2.2.0")

    def test_dkms_identity_and_output_match_arch_hook_contract(self) -> None:
        kernel = self.runtime.kernel
        source_name = PurePosixPath(kernel.source_directory).name
        hook_name, hook_version = source_name.rsplit("-", 1)
        self.assertEqual((hook_name, hook_version), (kernel.dkms_name, kernel.dkms_version))

        config = dkms_configuration(self.runtime)
        self.assertIn(f'PACKAGE_NAME="{hook_name}"', config)
        self.assertIn(f'PACKAGE_VERSION="{hook_version}"', config)
        self.assertIn('BUILT_MODULE_LOCATION[0]="."', config)
        self.assertIn('MAKE[0]="make KDIR=${kernel_source_dir} dkms"', config)
        self.assertNotIn("MO=", config)
        self.assertNotIn("CLEAN=", config)

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
        self.assertLess(len(bridge.socket_path.encode()), 108)
        self.assertNotEqual(bridge.kernel_access_group, bridge.client_group)
        self.assertEqual(bridge.directory_modes.runtime, 0o2750)
        self.assertEqual(bridge.directory_modes.state, 0o0700)
        self.assertEqual(bridge.directory_modes.configuration, 0o2751)
        self.assertGreater(
            bridge.restoration.max_pending_claims,
            bridge.restoration.max_pending_triggers,
        )
        self.assertGreater(
            bridge.restoration.authority_window_ms,
            bridge.restoration.lease_duration_ms,
        )
        desktop = self.runtime.desktop_access
        self.assertEqual(
            desktop.bridge_socket_path(1000),
            "/run/user/1000/hyperflux-next-bridge.sock",
        )
        self.assertEqual(desktop.bridge_socket_mode, 0o600)
        self.assertLess(len(desktop.bridge_socket_path(4_294_967_294).encode()), 108)

    def test_generated_linux_policy_is_hardened_and_non_activating(self) -> None:
        relay_unit = "hyperflux-next-openrazer-relay.service"
        service = systemd_service(self.runtime, wanted_units=(relay_unit,))
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
        self.assertIn(f"Wants={relay_unit}", service)
        self.assertIn(f"Before={relay_unit}", service)
        self.assertNotIn("hyperflux-next-activation.service", service)
        self.assertNotIn("hyperflux-next-confirmation.service", service)
        self.assertNotIn("Requires=", service)
        self.assertIn(
            f"RuntimeDirectoryMode={self.runtime.bridge.directory_modes.runtime:04o}",
            service,
        )
        self.assertIn(
            f"StateDirectoryMode={self.runtime.bridge.directory_modes.state:04o}",
            service,
        )
        self.assertIn(
            "ConfigurationDirectoryMode="
            f"{self.runtime.bridge.directory_modes.configuration:04o}",
            service,
        )

        self.assertEqual(
            self.runtime.operations.activation_path,
            "/usr/lib/hyperflux-next/hyperflux-next-activate",
        )

        rules = udev_rules(self.runtime)
        self.assertIn('MODE="0660"', rules)
        self.assertNotIn('MODE="0666"', rules)
        self.assertNotIn("SYSTEMD_WANTS", rules)

        users = sysusers(self.runtime)
        account = next(line for line in users.splitlines() if line.startswith("u "))
        self.assertTrue(account.endswith(f" {self.runtime.bridge.state_directory}"))
        self.assertNotIn("nologin", users)
        self.assertIn(f"g {self.runtime.bridge.client_group} - -", users)
        self.assertIn(
            f"m {self.runtime.bridge.service_account} {self.runtime.bridge.client_group}",
            users,
        )
        directories = tmpfiles(self.runtime)
        self.assertIn(
            f"d {self.runtime.bridge.state_directory} 0700", directories
        )

        desktop_socket = desktop_bridge_socket(self.runtime)
        desktop_proxy = desktop_bridge_proxy_service(self.runtime)
        self.assertIn("ListenStream=/run/user/%i/hyperflux-next-bridge.sock", desktop_socket)
        self.assertIn("SocketMode=0600", desktop_socket)
        self.assertIn("SocketUser=%i", desktop_socket)
        self.assertIn("WantedBy=user-runtime-dir@%i.service", desktop_socket)
        self.assertIn("DefaultDependencies=no", desktop_socket)
        self.assertIn("Conflicts=shutdown.target", desktop_socket)
        self.assertIn("Before=shutdown.target", desktop_socket)
        self.assertNotIn("sockets.target", desktop_socket)
        self.assertNotIn("SocketGroup=", desktop_socket)
        self.assertIn("User=%i", desktop_proxy)
        self.assertIn(f"Group={self.runtime.bridge.client_group}", desktop_proxy)
        self.assertIn(
            "ExecStart=/usr/lib/systemd/systemd-socket-proxyd "
            f"{self.runtime.bridge.socket_path}",
            desktop_proxy,
        )
        self.assertIn("NoNewPrivileges=yes", desktop_proxy)
        self.assertNotIn("SupplementaryGroups=", desktop_proxy)
        self.assertNotIn("user-runtime-dir@.service.d", desktop_socket + desktop_proxy)

    def test_configuration_directory_allows_doctor_traversal_without_public_listing(
        self,
    ) -> None:
        bridge = self.runtime.bridge
        mode = bridge.directory_modes.configuration
        self.assertEqual(mode & 0o007, 0o001)
        self.assertEqual(mode & 0o006, 0)
        self.assertIn(
            f"d {bridge.configuration_directory} 2751 root "
            f"{bridge.kernel_access_group} -",
            tmpfiles(self.runtime),
        )
        configuration = next(
            payload
            for payload in load_json(ROOT / "packaging" / "install.json")["payloads"]
            if payload["id"] == "default-configuration"
        )
        self.assertEqual(configuration["mode"], "0644")

    def test_default_configuration_is_operational_and_restoration_off(self) -> None:
        config = default_bridge_configuration(self.runtime)
        self.assertIn('"mode": "qualified-live"', config)
        self.assertIn('"enabled": false', config)
        self.assertIn('"mode": "0660"', config)

    def test_disconnected_activation_promotes_only_operational_mode(self) -> None:
        activation = (
            ROOT
            / "crates"
            / "hfx-ops"
            / "src"
            / "bin"
            / "hyperflux-next-activate.rs"
        ).read_text().partition("#[cfg(test)]")[0]
        self.assertIn("enable_operational_configuration", activation)
        self.assertIn("BRIDGE_CONFIGURATION_FILE_PATH", activation)

        configuration = (
            ROOT / "crates" / "hfx-ops" / "src" / "configuration.rs"
        ).read_text()
        operational = configuration.partition(
            "pub fn enable_operational_configuration"
        )[2].partition("/// Atomically enables profile-qualified writes")[0]
        self.assertIn("config.mode = BridgeMode::QualifiedLive", operational)
        self.assertNotIn("restoration.enabled = true", operational)

    def test_installed_cli_has_no_mutating_or_device_lab_commands(self) -> None:
        source = (
            ROOT / "crates" / "hfx-ops" / "src" / "bin" / "hyperfluxctl.rs"
        ).read_text().partition("#[cfg(test)]")[0]
        self.assertNotIn('"--repair"', source)
        # The installed qualification command only reports; the console
        # server and the evidence lifecycle stay source-only.
        self.assertIn('"qualification" => qualification(rest)', source)
        self.assertNotIn("serve_qualification_console", source)
        self.assertNotIn("qualification_lifecycle", source)
        self.assertNotIn('"native" =>', source)
        self.assertNotIn("restart_bridge", source)
        self.assertNotIn("enable_qualification_configuration", source)
        self.assertNotIn("request_system_suspend", source)

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

        public_state = copy.deepcopy(self.value)
        public_state["bridge"]["directory_modes"]["state"] = "0755"
        cases.append(public_state)

        unshared_configuration = copy.deepcopy(self.value)
        unshared_configuration["bridge"]["directory_modes"]["configuration"] = "0700"
        cases.append(unshared_configuration)

        doctor_inaccessible_configuration = copy.deepcopy(self.value)
        doctor_inaccessible_configuration["bridge"]["directory_modes"][
            "configuration"
        ] = "2750"
        cases.append(doctor_inaccessible_configuration)

        publicly_listable_configuration = copy.deepcopy(self.value)
        publicly_listable_configuration["bridge"]["directory_modes"][
            "configuration"
        ] = "2755"
        cases.append(publicly_listable_configuration)

        public_desktop_socket = copy.deepcopy(self.value)
        public_desktop_socket["desktop_access"]["bridge_socket_mode"] = "0660"
        cases.append(public_desktop_socket)

        unreviewed_proxy = copy.deepcopy(self.value)
        unreviewed_proxy["desktop_access"]["socket_proxy_path"] = "/usr/bin/socat"
        cases.append(unreviewed_proxy)

        for index, value in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "runtime").mkdir()
                shutil.copytree(ROOT / "uapi", root / "uapi")
                shutil.copy2(ROOT / "Cargo.toml", root / "Cargo.toml")
                (root / "runtime" / "linux.json").write_text(
                    __import__("json").dumps(value), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_linux_runtime(root)

    def test_cargo_workspace_version_mirrors_the_product_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "runtime", root / "runtime")
            shutil.copytree(ROOT / "uapi", root / "uapi")
            manifest = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
            version = load_linux_runtime(ROOT).product.version
            (root / "Cargo.toml").write_text(
                manifest.replace(f'version = "{version}"', 'version = "0.0.0"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ModelError, "Cargo workspace version"):
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
