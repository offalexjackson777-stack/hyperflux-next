# SPDX-License-Identifier: GPL-2.0-only

"""The OpenRGB plugin a release builds against its own Qt."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arch_split_support import ROOT  # noqa: E402

from hfxdev import distribution_openrgb  # noqa: E402
from hfxdev.distributions import load_distribution_catalog  # noqa: E402
from hfxdev.model import ModelError  # noqa: E402
from hfxdev.package_pipeline import ArtifactSet  # noqa: E402
from hfxdev.package_pipeline_staging import stage_rootfs  # noqa: E402


class ReleaseOpenRGBPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_distribution_catalog(ROOT)

    def _artifacts(self, root: Path) -> ArtifactSet:
        return ArtifactSet(
            root=root,
            revision="0" * 40,
            source_date_epoch=1,
            install_manifest_sha256="1" * 64,
            linux_runtime_sha256="2" * 64,
            python="3.14",
            target="x86_64-unknown-linux-gnu",
            artifacts=(),
            omitted=(),
        )

    def test_only_a_release_with_its_own_qt_builds_the_plugin(self) -> None:
        self.assertEqual(
            [
                target.id
                for target in self.catalog.targets.values()
                if target.openrgb_plugin_release_build
            ],
            ["debian"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            for target_id in ("arch", "ubuntu", "fedora", "opensuse"):
                with self.subTest(target=target_id), self.assertRaises(ModelError):
                    distribution_openrgb.build_release_openrgb_plugin(
                        ROOT,
                        self._artifacts(Path(temporary)),
                        self.catalog.targets[target_id],
                        Path(temporary) / target_id,
                    )

    def test_debian_build_uses_qt_5_and_keeps_local_paths_out(self) -> None:
        commands: list[tuple[list[str], dict[str, str]]] = []
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            upstream = Path(temporary) / "openrgb"
            native = Path(temporary) / "native"

            def run(command, *, cwd, environment, label, timeout_seconds=900):
                commands.append((command, environment))
                if command[1] == "--build":
                    module = work / "build" / "hyperflux-next-openrgb.so"
                    module.write_bytes(b"\x7fELF")

            with (
                mock.patch.object(distribution_openrgb, "openrgb_source", return_value=upstream),
                mock.patch.object(
                    distribution_openrgb,
                    "prepare_openrgb_native_source",
                    return_value=mock.Mock(root=native),
                ),
                mock.patch.object(distribution_openrgb, "_run", side_effect=run),
                mock.patch.dict("os.environ", {"CMAKE_GENERATOR": "Ninja"}),
            ):
                (work / "build").mkdir(parents=True)
                with mock.patch.object(Path, "mkdir"):
                    module = distribution_openrgb.build_release_openrgb_plugin(
                        ROOT,
                        self._artifacts(Path(temporary)),
                        self.catalog.targets["debian"],
                        work,
                    )
        self.assertEqual(module.name, "hyperflux-next-openrgb.so")
        configure, environment = commands[0]
        self.assertIn("-DHFX_OPENRGB_QT_MAJOR=5", configure)
        self.assertIn(f"-DHFX_OPENRGB_SOURCE_DIR={upstream}", configure)
        self.assertIn(f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native}", configure)
        self.assertNotIn("CMAKE_GENERATOR", environment)
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], "1")
        for path in (upstream, native, ROOT.resolve()):
            self.assertIn(f"-ffile-prefix-map={path}=", environment["CXXFLAGS"])
        build, _ = commands[1]
        self.assertEqual(build[3:5], ["--target", "hyperflux-next-openrgb"])
        strip, _ = commands[2]
        self.assertEqual(strip[:2], ["strip", "--strip-unneeded"])
        self.assertEqual(strip[-1], str(module))
        self.assertEqual(len(commands), 3)

    def test_staging_takes_a_release_module_only_for_full_with_the_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "plugin.so"
            for component, plugin in (("linux", True), ("full", False)):
                with self.subTest(component=component, plugin=plugin):
                    with self.assertRaisesRegex(ModelError, "release plugin module"):
                        stage_rootfs(
                            ROOT,
                            Path(temporary) / "missing.json",
                            Path(temporary) / "stage",
                            component,
                            openrgb_plugin=plugin,
                            openrgb_plugin_module=module,
                        )


if __name__ == "__main__":
    unittest.main()
