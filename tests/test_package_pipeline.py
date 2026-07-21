# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.install import load_install_manifest
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, sha256_file
from hfxdev.package_pipeline import (
    BuiltArtifact,
    _base_environment,
    _build_python,
    _install_wheels,
    _inspect_staged_files,
    _provenance_inputs,
    _provenance_materials,
    _tree_digest,
    load_artifact_set,
)
from hfxdev.toolchains import load_toolchain_pins


class PackagePipelineTests(unittest.TestCase):
    def _wheel(
        self,
        path: Path,
        distribution: str,
        *,
        package_file: str = "hyperflux_sdk.py",
        entry_point: str | None = None,
    ) -> None:
        normalized = distribution.replace("-", "_")
        info = f"{normalized}-1.dist-info"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(package_file, "VERSION = 1\n")
            archive.writestr(
                f"{info}/METADATA",
                f"Metadata-Version: 2.4\nName: {distribution}\nVersion: 1\n",
            )
            archive.writestr(
                f"{info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{info}/RECORD", "")
            if entry_point is not None:
                archive.writestr(
                    f"{info}/entry_points.txt",
                    "[polychromatic.backends]\n"
                    "hyperflux = hyperflux_sdk:Backend\n"
                    "[console_scripts]\n"
                    f"{entry_point}\n",
                )

    def _artifact_manifest(self, directory: Path) -> Path:
        install = load_install_manifest(ROOT)
        runtime = load_linux_runtime(ROOT)
        pins = load_toolchain_pins(ROOT)
        inputs = _provenance_inputs(ROOT, install, runtime)
        artifacts = []
        for build in install.builds:
            path = directory / "files" / build.id / (
                f"{build.id}.whl" if build.kind == "python-project" else build.target or build.id
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"artifact:{build.id}\n".encode())
            value = {
                "build_id": build.id,
                "kind": "python-wheel" if build.kind == "python-project" else build.kind,
                "path": path.relative_to(directory).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "mode": "0644" if build.kind == "python-project" else "0755",
            }
            if build.destination is not None:
                value["destination"] = build.destination
            else:
                value["distribution"] = build.distribution
            artifacts.append(value)
        manifest = {
            "$schema": "https://hyperflux.dev/schemas/package-build-manifest-v2.json",
            "schema": "hyperflux-package-build-manifest-v2",
            "source": {"revision": "a" * 40, "source_date_epoch": 1_700_000_000},
            "builder": {
                "id": "https://hyperflux.dev/builders/hfx-package-v2",
                "network_access": False,
            },
            "toolchain": {
                "rustc": pins.rustc,
                "cargo": pins.cargo,
                "python": "3.14.0",
                "pip": pins.pip,
                "setuptools": pins.setuptools,
                "wheel": pins.wheel,
                "clang": pins.clang,
                "cmake": pins.cmake,
                "ninja": pins.ninja,
                "target": "x86_64-unknown-linux-gnu",
            },
            "inputs": inputs,
            "materials": _provenance_materials(
                ROOT, inputs, {"openrgb-source"}
            ),
            "artifacts": artifacts,
            "omitted": [],
        }
        path = directory / "package-build-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_artifact_manifest_binds_every_declared_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._artifact_manifest(Path(temporary))
            with patch(
                "hfxdev.package_pipeline._source_identity",
                return_value=("a" * 40, 1_700_000_000),
            ):
                artifacts = load_artifact_set(ROOT, path)
            self.assertEqual(len(artifacts.artifacts), 7)
            self.assertFalse(artifacts.omitted)

    def test_artifact_tamper_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = self._artifact_manifest(directory)
            target = next((directory / "files").rglob("*operations-cli*"))
            if target.is_dir():
                target = next(target.iterdir())
            target.write_bytes(b"tampered\n")
            with patch(
                "hfxdev.package_pipeline._source_identity",
                return_value=("a" * 40, 1_700_000_000),
            ), self.assertRaisesRegex(ModelError, "digest mismatch"):
                load_artifact_set(ROOT, path)

    def test_toolchain_tamper_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._artifact_manifest(Path(temporary))
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["toolchain"]["cargo"] = "cargo 0.0.0 (forged)"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch(
                "hfxdev.package_pipeline._source_identity",
                return_value=("a" * 40, 1_700_000_000),
            ), self.assertRaisesRegex(ModelError, "toolchain differs"):
                load_artifact_set(ROOT, path)

    def test_material_tamper_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._artifact_manifest(Path(temporary))
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["materials"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch(
                "hfxdev.package_pipeline._source_identity",
                return_value=("a" * 40, 1_700_000_000),
            ), self.assertRaisesRegex(ModelError, "materials differ"):
                load_artifact_set(ROOT, path)

    def test_tree_digest_is_order_independent_and_mode_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "usr" / "bin" / "first"
            second = root / "etc" / "second"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            first.chmod(0o755)
            second.chmod(0o644)
            digest = _tree_digest([first, second], root)
            self.assertEqual(digest, _tree_digest([second, first], root))
            second.chmod(0o600)
            self.assertNotEqual(digest, _tree_digest([first, second], root))

    def test_private_build_path_is_rejected_from_staged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "usr" / "share" / "leak.txt"
            file.parent.mkdir(parents=True)
            private_path = "/" + "home/private/checkout"
            file.write_text(f"source={private_path}\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "private build path"):
                _inspect_staged_files(root, (os.fsencode(ROOT),))

    def test_build_environment_remaps_source_output_and_dependency_paths(self) -> None:
        private_root = Path("/" + "home/builder")
        source = (private_root / "source").resolve()
        output = source / "build/candidate"
        cargo_home = str(private_root / "cargo")
        with patch.dict(os.environ, {"CARGO_HOME": cargo_home}):
            environment = _base_environment(source, output, 1_700_000_000)
        for private in (str(source), str(output), cargo_home):
            self.assertIn(f"--remap-path-prefix={private}=", environment["RUSTFLAGS"])
            self.assertIn(f"-ffile-prefix-map={private}=", environment["CFLAGS"])
            self.assertIn(f"-fdebug-prefix-map={private}=", environment["CXXFLAGS"])
        self.assertEqual(environment["CARGO_NET_OFFLINE"], "true")
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["CC"], "clang")
        self.assertEqual(environment["CMAKE_GENERATOR"], "Ninja")

    def test_python_build_uses_only_the_tracked_source_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            build = next(
                item
                for item in load_install_manifest(ROOT).builds
                if item.id == "python-sdk"
            )
            source = ROOT / (build.source or "")
            residue = source / "hyperflux_sdk" / "__pycache__" / "ignored.pyc"
            residue.parent.mkdir(exist_ok=True)
            residue.write_bytes(b"ignored")

            def fake_wheel(command: list[str], **_: object) -> None:
                projected = Path(command[-1])
                self.assertFalse((projected / "hyperflux_sdk/__pycache__").exists())
                self.assertIn("--no-index", command)
                self.assertIn("--no-build-isolation", command)
                wheel_directory = Path(command[command.index("--wheel-dir") + 1])
                self._wheel(
                    wheel_directory / "hyperflux_next_sdk-1-py3-none-any.whl",
                    "hyperflux-next-sdk",
                )

            try:
                with patch("hfxdev.package_pipeline._run", side_effect=fake_wheel):
                    artifact = _build_python(ROOT, output, {}, build)
                self.assertEqual(artifact.distribution, "hyperflux-next-sdk")
            finally:
                residue.unlink(missing_ok=True)

    def test_wheel_staging_is_repeatable_without_persistent_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_path = root / "artifacts" / "hyperflux_next_sdk-1-py3-none-any.whl"
            wheel_path.parent.mkdir()
            self._wheel(
                wheel_path,
                "hyperflux-next-sdk",
                entry_point="hyperflux-tool = hyperflux_sdk:main",
            )
            wheel = BuiltArtifact(
                build_id="python-sdk",
                kind="python-wheel",
                path=wheel_path,
                sha256=sha256_file(wheel_path),
                size=wheel_path.stat().st_size,
                mode=0o644,
                destination=None,
                distribution="hyperflux-next-sdk",
            )

            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            module_directory = "/usr/lib/hyperflux-next/python"
            _install_wheels(first, [wheel], module_directory)
            _install_wheels(second, [wheel], module_directory)

            relative = Path("usr/lib/hyperflux-next/python/hyperflux_sdk.py")
            self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes())
            launcher = first / "usr/bin/hyperflux-tool"
            self.assertTrue(launcher.is_file())
            self.assertIn(module_directory, launcher.read_text(encoding="utf-8"))
            self.assertNotIn("python3.14", launcher.read_text(encoding="utf-8"))

    def test_wheel_file_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheels = []
            for build_id, distribution in (
                ("python-sdk", "hyperflux-next-sdk"),
                ("python-adapter", "hyperflux-next-adapter"),
            ):
                path = root / f"{distribution}-1-py3-none-any.whl"
                self._wheel(path, distribution, package_file="shared.py")
                wheels.append(
                    BuiltArtifact(
                        build_id=build_id,
                        kind="python-wheel",
                        path=path,
                        sha256=sha256_file(path),
                        size=path.stat().st_size,
                        mode=0o644,
                        destination=None,
                        distribution=distribution,
                    )
                )

            stage = root / "stage"
            stage.mkdir()
            with self.assertRaisesRegex(ModelError, "destination collision"):
                _install_wheels(stage, wheels, "/usr/lib/hyperflux-next/python")


if __name__ == "__main__":
    unittest.main()
