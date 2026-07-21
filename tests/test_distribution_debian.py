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

from hfxdev.distribution_debian import (
    DEBIAN_CONFIGURATION,
    DEBIAN_MAINTAINER,
    _debian_control,
    _debian_md5sums,
    _debian_scripts,
    _debian_version,
)
from hfxdev.distribution_native import (
    NativePackageContext,
    tree_digest,
    tree_files,
)
from hfxdev.distributions import load_distribution_catalog
from hfxdev.linux_runtime import load_linux_runtime
from hfxdev.model import ModelError
from hfxdev.package_pipeline import ArtifactSet


class DebianPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_linux_runtime(ROOT)
        cls.catalog = load_distribution_catalog(ROOT)
        cls.target = cls.catalog.targets["debian"]

    def context(self, root: Path) -> NativePackageContext:
        package_root = root / "package-root"
        payload = package_root / "etc/hyperflux-next/bridge.json"
        payload.parent.mkdir(parents=True)
        payload.write_text("{}\n", encoding="utf-8")
        payload.chmod(0o640)
        files = tree_files(package_root)
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
            architecture="amd64",
            payload_sha256=tree_digest(package_root, files),
            payload_file_count=len(files),
        )

    def test_development_version_sorts_before_a_future_stable_version(self) -> None:
        self.assertEqual(_debian_version("0.0.0-dev.1", 1), "0.0.0~dev.1-1")
        with self.assertRaises(ModelError):
            _debian_version("unsafe/version", 1)

    def test_control_comes_from_distribution_and_runtime_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(Path(directory))
            control = _debian_control(context)
        self.assertIn(f"Package: {self.runtime.product.package_name}\n", control)
        self.assertIn("Version: 0.0.0~dev.1-1\n", control)
        self.assertIn("Architecture: amd64\n", control)
        self.assertIn(f"Maintainer: {DEBIAN_MAINTAINER}\n", control)
        self.assertIn("Depends: dkms, libc6, libgcc-s1, python3 (>= 3.11), systemd, udev\n", control)
        self.assertIn("Suggests: openrgb, polychromatic, python3-dbus, python3-gi\n", control)
        self.assertIn("Conflicts: hyperflux-v2-linux-dkms\n", control)
        self.assertIn(f"Description: {self.catalog.description}\n", control)

    def test_maintainer_scripts_are_posix_and_delegate_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scripts = _debian_scripts(self.context(Path(directory)))
        self.assertEqual(set(scripts), {"preinst", "postinst", "prerm", "postrm"})
        activation = self.runtime.operations.activation_path
        combined = "\n".join(scripts.values())
        for command in ("fresh-install", "pre-update", "post-update", "pre-remove"):
            self.assertIn(f"{activation} {command}", combined)
        self.assertIn("/usr/lib/dkms/common.postinst", scripts["postinst"])
        self.assertIn(
            f"dkms remove -m {self.runtime.kernel.dkms_name} "
            f"-v {self.runtime.product.version} --all",
            scripts["prerm"],
        )
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

    def test_configuration_and_payload_checksums_are_declared_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(Path(directory))
            first = _debian_md5sums(context.package_root)
            second = _debian_md5sums(context.package_root)
        self.assertEqual(DEBIAN_CONFIGURATION, "/etc/hyperflux-next/bridge.json")
        self.assertEqual(first, second)
        self.assertRegex(
            first,
            r"^[0-9a-f]{32}  etc/hyperflux-next/bridge\.json\n$",
        )

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is unavailable")
    def test_real_debian_archive_is_reproducible_and_self_verified(self) -> None:
        from hfxdev.distribution_debian import build_debian_package
        from hfxdev.model import sha256_file

        results = []
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for directory in (first, second):
                context = self.context(Path(directory))
                context.packages.mkdir()
                package = build_debian_package(context)
                results.append((package.name, sha256_file(package), package.stat().st_size))
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
