# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.distribution_native import (
    NativePackageContext,
    tree_digest,
    tree_files,
)
from hfxdev.distribution_rpm import (
    RPM_BUILDHOST,
    RPM_CONFIGURATION,
    RPM_LICENSE_PATH,
    _rpm_scripts,
    _rpm_spec,
    _rpm_version,
)
from hfxdev.distributions import load_distribution_catalog
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError
from hfxdev.package_pipeline import ArtifactSet


class RpmPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_linux_runtime(ROOT)
        cls.catalog = load_distribution_catalog(ROOT)
        cls.target = cls.catalog.targets["rpm"]

    def context(self, root: Path) -> NativePackageContext:
        package_root = root / "package-root"
        files = {
            "etc/hyperflux-next/bridge.json": ("{}\n", 0o640),
            "usr/share/licenses/hyperflux-next/LICENSE": ("license\n", 0o644),
            "usr/lib/hyperflux-next/value": ("stable\n", 0o755),
        }
        for relative, (content, mode) in files.items():
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            path.chmod(mode)
        payload_files = tree_files(package_root)
        return NativePackageContext(
            repository_root=ROOT,
            workspace_root=root,
            package_root=package_root,
            packages=root / "packages",
            runtime=self.runtime,
            catalog=self.catalog,
            target=self.target,
            artifacts=ArtifactSet(
                root=ROOT,
                revision="a" * 40,
                source_date_epoch=1_700_000_000,
                install_manifest_sha256="b" * 64,
                linux_runtime_sha256="c" * 64,
                python="3.14.6",
                target="x86_64-unknown-linux-gnu",
                artifacts=(),
                omitted=(),
            ),
            architecture="x86_64",
            payload_sha256=tree_digest(package_root, payload_files),
            payload_file_count=len(payload_files),
        )

    def test_development_version_sorts_before_a_future_stable_version(self) -> None:
        self.assertEqual(_rpm_version("0.0.0-dev.1"), "0.0.0~dev.1")
        with self.assertRaises(ModelError):
            _rpm_version("unsafe/version")

    def test_spec_uses_catalog_metadata_and_preserves_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec = _rpm_spec(self.context(Path(directory)))
        self.assertIn(f"Name: {self.runtime.product.package_name}\n", spec)
        self.assertIn("Version: 0.0.0~dev.1\n", spec)
        self.assertIn("BuildArch: x86_64\n", spec)
        self.assertIn("AutoReqProv: no\n", spec)
        for dependency in self.target.dependencies_for("3.14.6"):
            expected = dependency.replace(">=", " >= ").replace("<", " < ")
            self.assertIn(f"Requires: {expected}\n", spec)
        for item in self.target.optional_dependencies:
            self.assertIn(f"Suggests: {item.package}\n", spec)
        self.assertIn(f"%config(noreplace) %attr(0640,root,root) {RPM_CONFIGURATION}", spec)
        self.assertIn(f"%license %attr(0644,root,root) {RPM_LICENSE_PATH}", spec)

    def test_scriptlets_are_posix_and_use_safe_dkms_upgrade_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scripts = _rpm_scripts(self.context(Path(directory)))
        self.assertEqual(
            set(scripts), {"pre", "post", "preun", "postun", "posttrans"}
        )
        combined = "\n".join(scripts.values())
        activation = self.runtime.operations.activation_path
        for command in ("fresh-install", "pre-update", "post-update", "pre-remove"):
            self.assertIn(f"{activation} {command}", combined)
        self.assertIn("dkms add", scripts["post"])
        self.assertIn("--rpm_safe_upgrade", scripts["post"])
        self.assertIn("dkms remove", scripts["preun"])
        self.assertIn("--rpm_safe_upgrade", scripts["preun"])
        self.assertIn(self.runtime.update_state_path, scripts["posttrans"])
        for name, script in scripts.items():
            result = subprocess.run(
                ["sh", "-n"],
                input=script,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
        for forbidden in ("modprobe", "unbind", "rebind", "hidraw", "1532"):
            self.assertNotIn(forbidden, combined)

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("rpmbuild", "rpm", "rpm2cpio", "cpio")),
        "RPM build tools are unavailable",
    )
    def test_real_rpm_archive_is_reproducible_and_self_verified(self) -> None:
        from hfxdev.distribution_rpm import build_rpm_package
        from hfxdev.model import sha256_file

        results = []
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for directory in (first, second):
                context = self.context(Path(directory))
                context.packages.mkdir()
                package = build_rpm_package(context)
                results.append((package.name, sha256_file(package), package.stat().st_size))
        self.assertEqual(results[0], results[1])
        self.assertEqual(RPM_BUILDHOST, "hyperflux.invalid")


if __name__ == "__main__":
    unittest.main()
