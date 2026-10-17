# SPDX-License-Identifier: GPL-2.0-only

"""Owner hardware reports: ingest, privacy refusal, records, and the documented tier."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.cli import main as hfx_main
from hfxdev.generators.governance import device_support
from hfxdev.generators.profiles import markdown as profile_markdown
from hfxdev.governance import load_github_governance
from hfxdev.model import ModelError, load_json
from hfxdev.owner_reports import (
    CONFIRMATION_LABELS,
    CONFIRMATIONS_HEADING,
    REPORT_HEADING,
    OwnerReportRefused,
    build_owner_record,
    load_owner_confirmations,
    load_owner_report_policy,
    load_owner_reports,
    owner_confirmed_routes,
    parse_issue_body,
    private_text_findings,
    write_owner_record,
    _validate_record,
)
from hfxdev.profiles import compiled_catalog
from hfxdev.schema_subset import SchemaViolation, validate
from hfxdev.source_validated import load_source_validated_catalog
from hfxdev.source_validated_catalog import (
    OWNER_CONFIRMED,
    SOURCE_QUALIFIED_OPERATIONAL,
    documented_support_tier,
    public_support_tier,
)


FIXTURES = ROOT / "apps" / "device-qualification" / "tests" / "fixtures"
REPORT_SCHEMA = ROOT / "apps" / "device-qualification" / "contracts" / "local-qualification-view.schema.json"

# The Razer Basilisk V3 Pro (wireless product ID 00ab) is expected to work in both apps;
# 02ba works only in OpenRazer; 00cd is tested; 00db is not supported yet.
EXPECTED_PID = 0x00AB
EXPECTED_ROUTE = "razer.1532.00ab.mouse"
OPENRAZER_ONLY_PID = 0x02BA
TESTED_PID = 0x00CD
BLOCKED_PID = 0x00DB


def _home(name: str) -> str:
    # Built at run time: the repository privacy scan refuses literal home paths.
    return "/" + "home" + f"/{name}/"


def expected_report(*, product_id: int = EXPECTED_PID) -> dict:
    """A ready report with one unprofiled device, shaped like the daemon reports one."""
    report = load_json(FIXTURES / "rust-qualified-report.json")
    report["collection"]["diagnostic_findings"] = [
        finding
        for finding in report["collection"]["diagnostic_findings"]
        if finding["severity"] not in {"error", "critical"}
    ]
    report["receivers"][0]["devices"].append(
        {
            "device_id": "device-3",
            "kind": "other",
            "model_name": None,
            "vendor_id": None,
            "product_id": product_id,
            "profile": None,
            "presence": "active",
            "support": "identified",
            "battery": {"availability": "reported", "percentage": 55},
            "capabilities": [],
        }
    )
    return report


def issue_body(report: dict | str, ticked: tuple[str, ...] = ("input", "openrgb-lighting"), *, extra: str = "") -> str:
    text = report if isinstance(report, str) else json.dumps(report, indent=2)
    boxes = "\n".join(
        f"- [{'X' if identifier in ticked else ' '}] {label}"
        for identifier, label in CONFIRMATION_LABELS.items()
    )
    return (
        "### Product ID\n\n00ab\n\n"
        "### What happened\n\n"
        f"{extra or 'It works fine.'}\n\n"
        f"### {REPORT_HEADING}\n\n```json\n{text}\n```\n\n"
        f"### {CONFIRMATIONS_HEADING}\n\n{boxes}\n"
    )


def build(report: dict, ticked: tuple[str, ...] = ("input", "openrgb-lighting"), **options) -> dict:
    return build_owner_record(ROOT, issue_body(report, ticked), issue=options.pop("issue", 42), **options)


class SchemaSubsetContracts(unittest.TestCase):
    def test_every_qualification_fixture_matches_the_report_format(self) -> None:
        schema = load_json(REPORT_SCHEMA)
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                validate(schema, load_json(path))

    def test_rules_are_enforced_and_messages_never_carry_values(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["count"],
            "properties": {
                "count": {"type": "integer", "minimum": 1},
                "tags": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z]+$"}},
                "when": {"type": "string", "format": "date-time"},
            },
        }
        validate(schema, {"count": 2, "tags": ["a", "b"], "when": "2026-10-17T10:00:00Z"})
        unlisted_value = "s3cretValue99"
        cases = {
            "missing": {},
            "boolean is not integer": {"count": True},
            "minimum": {"count": 0},
            "duplicate": {"count": 1, "tags": ["a", "a"]},
            "pattern": {"count": 1, "tags": [unlisted_value]},
            "date-time": {"count": 1, "when": unlisted_value},
            "extra field": {"count": 1, unlisted_value: 1},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(SchemaViolation) as caught:
                    validate(schema, value)
                self.assertNotIn(unlisted_value, str(caught.exception))
                self.assertTrue(str(caught.exception).startswith("$"))

    def test_unknown_keywords_are_refused_rather_than_ignored(self) -> None:
        with self.assertRaisesRegex(ModelError, "unsupported keywords: patternProperties"):
            validate({"type": "object", "patternProperties": {}}, {})

    def test_one_of_requires_exactly_one_match(self) -> None:
        schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
        validate(schema, 1.5)
        with self.assertRaises(SchemaViolation):
            validate(schema, 1)


class OwnerReportAcceptance(unittest.TestCase):
    def test_policy_and_committed_records_load(self) -> None:
        policy = load_owner_report_policy(ROOT)
        self.assertEqual(policy["confirmations"]["required"], ["input"])
        self.assertGreaterEqual(policy["minimum_reports"], 1)
        load_owner_reports(ROOT)
        load_owner_confirmations(ROOT)

    def test_issue_body_with_ticked_boxes_becomes_a_record(self) -> None:
        record = build(expected_report())
        self.assertEqual(record["route_id"], EXPECTED_ROUTE)
        self.assertEqual(record["id"], f"claim.owner-report.{EXPECTED_ROUTE}.issue-42")
        self.assertEqual(record["model_name"], "Razer Basilisk V3 Pro")
        self.assertEqual(record["confirmations"], ["input", "openrgb-lighting"])
        self.assertEqual(record["source_refs"], [{"kind": "github-issue", "issue": 42}])
        self.assertEqual(record["report"]["generated_on"], "2026-07-22")
        self.assertEqual(record["report"]["finding_ids"], ["HFX-SERVICE-002"])
        self.assertEqual(record["observed"]["reported_kind"], "other")
        validate(load_json(ROOT / "schemas" / "owner-report.schema.json"), record)

    def test_raw_report_takes_confirmations_from_flags(self) -> None:
        record = build_owner_record(
            ROOT,
            json.dumps(expected_report()),
            issue=7,
            confirmations=["input", "openrazer-lighting", "openrgb-lighting"],
        )
        self.assertEqual(record["confirmations"], ["input", "openrazer-lighting", "openrgb-lighting"])

    def test_confirmations_cannot_come_from_both_the_form_and_flags(self) -> None:
        with self.assertRaisesRegex(OwnerReportRefused, "do not also pass --confirmed"):
            build_owner_record(ROOT, issue_body(expected_report()), issue=7, confirmations=["input"])

    def test_record_hash_is_independent_of_formatting(self) -> None:
        report = expected_report()
        compact = build_owner_record(ROOT, json.dumps(report), issue=1, confirmations=["input", "openrgb-lighting"])
        spaced = build_owner_record(
            ROOT, json.dumps(report, indent=4), issue=1, confirmations=["input", "openrgb-lighting"]
        )
        self.assertEqual(compact["report"]["sha256"], spaced["report"]["sha256"])

    def test_parse_issue_body_handles_unticked_and_empty_answers(self) -> None:
        _, ticked = parse_issue_body(issue_body(expected_report(), ()))
        self.assertEqual(ticked, frozenset())
        empty = issue_body("_No response_")
        with self.assertRaisesRegex(OwnerReportRefused, "section is empty"):
            parse_issue_body(empty.replace("```json\n_No response_\n```", "_No response_"))
        with self.assertRaisesRegex(OwnerReportRefused, "no hardware report section"):
            parse_issue_body("### What happened\n\nnothing\n")

    def test_unknown_checkbox_wording_is_refused(self) -> None:
        body = issue_body(expected_report()) + "- [X] I tested every feature\n"
        with self.assertRaisesRegex(OwnerReportRefused, "option the form does not offer"):
            build_owner_record(ROOT, body, issue=3)

    def test_several_code_blocks_are_refused(self) -> None:
        text = json.dumps(expected_report()) + "\n```\n```json\n{}"
        with self.assertRaisesRegex(OwnerReportRefused, "exactly one JSON code block"):
            build_owner_record(ROOT, issue_body(text), issue=3)

    def test_two_expected_devices_need_an_explicit_choice(self) -> None:
        report = expected_report()
        second = copy.deepcopy(report["receivers"][0]["devices"][-1])
        second.update(device_id="device-4", product_id=0x00B0)
        report["receivers"][0]["devices"].append(second)
        with self.assertRaisesRegex(OwnerReportRefused, "choose one with --device"):
            build(report)
        record = build(report, device_id="device-4")
        self.assertEqual(record["route_id"], "razer.1532.00b0.mouse")


class OwnerReportRefusals(unittest.TestCase):
    def refused(self, report: dict | str, pattern: str, ticked=("input", "openrgb-lighting"), **options) -> str:
        body = issue_body(report, ticked)
        with self.assertRaisesRegex(OwnerReportRefused, pattern) as caught:
            build_owner_record(ROOT, body, issue=options.pop("issue", 42), **options)
        return str(caught.exception)

    def test_invalid_or_ambiguous_json_is_refused(self) -> None:
        self.refused("{not json", r"not valid JSON \(line 1")
        self.refused('{"schema": 1, "schema": 2}', "repeats a field name")
        self.refused('{"value": NaN}', "number JSON does not allow")
        self.refused("[]", "must be a JSON object")

    def test_oversized_reports_are_refused(self) -> None:
        policy = load_owner_report_policy(ROOT)
        padding = " " * (policy["report"]["maximum_bytes"] + 1)
        with self.assertRaisesRegex(OwnerReportRefused, "larger than"):
            build_owner_record(ROOT, "{" + padding + "}", issue=1, confirmations=["input"])

    def test_reports_that_break_the_format_are_refused_without_naming_fields(self) -> None:
        report = expected_report()
        report["receivers"][0]["devices"][-1]["owner_note"] = "x"
        message = self.refused(report, "field\\(s\\) the report format does not allow")
        self.assertNotIn("owner_note", message)

    def test_edited_reports_are_refused(self) -> None:
        cases = []
        report = expected_report()
        del report["collection"]
        cases.append((report, "not made by hyperfluxctl qualification"))
        report = expected_report()
        report["collection"]["bridge_requests"] = ["diagnostics"]
        cases.append((report, "did not read the device list"))
        report = expected_report()
        report["companion"]["simulation"] = True
        cases.append((report, "simulator"))
        report = expected_report()
        report["companion"]["endpoint"] = "http://127.0.0.1:47822"
        cases.append((report, "endpoint is not the fixed local address"))
        report = expected_report()
        report["companion"]["state"] = "driver-pending"
        cases.append((report, "was not ready"))
        report = expected_report()
        report["companion"]["version"] = "2.0.9"
        cases.append((report, r"companion\.version is older than"))
        report = expected_report()
        report["system"]["driver_version"] = "2.1.0-dev"
        cases.append((report, "driver_version is not a release version"))
        report = expected_report()
        report["collection"]["diagnostic_findings"].append({"finding_id": "HFX-TRANSPORT-001", "severity": "error"})
        cases.append((report, "severity error"))
        report = expected_report()
        report["collection"]["diagnostic_findings"].append({"finding_id": "HFX-MADE-UP-999", "severity": "info"})
        cases.append((report, "not a known HyperFlux problem code"))
        for report, pattern in cases:
            with self.subTest(pattern=pattern):
                self.refused(report, pattern)

    def test_identifiers_must_be_ordinals(self) -> None:
        report = expected_report()
        report["receivers"][0]["devices"][-1]["device_id"] = "mouse-a"
        self.refused(report, "not a numbered device-N name")
        report = expected_report()
        report["receivers"][0]["receiver_id"] = "hidraw-three"
        self.refused(report, "not a numbered receiver-N name")

    def test_receiver_must_be_supported_and_current(self) -> None:
        report = expected_report()
        report["receivers"][0]["profile"]["digest"] = "0" * 64
        self.refused(report, "different receiver profile; ask the owner to update")
        report = expected_report()
        report["receivers"][0]["profile"]["id"] = "receiver.razer.other.1532-0000"
        self.refused(report, "not a receiver profile in this repository")
        report = expected_report()
        report["receivers"][0]["product_id"] = 0x0001
        self.refused(report, "USB ID does not match")
        report = expected_report()
        report["receivers"][0]["lifecycle"] = "recovering"
        self.refused(report, "lifecycle is recovering")

    def test_only_devices_expected_to_work_can_be_confirmed(self) -> None:
        self.refused(expected_report(product_id=TESTED_PID), "already tested on real hardware", device_id="device-3")
        self.refused(expected_report(product_id=BLOCKED_PID), "not supported yet", device_id="device-3")
        self.refused(expected_report(product_id=0x0001), "not on the receiver's compatibility list", device_id="device-3")
        self.refused(expected_report(product_id=TESTED_PID), "no device that is listed as expected to work")
        report = expected_report()
        report["receivers"][0]["devices"][-1]["presence"] = "unavailable"
        self.refused(report, "wake the device")
        report = expected_report()
        report["receivers"][0]["devices"][-1]["vendor_id"] = 1
        self.refused(report, "vendor_id does not match")

    def test_confirmations_must_be_complete_and_possible(self) -> None:
        self.refused(expected_report(), "works normally through the receiver", ticked=("openrgb-lighting",))
        self.refused(expected_report(), "lighting changed in an app", ticked=("input",))
        # 02ba has no OpenRGB support upstream, so ticking OpenRGB cannot be true.
        self.refused(
            expected_report(product_id=OPENRAZER_ONLY_PID),
            "ticked openrgb-lighting, but that app has no known way",
            ticked=("input", "openrgb-lighting"),
        )
        record = build(expected_report(product_id=OPENRAZER_ONLY_PID), ("input", "openrazer-lighting"))
        self.assertEqual(record["route_id"], "razer.1532.02ba.keyboard")


class OwnerReportPrivacy(unittest.TestCase):
    """Reports with private text are refused; the refusal names a place, never the text."""

    PRIVATE_SAMPLES = {
        "a private file path": _home("alice") + "hyperflux.log",
        "a home folder path": "see ~/reports/out.json",
        "a Windows file path": "C:\\Users\\alice\\report.json",
        "an email address or user@host name": "alice@workstation",
        "a MAC address": "a4:5e:60:12:34:56",
        "a UUID": "123e4567-e89b-12d3-a456-426614174000",
        "a network address": "192.168.1.20",
        "a USB bus path": "3-2.1:1.0",
        "a local network host name": "desk-pc.lan",
        "a serial number, host name, or user name label": "Serial PM2213",
        "an environment variable": "HOME=x",
        "a serial number or other unique code": "PM2213H09812345",
    }

    def test_each_category_is_refused_by_path_and_never_echoed(self) -> None:
        for category, sample in self.PRIVATE_SAMPLES.items():
            with self.subTest(category=category):
                report = expected_report()
                report["receivers"][0]["devices"][-1]["model_name"] = sample
                with self.assertRaises(OwnerReportRefused) as caught:
                    build(report)
                message = str(caught.exception)
                self.assertIn(f"$.receivers[0].devices[2].model_name: looks like {category}", message)
                self.assertNotIn(sample, message)
                self.assertIn("make a fresh report", message)

    def test_private_text_anywhere_in_the_report_is_found(self) -> None:
        report = expected_report()
        report["plans"][0]["summary"] = "Ran from " + _home("bob") + "src"
        report["receivers"][0]["model_name"] = "Receiver SN 0012345678901"
        findings = private_text_findings(report)
        self.assertIn("$.plans[0].summary: looks like a private file path", findings)
        self.assertIn("$.receivers[0].model_name: looks like a serial number or other unique code", findings)

    def test_clean_reports_and_digests_are_not_flagged(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                self.assertEqual(private_text_findings(load_json(path)), [])

    def test_free_text_in_the_issue_never_reaches_the_record(self) -> None:
        # Redaction by omission: the record is built from a fixed list of fields, so
        # whatever the owner wrote around the report is dropped, not copied.
        private_texts = [_home("carol") + "notes.txt", "carol@example.lan", "SN PM2213H0981", "carol-desktop"]
        body = issue_body(expected_report(), extra="\n".join(private_texts))
        record = build_owner_record(ROOT, body, issue=9)
        dumped = json.dumps(record)
        for private_text in private_texts:
            self.assertNotIn(private_text, dumped)
        allowed = {
            "$schema", "schema", "id", "summary", "evidence_level", "privacy", "route_id",
            "model_name", "source_refs", "report", "observed", "confirmations", "limitations",
        }
        self.assertEqual(set(record), allowed)

    def test_report_model_names_are_replaced_by_the_catalog_name(self) -> None:
        report = expected_report()
        report["receivers"][0]["devices"][-1]["model_name"] = "Basilisk of the night"
        record = build(report)
        self.assertEqual(record["model_name"], "Razer Basilisk V3 Pro")
        self.assertNotIn("night", json.dumps(record))

    def test_committed_records_are_scanned_again(self) -> None:
        record = build(expected_report())
        record["summary"] = "Owner at " + _home("dave") + "x"
        with self.assertRaisesRegex(ModelError, "looks private") as caught:
            _validate_record(ROOT, record, "evidence/owner-reports/example.json")
        self.assertNotIn("dave", str(caught.exception))


class OwnerRecordFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_write_refuses_to_overwrite(self) -> None:
        record = build(expected_report())
        path = write_owner_record(ROOT, record, self.output)
        self.assertEqual(path.name, f"{EXPECTED_ROUTE}--issue-42.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
        with self.assertRaisesRegex(ModelError, "already exists"):
            write_owner_record(ROOT, record, self.output)

    def test_confirmed_routes_need_the_minimum_number_of_reports(self) -> None:
        records = [build(expected_report(), issue=11), build(expected_report(), issue=12)]
        policy = load_owner_report_policy(ROOT)
        self.assertEqual(owner_confirmed_routes(records, policy), {EXPECTED_ROUTE: (11, 12)})
        self.assertEqual(owner_confirmed_routes(records, {**policy, "minimum_reports": 3}), {})

    def test_cli_check_prints_and_writes_nothing(self) -> None:
        report = self.output / "issue.md"
        report.write_text(issue_body(expected_report()), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = hfx_main(["qualification", "ingest", str(report), "--issue", "42", "--check"])
        self.assertEqual(status, 0)
        self.assertIn("Owner report accepted: Razer Basilisk V3 Pro", out.getvalue())
        self.assertIn(f'"route_id": "{EXPECTED_ROUTE}"', out.getvalue())
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["issue.md"])

    def test_cli_writes_to_the_output_directory(self) -> None:
        report = self.output / "report.json"
        report.write_text(json.dumps(expected_report()), encoding="utf-8")
        records = self.output / "records"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = hfx_main(
                [
                    "qualification", "ingest", str(report), "--issue", "5",
                    "--confirmed", "input", "--confirmed", "openrazer-lighting",
                    "--output-dir", str(records),
                ]
            )
        self.assertEqual(status, 0)
        written = records / f"{EXPECTED_ROUTE}--issue-5.json"
        self.assertTrue(written.is_file())
        self.assertIn("Run ./hfx generate", out.getvalue())

    def test_cli_refusal_exits_non_zero_without_echoing_the_report(self) -> None:
        report = expected_report()
        private_path = _home("erin") + "x"
        report["receivers"][0]["devices"][-1]["model_name"] = private_path
        path = self.output / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            status = hfx_main(
                ["qualification", "ingest", str(path), "--issue", "5", "--confirmed", "input",
                 "--confirmed", "openrgb-lighting", "--output-dir", str(self.output / "records")]
            )
        self.assertEqual(status, 1)
        self.assertIn("looks like a private file path", err.getvalue())
        self.assertNotIn(private_path, err.getvalue())
        self.assertFalse((self.output / "records").exists())


class OwnerConfirmedDocumentation(unittest.TestCase):
    def test_tier_promotion_is_documentation_only(self) -> None:
        devices = {route["route_id"]: route for route in load_source_validated_catalog(ROOT)["devices"]}
        confirmed = {EXPECTED_ROUTE: (42,), "razer.1532.00cd.mouse": (43,)}
        self.assertEqual(public_support_tier(devices[EXPECTED_ROUTE]), SOURCE_QUALIFIED_OPERATIONAL)
        self.assertEqual(documented_support_tier(devices[EXPECTED_ROUTE], confirmed), OWNER_CONFIRMED)
        # A record never changes a tested or blocked device's documented tier.
        self.assertEqual(
            documented_support_tier(devices["razer.1532.00cd.mouse"], confirmed),
            public_support_tier(devices["razer.1532.00cd.mouse"]),
        )

    def test_supported_hardware_page_lists_confirmed_devices_with_issue_links(self) -> None:
        catalog = compiled_catalog(ROOT)
        source_validated = load_source_validated_catalog(ROOT)
        empty = profile_markdown(catalog, source_validated, {})
        self.assertIn("No owner has confirmed a device yet", empty)
        self.assertIn("../user/hardware.md#confirm-that-your-device-works", empty)
        page = profile_markdown(
            catalog, source_validated, {EXPECTED_ROUTE: (42,)}, issues_url="https://example.invalid/issues"
        )
        confirmed = page.split("## Confirmed By Owners", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Razer Basilisk V3 Pro", confirmed)
        self.assertIn("[#42](https://example.invalid/issues/42)", confirmed)
        self.assertIn("**Confirmed by owners:** 1", page)

    def test_generated_page_matches_committed_records(self) -> None:
        page = (ROOT / "docs" / "generated" / "supported-hardware.md").read_text(encoding="utf-8")
        confirmed = load_owner_confirmations(ROOT)
        self.assertIn(f"**Confirmed by owners:** {len(confirmed)}", page)

    def test_device_support_form_offers_the_exact_checkbox_wording(self) -> None:
        form = device_support(load_github_governance(ROOT))
        field = form.split("id: owner-check", 1)[1].split("- type:", 1)[0]
        self.assertIn(CONFIRMATIONS_HEADING, field)
        for label in CONFIRMATION_LABELS.values():
            self.assertIn(label, field)
        self.assertNotIn("required: true", field)
        self.assertIn("paste what it prints, unchanged", form)

    def test_user_and_developer_pages_carry_the_linked_sections(self) -> None:
        hardware = (ROOT / "docs" / "user" / "hardware.md").read_text(encoding="utf-8")
        onboarding = (ROOT / "docs" / "developer" / "device-onboarding.md").read_text(encoding="utf-8")
        self.assertIn("\n## Confirm That Your Device Works\n", hardware)
        self.assertIn("\n## Owner Reports\n", onboarding)
        self.assertIn("./hfx qualification ingest issue.md --issue N", onboarding)


if __name__ == "__main__":
    unittest.main()
