# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from hfxdev.journey import (
    extract_journey_sources,
    load_journey_private_overlay,
    load_journey_policy,
    validate_journey_event,
)
from hfxdev.journey_coverage import load_journey_source_coverage
from hfxdev.model import ModelError


def _line(value: object) -> str:
    return json.dumps(value, separators=(",", ":")) + "\n"


def _meta(identifier: str, parent: str | None = None) -> dict[str, object]:
    return {
        "type": "session_meta",
        "payload": {
            "id": identifier,
            "session_id": identifier,
            "forked_from_id": parent,
            "timestamp": "2026-07-29T10:00:00Z",
        },
    }


def _message(identifier: str, role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": identifier,
            "role": role,
            "content": [
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text,
                }
            ],
        },
    }


def _private_overlay(base: Path) -> Path:
    path = base / ".private-journey-overlay.json"
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "schema": "hyperflux-private-journey-overlay-v1",
                    "context_break_terms": ["build console", "gpu"],
                    "control_envelope_prefixes": ["<environment_context"],
                    "publication_forbidden_terms": ["private tool"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
    return path


def _extract(sources: list[Path], output: Path) -> dict[str, object]:
    overlay = _private_overlay(output.parent)
    digest = hashlib.sha256(overlay.read_bytes()).hexdigest()
    policy = replace(load_journey_policy(ROOT), private_overlay_sha256=digest)
    with patch("hfxdev.journey.load_journey_policy", return_value=policy):
        return extract_journey_sources(ROOT, sources, output, overlay)


class JourneyPipelineTests(unittest.TestCase):
    def test_policy_has_exact_six_era_information_architecture(self) -> None:
        policy = load_journey_policy(ROOT)
        self.assertEqual(
            [identifier for identifier, _ in policy.eras],
            [
                "receiver-problem",
                "transport-discovery",
                "bridge-foundation",
                "application-compatibility",
                "evidence-recovery",
                "reconstruction-2-0",
            ],
        )

    def test_private_overlay_is_owner_only_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            overlay = _private_overlay(Path(temporary))
            digest = hashlib.sha256(overlay.read_bytes()).hexdigest()
            loaded = load_journey_private_overlay(overlay, digest)
            self.assertEqual(loaded.context_break_terms, ("build console", "gpu"))
            symbolic = Path(temporary) / "private-overlay-link.json"
            symbolic.symlink_to(overlay)
            with self.assertRaisesRegex(ModelError, "private overlay"):
                load_journey_private_overlay(symbolic, digest)
            overlay.chmod(0o644)
            with self.assertRaisesRegex(ModelError, "mode-0600"):
                load_journey_private_overlay(overlay, digest)
            overlay.chmod(0o600)
            with self.assertRaisesRegex(ModelError, "does not match"):
                load_journey_private_overlay(overlay, "0" * 64)

    def test_public_source_coverage_binds_private_aggregates_and_provenance(self) -> None:
        coverage = load_journey_source_coverage(ROOT)
        self.assertEqual(coverage["publication"]["direct_quotes_approved"], 0)
        self.assertFalse(coverage["publication"]["raw_project_records_published"])
        self.assertEqual(coverage["prior_art_review"]["absolute_novelty_claims"], 0)
        self.assertEqual(coverage["private_evidence"]["extracted_event_count"], 1018)
        self.assertEqual(coverage["private_evidence"]["curated_event_count"], 16)
        self.assertEqual(
            coverage["private_evidence"]["pipeline_overlay_sha256"],
            load_journey_policy(ROOT).private_overlay_sha256,
        )
        self.assertEqual(
            [era["motivation_event_count"] for era in coverage["eras"]],
            [2, 2, 2, 3, 3, 4],
        )
        serialized = json.dumps(coverage)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("source-thread", serialized)
        self.assertNotIn("session_id", serialized)
        for era in coverage["eras"]:
            kinds = {source["kind"] for source in era["technical_sources"]}
            self.assertIn("commit", kinds)
            self.assertTrue(kinds & {"authority", "evidence"})
            for source in era["technical_sources"]:
                reference = source["reference"]
                if source["kind"] == "commit":
                    self.assertRegex(reference, r"^[0-9a-f]{40}$")
                else:
                    self.assertTrue((ROOT / reference).is_file())

    def test_public_source_coverage_does_not_require_private_history_objects(self) -> None:
        canonical = json.loads(
            (ROOT / "assurance" / "journey-source-coverage.json").read_text(
                encoding="utf-8"
            )
        )
        historical_revision = "0" * 40
        canonical["eras"][0]["technical_sources"][0]["reference"] = historical_revision

        def fake_load(path: Path) -> object:
            if path.name == "journey-source-coverage.json":
                return canonical
            return json.loads(path.read_text(encoding="utf-8"))

        with patch("hfxdev.journey_coverage.load_json", side_effect=fake_load):
            coverage = load_journey_source_coverage(ROOT)
        self.assertEqual(
            coverage["eras"][0]["technical_sources"][0]["reference"],
            historical_revision,
        )

    def test_public_source_coverage_rejects_privacy_and_claim_drift(self) -> None:
        canonical = json.loads(
            (ROOT / "assurance" / "journey-source-coverage.json").read_text(
                encoding="utf-8"
            )
        )
        mutations = (
            lambda value: value["publication"].update(direct_quotes_approved=1),
            lambda value: value["publication"].update(raw_project_records_published=True),
            lambda value: value["prior_art_review"].update(absolute_novelty_claims=1),
            lambda value: value["eras"][0].update(motivation_event_count=3),
            lambda value: value["prior_art_review"]["sources"].__setitem__(
                0, "/home/private-source.jsonl"
            ),
            lambda value: value["eras"][0]["technical_sources"][0].update(
                reference="deadbeef"
            ),
        )
        for mutate in mutations:
            value = copy.deepcopy(canonical)
            mutate(value)

            def fake_load(path: Path) -> object:
                if path.name == "journey-source-coverage.json":
                    return value
                return json.loads(path.read_text(encoding="utf-8"))

            with self.subTest(mutation=mutate), patch(
                "hfxdev.journey_coverage.load_json", side_effect=fake_load
            ), self.assertRaises(ModelError):
                load_journey_source_coverage(ROOT)

    def test_public_journey_has_six_eras_source_map_and_no_private_voice_leak(self) -> None:
        coverage = load_journey_source_coverage(ROOT)
        text = (ROOT / coverage["public_document"]).read_text(encoding="utf-8")
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(
            headings,
            [
                "## Era 1: The Receiver Problem",
                "## Era 2: Learning The Transport",
                "## Era 3: From Prototype To Bridge",
                "## Era 4: Application Compatibility",
                "## Era 5: Evidence And Recovery",
                "## Era 6: Reconstructing 2.0",
                "## Technical Appendix",
            ],
        )
        self.assertEqual(text.count("# Project Journey"), 1)
        self.assertGreaterEqual(len(re.findall(r"\bI\b", text)), 6)
        self.assertIn("### Technical source map", text)
        self.assertIn("### Product and package boundary", text)
        self.assertIn("### Accepted limitations", text)
        self.assertIn("### Prior-art and novelty boundary", text)
        self.assertIn("Ripple", text)
        self.assertIn("Quartz", text)
        for era in coverage["eras"]:
            commit = next(
                source["reference"]
                for source in era["technical_sources"]
                if source["kind"] == "commit"
            )
            self.assertIn(commit[:7], text)
        forbidden = (
            r"/(?:home|tmp)/shini(?:/|\b)",
            r"\b019[0-9a-f]{5}-[0-9a-f-]{20,}\b",
            r"^> (?!\[!)",
            r"(?:first[- ]ever|world.?s first|only project|unprecedented)",
            r"(?:reserves the approved|drafting is intentionally held|final appendix will)",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))
        self.assertNotIn("\u201c", text)
        self.assertNotIn("\u201d", text)

    def test_streaming_extraction_is_private_filtered_and_globally_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            parent = base / "parent.jsonl"
            fork = base / "fork.jsonl"
            parent.write_text(
                "".join(
                    [
                        _line(_meta("parent")),
                        _line(_message("prelude", "user", "Can we test it?")),
                        _line(_message("topic", "assistant", "HyperFlux receiver evidence is ready.")),
                        _line(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "id": "developer",
                                    "role": "developer",
                                    "content": [{"type": "input_text", "text": "private prompt"}],
                                },
                            }
                        ),
                        _line(
                            {
                                "type": "response_item",
                                "payload": {"type": "custom_tool_call_output", "output": "secret tool dump"},
                            }
                        ),
                        _line(_message("followup", "user", "pass")),
                        _line({"type": "compacted", "payload": {"message": "duplicate summary"}}),
                    ]
                ),
                encoding="utf-8",
            )
            fork.write_text(
                "".join(
                    [
                        _line(_meta("fork", "parent")),
                        _line(_message("topic", "assistant", "HyperFlux receiver evidence is ready.")),
                        _line(_message("new", "user", "OpenRGB now sees both routes.")),
                    ]
                ),
                encoding="utf-8",
            )
            output = base / "private"
            manifest = _extract([fork, parent], output)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["event_count"], 3)
            self.assertEqual(len(manifest["sources"]), 2)
            for path in (output, output / "source-manifest.json", output / "event-ledger.jsonl"):
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(mode, 0o700 if path == output else 0o600)
            ledger = [
                json.loads(line)
                for line in (output / "event-ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            combined = json.dumps(ledger)
            self.assertNotIn("private prompt", combined)
            self.assertNotIn("secret tool dump", combined)
            self.assertNotIn("duplicate summary", combined)
            self.assertNotIn("Can we test it?", combined)
            self.assertTrue(all(item["era"] == "unassigned" for item in ledger))
            self.assertTrue(all(item["confidence"] == "motivation-only" for item in ledger))
            self.assertTrue(all(item["publication_use"] == "paraphrase-only" for item in ledger))
            self.assertTrue(
                all(source["kind"] == "chat" for item in ledger for source in item["sources"])
            )
            duplicated = [
                item for item in ledger if item["narrative"] == "HyperFlux receiver evidence is ready."
            ]
            self.assertEqual(len(duplicated), 1)
            self.assertEqual(len(duplicated[0]["sources"]), 2)
            child_source = next(
                source for source in manifest["sources"] if source["path"] == str(fork)
            )
            self.assertEqual(len(child_source["ancestor_session_id_hashes"]), 1)

    def test_control_envelopes_and_unrelated_incidents_do_not_cross_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "mixed.jsonl"
            source.write_text(
                "".join(
                    [
                        _line(_meta("mixed")),
                        _line(
                            _message(
                                "environment",
                                "user",
                                "<environment_context>HyperFlux workspace</environment_context>",
                            )
                        ),
                        _line(
                            _message(
                                "fan",
                                "assistant",
                                "Build console fans are loud; HyperFlux remains untouched.",
                            )
                        ),
                        _line(
                            _message(
                                "route",
                                "assistant",
                                "HyperFlux OpenRazer route evidence is ready.",
                            )
                        ),
                        _line(
                            _message(
                                "assistant-context",
                                "assistant",
                                "This generic follow-up has no product authority.",
                            )
                        ),
                        _line(_message("pass", "user", "pass")),
                        _line(
                            _message(
                                "gpu",
                                "user",
                                "The NVIDIA GPU fan issue is unrelated.",
                            )
                        ),
                        _line(_message("after", "assistant", "This is system-support context.")),
                    ]
                ),
                encoding="utf-8",
            )
            output = base / "private"
            _extract([source], output)
            narratives = [
                json.loads(line)["narrative"]
                for line in (output / "event-ledger.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                narratives,
                ["HyperFlux OpenRazer route evidence is ready.", "pass"],
            )

    def test_output_is_never_overwritten_and_symbolic_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.jsonl"
            source.write_text(_line(_meta("one")), encoding="utf-8")
            output = base / "private"
            output.mkdir()
            with self.assertRaisesRegex(ModelError, "must not already exist"):
                _extract([source], output)
            output.rmdir()
            symbolic = base / "symbolic.jsonl"
            symbolic.symlink_to(source)
            with self.assertRaisesRegex(ModelError, "regular non-symbolic"):
                _extract([symbolic], output)
            if output.exists():
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_failed_extraction_keeps_temporary_index_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.jsonl"
            source.write_text(_line(_meta("failure")), encoding="utf-8")
            output = base / "private"
            with patch(
                "hfxdev.journey._scan_source",
                side_effect=ModelError("injected extraction failure"),
            ):
                with self.assertRaisesRegex(ModelError, "injected extraction failure"):
                    _extract([source], output)
            index = output / ".journey-index.sqlite3"
            self.assertTrue(index.is_file())
            self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o600)

    def test_oversized_source_is_accounted_and_requires_review(self) -> None:
        policy = load_journey_policy(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "oversized.jsonl"
            descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, _line(_meta("large")).encode())
                remaining = policy.maximum_line_bytes + 1
                block = b"x" * 65536
                while remaining:
                    chunk = block[: min(len(block), remaining)]
                    os.write(descriptor, chunk)
                    remaining -= len(chunk)
                os.write(descriptor, b"\n")
            finally:
                os.close(descriptor)
            manifest = _extract([source], base / "private")
            self.assertEqual(manifest["status"], "requires-review")
            self.assertEqual(manifest["sources"][0]["excluded_oversized"], 1)

    def test_technical_claims_require_non_chat_authority(self) -> None:
        event = {
            "schema": "hyperflux-private-journey-event-v1",
            "id": "narrative-example",
            "date": "2026-07-29",
            "era": "unassigned",
            "role": "assistant",
            "claim": "technical claim",
            "outcome": "pending-review",
            "then_status": "as-recorded",
            "current_status": "unreviewed",
            "supersession": None,
            "confidence": "technical-source-backed",
            "privacy": "private-source",
            "publication_use": "paraphrase-only",
            "narrative_sha256": "0" * 64,
            "narrative": "private",
            "sources": [{"kind": "chat", "reference": "source:1:item"}],
        }
        with self.assertRaisesRegex(ModelError, "require commit, issue, mission, or evidence"):
            validate_journey_event(event)
        event["sources"].append(
            {"kind": "commit", "reference": "c348305bb9c6790141932ddf79f707e5223bb51c"}
        )
        validate_journey_event(event)


if __name__ == "__main__":
    unittest.main()
