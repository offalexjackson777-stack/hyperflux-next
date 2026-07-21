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
    _run_openrgb_adapter_contracts,
    _run_openrgb_thread_sanitizer,
)


class VerificationGuardTests(unittest.TestCase):
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

    def test_openrgb_release_and_sanitizer_lanes_share_complete_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "openrgb"
            source.mkdir()
            (source / "OpenRGBPluginInterface.h").write_text("// fixture\n", encoding="utf-8")
            node = SimpleNamespace(timeout_seconds=300)
            with patch.dict(os.environ, {"HFX_OPENRGB_SOURCE_DIR": str(source)}):
                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_adapter_contracts(root, node)
                    self.assertEqual(command.call_count, 3)
                    release_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=Release", release_configure)
                    self.assertNotIn("-DHFX_OPENRGB_THREAD_SANITIZER=ON", release_configure)
                    self.assertEqual(command.call_args_list[2].args[1][0], "ctest")
                    self.assertIsNone(command.call_args_list[2].kwargs["environment"])

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


if __name__ == "__main__":
    unittest.main()
