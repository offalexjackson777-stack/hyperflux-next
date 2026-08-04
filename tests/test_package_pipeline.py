# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.install import load_install_manifest
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError, sha256_file
from hfxdev.package_pipeline import (
    _base_environment,
    _build_cargo,
    _build_cmake,
    _component_directory_modes,
    _inspect_staged_files,
    _normalize_tree,
    _openrazer_dkms_projection_files,
    _openrazer_source_file_projection,
    _private_stage_prefixes,
    _provenance_inputs,
    _provenance_materials,
    _source_tree_has_license,
    _tree_digest,
    load_artifact_set,
)
from hfxdev.toolchains import load_toolchain_pins


class PackagePipelineTests(unittest.TestCase):
    def test_cargo_builds_use_their_declared_profiles_and_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            environment = {"CARGO_TARGET_DIR": str(output / "target")}
            install = load_install_manifest(ROOT)
            builds = [
                install.build("bridge-daemon"),
                install.build("openrazer-native-relay"),
            ]
            commands: list[list[str]] = []
            copied: list[Path] = []

            def capture_run(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                label: str,
                timeout_seconds: int = 900,
            ) -> None:
                del cwd, environment, label, timeout_seconds
                commands.append(command)

            def capture_copy(
                source: Path,
                artifacts_root: Path,
                build: object,
                **kwargs: object,
            ) -> object:
                del artifacts_root, build, kwargs
                copied.append(source)
                return object()

            with (
                patch("hfxdev.package_pipeline._run", side_effect=capture_run),
                patch(
                    "hfxdev.package_pipeline._copy_artifact",
                    side_effect=capture_copy,
                ),
            ):
                _build_cargo(ROOT, output, environment, builds)

            self.assertEqual(
                [
                    (command[command.index("--profile") + 1], command[-1])
                    for command in commands
                ],
                [
                    ("release", "hyperflux-next-bridge"),
                    ("relay", "hyperflux-next-uhid-relay"),
                ],
            )
            self.assertEqual(
                copied,
                [
                    output / "target/release/hyperflux-next-bridge",
                    output / "target/relay/hyperflux-next-uhid-relay",
                ],
            )

    def test_source_tree_license_contract_accepts_legacy_and_reuse_layouts(self) -> None:
        self.assertTrue(_source_tree_has_license({"LICENSE"}))
        self.assertTrue(
            _source_tree_has_license({"LICENSES/GPL-2.0-or-later.txt"})
        )
        self.assertFalse(_source_tree_has_license({"LICENSES/README.md"}))
        self.assertFalse(_source_tree_has_license({"nested/LICENSE"}))

    def test_openrazer_dkms_projection_accepts_reuse_licenses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            projection = root / "projection"
            for path in (
                prepared / "Makefile",
                prepared / "install_files" / "dkms" / "dkms.conf",
                prepared / "driver" / "Makefile",
                prepared / "driver" / "driver.c",
                prepared / "driver" / "driver.h",
                prepared / "LICENSES" / "GPL-2.0-or-later.txt",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture:{path.name}\n", encoding="utf-8")

            selected = _openrazer_dkms_projection_files(prepared, projection)
            destinations = {
                destination.relative_to(projection).as_posix()
                for _, destination in selected
            }

            self.assertIn("LICENSES/GPL-2.0-or-later.txt", destinations)
            self.assertNotIn("LICENSE", destinations)
            self.assertIn("driver/driver.c", destinations)
            self.assertIn("driver/driver.h", destinations)

    def test_openrazer_dkms_projection_requires_a_regular_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            projection = root / "projection"
            prepared.mkdir()

            with self.assertRaisesRegex(ModelError, "no regular license text"):
                _openrazer_dkms_projection_files(prepared, projection)

    def test_openrazer_access_files_keep_authoritative_destinations_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            rules = prepared / "install_files/udev/99-razer.rules"
            helper = prepared / "install_files/udev/razer_mount"
            rules.parent.mkdir(parents=True)
            rules.write_text('GROUP="openrazer"\n', encoding="utf-8")
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            install = load_install_manifest(ROOT)

            expected = {
                "openrazer-native-udev-rules": (
                    "/usr/lib/udev/rules.d/99-razer.rules",
                    0o644,
                    'GROUP="openrazer"\n',
                ),
                "openrazer-native-mount-helper": (
                    "/usr/lib/udev/razer_mount",
                    0o755,
                    "#!/bin/sh\n",
                ),
            }
            for build_id, (destination, mode, content) in expected.items():
                artifact = _openrazer_source_file_projection(
                    root / "artifacts",
                    install.build(build_id),
                    SimpleNamespace(root=prepared),
                )
                self.assertEqual(artifact.kind, "source-file")
                self.assertEqual(artifact.destination, destination)
                self.assertEqual(artifact.mode, mode)
                self.assertEqual(artifact.path.stat().st_mode & 0o777, mode)
                self.assertEqual(artifact.path.read_text(encoding="utf-8"), content)

    def test_openrazer_access_file_rejects_a_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            source = prepared / "install_files/udev/99-razer.rules"
            source.parent.mkdir(parents=True)
            target = root / "outside.rules"
            target.write_text('GROUP="openrazer"\n', encoding="utf-8")
            source.symlink_to(target)
            build = load_install_manifest(ROOT).build("openrazer-native-udev-rules")

            with self.assertRaisesRegex(ModelError, "transformed source file is missing"):
                _openrazer_source_file_projection(
                    root / "artifacts",
                    build,
                    SimpleNamespace(root=prepared),
                )

    def test_openrgb_build_uses_the_source_bound_native_transform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            upstream = output / "openrgb-upstream"
            upstream.mkdir()
            native = output / "work" / "openrgb-native-native-source"
            build = load_install_manifest(ROOT).build("openrgb-native")
            commands: list[tuple[list[str], dict[str, str]]] = []

            def capture_run(
                command: list[str],
                *,
                cwd: Path,
                environment: dict[str, str],
                label: str,
                timeout_seconds: int = 900,
            ) -> None:
                del cwd, label, timeout_seconds
                commands.append((command, environment.copy()))

            expected = object()
            with (
                patch(
                    "hfxdev.package_pipeline._pinned_source",
                    return_value=upstream,
                ),
                patch(
                    "hfxdev.package_pipeline.prepare_openrgb_native_source",
                    return_value=SimpleNamespace(root=native),
                ) as prepare,
                patch("hfxdev.package_pipeline._run", side_effect=capture_run),
                patch(
                    "hfxdev.package_pipeline._copy_artifact",
                    return_value=expected,
                ),
            ):
                actual = _build_cmake(
                    ROOT,
                    output,
                    {},
                    build,
                    {"openrgb-source": upstream},
                    "a" * 40,
                )

            self.assertIs(actual, expected)
            prepare.assert_called_once_with(ROOT, upstream, native)
            configure, environment = commands[0]
            self.assertIn(f"-DHFX_OPENRGB_SOURCE_DIR={upstream}", configure)
            self.assertIn(f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native}", configure)
            self.assertIn(f"-ffile-prefix-map={upstream}=.upstream/openrgb-source", environment["CXXFLAGS"])
            self.assertIn(
                f"-ffile-prefix-map={native}=.upstream/openrgb-source-native",
                environment["CXXFLAGS"],
            )

    def _artifact_manifest(self, directory: Path) -> Path:
        install = load_install_manifest(ROOT)
        runtime = load_linux_runtime(ROOT)
        pins = load_toolchain_pins(ROOT)
        inputs = _provenance_inputs(ROOT, install, runtime)
        artifacts = []
        for build in install.builds:
            artifact_kind = {
                "source-transform": "source-tree",
                "source-transform-file": "source-file",
            }.get(build.kind, build.kind)
            path = directory / "files" / build.id / (build.target or build.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"artifact:{build.id}\n".encode())
            value = {
                "build_id": build.id,
                "distribution_component": build.distribution_component,
                "kind": artifact_kind,
                "path": path.relative_to(directory).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "mode": f"{(build.mode or 0o644):04o}",
            }
            value["destination"] = build.destination
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
                ROOT, inputs, {"openrgb-source", "openrazer-source"}
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
            self.assertEqual(
                len(artifacts.artifacts),
                len(load_install_manifest(ROOT).builds),
            )
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

    def test_tree_normalization_preserves_authoritative_configuration_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration = root / "etc" / "hyperflux-next"
            configuration.mkdir(parents=True)
            sibling = root / "usr" / "share" / "hyperflux-next"
            sibling.mkdir(parents=True)
            _normalize_tree(
                root,
                1_700_000_000,
                (("/etc/hyperflux-next", 0o2750),),
            )
            self.assertEqual(configuration.stat().st_mode & 0o7777, 0o2750)
            self.assertEqual(sibling.stat().st_mode & 0o7777, 0o0755)

    def test_only_core_staging_requires_the_configuration_directory(self) -> None:
        runtime = load_linux_runtime(ROOT)
        self.assertEqual(
            _component_directory_modes(runtime, "linux"),
            ((runtime.bridge.configuration_directory, 0o2751),),
        )
        self.assertEqual(_component_directory_modes(runtime, "full"), ())

    def test_private_build_path_is_rejected_from_staged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "usr" / "share" / "leak.txt"
            file.parent.mkdir(parents=True)
            private_path = "/" + "home/private/checkout"
            file.write_text(f"source={private_path}\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "private build path"):
                _inspect_staged_files(root, (os.fsencode(ROOT),))

    def test_canonical_container_workspace_is_public_but_scratch_is_private(self) -> None:
        canonical = Path("/workspaces/hyperflux-next")
        artifacts = canonical / "build/package-contracts/artifacts"
        prefixes = _private_stage_prefixes(canonical, artifacts, canonical)
        self.assertEqual(prefixes, (os.fsencode(artifacts),))

        checkout = Path("/srv/build/hyperflux-next")
        prefixes = _private_stage_prefixes(checkout, checkout / "artifacts", canonical)
        self.assertIn(os.fsencode(checkout), prefixes)
        self.assertIn(os.fsencode(checkout / "artifacts"), prefixes)

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
        self.assertEqual(environment["CARGO_PROFILE_RELEASE_STRIP"], "symbols")
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["CC"], "clang")
        self.assertEqual(environment["CMAKE_GENERATOR"], "Ninja")

if __name__ == "__main__":
    unittest.main()
