# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.knowledge import (
    _validate_rules,
    compiled_knowledge_catalog,
    load_knowledge_inputs,
)
from hfxdev.model import ModelError, load_json
from hfxdev.openrazer import extract_openrazer_catalog as extract_openrazer
from hfxdev.openrgb import extract_openrgb_catalog as extract_openrgb
from hfxdev.profiles import load_profile_inputs


class UpstreamImporterTests(unittest.TestCase):
    def test_openrazer_ast_import_preserves_inheritance_without_importing_code(self) -> None:
        source = ROOT / "tests" / "fixtures" / "upstreams" / "openrazer"
        catalog = extract_openrazer(
            source,
            repository="https://github.com/openrazer/openrazer.git",
            commit="0" * 40,
            version="fixture",
            license_expression="GPL-2.0-or-later",
        )
        records = {record["record_id"]: record for record in catalog["records"]}
        mouse = records["openrazer:ExampleMouseWireless"]
        self.assertEqual(mouse["usb_identity"], {"vendor_id": 0x1532, "product_id": 0x00A8})
        self.assertEqual(mouse["source_route"], "vendor-wireless-receiver")
        self.assertEqual(mouse["facts"]["dpi_max"], 30000)
        self.assertEqual(mouse["lighting_topology"], {"matrix_dimensions": [1, 3]})
        self.assertEqual(
            mouse["settings_methods"],
            ["get_battery", "get_dpi_xy", "is_charging", "set_dpi_xy"],
        )

    def test_openrgb_initializer_import_resolves_pids_zones_and_layouts(self) -> None:
        source = ROOT / "tests" / "fixtures" / "upstreams" / "openrgb"
        catalog = extract_openrgb(
            source,
            repository="https://github.com/CalcProgrammer1/OpenRGB.git",
            commit="0" * 40,
            version="fixture",
            license_expression="GPL-2.0-only",
        )
        records = {record["record_id"]: record for record in catalog["records"]}
        mouse = records["openrgb:example_mouse_wireless_device"]
        self.assertEqual(mouse["usb_identity"], {"vendor_id": 0x1532, "product_id": 0x00A8})
        self.assertTrue(mouse["facts"]["detector_registered"])
        self.assertEqual(mouse["lighting_topology"]["application_slot_count"], 3)
        self.assertEqual(
            [zone["name"] for zone in mouse["lighting_topology"]["zones"]],
            ["Logo", "LED Strip"],
        )
        keyboard = records["openrgb:example_keyboard_wired_device"]
        self.assertEqual(keyboard["lighting_topology"]["layout_key"], "example_keyboard_layout")


class DeviceKnowledgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inputs = load_knowledge_inputs(ROOT)
        cls.catalog = compiled_knowledge_catalog(ROOT)

    def test_all_official_candidates_have_one_reviewed_link(self) -> None:
        self.assertEqual(len(self.catalog["candidates"]), 12)
        self.assertEqual(len(self.inputs.links), 12)
        self.assertTrue(all(link["assurance"] == "reviewed-link" for link in self.inputs.links))

    def test_active_snapshot_preserves_the_historical_snapshot(self) -> None:
        self.assertEqual(
            self.catalog["candidate_snapshot_id"],
            "catalog.razer.hyperflux-v2.2026-07-21",
        )
        self.assertEqual(
            self.catalog["candidate_snapshot_history"],
            [
                {
                    "snapshot_id": "catalog.razer.hyperflux-v2.2026-07-13",
                    "retrieved_utc": "2026-07-13",
                    "supersedes_snapshot_id": None,
                    "candidate_count": 11,
                },
                {
                    "snapshot_id": "catalog.razer.hyperflux-v2.2026-07-21",
                    "retrieved_utc": "2026-07-21",
                    "supersedes_snapshot_id": "catalog.razer.hyperflux-v2.2026-07-13",
                    "candidate_count": 12,
                },
            ],
        )

    def test_source_knowledge_never_grants_transport_authority(self) -> None:
        self.assertEqual(
            self.catalog["policy"],
            {
                "source_knowledge_grants_transport_authority": False,
                "reviewed_candidate_links_required": True,
                "unmapped_selected_methods_allowed": False,
                "controls_require_qualified_hyperflux_capabilities": True,
                "source_conflicts_are_not_promoted": True,
            },
        )

    def test_reviewed_product_knowledge_covers_every_candidate(self) -> None:
        self.assertEqual(self.catalog["reviewed_on"], "2026-07-21")
        self.assertEqual(len(self.catalog["reviewed_sources"]), 35)
        self.assertEqual(
            sum(len(candidate["reviewed_facts"]) for candidate in self.catalog["candidates"]),
            191,
        )
        self.assertEqual(
            sum(len(candidate["knowledge_gaps"]) for candidate in self.catalog["candidates"]),
            23,
        )
        for candidate in self.catalog["candidates"]:
            self.assertTrue(candidate["reviewed_facts"])
            self.assertEqual(
                [route["id"] for route in candidate["transport_matrix"]],
                ["wired", "vendor-wireless-receiver", "bluetooth", "hyperflux-receiver"],
            )
            bluetooth = next(
                route for route in candidate["transport_matrix"] if route["id"] == "bluetooth"
            )
            self.assertEqual(bluetooth["product_state"], "documented")
            self.assertEqual(bluetooth["hyperflux_state"], "outside-hyperflux")

    def test_official_sources_and_upstream_reports_remain_distinct(self) -> None:
        source_kinds = {source["kind"] for source in self.catalog["reviewed_sources"]}
        self.assertEqual(
            source_kinds,
            {
                "official-compatibility",
                "official-manual",
                "official-product",
                "official-support",
                "upstream-issue",
            },
        )
        for source in self.catalog["reviewed_sources"]:
            self.assertTrue(source["url"].startswith("https://"))
        for candidate in self.catalog["candidates"]:
            for fact in candidate["reviewed_facts"]:
                if fact["claim_state"] == "reported-upstream":
                    self.assertEqual(fact["source_kinds"], ["upstream-issue"])
                    self.assertEqual(fact["assurance"], "upstream-reported")

    def test_documentation_conflicts_are_preserved_instead_of_normalized(self) -> None:
        keyboard = next(
            candidate
            for candidate in self.catalog["candidates"]
            if candidate["candidate_id"]
            == "razer-blackwidow-v4-low-profile-tenkeyless-hyperspeed"
        )
        conflicts = {
            gap["id"] for gap in keyboard["knowledge_gaps"] if gap["status"] == "source-conflict"
        }
        self.assertEqual(
            conflicts,
            {"macro-documentation-conflict", "programmability-documentation-conflict"},
        )
        macros = next(
            fact for fact in keyboard["reviewed_facts"] if fact["id"] == "macros"
        )
        self.assertIn("Source conflict", macros["value"])
        self.assertFalse(macros["evidence_layers"]["hyperflux_route_mapping"])

    def test_product_documentation_does_not_require_a_pinned_code_record(self) -> None:
        mouse = next(
            candidate
            for candidate in self.catalog["candidates"]
            if candidate["candidate_id"] == "razer-cobra-hyperspeed"
        )
        self.assertEqual(mouse["source_records"], [])
        self.assertEqual(mouse["coverage"]["reviewed_fact_count"], 13)
        self.assertTrue(
            all(not fact["implementation_records"] for fact in mouse["reviewed_facts"])
        )
        self.assertTrue(
            all(not fact["hyperflux_capabilities"] for fact in mouse["reviewed_facts"])
        )

    def test_blackwidow_v4_mini_uses_only_exact_upstream_records(self) -> None:
        keyboard = next(
            candidate
            for candidate in self.catalog["candidates"]
            if candidate["candidate_id"] == "razer-blackwidow-v4-mini-hyperspeed"
        )
        self.assertEqual(keyboard["hyperflux_support"], "candidate-only")
        self.assertEqual(
            [record["record_id"] for record in keyboard["source_records"]],
            [
                "openrazer:RazerBlackWidowV4MiniHyperSpeedWired",
                "openrazer:RazerBlackWidowV4MiniHyperSpeedWireless",
            ],
        )
        self.assertEqual(len(keyboard["reviewed_facts"]), 22)
        self.assertEqual(
            {gap["id"] for gap in keyboard["knowledge_gaps"]},
            {
                "charging-duration-conflict",
                "hyperflux-route",
                "manual-title-conflict",
                "regional-availability",
            },
        )

    def test_every_selected_source_supports_a_fact_or_explicit_gap(self) -> None:
        for candidate in self.catalog["candidates"]:
            used = {
                source_id
                for fact in candidate["reviewed_facts"]
                for source_id in fact["source_ids"]
            }
            used.update(
                source_id
                for gap in candidate["knowledge_gaps"]
                for source_id in gap["source_ids"]
            )
            self.assertEqual(set(candidate["reviewed_source_ids"]), used)

    def test_community_bluetooth_identity_stays_with_the_reported_35k_model(self) -> None:
        candidates = {
            value["candidate_id"]: value for value in self.catalog["candidates"]
        }
        report_id = "github-openrazer-basilisk-bluetooth-2407"
        self.assertNotIn(
            report_id,
            candidates["razer-basilisk-v3-pro"]["reviewed_source_ids"],
        )
        mouse = candidates["razer-basilisk-v3-pro-35k"]
        self.assertIn(report_id, mouse["reviewed_source_ids"])
        identity = next(
            fact for fact in mouse["reviewed_facts"] if fact["id"] == "bluetooth-identity"
        )
        self.assertEqual(identity["source_ids"], [report_id])
        self.assertEqual(identity["assurance"], "upstream-reported")

    def test_product_claims_do_not_absorb_openrgb_matrix_geometry(self) -> None:
        keyboard = next(
            value
            for value in self.catalog["candidates"]
            if value["candidate_id"]
            == "razer-blackwidow-v4-low-profile-tenkeyless-hyperspeed"
        )
        lighting = next(
            fact for fact in keyboard["reviewed_facts"] if fact["id"] == "lighting-topology"
        )
        self.assertEqual(lighting["semantic_capability"], "lighting.chroma")
        self.assertNotIn("6 by 18", lighting["value"])
        openrgb_records = [
            record
            for record in keyboard["source_records"]
            if record["record_id"].startswith("openrgb:")
        ]
        self.assertTrue(openrgb_records)
        self.assertTrue(
            all(
                record["lighting_topology"]["rows"] == 6
                and record["lighting_topology"]["columns"] == 18
                for record in openrgb_records
            )
        )

    def test_source_catalogs_match_exact_integration_pins(self) -> None:
        sources = {value["upstream_id"]: value for value in self.catalog["upstreams"]}
        self.assertEqual(sources["openrazer"]["record_count"], 232)
        self.assertEqual(sources["openrgb"]["record_count"], 112)
        self.assertEqual(
            sources["openrazer"]["commit"],
            "6820f9da169d354bc7e6e93a0aa8683a6bb75792",
        )
        self.assertEqual(
            sources["openrgb"]["commit"],
            "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0",
        )
        self.assertEqual(len(self.inputs.source_catalogs["openrazer"]["records"]), 232)
        self.assertEqual(len(self.inputs.source_catalogs["openrgb"]["records"]), 112)

    def test_naga_knowledge_is_rich_but_fails_closed_without_route_profile(self) -> None:
        naga = next(
            value for value in self.catalog["candidates"] if value["candidate_id"] == "razer-naga-v2-pro"
        )
        self.assertEqual(naga["knowledge_status"], "conflicted")
        self.assertEqual(naga["hyperflux_support"], "candidate-only")
        self.assertEqual(len(naga["settings"]), 14)
        self.assertTrue(all(setting["control_state"] == "blocked" for setting in naga["settings"]))
        self.assertEqual(
            {(item["route"], tuple(item["values"])) for item in naga["source_conflicts"]},
            {
                ("direct-usb", ("2", "3")),
                ("vendor-wireless-receiver", ("2", "3")),
            },
        )

    def test_physically_qualified_devices_enable_only_intersecting_controls(self) -> None:
        mouse = next(
            value
            for value in self.catalog["candidates"]
            if value["candidate_id"] == "razer-basilisk-v3-pro-35k"
        )
        enabled = {
            setting["semantic_capability"]
            for setting in mouse["settings"]
            if setting["control_state"] == "enabled"
        }
        self.assertEqual(
            enabled,
            {
                "identity.device-kind",
                "lighting.brightness",
                "lighting.direct-frame",
                "telemetry.battery-percent",
            },
        )
        self.assertNotIn("input.dpi-xy", enabled)
        self.assertNotIn("input.scroll-mode", enabled)
        qualified_facts = [
            fact for fact in mouse["reviewed_facts"] if fact["assurance"] == "physically-qualified"
        ]
        self.assertEqual(
            [fact["semantic_capability"] for fact in qualified_facts],
            ["lighting.addressable-zones"],
        )
        self.assertTrue(qualified_facts[0]["hyperflux_evidence_claims"])

    def test_openrgb_only_topology_documents_but_does_not_enable_lighting(self) -> None:
        keyboard = next(
            value
            for value in self.catalog["candidates"]
            if value["candidate_id"]
            == "razer-blackwidow-v4-low-profile-tenkeyless-hyperspeed"
        )
        self.assertEqual(len(keyboard["settings"]), 1)
        setting = keyboard["settings"][0]
        self.assertEqual(setting["semantic_capability"], "lighting.direct-frame")
        self.assertEqual(setting["control_state"], "blocked")
        self.assertEqual(setting["source_methods"], [])

    def test_route_qualified_profile_identity_is_present_in_reviewed_sources(self) -> None:
        profiles = {
            profile["profile_id"]: profile for profile in load_profile_inputs(ROOT).profiles
        }
        for candidate in self.catalog["candidates"]:
            if candidate["hyperflux_support"] != "route-qualified":
                continue
            source_identities = {
                (
                    record["usb_identity"]["vendor_id"],
                    record["usb_identity"]["product_id"],
                )
                for record in candidate["source_records"]
            }
            for profile_id in candidate["hyperflux_profile_ids"]:
                identity = profiles[profile_id]["identity"]
                self.assertTrue(
                    any(
                        product_id == identity["product_id"]
                        and vendor_id in {None, identity["vendor_id"]}
                        for vendor_id, product_id in source_identities
                    )
                )

    def test_selected_methods_cannot_be_silently_double_mapped(self) -> None:
        mapping = load_json(ROOT / "knowledge" / "capability-map.json")
        changed = deepcopy(mapping)
        changed["rules"][1]["methods"].append("get_battery")
        changed["rules"][1]["methods"].sort()
        with self.assertRaisesRegex(ModelError, "duplicate mapped upstream method"):
            _validate_rules(changed)


if __name__ == "__main__":
    unittest.main()
