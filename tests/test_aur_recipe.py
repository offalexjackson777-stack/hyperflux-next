# SPDX-License-Identifier: GPL-2.0-only

"""Contracts for the source-built AUR recipe of the four-output topology."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from arch_split_support import ROOT, candidate_split, makepkg_available

from hfxdev.arch_official_snapshot import ProviderAlignment
from hfxdev.arch_split import load_arch_split
from hfxdev.arch_repository import load_arch_repository_policy
from hfxdev.aur_install import DEFAULT_PKGEXT, install_plans
from hfxdev.aur_recipe import (
    PUBLICATION,
    QUALIFICATION,
    RECIPE_FILES,
    SKIP,
    UpstreamPin,
    _transform_function,
    bound_paths,
    generate_aur_recipe,
    git_archive_sha256,
    load_aur_authority,
    write_aur_recipe,
)
from hfxdev.install import load_install_manifest
from hfxdev.model import ModelError
from hfxdev.openrazer_native_route import load_native_deployment
from hfxdev.openrazer_native_route import load_native_transform as openrazer_transform
from hfxdev.openrgb_native_route import load_native_transform as openrgb_transform


TAG = "v2.0.1-2"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "HyperFlux Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "HyperFlux Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
LOCAL_PATH_MARKERS = (str(ROOT), "/home/", "/tmp/", "/root/")
ALIGNED = ProviderAlignment(
    candidate="2.0.1-2", provided=(), transformed_commit="", official_versions=(), problems=()
)


def _transforms() -> dict[str, dict]:
    return {"openrazer": openrazer_transform(ROOT), "openrgb": openrgb_transform(ROOT)}


def _git(repository: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_IDENTITY, **(environment or {})},
    )
    return result.stdout.strip()


def _init(repository: Path) -> None:
    repository.mkdir(parents=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "tag.gpgsign", "false")


def _commit_all(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def source_repository(workspace: Path) -> tuple[Path, str]:
    """A Git repository holding exactly the files the recipe binds."""

    repository = workspace / "hyperflux-next"
    _init(repository)
    for relative in bound_paths(ROOT, load_install_manifest(ROOT), _transforms()):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return repository, _commit_all(repository, "source")


def upstream_repository(workspace: Path, name: str) -> tuple[Path, str]:
    repository = workspace / f"upstream-{name}"
    _init(repository)
    (repository / "README").write_text(f"synthetic {name}\n", encoding="utf-8")
    return repository, _commit_all(repository, name)


def pinned_transforms(commits: dict[str, str]) -> dict[str, dict]:
    """The real transforms re-pinned to synthetic upstream commits."""

    transforms = copy.deepcopy(_transforms())
    for name, commit in commits.items():
        transforms[name]["upstream"]["commit"] = commit
    return transforms


class GnuPG:
    """A throwaway keyring for signed-tag publication tests."""

    def __init__(self, workspace: Path) -> None:
        self.home = workspace / "gnupg"
        self.home.mkdir(mode=0o700)
        self.environment = {"GNUPGHOME": str(self.home)}

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gpg", "--batch", "--no-tty", *arguments],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **self.environment},
        )

    def generate(self, identity: str) -> str:
        self.run(
            "--pinentry-mode", "loopback", "--passphrase", "",
            "--quick-generate-key", identity, "ed25519", "sign", "never",
        )
        listing = self.run("--with-colons", "--fingerprint", identity).stdout
        return [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")][0]

    def sign_tag(self, repository: Path, fingerprint: str, tag: str, *, force: bool = False) -> None:
        arguments = ["-c", f"user.signingkey={fingerprint}", "tag", "-s", "-m", tag]
        if force:
            arguments.append("-f")
        _git(repository, *arguments, tag, environment=self.environment)

    def close(self) -> None:
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            check=False,
            capture_output=True,
            env={**os.environ, **self.environment},
        )


def _code(pkgbuild: str) -> str:
    """The PKGBUILD without comment lines: what bash actually runs."""

    return "\n".join(line for line in pkgbuild.splitlines() if not line.lstrip().startswith("#"))


def _variable(pkgbuild: str, name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", pkgbuild, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def fake_build_tree(srcdir: Path, manifest) -> None:
    """Lay out what prepare() and build() leave in srcdir, without building."""

    for payload in manifest.files:
        relative = payload.source.resolve().relative_to(ROOT)
        target = srcdir / "hyperflux-next" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(payload.source, target)
    for build in manifest.builds:
        if build.kind == "cargo-binary":
            target = srcdir / "target" / build.cargo_profile / build.target
        elif build.kind == "cmake-module":
            target = srcdir / "build-openrgb-native" / build.output
        elif build.kind == "source-transform-file":
            target = srcdir / "openrazer-native" / build.source
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"built {build.id}\n", encoding="utf-8")
    native = srcdir / "openrazer-native"
    for relative in (
        "Makefile",
        "LICENSE",
        "install_files/dkms/dkms.conf",
        "driver/Makefile",
        *(f"driver/razer{n}.{ext}" for n in ("common", "kbd_driver", "mouse_driver") for ext in ("c", "h")),
        "driver/razercommon.o",
    ):
        target = native / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{relative}\n", encoding="utf-8")


class AurAuthorityContracts(unittest.TestCase):
    def test_authority_requires_signed_tags_and_zero_bsd_packaging(self) -> None:
        authority = load_aur_authority(ROOT)
        self.assertEqual("hyperflux-next", authority.source_name)
        self.assertTrue(authority.repository.startswith("https://"))
        self.assertEqual(("!strip", "!debug", "!lto"), authority.options)
        self.assertIn("cargo", authority.makedepends)
        self.assertNotIn("rust", authority.makedepends)
        value = json.loads((ROOT / "packaging/aur.json").read_text(encoding="utf-8"))
        self.assertEqual("signed-tag", value["source"]["publication_reference"])
        self.assertEqual("0BSD", value["packaging_license"]["spdx"])

    def test_bound_paths_cover_every_authority_and_payload(self) -> None:
        manifest = load_install_manifest(ROOT)
        paths = set(bound_paths(ROOT, manifest, _transforms()))
        for required in (
            "Cargo.lock",
            "packaging/arch-split.json",
            "packaging/aur.json",
            "packaging/install.json",
            "integrations/openrazer/native-route/transform.json",
            "integrations/openrgb/native-route/transform.json",
        ):
            self.assertIn(required, paths)
        for payload in manifest.files:
            self.assertIn(payload.source.resolve().relative_to(ROOT).as_posix(), paths)
        for name, transform in _transforms().items():
            self.assertIn(f"integrations/{name}/native-route/{transform['patch']['path']}", paths)


class QualificationRecipeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace_handle = tempfile.TemporaryDirectory()
        workspace = Path(cls.workspace_handle.name)
        cls.repository, cls.commit = source_repository(workspace)
        cls.split = candidate_split()
        cls.recipe = generate_aur_recipe(
            ROOT,
            commit=cls.commit,
            source_repository=cls.repository,
            split=cls.split,
        )
        cls.pkgbuild = cls.recipe.files["PKGBUILD"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace_handle.cleanup()

    def test_recipe_is_the_flat_four_output_source_build(self) -> None:
        recipe = self.recipe
        self.assertEqual(QUALIFICATION, recipe.mode)
        self.assertEqual(set(RECIPE_FILES), set(recipe.files))
        self.assertEqual("2.0.1-2", recipe.version)
        self.assertEqual(
            "(" + " ".join(f"'{output.package_name}'" for output in self.split.outputs) + ")",
            _variable(self.pkgbuild, "pkgname"),
        )
        self.assertEqual("('x86_64')", _variable(self.pkgbuild, "arch"))
        self.assertEqual("2.0.1", _variable(self.pkgbuild, "pkgver"))
        self.assertEqual("2", _variable(self.pkgbuild, "pkgrel"))
        self.assertEqual(f"'{self.commit}'", _variable(self.pkgbuild, "_commit"))
        self.assertNotIn("validpgpkeys", self.pkgbuild)
        self.assertIn("# Qualification only: source", self.pkgbuild)
        self.assertIn("# Qualification only: openrazer", self.pkgbuild)
        # The committed snapshot does not prove alignment yet, and the
        # qualification recipe says so instead of claiming it.
        self.assertIn("# Qualification only: provider gate: official extra-testing", self.pkgbuild)

    def test_recipe_has_no_install_scripts_dkms_calls_or_local_inputs(self) -> None:
        for name, text in self.recipe.files.items():
            for marker in LOCAL_PATH_MARKERS:
                self.assertNotIn(marker, text, f"{name} names local path {marker}")
        code = _code(self.pkgbuild)
        self.assertNotRegex(code, r"(?m)^\s*install=")
        self.assertNotRegex(code, r"\bdkms (add|build|install|remove|autoinstall)\b")
        self.assertNotRegex(code, r"\b(systemctl|modprobe|udevadm|pacman)\b")
        sources = _variable(self.pkgbuild, "source")
        self.assertNotRegex(sources, r"\.tar|\.zst|\.pkg|file://|https?://(?!github\.com/)")
        self.assertEqual(3, sources.count("::git+https://"))

    def test_source_pins_match_the_transform_authorities(self) -> None:
        sources = _variable(self.pkgbuild, "source")
        for name, transform in _transforms().items():
            self.assertIn(
                f"'{name}::git+{transform['upstream']['repository']}#commit={transform['upstream']['commit']}'",
                sources,
            )
            self.assertIn(transform["patch"]["sha256"], self.pkgbuild)
            for record in transform["files"]:
                self.assertIn(f"{record['source_sha256']}  {record['path']}\n", self.pkgbuild)
                self.assertIn(f"{record['transformed_sha256']}  {record['path']}\n", self.pkgbuild)
        repository = load_aur_authority(ROOT).repository
        self.assertIn(f"'hyperflux-next::git+{repository}#commit={self.commit}'", sources)
        sums = _variable(self.pkgbuild, "sha256sums")
        self.assertEqual(
            f"('{git_archive_sha256(self.repository, self.commit)}' '{SKIP}' '{SKIP}')", sums
        )

    def test_network_is_used_only_by_the_locked_fetch_in_prepare(self) -> None:
        functions = dict(re.findall(r"(?ms)^(\w+)\(\) \{\n(.*?)^\}", self.pkgbuild))
        self.assertIn(
            "cargo fetch --locked --target \"$(rustc -vV | sed -n 's/^host: //p')\"",
            functions["prepare"],
        )
        cargo_lines = [
            line.strip()
            for body in functions.values()
            for line in body.splitlines()
            if line.strip().startswith("cargo ")
        ]
        for line in cargo_lines:
            if line.startswith("cargo fetch"):
                continue
            self.assertIn("--frozen", line)
        self.assertIn("CARGO_NET_OFFLINE=true", functions["_hfx_environment"])
        for stage in ("build", "check"):
            self.assertTrue(functions[stage].lstrip().startswith("_hfx_environment"))
        self.assertIn("cargo test --frozen", functions["check"])
        self.assertNotRegex(self.pkgbuild, r"\b(curl|wget|git clone|git fetch|rustup (install|update))\b")

    def test_cargo_targets_match_the_install_manifest(self) -> None:
        manifest = load_install_manifest(ROOT)
        for build in manifest.builds:
            if build.kind == "cargo-binary":
                self.assertRegex(
                    self.pkgbuild,
                    rf"cargo build --frozen --profile {build.cargo_profile} --package {build.package}[^\n]* --bin {build.target}\b",
                )
            if build.kind == "cmake-module":
                self.assertIn(f"--target {build.target}", self.pkgbuild)

    def test_srcinfo_equals_makepkg_printsrcinfo(self) -> None:
        if not makepkg_available():
            self.skipTest("makepkg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "PKGBUILD").write_text(self.pkgbuild, encoding="utf-8")
            printed = subprocess.run(
                ["makepkg", "--printsrcinfo"],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
            ).stdout
        without_comments = "".join(
            line for line in printed.splitlines(keepends=True) if not line.startswith("#")
        )
        self.assertEqual(without_comments, self.recipe.files[".SRCINFO"])

    def test_srcinfo_dependency_closure_keeps_core_out_of_full(self) -> None:
        names = {output.package_name for output in self.split.outputs}
        depends: dict[str, set[str]] = {}
        current = None
        for line in self.recipe.files[".SRCINFO"].splitlines():
            key, _, value = line.strip().partition(" = ")
            if key == "pkgname":
                current = value
                depends[current] = set()
            elif key == "depends" and current is not None:
                name = re.split(r"[<>=]", value, maxsplit=1)[0]
                if name in names:
                    depends[current].add(name)

        def closure(request: str) -> set[str]:
            seen, pending = set(), [request]
            while pending:
                item = pending.pop()
                if item not in seen:
                    seen.add(item)
                    pending.extend(depends[item])
            return seen

        for plan in install_plans(self.split):
            with self.subTest(edition=plan.edition):
                self.assertEqual(set(plan.packages), closure(plan.request))

    def test_pkgbuild_states_the_exact_edition_commands(self) -> None:
        comments = [line for line in self.pkgbuild.splitlines() if line.startswith("#")]
        for plan in install_plans(self.split):
            with self.subTest(edition=plan.edition):
                self.assertIn(f"# {plan.label} with an AUR helper: {plan.helper_command}", comments)
                for command in plan.manual_commands:
                    self.assertIn(f"#   {command}", comments)
        self.assertNotRegex(_code(self.pkgbuild), r"makepkg")

    def test_makepkg_package_list_matches_the_install_plans(self) -> None:
        if not makepkg_available():
            self.skipTest("makepkg is unavailable")
        core, full = install_plans(self.split)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "PKGBUILD").write_text(self.pkgbuild, encoding="utf-8")
            printed = subprocess.run(
                ["makepkg", "--packagelist"],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C", "PKGDEST": directory, "PKGEXT": DEFAULT_PKGEXT},
            ).stdout
        listed = tuple(PurePosixPath(line).name for line in printed.splitlines() if line)
        self.assertEqual(full.files, listed)
        self.assertLess(set(core.files), set(listed))

    def test_packaging_sources_are_zero_bsd(self) -> None:
        self.assertIn("SPDX-License-Identifier: 0BSD", self.pkgbuild)
        self.assertTrue(self.recipe.files["LICENSE"].startswith("Copyright HyperFlux Next contributors\n"))
        self.assertIn("Permission to use, copy, modify, and/or distribute", self.recipe.files["LICENSE"])
        reuse = self.recipe.files["REUSE.toml"]
        self.assertIn('SPDX-License-Identifier = "0BSD"', reuse)
        self.assertIn('"PKGBUILD", ".SRCINFO"', reuse)

    def test_package_functions_install_exactly_the_split_ownership(self) -> None:
        if not shutil.which("bash") or not shutil.which("install"):
            self.skipTest("bash and install are required")
        manifest = load_install_manifest(ROOT)
        builds = {build.id: build for build in manifest.builds}
        payload_modes = {str(item.destination): item for item in manifest.files}
        with tempfile.TemporaryDirectory() as directory:
            srcdir = Path(directory, "src")
            fake_build_tree(srcdir, manifest)
            for output in self.split.outputs:
                pkgdir = Path(directory, "pkg", output.package_name)
                pkgdir.mkdir(parents=True)
                script = (
                    f"set -e\numask 022\nsource ./PKGBUILD\npackage_{output.package_name}\n"
                )
                Path(directory, "PKGBUILD").write_text(self.pkgbuild, encoding="utf-8")
                subprocess.run(
                    ["bash", "-c", script],
                    cwd=directory,
                    check=True,
                    capture_output=True,
                    env={
                        "PATH": os.environ["PATH"],
                        "srcdir": str(srcdir),
                        "pkgdir": str(pkgdir),
                    },
                )
                installed = {
                    "/" + path.relative_to(pkgdir).as_posix(): path
                    for path in pkgdir.rglob("*")
                    if path.is_file()
                }
                expected = set(output.file_destinations)
                tree_files = {
                    path for path in installed
                    if any(path.startswith(f"{tree}/") for tree in output.tree_destinations)
                }
                self.assertEqual(expected, set(installed) - tree_files, output.id)
                for path in installed:
                    self.assertEqual(output.id, self.split.owner_of(path), path)
                for tree in output.tree_destinations:
                    self.assertEqual(
                        {
                            f"{tree}/Makefile",
                            f"{tree}/dkms.conf",
                            f"{tree}/LICENSE",
                            f"{tree}/driver/Makefile",
                            *(f"{tree}/driver/razer{n}.{ext}" for n in ("common", "kbd_driver", "mouse_driver") for ext in ("c", "h")),
                        },
                        tree_files,
                    )
                for path, file in installed.items():
                    mode = stat.S_IMODE(file.stat().st_mode)
                    if path in payload_modes:
                        payload = payload_modes[path]
                        self.assertEqual(payload.mode, mode, path)
                        self.assertEqual(payload.source.read_bytes(), file.read_bytes(), path)
                    elif path in tree_files:
                        self.assertEqual(0o644, mode, path)
                    else:
                        build = next(
                            builds[item]
                            for item in output.builds
                            if builds[item].destination == path
                        )
                        self.assertEqual(build.mode, mode, path)
                for destination, mode in output.directory_modes:
                    directory_path = pkgdir.joinpath(*PurePosixPath(destination).parts[1:])
                    self.assertEqual(mode, stat.S_IMODE(directory_path.stat().st_mode))


class TransformHelperContracts(unittest.TestCase):
    """The shell transform reproduces the repository transform and fails closed."""

    def _fixture(self, workspace: Path, name: str) -> tuple[UpstreamPin, Path, str]:
        upstream = workspace / "src" / name
        _init(upstream)
        files = {"driver/razercommon.c": "int value = 1;\n"}
        if name == "openrazer":
            deployment = load_native_deployment(ROOT).driver
            modules = "".join(
                f'BUILT_MODULE_NAME[{index}]="{module}"\n'
                for index, module in enumerate(reversed(deployment.modules))
            )
            files["install_files/dkms/dkms.conf"] = (
                f'PACKAGE_NAME="{deployment.dkms_name}"\n'
                f'PACKAGE_VERSION="{deployment.dkms_version}"\n{modules}'
            )
        for relative, text in files.items():
            (upstream / relative).parent.mkdir(parents=True, exist_ok=True)
            (upstream / relative).write_text(text, encoding="utf-8")
        commit = _commit_all(upstream, "upstream")
        (upstream / "driver/razercommon.o").write_bytes(b"\x7fELF")
        patch_text = (
            "--- a/driver/razercommon.c\n"
            "+++ b/driver/razercommon.c\n"
            "@@ -1 +1 @@\n"
            "-int value = 1;\n"
            "+int value = 2;\n"
        )
        patch_relative = "patches/0001-test.patch"
        patch = workspace / "src/hyperflux-next/integrations" / name / "native-route" / patch_relative
        patch.parent.mkdir(parents=True)
        patch.write_text(patch_text, encoding="utf-8")
        digest = lambda text: __import__("hashlib").sha256(text.encode()).hexdigest()  # noqa: E731
        transform = {
            "upstream": {"commit": commit},
            "patch": {"path": patch_relative, "sha256": digest(patch_text)},
            "files": [
                {
                    "path": "driver/razercommon.c",
                    "source_sha256": digest("int value = 1;\n"),
                    "transformed_sha256": digest("int value = 2;\n"),
                }
            ],
        }
        pin = UpstreamPin(id=name, url="https://example.invalid/x.git", commit=commit, checksum=SKIP, transform=transform)
        return pin, upstream, commit

    def _run(self, workspace: Path, pin: UpstreamPin) -> subprocess.CompletedProcess[str]:
        function = "\n".join(_transform_function(pin, "hyperflux-next", load_native_deployment(ROOT)))
        return subprocess.run(
            ["bash", "-c", f"{function}\n_hfx_transform_{pin.id}"],
            capture_output=True,
            text=True,
            env={**os.environ, "srcdir": str(workspace / "src")},
        )

    def test_transform_writes_the_pipeline_result(self) -> None:
        for name in ("openrazer", "openrgb"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                pin, _, commit = self._fixture(workspace, name)
                result = self._run(workspace, pin)
                self.assertEqual(0, result.returncode, result.stderr)
                native = workspace / "src" / f"{name}-native"
                self.assertEqual("int value = 2;\n", (native / "driver/razercommon.c").read_text())
                self.assertFalse((native / ".git").exists())
                self.assertEqual(name == "openrgb", (native / "driver/razercommon.o").exists())
                path = native / "hyperflux-native-transform-result.json"
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                value = json.loads(path.read_text())
                self.assertEqual(commit, value["source_commit"])
                self.assertEqual(pin.transform["patch"]["sha256"], value["patch_sha256"])
                self.assertEqual(
                    [{"path": "driver/razercommon.c", "sha256": pin.transform["files"][0]["transformed_sha256"]}],
                    value["transformed_files"],
                )
                self.assertFalse(value["network_access_executed"])
                self.assertEqual(json.dumps(value, indent=2, sort_keys=True) + "\n", path.read_text())

    def test_transform_refuses_tampered_inputs(self) -> None:
        def tamper_source(workspace: Path, upstream: Path, pin: UpstreamPin) -> UpstreamPin:
            (upstream / "driver/razercommon.c").write_text("int value = 3;\n")
            return pin

        def tamper_patch(workspace: Path, upstream: Path, pin: UpstreamPin) -> UpstreamPin:
            patch = workspace / "src/hyperflux-next/integrations" / pin.id / "native-route" / pin.transform["patch"]["path"]
            patch.write_text(patch.read_text() + "\n")
            return pin

        def wrong_commit(workspace: Path, upstream: Path, pin: UpstreamPin) -> UpstreamPin:
            return UpstreamPin(pin.id, pin.url, "0" * 40, pin.checksum, pin.transform)

        def wrong_result(workspace: Path, upstream: Path, pin: UpstreamPin) -> UpstreamPin:
            transform = copy.deepcopy(pin.transform)
            transform["files"][0]["transformed_sha256"] = "0" * 64
            return UpstreamPin(pin.id, pin.url, pin.commit, pin.checksum, transform)

        def wrong_dkms(workspace: Path, upstream: Path, pin: UpstreamPin) -> UpstreamPin:
            conf = upstream / "install_files/dkms/dkms.conf"
            conf.write_text(conf.read_text().replace("PACKAGE_VERSION", "PACKAGE_VERSION_X"))
            return pin

        cases = {
            "source": ("openrgb", tamper_source),
            "patch": ("openrgb", tamper_patch),
            "commit": ("openrgb", wrong_commit),
            "result": ("openrgb", wrong_result),
            "dkms": ("openrazer", wrong_dkms),
        }
        for label, (name, tamper) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                pin, upstream, _ = self._fixture(workspace, name)
                result = self._run(workspace, tamper(workspace, upstream, pin))
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(
                    (workspace / "src" / f"{name}-native" / "hyperflux-native-transform-result.json").exists()
                )


class PublicationRecipeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("gpg") or not shutil.which("gpgconf"):
            raise unittest.SkipTest("gpg is unavailable")
        cls.workspace_handle = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.workspace_handle.name)
        cls.gnupg = GnuPG(cls.workspace)
        try:
            cls.fingerprint = cls.gnupg.generate("HyperFlux Test Release <release@example.invalid>")
            cls.other = cls.gnupg.generate("Someone Else <other@example.invalid>")
        except Exception:
            cls.gnupg.close()
            cls.workspace_handle.cleanup()
            raise
        cls.repository, cls.commit = source_repository(cls.workspace)
        cls.gnupg.sign_tag(cls.repository, cls.fingerprint, TAG)
        cls.upstreams = {name: upstream_repository(cls.workspace, name) for name in ("openrazer", "openrgb")}
        cls.split = candidate_split()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gnupg.close()
        cls.workspace_handle.cleanup()

    def _generate(self, **overrides):
        arguments = dict(
            tag=TAG,
            source_repository=self.repository,
            source_url=f"file://{self.repository}",
            upstream_checkouts={name: path for name, (path, _) in self.upstreams.items()},
            upstream_urls={name: f"file://{path}" for name, (path, _) in self.upstreams.items()},
            transforms=pinned_transforms({name: commit for name, (_, commit) in self.upstreams.items()}),
            split=self.split,
            signing_fingerprint=self.fingerprint,
            environment=self.gnupg.environment,
            provider_alignment=ALIGNED,
        )
        arguments.update(overrides)
        return generate_aur_recipe(ROOT, **arguments)

    def test_publication_recipe_pins_the_signed_tag(self) -> None:
        recipe = self._generate()
        pkgbuild = recipe.files["PKGBUILD"]
        self.assertEqual(PUBLICATION, recipe.mode)
        self.assertEqual((), recipe.disclosures)
        self.assertNotIn("Qualification only", pkgbuild)
        self.assertNotIn(SKIP, pkgbuild)
        self.assertEqual(f"('{self.fingerprint}')", _variable(pkgbuild, "validpgpkeys"))
        self.assertIn(f"#tag={TAG}?signed'", _variable(pkgbuild, "source"))
        self.assertEqual(self.commit, recipe.source.commit)
        self.assertEqual(f"'{self.commit}'", _variable(pkgbuild, "_commit"))

    def test_project_policy_is_the_default_signer(self) -> None:
        with self.assertRaisesRegex(ModelError, load_arch_repository_policy(ROOT).primary_fingerprint):
            self._generate(signing_fingerprint=None)

    def test_makepkg_verifies_sources_from_an_empty_cache(self) -> None:
        if not makepkg_available() or not shutil.which("git"):
            self.skipTest("makepkg is unavailable")
        recipe = self._generate()
        with tempfile.TemporaryDirectory(dir=self.workspace) as directory:
            build = Path(directory, "recipe")
            write_aur_recipe(recipe, build)
            self.assertEqual(0, self._verifysource(build).returncode)

            # A tag re-pointed to another revision, even one the same key signs,
            # no longer matches the recipe's checksum.
            (self.repository / "Cargo.toml").write_text(
                (ROOT / "Cargo.toml").read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )
            moved = _commit_all(self.repository, "moved")
            try:
                self.gnupg.sign_tag(self.repository, self.fingerprint, TAG, force=True)
                result = self._verifysource(build)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("sha256sums", result.stdout + result.stderr)
            finally:
                _git(self.repository, "reset", "-q", "--hard", self.commit)
                self.gnupg.sign_tag(self.repository, self.fingerprint, TAG, force=True)
                self.assertNotEqual(moved, _git(self.repository, "rev-parse", "HEAD"))

    def _verifysource(self, build: Path) -> subprocess.CompletedProcess[str]:
        cache = Path(tempfile.mkdtemp(dir=self.workspace))
        return subprocess.run(
            ["makepkg", "--verifysource", "--nodeps", "--noconfirm"],
            cwd=build,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                **self.gnupg.environment,
                "SRCDEST": str(cache),
                "BUILDDIR": str(cache / "build"),
                "LC_ALL": "C",
            },
        )

    def test_publication_refusals(self) -> None:
        commits = {name: commit for name, (_, commit) in self.upstreams.items()}

        def lightweight() -> None:
            _git(self.repository, "tag", "-f", "lightweight", self.commit)
            self._generate(tag="lightweight")

        def unsigned() -> None:
            _git(self.repository, "tag", "-f", "-a", "-m", "unsigned", "unsigned", self.commit)
            self._generate(tag="unsigned")

        def other_signer() -> None:
            self.gnupg.sign_tag(self.repository, self.other, "other-signer")
            self._generate(tag="other-signer")

        cases = {
            "lightweight": (lightweight, "lightweight"),
            "unsigned": (unsigned, "not signed"),
            "other signer": (other_signer, "not signed by the project key"),
            "tag and commit": (lambda: self._generate(commit=self.commit), "exactly one"),
            "no upstream checkout": (
                lambda: self._generate(upstream_checkouts={"openrgb": self.upstreams["openrgb"][0]}),
                "openrazer checkout",
            ),
            "upstream lacks pin": (
                lambda: self._generate(transforms=pinned_transforms({**commits, "openrgb": "f" * 40})),
                "does not contain the pinned commit",
            ),
            "provider gate": (
                lambda: self._generate(provider_alignment=None),
                "OpenRazer provider alignment gate failed: .*not observed",
            ),
            "stale provider gate": (
                lambda: self._generate(
                    provider_alignment=ProviderAlignment("2.0.1-1", (), "", (), ())
                ),
                "evaluated for another release",
            ),
            "inactive split": (
                lambda: self._generate(split=load_arch_split(ROOT)),
                "has no four-output AUR recipe",
            ),
        }
        for label, (operation, message) in cases.items():
            with self.subTest(case=label), self.assertRaisesRegex(ModelError, message):
                operation()

    def test_revision_must_carry_the_generating_authorities(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.workspace) as directory:
            repository, _ = source_repository(Path(directory))
            path = repository / "packaging/arch-split.json"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            changed = _commit_all(repository, "drift")
            with self.assertRaisesRegex(ModelError, "differs from the generating tree at packaging/arch-split.json"):
                generate_aur_recipe(ROOT, commit=changed, source_repository=repository, split=self.split)
            shutil.copyfile(ROOT / "packaging/arch-split.json", path)
            (repository / "packaging/aur.json").unlink()
            missing = _commit_all(repository, "missing")
            with self.assertRaisesRegex(ModelError, "does not contain packaging/aur.json"):
                generate_aur_recipe(ROOT, commit=missing, source_repository=repository, split=self.split)


class RecipeOutputContracts(unittest.TestCase):
    def test_output_directory_must_be_new_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = source_repository(Path(directory))
            recipe = generate_aur_recipe(ROOT, commit=commit, source_repository=repository, split=candidate_split())
            target = Path(directory, "out")
            written = write_aur_recipe(recipe, target)
            self.assertEqual(sorted(RECIPE_FILES), sorted(path.name for path in written))
            self.assertTrue(all(path.parent == target for path in written))
            with self.assertRaisesRegex(ModelError, "new or empty"):
                write_aur_recipe(recipe, target)


if __name__ == "__main__":
    unittest.main()
