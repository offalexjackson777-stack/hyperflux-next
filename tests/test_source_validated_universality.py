# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.knowledge import load_knowledge_inputs
from hfxdev.model import ModelError, load_json
from hfxdev.native_control import load_native_control_policy
from hfxdev.openrazer import (
    _ClassCatalog,
    effective_event_regex,
    extract_openrazer_catalog,
    supported_poll_rates_contract,
)
from hfxdev.profiles import load_profile_inputs
from hfxdev.source_validated import (
    ABSENT,
    BACKEND_REQUIRED,
    SOURCE_VALIDATED,
    _openrazer_constructor_rules,
    _openrazer_integration,
    _source_validated_devices,
    load_source_validated_catalog,
    transformed_source_validated_catalog,
)


OFFICIAL_ROUTES = {
    "razer.1532.00a8.mouse",
    "razer.1532.00ab.mouse",
    "razer.1532.00b0.mouse",
    "razer.1532.00cd.mouse",
    "razer.1532.00db.mouse",
    "razer.1532.025c.keyboard",
    "razer.1532.0290.keyboard",
    "razer.1532.0296.keyboard",
    "razer.1532.02ba.keyboard",
    "razer.1532.02c9.keyboard",
    "razer.1532.02d2.keyboard",
    "razer.1532.02d5.keyboard",
}


def _by_route(devices: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(device["route_id"]): device for device in devices}


def _add_exact_driver_binding(
    source: Path,
    *,
    kind: str,
    product_id: int,
    synthetic_define: str,
    parent_define: str,
) -> None:
    stem = "razerkbd" if kind == "keyboard" else "razermouse"
    header_path = source / "driver" / f"{stem}_driver.h"
    driver_path = source / "driver" / f"{stem}_driver.c"

    header = header_path.read_text(encoding="utf-8")
    header_anchor = f"#define {parent_define} "
    if header.count(header_anchor) != 1:
        raise AssertionError(f"expected one upstream header anchor for {parent_define}")
    header = header.replace(
        header_anchor,
        f"#define {synthetic_define} 0x{product_id:04X}\n{header_anchor}",
        1,
    )
    header_path.write_text(header, encoding="utf-8")

    driver = driver_path.read_text(encoding="utf-8")
    case_anchor = f"case {parent_define}:"
    if driver.count(case_anchor) == 0:
        raise AssertionError(f"upstream driver has no PID branches for {parent_define}")
    driver = driver.replace(
        case_anchor,
        f"case {synthetic_define}:\n    {case_anchor}",
    )
    hid_anchor = "{ HID_USB_DEVICE(USB_VENDOR_ID_RAZER," f"{parent_define}) }}"
    if driver.count(hid_anchor) != 1:
        raise AssertionError(f"expected one upstream HID-table anchor for {parent_define}")
    driver = driver.replace(
        hid_anchor,
        "{ HID_USB_DEVICE(USB_VENDOR_ID_RAZER,"
        f"{synthetic_define}) }},\n    {hid_anchor}",
        1,
    )
    driver_path.write_text(driver, encoding="utf-8")


def _add_synthetic_openrazer_pair(
    source: Path,
    *,
    kind: str,
    wired_class: str,
    wireless_class: str,
    parent_wired_class: str,
    model_name: str,
    wired_pid: int,
    wireless_pid: int,
    parent_wireless_define: str,
    synthetic_wireless_define: str,
) -> None:
    hardware = source / "daemon/openrazer_daemon/hardware"
    path = hardware / ("keyboards.py" if kind == "keyboard" else "mouse.py")
    with path.open("a", encoding="utf-8") as output:
        output.write(
            f"\n\nclass {wired_class}({parent_wired_class}):\n"
            f"    \"\"\"Class for the synthetic {model_name} (Wired) fixture.\"\"\"\n"
            "    USB_VID = 0x1532\n"
            f"    USB_PID = 0x{wired_pid:04X}\n\n\n"
            f"class {wireless_class}({wired_class}):\n"
            f"    \"\"\"Class for the synthetic {model_name} (Wireless) fixture.\"\"\"\n"
            f"    USB_PID = 0x{wireless_pid:04X}\n"
        )
    _add_exact_driver_binding(
        source,
        kind=kind,
        product_id=wireless_pid,
        synthetic_define=synthetic_wireless_define,
        parent_define=parent_wireless_define,
    )


class SourceValidatedUniversalityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        openrazer = os.environ.get("HFX_OPENRAZER_SOURCE_DIR")
        openrgb = os.environ.get("HFX_OPENRGB_SOURCE_DIR")
        if openrazer is None or openrgb is None:
            raise unittest.SkipTest("pinned OpenRazer and OpenRGB sources are required")
        cls.openrazer_source = Path(openrazer).resolve()
        cls.openrgb_source = Path(openrgb).resolve()
        cls.inputs = load_knowledge_inputs(ROOT)
        cls.profile_inputs = load_profile_inputs(ROOT)
        cls.catalog = load_source_validated_catalog(ROOT)

    def test_committed_catalog_is_fresh_exact_and_separates_assurance(self) -> None:
        self.assertEqual(
            transformed_source_validated_catalog(
                ROOT, self.openrazer_source, self.openrgb_source
            ),
            self.catalog,
        )
        devices = self.catalog["devices"]
        by_route = _by_route(devices)
        self.assertEqual(set(by_route), OFFICIAL_ROUTES)
        self.assertEqual(len(devices), 12)
        self.assertEqual(
            sum(device["operational_state"] == SOURCE_VALIDATED for device in devices),
            10,
        )
        self.assertEqual(
            sum(device["operational_state"] == BACKEND_REQUIRED for device in devices),
            2,
        )
        self.assertEqual(
            sum(
                device["integrations"]["openrazer"]["state"] == SOURCE_VALIDATED
                for device in devices
            ),
            8,
        )
        self.assertEqual(
            sum(
                device["integrations"]["openrgb"]["state"] == SOURCE_VALIDATED
                for device in devices
            ),
            9,
        )
        verified = {
            device["route_id"]
            for device in devices
            if device["hardware_assurance"]["state"] == "hardware-verified"
        }
        self.assertEqual(
            verified,
            {"razer.1532.00cd.mouse", "razer.1532.0296.keyboard"},
        )

    def test_canonical_scope_has_exact_official_identity_pairs(self) -> None:
        scope = self.profile_inputs.official_receiver_scope
        self.assertEqual(scope["vendor_id"], 0x1532)
        self.assertEqual(scope["receiver_product_id"], 0x00CF)
        actual = {
            (entry["wireless_product_id"], entry["direct_usb_product_id"])
            for entry in scope["entries"]
        }
        self.assertEqual(
            actual,
            {
                (0x00AB, 0x00AA),
                (0x00CD, 0x00CC),
                (0x025C, 0x025A),
                (0x02C9, 0x02CC),
                (0x02D2, 0x02D4),
                (0x02BA, 0x02B9),
                (0x02D5, 0x02D7),
                (0x00DB, 0x00DA),
                (0x00B0, 0x00AF),
                (0x0290, 0x0292),
                (0x0296, 0x0298),
                (0x00A8, 0x00A7),
            },
        )

    def test_every_openrazer_route_has_one_source_bound_publication_identity(self) -> None:
        for device in self.catalog["devices"]:
            integration = device["integrations"]["openrazer"]
            if integration["state"] != SOURCE_VALIDATED:
                continue
            identity = integration["publication_identity"]
            self.assertEqual(identity["method"], "get_serial")
            self.assertNotEqual(identity["transaction_id"], 0)
            normalized = bytearray(90)
            normalized[1] = identity["transaction_id"]
            normalized[5] = identity["data_size"]
            normalized[6] = identity["command_class"]
            normalized[7] = identity["command_id"]
            normalized[8 : 8 + identity["data_size"]] = bytes(identity["arguments"])
            self.assertEqual(
                hashlib.sha256(normalized).hexdigest(),
                identity["normalized_sha256"],
            )
            self.assertEqual(
                sum(
                    "openrazer" in rule["integrations"]
                    and identity["method"] in rule["methods"]
                    and rule["direction"] == "read"
                    and identity["transaction_id"]
                    in rule.get("transaction_ids", [])
                    for rule in device["rules"]
                ),
                1,
            )

        routes = _by_route(self.catalog["devices"])
        self.assertEqual(
            routes["razer.1532.00cd.mouse"]["integrations"]["openrazer"]
            ["publication_identity"]["transaction_id"],
            0x1F,
        )
        self.assertEqual(
            routes["razer.1532.0296.keyboard"]["integrations"]["openrazer"]
            ["publication_identity"]["transaction_id"],
            0xFF,
        )

    def test_original_basilisk_is_automatic_and_uses_exact_upstream_records(self) -> None:
        device = _by_route(self.catalog["devices"])["razer.1532.00ab.mouse"]
        self.assertEqual(device["operational_state"], SOURCE_VALIDATED)
        self.assertEqual(
            device["hardware_assurance"],
            {"state": "not-hardware-verified", "profile_id": None},
        )
        openrazer = device["integrations"]["openrazer"]
        self.assertEqual(openrazer["class_name"], "RazerBasiliskV3ProWireless")
        self.assertEqual(openrazer["direct_usb_reference"]["product_id"], 0x00AA)
        self.assertTrue(
            {
                "get_battery",
                "get_brightness",
                "get_device_mode",
                "get_dpi_stages",
                "get_dpi_xy",
                "get_idle_time",
                "get_poll_rate",
                "get_scroll_acceleration",
                "get_scroll_mode",
                "get_scroll_smart_reel",
                "is_charging",
                "set_brightness",
                "set_custom_effect",
                "set_device_mode",
                "set_dpi_stages",
                "set_dpi_xy",
                "set_idle_time",
                "set_key_row",
                "set_poll_rate",
                "set_scroll_acceleration",
                "set_scroll_mode",
                "set_scroll_smart_reel",
            }.issubset(openrazer["methods"])
        )
        topology = device["integrations"]["openrgb"]["lighting_topology"]
        self.assertEqual(topology["application_slot_count"], 13)
        self.assertEqual(
            [(zone["name"], zone["columns"]) for zone in topology["zones"]],
            [("Scroll Wheel", 1), ("Logo", 1), ("LED Strip", 11)],
        )

    def test_ripple_requires_route_associated_event_source(self) -> None:
        policy = load_native_control_policy(ROOT)
        self.assertEqual(
            policy["event_source_required_methods"],
            ["set_ripple_effect", "set_ripple_effect_random_colour"],
        )
        required = set(policy["event_source_required_methods"])
        for device in self.catalog["devices"]:
            openrazer = device["integrations"]["openrazer"]
            if openrazer["state"] != SOURCE_VALIDATED:
                continue
            self.assertTrue(required.isdisjoint(openrazer["methods"]), device["route_id"])
            self.assertFalse(
                any(required & set(rule["methods"]) for rule in device["rules"]),
                device["route_id"],
            )

        keyboard = _by_route(self.catalog["devices"])["razer.1532.0296.keyboard"]
        openrazer = keyboard["integrations"]["openrazer"]
        self.assertIn("set_key_row", openrazer["methods"])
        self.assertIn("set_custom_effect", openrazer["methods"])
        self.assertTrue(
            any("set_key_row" in rule["methods"] for rule in keyboard["rules"])
        )
        self.assertTrue(
            any("set_custom_effect" in rule["methods"] for rule in keyboard["rules"])
        )

    def test_global_event_matcher_is_ambiguous_for_same_model_receivers(self) -> None:
        route = next(
            value
            for value in load_json(
                ROOT / "integrations/openrazer/native-route/catalog.json"
            )["routes"]
            if value["wireless_product_id"] == 0x0296
        )
        matcher = re.compile(route["event_file_regex"])
        aliases = (
            "hyperflux-rx-a-DSV2Pro_TKL_000000000000-if01-event-mouse",
            "hyperflux-rx-b-DSV2Pro_TKL_000000000000-if01-event-mouse",
        )
        self.assertEqual([value for value in aliases if matcher.match(value)], list(aliases))

    def test_generic_parser_cases_are_source_truthful(self) -> None:
        records = self.inputs.source_catalogs["openrazer"]["records"]
        naga = next(
            deepcopy(record)
            for record in records
            if record["record_id"] == "openrazer:RazerNagaV2ProWireless"
        )
        self.assertIsNone(
            effective_event_regex(
                self.openrazer_source / naga["source_location"]["path"],
                naga["source_device_key"],
            )
        )
        integration, _rules = _openrazer_integration(
            ROOT,
            self.openrazer_source,
            _ClassCatalog(self.openrazer_source),
            naga,
            _openrazer_constructor_rules(ROOT),
        )
        self.assertEqual(integration["state"], BACKEND_REQUIRED)
        self.assertEqual(
            integration["reason"],
            "uncovered-wire-methods:set_none_effect,set_static_effect",
        )

        routes = _by_route(self.catalog["devices"])
        blackwidow_v3 = routes["razer.1532.025c.keyboard"]
        self.assertEqual(
            blackwidow_v3["integrations"]["openrazer"]["state"], SOURCE_VALIDATED
        )
        self.assertEqual(
            blackwidow_v3["integrations"]["openrazer"]["direct_usb_reference"][
                "product_id"
            ],
            0x025A,
        )
        charge_rules = [
            rule
            for rule in blackwidow_v3["rules"]
            if "set_charge_colour" in rule["methods"]
        ]
        self.assertEqual(
            {(rule["command_class"], rule["command_id"]) for rule in charge_rules},
            {(0x03, 0x01), (0x03, 0x10)},
        )

        blackwidow_v4_tkl = routes["razer.1532.02d5.keyboard"]
        poll_rule = next(
            rule
            for rule in blackwidow_v4_tkl["rules"]
            if rule["methods"] == ["set_poll_rate"]
        )
        self.assertEqual(
            (poll_rule["command_class"], poll_rule["command_id"], poll_rule["data_sizes"]),
            (0x00, 0x40, [2]),
        )
        self.assertEqual(poll_rule["matcher"]["fields"][0]["bytes"], [0])

    def test_adapter_results_are_independent_and_receiver_is_never_a_device(self) -> None:
        devices = _by_route(self.catalog["devices"])
        self.assertNotIn("razer.1532.00cf.mouse", devices)
        self.assertNotIn("razer.1532.00cf.keyboard", devices)

        naga = devices["razer.1532.00a8.mouse"]
        self.assertEqual(naga["integrations"]["openrazer"]["state"], BACKEND_REQUIRED)
        self.assertEqual(naga["integrations"]["openrgb"]["state"], SOURCE_VALIDATED)
        self.assertEqual(naga["operational_state"], SOURCE_VALIDATED)

        openrazer_only = devices["razer.1532.02ba.keyboard"]
        self.assertEqual(openrazer_only["integrations"]["openrazer"]["state"], SOURCE_VALIDATED)
        self.assertEqual(openrazer_only["integrations"]["openrgb"]["state"], ABSENT)
        self.assertTrue(
            all(rule["integrations"] == ["openrazer"] for rule in openrazer_only["rules"])
        )

        openrgb_only = devices["razer.1532.02d2.keyboard"]
        self.assertEqual(openrgb_only["integrations"]["openrazer"]["state"], ABSENT)
        self.assertEqual(openrgb_only["integrations"]["openrgb"]["state"], SOURCE_VALIDATED)
        self.assertTrue(
            all(rule["integrations"] == ["openrgb"] for rule in openrgb_only["rules"])
        )

        for route in ("razer.1532.00db.mouse", "razer.1532.02c9.keyboard"):
            self.assertEqual(devices[route]["operational_state"], BACKEND_REQUIRED)
            self.assertEqual(devices[route]["integrations"]["openrazer"]["state"], ABSENT)
            self.assertEqual(devices[route]["integrations"]["openrgb"]["state"], ABSENT)
            self.assertEqual(devices[route]["rules"], [])

    def test_nonofficial_merged_records_never_enter_receiver_scope(self) -> None:
        self.assertEqual(set(_by_route(self.catalog["devices"])), OFFICIAL_ROUTES)
        broad_identities = {
            (
                record["usb_identity"]["vendor_id"],
                record["usb_identity"]["product_id"],
                record["device_kind"],
            )
            for name in ("openrazer", "openrgb")
            for record in self.inputs.source_catalogs[name]["records"]
        }
        self.assertGreater(len(broad_identities), len(OFFICIAL_ROUTES))
        self.assertNotIn("razer.1532.0062.mouse", _by_route(self.catalog["devices"]))

    def test_ambiguous_exact_upstream_identity_fails_only_that_application(self) -> None:
        openrazer_catalog = deepcopy(self.inputs.source_catalogs["openrazer"])
        record = next(
            deepcopy(value)
            for value in openrazer_catalog["records"]
            if value["record_id"] == "openrazer:RazerBasiliskV3ProWireless"
        )
        record["record_id"] += ":duplicate-fixture"
        openrazer_catalog["records"].append(record)
        devices = _by_route(
            _source_validated_devices(
                ROOT,
                self.openrazer_source,
                self.openrgb_source,
                openrazer_catalog,
                self.inputs.source_catalogs["openrgb"],
            )
        )
        ambiguous = devices["razer.1532.00ab.mouse"]
        self.assertEqual(ambiguous["integrations"]["openrazer"]["state"], BACKEND_REQUIRED)
        self.assertEqual(ambiguous["integrations"]["openrgb"]["state"], SOURCE_VALIDATED)
        self.assertEqual(ambiguous["operational_state"], SOURCE_VALIDATED)

    def test_operational_generation_does_not_require_hardware_assurance(self) -> None:
        profiles = SimpleNamespace(
            profiles=[],
            official_receiver_scope=self.profile_inputs.official_receiver_scope,
        )
        with patch("hfxdev.source_validated.load_profile_inputs", return_value=profiles):
            devices = _by_route(
                _source_validated_devices(
                    ROOT,
                    self.openrazer_source,
                    self.openrgb_source,
                    self.inputs.source_catalogs["openrazer"],
                    self.inputs.source_catalogs["openrgb"],
                )
            )
        for route in (
            "razer.1532.00ab.mouse",
            "razer.1532.00cd.mouse",
            "razer.1532.0296.keyboard",
        ):
            self.assertEqual(devices[route]["operational_state"], SOURCE_VALIDATED)
            self.assertEqual(
                devices[route]["hardware_assurance"],
                {"state": "not-hardware-verified", "profile_id": None},
            )

    def test_projection_loader_rejects_identity_outside_official_scope(self) -> None:
        tampered = deepcopy(self.catalog)
        device = _by_route(tampered["devices"])["razer.1532.00ab.mouse"]
        device["identity"]["product_id"] = 0x00AE
        device["route_id"] = "razer.1532.00ae.mouse"
        device["model_name"] = "Non-official fixture"
        tampered["devices"].sort(key=lambda value: value["route_id"])

        catalog_path = ROOT / "integrations/native-route/source-validated.json"
        from hfxdev import source_validated

        original_load_json = source_validated.load_json

        def substituted_load(path: Path):
            return tampered if path == catalog_path else original_load_json(path)

        with patch.object(source_validated, "load_json", side_effect=substituted_load):
            with self.assertRaises(ModelError):
                load_source_validated_catalog(ROOT)

    def test_projection_loader_rejects_publication_identity_drift(self) -> None:
        tampered = deepcopy(self.catalog)
        identity = _by_route(tampered["devices"])["razer.1532.0296.keyboard"][
            "integrations"
        ]["openrazer"]["publication_identity"]
        identity["transaction_id"] ^= 1

        catalog_path = ROOT / "integrations/native-route/source-validated.json"
        from hfxdev import source_validated

        original_load_json = source_validated.load_json

        def substituted_load(path: Path):
            return tampered if path == catalog_path else original_load_json(path)

        with patch.object(source_validated, "load_json", side_effect=substituted_load):
            with self.assertRaises(ModelError):
                load_source_validated_catalog(ROOT)

    def test_supported_poll_rates_is_source_proven_software_only(self) -> None:
        contract = supported_poll_rates_contract(
            self.openrazer_source,
            _ClassCatalog(self.openrazer_source),
            "RazerBlackWidowV4TenkeylessHyperSpeedWireless",
        )
        self.assertEqual(contract["method"], "get_supported_poll_rates")
        self.assertTrue(contract["values"])
        device = _by_route(self.catalog["devices"])["razer.1532.02d5.keyboard"]
        self.assertIn(
            "get_supported_poll_rates",
            device["integrations"]["openrazer"]["methods"],
        )
        self.assertFalse(
            any(
                "get_supported_poll_rates" in rule["methods"]
                for rule in device["rules"]
            )
        )

    def test_future_exact_merged_records_are_consumed_without_engine_edits(self) -> None:
        before = _by_route(self.catalog["devices"])
        self.assertEqual(before["razer.1532.00db.mouse"]["operational_state"], BACKEND_REQUIRED)
        self.assertEqual(before["razer.1532.02c9.keyboard"]["operational_state"], BACKEND_REQUIRED)
        self.assertEqual(before["razer.1532.02d2.keyboard"]["integrations"]["openrazer"]["state"], ABSENT)
        self.assertEqual(before["razer.1532.02ba.keyboard"]["integrations"]["openrgb"]["state"], ABSENT)

        with TemporaryDirectory(prefix="official-future-", dir=ROOT / "build") as directory:
            source = Path(directory) / "openrazer"
            shutil.copytree(
                self.openrazer_source,
                source,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            _add_synthetic_openrazer_pair(
                source,
                kind="mouse",
                wired_class="HyperFluxFixtureCobraHyperSpeedWired",
                wireless_class="HyperFluxFixtureCobraHyperSpeedWireless",
                parent_wired_class="RazerBasiliskV3ProWired",
                model_name="Cobra HyperSpeed",
                wired_pid=0x00DA,
                wireless_pid=0x00DB,
                parent_wireless_define="USB_DEVICE_ID_RAZER_BASILISK_V3_PRO_WIRELESS",
                synthetic_wireless_define="USB_DEVICE_ID_HFX_FIXTURE_COBRA_HYPERSPEED_WIRELESS",
            )
            for suffix, wireless_pid, wired_pid in (
                ("LowProfileHyperSpeed", 0x02C9, 0x02CC),
                ("LowProfileTKL", 0x02D2, 0x02D4),
            ):
                _add_synthetic_openrazer_pair(
                    source,
                    kind="keyboard",
                    wired_class=f"HyperFluxFixtureBlackWidowV4{suffix}Wired",
                    wireless_class=f"HyperFluxFixtureBlackWidowV4{suffix}Wireless",
                    parent_wired_class="RazerBlackWidowV4TenkeylessHyperSpeedWired",
                    model_name=f"BlackWidow V4 {suffix}",
                    wired_pid=wired_pid,
                    wireless_pid=wireless_pid,
                    parent_wireless_define="USB_DEVICE_ID_RAZER_BLACKWIDOW_V4_TENKEYLESS_HYPERSPEED_WIRELESS",
                    synthetic_wireless_define=f"USB_DEVICE_ID_HFX_FIXTURE_{suffix.upper()}_WIRELESS",
                )

            upstream = self.inputs.source_catalogs["openrazer"]["source"]
            openrazer_catalog = extract_openrazer_catalog(
                source,
                repository=upstream["repository"],
                commit="synthetic-official-merged-fixture",
                version="future-fixture",
                license_expression=upstream["license_expression"],
            )
            openrgb_catalog = deepcopy(self.inputs.source_catalogs["openrgb"])
            template = next(
                deepcopy(record)
                for record in openrgb_catalog["records"]
                if record["usb_identity"]["product_id"] == 0x02D5
                and record["device_kind"] == "keyboard"
            )
            template["record_id"] = "openrgb:synthetic-official-02ba"
            template["source_device_key"] = "synthetic-official-02ba"
            template["usb_identity"]["product_id"] = 0x02BA
            openrgb_catalog["records"].append(template)

            nonofficial = deepcopy(template)
            nonofficial["record_id"] = "openrgb:synthetic-nonofficial-7f01"
            nonofficial["source_device_key"] = "synthetic-nonofficial-7f01"
            nonofficial["usb_identity"]["product_id"] = 0x7F01
            openrgb_catalog["records"].append(nonofficial)

            devices = _by_route(
                _source_validated_devices(
                    ROOT,
                    source,
                    self.openrgb_source,
                    openrazer_catalog,
                    openrgb_catalog,
                )
            )

        self.assertEqual(set(devices), OFFICIAL_ROUTES)
        self.assertNotIn("razer.1532.7f01.keyboard", devices)
        for route, direct_pid in (
            ("razer.1532.00db.mouse", 0x00DA),
            ("razer.1532.02c9.keyboard", 0x02CC),
            ("razer.1532.02d2.keyboard", 0x02D4),
        ):
            integration = devices[route]["integrations"]["openrazer"]
            self.assertEqual(integration["state"], SOURCE_VALIDATED)
            self.assertEqual(integration["direct_usb_reference"]["product_id"], direct_pid)
            self.assertTrue(integration["methods"])
            self.assertTrue(
                any("openrazer" in rule["integrations"] for rule in devices[route]["rules"])
            )
        self.assertEqual(
            devices["razer.1532.02ba.keyboard"]["integrations"]["openrgb"]["state"],
            SOURCE_VALIDATED,
        )

    def test_unmodeled_upstream_write_isolated_to_exact_integration(self) -> None:
        with TemporaryDirectory(prefix="unmodeled-write-", dir=ROOT / "build") as directory:
            source = Path(directory) / "openrazer"
            shutil.copytree(
                self.openrazer_source,
                source,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            driver_path = source / "driver/razermouse_driver.c"
            driver = driver_path.read_text(encoding="utf-8")
            anchor = (
                "static ssize_t razer_attr_write_device_idle_time(struct device *dev, "
                "struct device_attribute *attr, const char *buf, size_t count)\n"
                "{\n"
                "    struct razer_mouse_device *device = dev_get_drvdata(dev);\n"
                "    unsigned short idle_time = (unsigned short)simple_strtoul(buf, NULL, 10);\n"
                "    struct razer_report request = {0};\n"
                "    struct razer_report response = {0};\n\n"
            )
            self.assertEqual(driver.count(anchor), 1)
            driver = driver.replace(
                anchor,
                anchor
                + "    request = get_razer_report(0x7e, 0x01, 0x01);\n"
                + "    request.arguments[0] = 0x42;\n\n",
                1,
            )
            driver_path.write_text(driver, encoding="utf-8")
            devices = _by_route(
                _source_validated_devices(
                    ROOT,
                    source,
                    self.openrgb_source,
                    self.inputs.source_catalogs["openrazer"],
                    self.inputs.source_catalogs["openrgb"],
                )
            )

        mouse = devices["razer.1532.00ab.mouse"]
        self.assertEqual(mouse["integrations"]["openrazer"]["state"], BACKEND_REQUIRED)
        self.assertEqual(
            mouse["integrations"]["openrazer"]["reason"],
            "unmodeled-write-constructor:get_razer_report",
        )
        self.assertEqual(mouse["integrations"]["openrgb"]["state"], SOURCE_VALIDATED)
        self.assertEqual(mouse["operational_state"], SOURCE_VALIDATED)

    def test_native_control_policy_remains_capability_not_version_bound(self) -> None:
        policy = load_native_control_policy(ROOT)
        self.assertIn("source_validated_methods", policy)
        self.assertNotIn("required_runtime_version", policy)


if __name__ == "__main__":
    unittest.main()
