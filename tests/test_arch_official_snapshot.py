# SPDX-License-Identifier: GPL-2.0-only

"""Contracts for the official Arch snapshot and the OpenRazer provider gate."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from arch_split_support import ROOT, candidate_split

from hfxdev.arch_official_snapshot import (
    AUTHORITY,
    RECEIPT_SCHEMA,
    REPOSITORIES,
    RULE,
    SCHEMA,
    SCHEMA_PATH,
    evaluate_provider_alignment,
    import_snapshot,
    load_snapshot,
    read_sync_database,
    upstream_version,
    validate_snapshot,
    vercmp,
    watched_packages,
)
from hfxdev.model import ModelError
from hfxdev.openrazer_native_route import load_native_deployment, load_native_transform


VERSION = load_native_deployment(ROOT).driver.dkms_version
DIGEST = "a" * 64


def _desc(name: str, version: str, base: str, depends: tuple[str, ...] = ()) -> str:
    lines = [
        "%FILENAME%", f"{name}-{version}-any.pkg.tar.zst", "",
        "%NAME%", name, "",
        "%BASE%", base, "",
        "%VERSION%", version, "",
        "%DESC%", f"{name} description", "",
        "%SHA256SUM%", hashlib.sha256(name.encode()).hexdigest(), "",
        "%BUILDDATE%", "1786810889", "",
        "%PACKAGER%", "Packager <packager@example.invalid>", "",
    ]
    if depends:
        lines += ["%DEPENDS%", *depends, ""]
    return "\n".join(lines) + "\n"


def sync_database(path: Path, packages: list[tuple[str, str, str, tuple[str, ...]]]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, version, base, depends in packages:
            data = _desc(name, version, base, depends).encode()
            member = tarfile.TarInfo(f"{name}-{version}/desc")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def official_family(version: str, daemon_depends: str = "openrazer-driver-dkms"):
    release = f"{version}-1"
    return [
        ("openrazer-daemon", release, "openrazer", ("dbus-python", daemon_depends)),
        ("openrazer-driver-dkms", release, "openrazer", ("dkms",)),
        ("openrgb", "1.0-2", "openrgb", ("qt6-base",)),
        ("python-openrazer", release, "openrazer", ("openrazer-daemon",)),
        ("unrelated", "1.0-1", "unrelated", ()),
    ]


def _receipt(root: Path, package: str, version: str) -> dict:
    path = root / "receipts" / f"{package}-{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": RECEIPT_SCHEMA, "package": package, "version": version, "result": "passed"}),
        encoding="utf-8",
    )
    return {
        "package": package,
        "version": version,
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class VersionOrderingContracts(unittest.TestCase):
    PAIRS = (
        ("3.12.4-1", "3.12.4-1", 0),
        ("3.12.4", "3.12.4-1", 0),
        ("3.12.2-2", "3.12.4-1", -1),
        ("3.12.10", "3.12.4", 1),
        ("1.0rc3-2", "1.0-2", -1),
        ("1.0", "1.0.1", -1),
        ("1:1.0-1", "2.0-1", 1),
        ("1.0a", "1.0", -1),
        ("1.0.a", "1.0", 1),
        ("001", "1", 0),
        ("1.0-2", "1.0-10", -1),
    )

    def test_vercmp_matches_pacman_ordering(self) -> None:
        for left, right, expected in self.PAIRS:
            with self.subTest(left=left, right=right):
                self.assertEqual(expected, vercmp(left, right))
                self.assertEqual(-expected, vercmp(right, left))

    def test_vercmp_agrees_with_the_host_tool(self) -> None:
        tool = shutil.which("vercmp")
        if tool is None:
            self.skipTest("vercmp is unavailable")
        values = sorted({value for pair in self.PAIRS for value in pair[:2]} | {"2.1.0-1", "2.0.1-1", "0.0.0_dev.1-2"})
        for left in values:
            for right in values:
                output = subprocess.run([tool, left, right], check=True, capture_output=True, text=True).stdout
                expected = max(-1, min(1, int(output)))
                with self.subTest(left=left, right=right):
                    self.assertEqual(expected, vercmp(left, right))

    def test_upstream_version_drops_epoch_and_release(self) -> None:
        self.assertEqual("3.12.4", upstream_version("3.12.4-1"))
        self.assertEqual("3.12.4", upstream_version("2:3.12.4-7"))


class SnapshotImportContracts(unittest.TestCase):
    def test_watched_packages_come_from_the_split_relations(self) -> None:
        self.assertEqual(
            ("openrazer-daemon", "openrazer-driver-dkms", "openrgb", "polychromatic", "python-openrazer"),
            watched_packages(candidate_split()),
        )

    def test_import_reads_only_watched_descriptors_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = sync_database(Path(directory, "extra.db"), official_family(VERSION))
            packages = read_sync_database(database, watched_packages(candidate_split()))
            self.assertEqual(
                ["openrazer-daemon", "openrazer-driver-dkms", "openrgb", "python-openrazer"],
                [item["name"] for item in packages],
            )
            daemon = packages[0]
            self.assertEqual(["dbus-python", "openrazer-driver-dkms"], daemon["depends"])
            self.assertEqual("openrazer", daemon["base"])
            self.assertEqual(1786810889, daemon["build_date"])
            self.assertNotIn("packager", json.dumps(packages).lower())

            value = import_snapshot(
                ROOT, {"extra": database}, candidate="2.1.0-1", split=candidate_split()
            )
            self.assertEqual(
                ["observed", "unobserved", "unobserved"],
                [item["state"] for item in value["repositories"]],
            )
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), value["repositories"][0]["database_sha256"])
            with self.assertRaisesRegex(ModelError, "not an official repository"):
                import_snapshot(ROOT, {"community": database}, candidate="2.1.0-1", split=candidate_split())

    def test_committed_snapshot_is_valid_and_targets_the_candidate(self) -> None:
        value = load_snapshot(ROOT)
        self.assertEqual("2.1.1-1", value["candidate"])
        self.assertEqual(list(watched_packages(candidate_split())), value["watched_packages"])
        self.assertEqual(list(REPOSITORIES), [item["name"] for item in value["repositories"]])
        self.assertTrue((ROOT / AUTHORITY).is_file())

    def test_validation_rejects_malformed_snapshots(self) -> None:
        base = load_snapshot(ROOT)
        mutations = {
            "schema": lambda value: value.update(schema="other"),
            "order": lambda value: value["repositories"].reverse(),
            "missing repository": lambda value: value["repositories"].pop(),
            "unwatched package": lambda value: value["repositories"][0]["packages"].append(
                {**value["repositories"][0]["packages"][0], "name": "zzz"}
            ),
            "rule": lambda value: value.update(rule="anything"),
            "receipt path": lambda value: value["app_behavior_receipts"].append(
                {"package": "openrazer-daemon", "version": "1-1", "path": "/etc/passwd", "sha256": DIGEST}
            ),
        }
        for label, mutate in mutations.items():
            value = copy.deepcopy(base)
            mutate(value)
            with self.subTest(case=label), self.assertRaises(ModelError):
                validate_snapshot(value)


class ProviderGateContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.handle = tempfile.TemporaryDirectory()
        self.workspace = Path(self.handle.name)
        self.split = candidate_split()

    def tearDown(self) -> None:
        self.handle.cleanup()

    def _snapshot(self, families: dict[str, list] | None = None, receipts: bool = True) -> dict:
        families = families or {name: official_family(VERSION) for name in REPOSITORIES}
        databases = {
            name: sync_database(self.workspace / f"{name}.db", packages)
            for name, packages in families.items()
        }
        value = import_snapshot(ROOT, databases, candidate="2.1.0-1", split=self.split)
        if receipts:
            value["app_behavior_receipts"] = [
                _receipt(self.workspace, package, f"{VERSION}-1")
                for package in ("openrazer-daemon", "python-openrazer")
            ]
        return value

    def _evaluate(self, snapshot: dict):
        return evaluate_provider_alignment(
            ROOT, split=self.split, snapshot=snapshot, receipt_root=self.workspace
        )

    def test_aligned_snapshot_with_receipts_passes(self) -> None:
        result = self._evaluate(self._snapshot())
        self.assertTrue(result.passed, result.problems)
        self.assertIn(("openrazer-driver-dkms", VERSION), result.provided)
        self.assertEqual(load_native_transform(ROOT)["upstream"]["commit"], result.transformed_commit)
        self.assertEqual(9, len(result.official_versions))
        self.assertTrue(json.loads(result.as_json())["passed"])

    def test_gate_fails_closed(self) -> None:
        aligned = {name: official_family(VERSION) for name in REPOSITORIES}
        newer = dict(aligned, **{"extra-testing": official_family("3.12.5")})
        constrained = dict(
            aligned,
            extra=official_family(VERSION, daemon_depends="openrazer-driver-dkms>=3.13.0"),
        )
        missing_driver = dict(
            aligned,
            extra=[item for item in official_family(VERSION) if item[0] != "openrazer-driver-dkms"],
        )
        cases = {
            "unobserved staging": (lambda: self._snapshot({"extra": aligned["extra"], "extra-testing": aligned["extra-testing"]}), "extra-staging was not observed"),
            "newer testing": (lambda: self._snapshot(newer), "extra-testing openrazer-daemon 3.12.5-1 differs"),
            "unsatisfied dependency": (lambda: self._snapshot(constrained), "requires openrazer-driver-dkms>=3.13.0"),
            "missing official driver": (lambda: self._snapshot(missing_driver), "extra does not carry the provided"),
            "no receipts": (lambda: self._snapshot(receipts=False), "no physical application receipt for official openrazer-daemon"),
            "other candidate": (lambda: dict(self._snapshot(), candidate="2.0.1-3"), "taken for 2.0.1-3"),
        }
        for label, (build, message) in cases.items():
            with self.subTest(case=label):
                result = self._evaluate(build())
                self.assertFalse(result.passed)
                self.assertTrue(any(message in problem for problem in result.problems), result.problems)

    def test_receipts_are_bound_by_digest_and_content(self) -> None:
        snapshot = self._snapshot()
        receipt = self.workspace / snapshot["app_behavior_receipts"][0]["path"]
        receipt.write_text(receipt.read_text() + " ", encoding="utf-8")
        result = self._evaluate(snapshot)
        self.assertTrue(any("differs from its digest" in item for item in result.problems))

        snapshot = self._snapshot()
        receipt = self.workspace / snapshot["app_behavior_receipts"][0]["path"]
        receipt.write_text(json.dumps({"schema": RECEIPT_SCHEMA, "package": "openrazer-daemon", "version": f"{VERSION}-1", "result": "failed"}))
        snapshot["app_behavior_receipts"][0]["sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
        result = self._evaluate(snapshot)
        self.assertTrue(any("does not prove" in item for item in result.problems))

    def test_unusable_snapshot_fails_closed(self) -> None:
        result = self._evaluate({"schema": SCHEMA, "$schema": SCHEMA_PATH, "rule": RULE})
        self.assertFalse(result.passed)
        self.assertIn("official snapshot is unusable", result.problems[0])

    def test_committed_snapshot_does_not_pass_without_testing_staging_and_receipts(self) -> None:
        result = evaluate_provider_alignment(ROOT, split=self.split)
        self.assertFalse(result.passed)
        self.assertTrue(any("extra-testing was not observed" in item for item in result.problems))


if __name__ == "__main__":
    unittest.main()
