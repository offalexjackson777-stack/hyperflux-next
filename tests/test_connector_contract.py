# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs" / "developer" / "connectors.md"
EXAMPLE = ROOT / "sdk" / "python" / "examples" / "read_integration_view.py"


class ConnectorContractTests(unittest.TestCase):
    def test_contract_preserves_sdk_authority_and_protocol_compatibility(self) -> None:
        text = DOCUMENT.read_text(encoding="utf-8")
        for heading in (
            "## Stable Boundary",
            "## Capability Rules",
            "## Compatibility",
            "## Minimal Example",
        ):
            self.assertIn(heading, text)
        self.assertIn("Protocol versions 1 through 7", text)
        self.assertIn("never replayed automatically", text)
        self.assertIn("receive no write authority", text)

    def test_minimal_example_uses_only_public_read_only_sdk_surface(self) -> None:
        source = EXAMPLE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(EXAMPLE))
        compile(tree, str(EXAMPLE), "exec")
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "hyperflux_sdk"
            for alias in node.names
        }
        self.assertEqual(
            imports,
            {
                "Client",
                "ClientConfig",
                "ClientId",
                "ClientName",
                "ProtocolFeatureId",
                "UnixChannelConfig",
            },
        )
        self.assertIn("Client.connect_unix", source)
        self.assertIn("client.integration_view()", source)
        self.assertIn("client.close()", source)
        for forbidden in (
            "1532",
            "00cf",
            "00cd",
            "0296",
            "/dev/",
            "/sys/",
            "/run/",
            "subprocess",
            "openrazer",
            "openrgb",
        ):
            self.assertNotIn(forbidden, source.casefold())


if __name__ == "__main__":
    unittest.main()
