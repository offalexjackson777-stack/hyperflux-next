# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.generators.readme import markdown as readme_markdown
from hfxdev.governance import load_github_governance
from hfxdev.licensing import (
    expression_for_path,
    load_licensing_policy,
    verify_licensing_policy,
)
from hfxdev.local_companion import load_local_companion
from hfxdev.model import ModelError, load_json
from hfxdev.portal_content import home_content
from hfxdev.portal_metadata import canonical_url
from hfxdev.portal_model import load_portal_config
from hfxdev.public_readiness import public_readiness


class RepositoryExperienceContracts(unittest.TestCase):
    def test_public_readiness_is_one_projection_for_readme_and_pages(self) -> None:
        readiness = public_readiness(ROOT)
        portal = load_portal_config(ROOT)
        self.assertEqual(
            load_json(ROOT / "generated" / "public-readiness.json"), readiness
        )
        readme = readme_markdown(load_github_governance(ROOT), readiness, portal)
        home = home_content(portal, readiness)
        for section in ("publication", "evidence"):
            self.assertIn(readiness[section]["summary"], readme)
            self.assertIn(readiness[section]["summary"], home)
        software = readiness["software"]
        self.assertIn(software["summary"], readme)
        self.assertIn(
            f"{software['gates_ready']}/{software['gates_total']}", home
        )
        self.assertEqual(readiness["portal_hardware_access"], "none")
        for route_id in (
            "home",
            "installation",
            "device-lab",
            "architecture",
            "repository-atlas",
            "repository-state",
        ):
            self.assertIn(canonical_url(portal, portal.route(route_id).path), readme)

    def test_local_companion_is_loopback_read_only_and_privacy_safe(self) -> None:
        contract = load_local_companion(ROOT)
        self.assertEqual(contract["base_url"], "http://127.0.0.1:47427")
        self.assertTrue(contract["snapshot"]["read_only"])
        self.assertEqual(
            contract["snapshot"]["evidence_states"],
            ["active", "sleeping", "disconnected", "unknown"],
        )
        self.assertEqual(contract["write_capabilities"]["default_state"], "disabled")
        self.assertLessEqual(contract["write_capabilities"]["maximum_ttl_seconds"], 300)
        self.assertTrue(all(value is False for value in contract["privacy"].values()))

    def test_local_companion_rejects_relaxed_write_or_privacy_boundaries(self) -> None:
        canonical = load_json(ROOT / "runtime" / "local-companion.json")
        mutations = (
            ("persistent writes", lambda value: value["write_capabilities"].update(maximum_ttl_seconds=301)),
            ("direct USB", lambda value: value["privacy"].update(direct_usb_access=True)),
            ("non-loopback", lambda value: value.update(base_url="http://0.0.0.0:47427")),
        )
        for label, mutate in mutations:
            with self.subTest(boundary=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "runtime").mkdir()
                value = copy.deepcopy(canonical)
                mutate(value)
                (root / "runtime" / "local-companion.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
                with self.assertRaises(ModelError):
                    load_local_companion(root)

    def test_license_policy_covers_spdx_sources_and_one_root_license(self) -> None:
        result = verify_licensing_policy(ROOT)
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["checked_spdx_files"], 100)
        self.assertEqual(result["unknown_license_files"], 0)
        self.assertEqual(
            sorted(path.name for path in ROOT.iterdir() if path.name.startswith("LICENSE")),
            ["LICENSE", "LICENSES"],
        )
        policy = load_licensing_policy(ROOT)
        self.assertEqual(
            expression_for_path(policy, "driver/kernel/uapi/hyperflux_next.h"),
            "GPL-2.0 WITH Linux-syscall-note",
        )
        self.assertEqual(expression_for_path(policy, "README.md"), "GPL-2.0-only")

    def test_ambiguous_root_license_document_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "governance").mkdir()
            shutil.copy2(ROOT / "governance" / "licensing.json", root / "governance")
            shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
            shutil.copytree(ROOT / "LICENSES", root / "LICENSES")
            (root / "LICENSE-DECISION.md").write_text("ambiguous\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "ambiguous root license"):
                verify_licensing_policy(root)


if __name__ == "__main__":
    unittest.main()
