# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "simulator-scenario.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "replay" / "qualified-lifecycle-v1.json"
PRIVATE_KEY = re.compile(r"(?:serial|machine|host|raw.*payload|private.*path)", re.IGNORECASE)


def _walk(value: object) -> list[tuple[str, object]]:
    entries: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            entries.append((key, child))
            entries.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            entries.extend(_walk(child))
    return entries


class SimulatorContractTests(unittest.TestCase):
    def test_schema_is_strict_and_bounded(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["events"]["maxItems"], 4096)
        self.assertEqual(schema["properties"]["initial"]["properties"]["children"]["maxItems"], 32)
        for definition in schema["$defs"].values():
            if isinstance(definition, dict) and definition.get("type") == "object":
                self.assertFalse(definition.get("additionalProperties", True))

    def test_committed_replay_is_sanitized_and_non_authoritative(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        provenance = fixture["provenance"]
        self.assertEqual(provenance["source"], "sanitized-replay")
        self.assertTrue(provenance["test_fixture"])
        self.assertFalse(provenance["hardware_claim_authority"])
        self.assertFalse(provenance["private_identifiers_exported"])
        for key, value in _walk(fixture):
            with self.subTest(key=key):
                self.assertIsNone(PRIVATE_KEY.search(key))
                if isinstance(value, str):
                    self.assertNotRegex(value, r"^/home/[^/]+/")

    def test_replay_uses_only_declared_logical_children(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        children = fixture["initial"]["children"]
        identifiers = [child["logical_device_id"] for child in children]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        declared = set(identifiers)
        for scheduled in fixture["events"]:
            event = scheduled["event"]
            referenced = []
            if "device_id" in event:
                referenced.append(event["device_id"])
            referenced.extend(event.get("targets", []))
            self.assertLessEqual(set(referenced), declared)

    def test_replay_generations_use_canonical_decimal_strings(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        generations = [fixture["initial"]["receiver_generation"]]
        generations.extend(event["generation_id"] for event in fixture["events"])
        for generation in generations:
            with self.subTest(generation=generation):
                self.assertIsInstance(generation, str)
                self.assertRegex(generation, r"^[1-9][0-9]*$")
                self.assertEqual(str(int(generation)), generation)


if __name__ == "__main__":
    unittest.main()
