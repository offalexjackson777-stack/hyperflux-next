# SPDX-License-Identifier: GPL-2.0-only

"""Contracts for the local official-Arch readiness dossier (ARCHDIST-001)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from arch_split_support import ROOT, candidate_split
from test_aur_recipe import source_repository

from hfxdev.arch_split import load_arch_split
from hfxdev.aur_dossier import (
    SNAPSHOT_RECEIPT,
    STATUSES,
    build_dossier,
    render_markdown,
    validate_dossier,
    write_dossier,
)
from hfxdev.aur_recipe import generate_aur_recipe, write_aur_recipe
from hfxdev.channel_parity import SCHEMA as CHANNEL_SCHEMA
from hfxdev.model import ModelError


REQUIRED_CLAIMS = (
    "stable-aur",
    "four-output-graph",
    "openrazer-provider",
    "upstream-matrix",
    "packaging-license",
    "license-map",
    "source-binding",
    "clean-chroot",
    "channel-parity",
    "lifecycle",
    "physical-evidence",
    "supported-kernels",
    "security-contact",
    "maintainer-contact",
    "limitations",
)


class DossierContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.handle = tempfile.TemporaryDirectory()
        workspace = Path(cls.handle.name)
        cls.split = candidate_split()
        repository, commit = source_repository(workspace)
        cls.recipe = workspace / "recipe"
        write_aur_recipe(
            generate_aur_recipe(ROOT, commit=commit, source_repository=repository, split=cls.split),
            cls.recipe,
        )
        cls.bare = build_dossier(ROOT, cls.split)
        cls.with_recipe = build_dossier(ROOT, cls.split, recipe_dir=cls.recipe)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.handle.cleanup()

    def _claims(self, dossier) -> dict:
        return {claim.id: claim for claim in dossier.claims}

    def test_every_requirement_has_one_claim(self) -> None:
        for dossier in (self.bare, self.with_recipe):
            self.assertEqual(REQUIRED_CLAIMS, tuple(claim.id for claim in dossier.claims))

    def test_every_claim_cites_its_receipt_and_the_official_snapshot(self) -> None:
        snapshot = (ROOT / "packaging/arch-official-snapshot.json").read_bytes()
        for dossier in (self.bare, self.with_recipe):
            self.assertEqual(snapshot, dossier.receipts[SNAPSHOT_RECEIPT])
            self.assertEqual(hashlib.sha256(snapshot).hexdigest(), dossier.snapshot_sha256)
            for claim in dossier.claims:
                with self.subTest(claim=claim.id):
                    self.assertIn(SNAPSHOT_RECEIPT, claim.receipts)
                    own = [name for name in claim.receipts if name != SNAPSHOT_RECEIPT]
                    self.assertEqual([f"{claim.id}.json"], own)
                    json.loads(dossier.receipts[own[0]])
                    for path in claim.sources:
                        digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                        self.assertEqual(digest, dossier.sources[path])

    def test_unmet_claims_state_their_gaps(self) -> None:
        for dossier in (self.bare, self.with_recipe):
            for claim in dossier.claims:
                with self.subTest(claim=claim.id):
                    self.assertIn(claim.status, STATUSES)
                    if claim.status in {"not-met", "not-evidenced", "static-only"}:
                        self.assertTrue(claim.gaps)

    def test_no_maintainer_contact_or_publication_is_implied(self) -> None:
        claims = self._claims(self.bare)
        self.assertEqual("external", claims["stable-aur"].status)
        self.assertEqual("external", claims["maintainer-contact"].status)
        self.assertFalse(json.loads(self.bare.receipts["maintainer-contact.json"])["contacted"])
        self.assertFalse(json.loads(self.bare.receipts["stable-aur.json"])["published"])
        self.assertEqual("none", json.loads(self.bare.as_json())["maintainer_contact"])
        self.assertIn("no Arch Package Maintainer has been contacted", render_markdown(self.bare))

    def test_claims_follow_the_live_gates(self) -> None:
        claims = self._claims(self.bare)
        self.assertEqual("met", claims["four-output-graph"].status)
        # The committed snapshot leaves extra-testing and extra-staging unobserved.
        self.assertEqual("not-met", claims["openrazer-provider"].status)
        self.assertIn("official extra-testing was not observed for this candidate", claims["openrazer-provider"].gaps)
        self.assertEqual("static-only", claims["lifecycle"].status)
        self.assertEqual("not-evidenced", claims["physical-evidence"].status)
        self.assertEqual("not-evidenced", claims["clean-chroot"].status)
        self.assertEqual("not-evidenced", claims["channel-parity"].status)
        for claim_id in ("packaging-license", "license-map", "source-binding"):
            self.assertEqual("not-evidenced", claims[claim_id].status)

    def test_a_recipe_evidences_licenses_and_discloses_qualification(self) -> None:
        claims = self._claims(self.with_recipe)
        self.assertEqual("met", claims["packaging-license"].status)
        self.assertEqual("met", claims["license-map"].status)
        licenses = json.loads(self.with_recipe.receipts["license-map.json"])["licenses"]
        self.assertEqual({o.package_name for o in self.split.outputs}, set(licenses))
        binding = claims["source-binding"]
        self.assertEqual("not-met", binding.status)
        self.assertTrue(any("is a commit, not a signed tag" in gap for gap in binding.gaps))
        limitations = json.loads(self.with_recipe.receipts["limitations.json"])
        self.assertTrue(any("is a commit, not a signed tag" in item for item in limitations["disclosures"]))

    def test_limitations_collect_every_gap(self) -> None:
        listed = json.loads(self.bare.receipts["limitations.json"])["gaps"]
        expected = [f"{c.id}: {gap}" for c in self.bare.claims if c.id != "limitations" for gap in c.gaps]
        self.assertEqual(expected, listed)
        disclosures = json.loads(self.bare.receipts["limitations.json"])["disclosures"]
        self.assertTrue(any("replaced openrazer-driver-dkms" in item for item in disclosures))

    def test_channel_parity_reports_are_bound_to_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory, "parity.json")
            value = {
                "schema": CHANNEL_SCHEMA,
                "candidate": "2.1.0-1",
                "passed": True,
                "compared_paths": 10,
                "problems": [],
                "disclosures": ["hyperflux-next-full /usr/bin/x compiled bytes differ"],
            }
            report.write_text(json.dumps(value), encoding="utf-8")
            claims = self._claims(build_dossier(ROOT, self.split, channel_report=report))
            self.assertEqual("met", claims["channel-parity"].status)
            report.write_text(json.dumps(value | {"passed": False, "problems": ["drift"]}), encoding="utf-8")
            claims = self._claims(build_dossier(ROOT, self.split, channel_report=report))
            self.assertEqual("not-met", claims["channel-parity"].status)
            self.assertEqual(("drift",), claims["channel-parity"].gaps)
            for mutation, message in (
                ({"candidate": "2.0.1-3"}, "is for 2.0.1-3"),
                ({"schema": "other"}, "is not hyperflux-channel-parity-v1"),
            ):
                report.write_text(json.dumps(value | mutation), encoding="utf-8")
                with self.assertRaisesRegex(ModelError, message):
                    build_dossier(ROOT, self.split, channel_report=report)

    def test_validation_refuses_unsupported_claims(self) -> None:
        claim = self.bare.claims[1]
        cases = {
            "does not cite the official snapshot": dataclasses.replace(claim, receipts=(f"{claim.id}.json",)),
            "has no receipt of its own": dataclasses.replace(claim, receipts=(SNAPSHOT_RECEIPT,)),
            "cites missing receipt": dataclasses.replace(claim, receipts=("gone.json", SNAPSHOT_RECEIPT)),
            "unknown status": dataclasses.replace(claim, status="probably"),
            "without a stated gap": dataclasses.replace(claim, status="not-met", gaps=()),
            "cites unrecorded source": dataclasses.replace(claim, sources=("README.md",)),
        }
        for message, broken in cases.items():
            with self.subTest(message=message):
                claims = tuple(broken if item.id == claim.id else item for item in self.bare.claims)
                with self.assertRaisesRegex(ModelError, message):
                    validate_dossier(dataclasses.replace(self.bare, claims=claims))

    def test_the_two_package_release_is_refused(self) -> None:
        with self.assertRaisesRegex(ModelError, "starts with the four-output split"):
            build_dossier(ROOT, load_arch_split(ROOT))

    def test_written_dossier_binds_receipts_by_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "dossier")
            write_dossier(self.with_recipe, output)
            index = json.loads((output / "dossier.json").read_text(encoding="utf-8"))
            for name, digest in index["receipts"].items():
                self.assertEqual(digest, hashlib.sha256((output / "receipts" / name).read_bytes()).hexdigest())
            markdown = (output / "DOSSIER.md").read_text(encoding="utf-8")
            for claim in self.with_recipe.claims:
                self.assertIn(f"## {claim.requirement}", markdown)
                self.assertIn(f"receipts/{claim.id}.json", markdown)
            with self.assertRaisesRegex(ModelError, "new or empty"):
                write_dossier(self.with_recipe, output)

    def test_cli_writes_the_dossier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "dossier")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "hfx"),
                    "package",
                    "aur-dossier",
                    "--candidate-release",
                    "2.1.0-1",
                    "--output",
                    str(output),
                    "--aur-recipe",
                    str(self.recipe),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                self.with_recipe.as_json(), (output / "dossier.json").read_text(encoding="utf-8")
            )
            self.assertIn("external: 2", result.stdout)


if __name__ == "__main__":
    unittest.main()
