# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.kernel_canary import kernel_canary_summary, kernel_release, run_kernel_canary
from hfxdev.model import ModelError


def _headers(base: Path, release: str, *, symvers: bool = True) -> Path:
    directory = base / release / "build"
    (directory / "include" / "config").mkdir(parents=True)
    (directory / "Makefile").write_text("all:\n", encoding="utf-8")
    (directory / "include" / "config" / "kernel.release").write_text(
        release + "\n", encoding="utf-8"
    )
    if symvers:
        (directory / "Module.symvers").write_text("", encoding="utf-8")
    return directory


class KernelCanaryTests(unittest.TestCase):
    def test_kernel_release_comes_from_the_prepared_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self.assertEqual(kernel_release(_headers(base, "7.3.0-rc2")), "7.3.0-rc2")
            unprepared = base / "unprepared"
            unprepared.mkdir()
            (unprepared / "Makefile").write_text("all:\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "no release identity"):
                kernel_release(unprepared)
            for hostile in ("../escape", ".."):
                (unprepared / "include" / "config").mkdir(parents=True, exist_ok=True)
                (unprepared / "include" / "config" / "kernel.release").write_text(
                    hostile, encoding="utf-8"
                )
                with self.subTest(release=hostile), self.assertRaises(ModelError):
                    kernel_release(unprepared)

    def test_every_kernel_and_driver_is_built_and_failures_do_not_stop_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stable = _headers(base, "7.2.6-arch1-1")
            mainline = _headers(base, "7.3.0-rc2", symvers=False)
            duplicate = _headers(base / "again", "7.2.6-arch1-1")
            calls: list[tuple[str, str, dict[str, str] | None]] = []

            def build(_root, build_directory, _source, module_directory, **options):
                calls.append(
                    (kernel_release(build_directory), module_directory.name, options["environment"])
                )
                if module_directory.name == "openrazer" and build_directory == mainline:
                    raise ModelError("Patched OpenRazer driver build failed with exit status 2")

            prepared = SimpleNamespace(root=base / "openrazer")
            with (
                patch("hfxdev.kernel_canary._openrazer_source", return_value=base),
                patch(
                    "hfxdev.kernel_canary.prepare_openrazer_native_source",
                    return_value=prepared,
                ),
                patch("hfxdev.kernel_canary._build_kernel_module_copy", side_effect=build),
            ):
                output = base / "out"
                status = run_kernel_canary(ROOT, output, [stable, mainline, duplicate])

            self.assertEqual(status, 1)
            self.assertEqual(
                calls,
                [
                    ("7.2.6-arch1-1", "hyperflux-next", None),
                    ("7.2.6-arch1-1", "openrazer", None),
                    ("7.3.0-rc2", "hyperflux-next", {"KBUILD_MODPOST_WARN": "1"}),
                    ("7.3.0-rc2", "openrazer", {"KBUILD_MODPOST_WARN": "1"}),
                ],
            )
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["schema"], "hyperflux-kernel-canary-result-v1")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                [(kernel["release"], kernel["symbols_checked"]) for kernel in result["kernels"]],
                [("7.2.6-arch1-1", True), ("7.3.0-rc2", False)],
            )
            summary = (output / "summary.md").read_text(encoding="utf-8")
            self.assertIn("| `7.3.0-rc2` | Builds | **Fails** |", summary)
            self.assertIn("never gates merges", summary)
            self.assertEqual(summary, kernel_canary_summary(result))

    def test_header_directories_must_be_explicit_and_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for values in ([], [Path("relative/build")], [Path(temporary) / "missing"]):
                with self.subTest(values=values), self.assertRaises(ModelError):
                    run_kernel_canary(ROOT, Path(temporary) / "out", values)


if __name__ == "__main__":
    unittest.main()
