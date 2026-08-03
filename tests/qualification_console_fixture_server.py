# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "device-qualification"
FIXTURE = APP / "tests" / "fixtures" / "rust-qualified-pair.json"
MAX_BODY = 16 * 1024


def fixture_view() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class State:
    def __init__(self, scenario: str = "qualified-pair") -> None:
        self.view = fixture_view()
        if scenario == "unknown":
            device = self.view["receivers"][0]["devices"][0]
            device["model_name"] = None
            device["profile"] = None
            device["support"] = "unknown"
            device["capabilities"] = []
            self.view["receivers"][0]["devices"] = [device]
            self.view["plans"] = []
            self.view["actions"] = []

    def action(self, path: str, body: dict[str, object]) -> dict[str, object]:
        if body.get("view_revision") != self.view["view_revision"]:
            raise ValueError("fixture revision changed")
        action = next(
            (
                item
                for item in self.view["actions"]
                if item["href"] == path and item["enabled"]
            ),
            None,
        )
        if action is None:
            raise ValueError("fixture action is unavailable")
        stage = next(
            stage
            for plan in self.view["plans"]
            for group in plan["groups"]
            for stage in group["stages"]
            if stage["action_id"] == action["id"]
        )
        observations = body.get("observations")
        if not isinstance(observations, dict):
            raise ValueError("fixture observations are malformed")
        for prompt in stage["observations"]:
            answer = observations.get(prompt["id"])
            choice = next(
                (choice for choice in prompt["choices"] if choice["id"] == answer),
                None,
            )
            if choice is None or choice["outcome"] != "pass":
                raise ValueError("fixture accepts only passing observations")
        stage["status"] = "passed"
        stage["result"] = {
            "summary": "The test fixture recorded a complete read-only stage outcome.",
            "completed_at": "2026-07-22T00:00:00Z",
            "evidence_refs": [f"test-fixture:{stage['stage_id']}"],
        }
        action["enabled"] = False
        plan = next(plan for plan in self.view["plans"] if plan["device_id"] == "mouse-test")
        evidence = plan["evidence"]
        evidence["run_id"] = "local-browser-qa"
        evidence["artifact_state"] = "collecting"
        evidence["completed_claims"].append(stage["stage_id"])
        evidence["missing_claims"].remove(stage["stage_id"])
        all_stages = [stage for group in plan["groups"] for stage in group["stages"]]
        current_index = all_stages.index(stage)
        if current_index + 1 < len(all_stages):
            next_stage = all_stages[current_index + 1]
            if next_stage["risk"] == "read-only":
                next_stage["status"] = "ready"
                next_action = next(
                    item
                    for item in self.view["actions"]
                    if item["id"] == next_stage["action_id"]
                )
                next_action["enabled"] = True
        self.view["view_revision"] += 1
        plan["verdict"] = "in-progress"
        return deepcopy(self.view)


class Handler(BaseHTTPRequestHandler):
    state = State()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/qualification/view":
            self.respond_json(deepcopy(self.state.view))
            return
        relative = "index.html" if self.path == "/" else self.path.removeprefix("/")
        candidate = (APP / relative).resolve()
        if not candidate.is_relative_to(APP.resolve()) or not candidate.is_file():
            self.send_error(404)
            return
        content_types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
        content_type = content_types.get(candidate.suffix)
        if content_type is None:
            self.send_error(404)
            return
        body = candidate.read_bytes()
        self.respond(200, f"{content_type}; charset=utf-8", body)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1/qualification/actions/"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("fixture request size is invalid")
            body = json.loads(self.rfile.read(length))
            self.respond_json(self.state.action(self.path, body))
        except (ValueError, json.JSONDecodeError) as error:
            self.respond_json({"error": str(error)}, status=409)

    def respond_json(self, value: object, status: int = 200) -> None:
        self.respond(
            status,
            "application/json; charset=utf-8",
            json.dumps(value, separators=(",", ":")).encode("utf-8"),
        )

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Test-only HyperFlux console fixture server")
    parser.add_argument("--port", type=int, default=47_822)
    parser.add_argument(
        "--scenario",
        choices=("qualified-pair", "unknown"),
        default="qualified-pair",
    )
    arguments = parser.parse_args()
    Handler.state = State(arguments.scenario)
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler)
    print(f"Test-only qualification fixture: http://127.0.0.1:{arguments.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
