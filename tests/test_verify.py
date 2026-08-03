# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.hfxdev.model import ModelError
from tools.hfxdev.verify import (
    _check_build_cache_clock,
    _kernel_build_directories,
    _openrazer_native_payload_errors_propagate,
    _openrazer_payload_retry_is_safe,
    _resolve_schema_reference,
    _run_openrgb_adapter_contracts,
    _run_openrgb_native_route_contracts,
    _run_openrgb_thread_sanitizer,
)


class VerificationGuardTests(unittest.TestCase):
    def test_openrazer_single_attempt_payload_needs_no_retry_guard(self) -> None:
        source = """
static int razer_send_payload(struct device *device)
{
    int err = razer_get_report(device);
    return err;
}
"""
        self.assertTrue(_openrazer_payload_retry_is_safe(source))

    def test_openrazer_retry_requires_an_uncertain_outcome_guard(self) -> None:
        unsafe = """
static int razer_send_payload(struct device *device)
{
    int err;
retry:
    err = razer_get_report(device);
    if (err)
        goto retry;
    return err;
}
"""
        guarded = unsafe.replace(
            "    if (err)\n",
            "    if (razer_hyperflux_error_is_uncertain(err))\n"
            "        return err;\n"
            "    if (err)\n",
        )
        self.assertFalse(_openrazer_payload_retry_is_safe(unsafe))
        self.assertTrue(_openrazer_payload_retry_is_safe(guarded))

    def test_openrazer_native_errors_return_before_response_parsing(self) -> None:
        source = """
static int razer_send_payload_transport(void) { return 0; }
#define razer_send_payload(device, request, response)                         \\
({                                                                            \\
    struct razer_mouse_device *__hyperflux_device = (device);                 \\
    int __hyperflux_error = razer_send_payload_transport(                     \\
        __hyperflux_device, (request), (response));                           \\
    if (__hyperflux_error &&                                                  \\
        razer_is_hyperflux_native(__hyperflux_device->hdev))                  \\
        return __hyperflux_error;                                             \\
    __hyperflux_error;                                                        \\
})
"""
        self.assertTrue(
            _openrazer_native_payload_errors_propagate(
                source, "razer_mouse_device"
            )
        )
        self.assertFalse(
            _openrazer_native_payload_errors_propagate(
                source.replace(
                    "razer_is_hyperflux_native(__hyperflux_device->hdev)",
                    "false",
                ),
                "razer_mouse_device",
            )
        )

    def test_openrazer_direct_payload_definition_cannot_bypass_native_wrapper(self) -> None:
        source = """
static int razer_send_payload_transport(void) { return 0; }
static int razer_send_payload(void) { return 0; }
#define razer_send_payload(device, request, response)                         \\
({                                                                            \\
    struct razer_mouse_device *__hyperflux_device = (device);                 \\
    int __hyperflux_error = razer_send_payload_transport(                     \\
        __hyperflux_device, (request), (response));                           \\
    if (__hyperflux_error &&                                                  \\
        razer_is_hyperflux_native(__hyperflux_device->hdev))                  \\
        return __hyperflux_error;                                             \\
    __hyperflux_error;                                                        \\
})
"""
        self.assertFalse(
            _openrazer_native_payload_errors_propagate(
                source, "razer_mouse_device"
            )
        )

    def test_kernel_build_directories_deduplicate_resolved_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "kernel-headers"
            canonical.mkdir()
            (canonical / "Makefile").write_text("# fixture\n", encoding="utf-8")
            alias = root / "kernel-headers-alias"
            alias.symlink_to(canonical, target_is_directory=True)

            with patch.dict(
                os.environ,
                {"HFX_KERNEL_BUILD_DIRS": os.pathsep.join((str(canonical), str(alias)))},
            ):
                self.assertEqual(_kernel_build_directories(), [canonical])

    def test_missing_and_current_build_caches_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _check_build_cache_clock(root, now=1_000.0)
            artifact = root / "target" / "debug" / "artifact"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"current")
            os.utime(artifact, (1_000.0, 1_000.0))
            _check_build_cache_clock(root, now=1_000.0)

    def test_future_dated_build_cache_is_rejected_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "target" / "debug" / "artifact"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"future")
            os.utime(artifact, (1_100.0, 1_100.0))
            with self.assertRaisesRegex(ModelError, "run cargo clean"):
                _check_build_cache_clock(root, now=1_000.0)

    def test_schema_reference_resolution_accepts_only_local_or_canonical_install_paths(
        self,
    ) -> None:
        root = Path("/checkout")
        document = root / "packaging" / "generated" / "bridge.json"
        self.assertEqual(
            _resolve_schema_reference(
                root,
                document,
                "/usr/share/hyperflux-next/schemas/bridge-config.schema.json",
            ),
            root / "schemas" / "bridge-config.schema.json",
        )
        self.assertEqual(
            _resolve_schema_reference(root, document, "../../schemas/bridge-config.schema.json"),
            root / "schemas" / "bridge-config.schema.json",
        )
        self.assertIsNone(
            _resolve_schema_reference(
                root,
                document,
                "/tmp/bridge-config.schema.json",
            )
        )

    def test_openrgb_release_and_sanitizer_lanes_share_complete_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "openrgb"
            source.mkdir()
            (source / "OpenRGBPluginInterface.h").write_text("// fixture\n", encoding="utf-8")
            native_source = root / "prepared-openrgb"
            native_source.mkdir()
            native_result = native_source / "hyperflux-native-transform-result.json"
            native_result.write_text(
                '{"schema":"hyperflux-openrgb-native-transform-result-v1",'
                '"network_access_executed":false,"hardware_queried":false,'
                '"hardware_write_executed":false}\n',
                encoding="utf-8",
            )
            native_result.chmod(0o600)
            prepared = SimpleNamespace(root=native_source, result=native_result)
            node = SimpleNamespace(timeout_seconds=300)
            with (
                patch("tools.hfxdev.verify._openrgb_source", return_value=source),
                patch(
                    "tools.hfxdev.verify.prepare_openrgb_native_source",
                    return_value=prepared,
                ) as prepare,
            ):
                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_adapter_contracts(root, node)
                    self.assertEqual(command.call_count, 3)
                    release_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=Release", release_configure)
                    self.assertNotIn("-DHFX_OPENRGB_THREAD_SANITIZER=ON", release_configure)
                    self.assertEqual(command.call_args_list[2].args[1][0], "ctest")
                    self.assertIsNone(command.call_args_list[2].kwargs["environment"])

                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_native_route_contracts(root, node)
                    self.assertEqual(command.call_count, 3)
                    native_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=Release", native_configure)
                    self.assertIn(
                        f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native_source}",
                        native_configure,
                    )

                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_thread_sanitizer(root, node)
                    self.assertEqual(command.call_count, 3)
                    sanitizer_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=RelWithDebInfo", sanitizer_configure)
                    self.assertIn("-DHFX_OPENRGB_THREAD_SANITIZER=ON", sanitizer_configure)
                    self.assertEqual(command.call_args_list[2].args[1][0], "ctest")
                    self.assertIn(
                        "halt_on_error=1",
                        command.call_args_list[2].kwargs["environment"]["TSAN_OPTIONS"],
                    )
                self.assertEqual(prepare.call_count, 2)


if __name__ == "__main__":
    unittest.main()
