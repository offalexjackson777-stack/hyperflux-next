# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "device-qualification"


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self._script_without_source = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link" and values.get("href"):
            self.assets.append(values["href"] or "")
        if tag == "script":
            source = values.get("src")
            if source:
                self.assets.append(source)
            else:
                self._script_without_source = True
        if tag == "style":
            self.inline_styles += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_without_source:
            self.inline_scripts += 1
            self._script_without_source = False


class DeviceQualificationConsoleTests(unittest.TestCase):
    def test_shipped_interface_is_the_canonical_dependency_free_source(self) -> None:
        expected = {
            "README.md",
            "assets/app.css",
            "assets/app.js",
            "assets/contract.js",
            "contracts/local-qualification-view.schema.json",
            "index.html",
            "tests/fixtures/rust-qualified-mouse.json",
            "tests/fixtures/rust-qualified-pair.json",
        }
        observed = {
            path.relative_to(APP).as_posix()
            for path in APP.rglob("*")
            if path.is_file()
        }
        self.assertEqual(observed, expected)
        for forbidden in ("package.json", "package-lock.json", "node_modules", "dist"):
            self.assertFalse((APP / forbidden).exists(), forbidden)

    def test_html_loads_only_explicit_same_origin_assets(self) -> None:
        parser = AssetParser()
        parser.feed((APP / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.assets, ["/assets/app.css", "/assets/app.js"])
        self.assertEqual(parser.inline_scripts, 0)
        self.assertEqual(parser.inline_styles, 0)
        self.assertIn("Local only", (APP / "index.html").read_text(encoding="utf-8"))

    def test_browser_has_no_remote_or_direct_hardware_surface(self) -> None:
        application = (APP / "assets" / "app.js").read_text(encoding="utf-8")
        contract = (APP / "assets" / "contract.js").read_text(encoding="utf-8")
        combined = application + contract
        for forbidden in (
            "navigator.hid",
            "navigator.usb",
            "WebSocket",
            "EventSource",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "window.open",
            "XMLHttpRequest",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotRegex(application, r"https?://")
        self.assertIn('fetchWithTimeout("/v1/qualification/view"', application)
        self.assertIn('action.href', application)
        self.assertIn('network_upload_executed', contract)

    def test_contract_and_rust_fixture_share_the_versioned_schema(self) -> None:
        schema = json.loads(
            (APP / "contracts" / "local-qualification-view.schema.json").read_text(
                encoding="utf-8"
            )
        )
        fixture = json.loads(
            (APP / "tests" / "fixtures" / "rust-qualified-mouse.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema"]["const"], fixture["schema"])
        self.assertEqual(schema["properties"]["api_version"]["const"], fixture["api_version"])
        self.assertFalse(fixture["companion"]["network_upload_executed"])
        self.assertFalse(fixture["companion"]["hardware_write_executed"])
        self.assertIn(
            "legacy-v2-detected",
            schema["properties"]["companion"]["properties"]["state"]["enum"],
        )

    def test_console_explains_a_v2_installation_without_calling_next_broken(self) -> None:
        application = (APP / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"legacy-v2-detected"', application)
        self.assertIn("This computer is running HyperFlux V2", application)
        self.assertIn("working V2 installation as a broken Next bridge", application)

    def test_operational_layout_has_bounded_responsive_and_accessibility_rules(self) -> None:
        html = (APP / "index.html").read_text(encoding="utf-8")
        css = (APP / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertIn('aria-live="polite"', html)
        self.assertIn("<noscript>", html)
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("@media (max-width: 430px)", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", css)
        self.assertNotIn("scroll-snap-type", css)
        self.assertNotIn("linear-gradient", css)
        self.assertNotIn("radial-gradient", css)
        self.assertIsNone(re.search(r"letter-spacing\s*:\s*-", css))
        self.assertLess((APP / "assets" / "app.js").stat().st_size, 64 * 1024)
        self.assertLess((APP / "assets" / "app.css").stat().st_size, 64 * 1024)


if __name__ == "__main__":
    unittest.main()
