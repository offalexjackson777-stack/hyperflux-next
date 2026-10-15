# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from argparse import Namespace
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from tests.openrgb_packaged_plugin_sdk_qualification import (
    DESKTOP_UID,
    MAX_DIAGNOSTIC_LINE_BYTES,
    MAX_REQUEST_METHODS,
    BridgeEvidence,
    HarnessProcess,
    QualificationFailure,
    SDK_PORT,
    SYSTEM_OPENRGB_PLUGIN_DIRECTORY,
    SyntheticNativeBridge,
    harness_command,
    isolation_command,
    read_bounded_line,
    runtime_socket_path,
    subordinate_id_start,
)
from hfxdev.model import ModelError
from hfxdev.verify_packaging import (
    EXTERNAL_OPENRGB_QUALIFICATION_BINDING,
    OPENRGB_QUALIFICATION_REQUEST_SCHEMA,
    PACKAGE_BUILD_REUSE_NAME,
    _clean_source_identity,
    _consume_external_openrgb_qualification,
    _record_package_build_reuse,
    _reusable_package_build,
    finalize_external_openrgb_qualification,
)


class OpenRgbPackagedQualificationTests(unittest.TestCase):
    @staticmethod
    def cleanup_harness(process: Mock) -> HarnessProcess:
        harness = HarnessProcess.__new__(HarnessProcess)
        harness.process = process
        harness.command = Mock()
        harness._event_overflow = threading.Event()
        harness._diagnostic_truncation = threading.Event()
        harness._cleanup_failure = None
        harness._stdout_thread = Mock()
        harness._stderr_thread = Mock()
        harness._stdout_thread.is_alive.return_value = False
        harness._stderr_thread.is_alive.return_value = False
        return harness

    def test_outer_isolation_uses_only_the_current_identity_mapping(self) -> None:
        arguments = Namespace(
            harness=Path("/tmp/harness"),
            plugin=Path("/tmp/plugin.so"),
            openrgb_commit="a" * 40,
            evidence=Path("/tmp/evidence.json"),
            runtime="system",
        )
        command = isolation_command(arguments, pinned_container=False)
        self.assertEqual(command[:3], ("unshare", "--user", "--map-root-user"))
        self.assertNotIn("--map-users", " ".join(command))
        self.assertNotIn("--map-groups", " ".join(command))
        self.assertIn("--mount", command)
        self.assertIn("--net", command)
        self.assertIn("--pid", command)
        self.assertNotIn("--mount-proc", command)
        self.assertIn("--isolation-authority", command)
        self.assertEqual(
            command[command.index("--isolation-authority") + 1], "user-namespace"
        )

        pinned = isolation_command(arguments, pinned_container=True)
        self.assertNotIn("--mount-proc", pinned)
        self.assertIn("--runtime", pinned)
        self.assertEqual(pinned[pinned.index("--runtime") + 1], "system")

    def test_desktop_isolation_maps_one_bounded_unprivileged_identity(self) -> None:
        arguments = Namespace(
            harness=Path("/tmp/harness"),
            plugin=Path("/tmp/plugin.so"),
            openrgb_commit="a" * 40,
            evidence=Path("/tmp/evidence.json"),
            runtime="desktop",
        )
        command = isolation_command(
            arguments,
            pinned_container=False,
            subordinate_ids=(200_000, 300_000),
        )
        self.assertIn(f"--map-users={DESKTOP_UID}:200000:1", command)
        self.assertIn(f"--map-groups={DESKTOP_UID}:300000:1", command)
        self.assertNotIn("--mount-proc", command)
        self.assertEqual(command[command.index("--runtime") + 1], "desktop")
        with self.assertRaisesRegex(QualificationFailure, "host subordinate-ID mapping"):
            isolation_command(arguments, pinned_container=True)

    def test_external_oci_evidence_is_hash_and_source_bound(self) -> None:
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as temporary:
            root = Path(temporary)
            harness = root / "harness"
            plugin = root / "plugin.so"
            evidence = root / "evidence.json"
            request = root / "request.json"
            binding = root / "binding.json"
            destination = root / "consumed.json"
            harness.write_bytes(b"harness")
            plugin.write_bytes(b"plugin")
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD^{commit}"],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            inputs = {
                "harness_sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
                "openrgb_commit": "a" * 40,
                "plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
                "runtime": "system",
            }
            request.write_text(
                json.dumps(
                    {
                        "schema": OPENRGB_QUALIFICATION_REQUEST_SCHEMA,
                        "source": {"revision": revision, "tree": tree},
                        "inputs": inputs,
                        "paths": {
                            "harness": harness.relative_to(ROOT).as_posix(),
                            "plugin": plugin.relative_to(ROOT).as_posix(),
                        },
                    }
                ),
                encoding="utf-8",
            )
            evidence.write_text(
                json.dumps(
                    {
                        "schema": "hyperflux-openrgb-isolated-qualification-v1",
                        "isolation": {
                            "authority": "outer-oci-container",
                            "runtime": "system",
                            "container_root": True,
                            "harness_capability_bounding_set": "empty",
                            "harness_capability_drop_preflight": "pass",
                            "outer_setup_capabilities": [
                                "DAC_OVERRIDE",
                                "NET_ADMIN",
                                "SETPCAP",
                                "SYS_ADMIN",
                            ],
                            "network_namespace": "loopback-only",
                            "process_view": "empty-tmpfs",
                            "sysfs": "empty-tmpfs",
                            "hidraw_visible": False,
                            "uhid_visible": False,
                        },
                        "inputs": inputs,
                        "positive": {"result": "pass"},
                        "historical_constant_identity": {"result": "pass"},
                        "missing_ownership_leases": {"result": "pass"},
                        "missing_native_receiver_tunnel": {"result": "pass"},
                    }
                ),
                encoding="utf-8",
            )
            finalize_external_openrgb_qualification(
                ROOT,
                request_path=request,
                evidence_path=evidence,
                binding_path=binding,
            )
            _consume_external_openrgb_qualification(
                ROOT,
                binding_path=binding,
                destination=destination,
                harness=harness,
                plugin=plugin,
                openrgb_commit="a" * 40,
            )
            self.assertEqual(destination.read_bytes(), evidence.read_bytes())

            rebuilt_harness = root / "rebuilt-harness"
            rebuilt_plugin = root / "rebuilt-plugin.so"
            rebuilt_harness.write_bytes(harness.read_bytes())
            rebuilt_plugin.write_bytes(plugin.read_bytes())
            _consume_external_openrgb_qualification(
                ROOT,
                binding_path=binding,
                destination=destination,
                harness=rebuilt_harness,
                plugin=rebuilt_plugin,
                openrgb_commit="a" * 40,
            )
            self.assertEqual(destination.read_bytes(), evidence.read_bytes())

            plugin.write_bytes(b"drift")
            with self.assertRaisesRegex(ModelError, "binding is invalid"):
                _consume_external_openrgb_qualification(
                    ROOT,
                    binding_path=binding,
                    destination=destination,
                    harness=harness,
                    plugin=plugin,
                    openrgb_commit="a" * 40,
                )

    def test_harness_namespace_root_has_no_capability_path(self) -> None:
        command = harness_command(Path("/tmp/harness"))
        self.assertEqual(
            command,
            (
                "setpriv",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "/tmp/harness",
                str(SDK_PORT),
                str(SYSTEM_OPENRGB_PLUGIN_DIRECTORY),
            ),
        )

        desktop = harness_command(Path("/tmp/harness"), "desktop")
        self.assertIn(f"--reuid={DESKTOP_UID}", desktop)
        self.assertIn(f"--regid={DESKTOP_UID}", desktop)
        self.assertIn("--clear-groups", desktop)

    def test_diagnostics_retain_one_bounded_line_and_drain_the_remainder(self) -> None:
        stream = io.BytesIO(
            b"x" * (MAX_DIAGNOSTIC_LINE_BYTES + 123) + b"\nnext\n"
        )
        first = read_bounded_line(stream)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(len(first[0].encode("utf-8")), MAX_DIAGNOSTIC_LINE_BYTES)
        self.assertTrue(first[1])
        self.assertEqual(read_bounded_line(stream), ("next", False))
        self.assertIsNone(read_bounded_line(stream))

    def test_subordinate_range_and_runtime_paths_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subuid"
            path.write_text("other:100000:65536\nworker:200000:65536\n", encoding="utf-8")
            self.assertEqual(subordinate_id_start(path, "worker"), 200_000)
        self.assertEqual(runtime_socket_path("system"), Path("/run/hyperflux-next/bridge.sock"))
        self.assertEqual(
            runtime_socket_path("desktop"),
            Path(f"/run/user/{DESKTOP_UID}/hyperflux-next-bridge.sock"),
        )

    def test_bridge_request_evidence_fails_closed_at_its_cardinality_bound(self) -> None:
        evidence = BridgeEvidence()
        for index in range(MAX_REQUEST_METHODS):
            self.assertTrue(evidence.record_request(f"method-{index}"))
        self.assertFalse(evidence.record_request("one-method-too-many"))
        snapshot = evidence.snapshot()
        self.assertEqual(len(snapshot["requests"]), MAX_REQUEST_METHODS)
        self.assertTrue(snapshot["evidence_overflow"])

    def test_bridge_protocol_and_teardown_failures_are_latched(self) -> None:
        evidence = BridgeEvidence()
        evidence.record_protocol_error(QualificationFailure("malformed client"))
        evidence.record_teardown_failure("worker exceeded its stop bound")
        snapshot = evidence.snapshot()
        self.assertEqual(snapshot["protocol_errors"], 1)
        self.assertEqual(snapshot["first_protocol_error"], "malformed client")
        self.assertEqual(snapshot["teardown_failures"], 1)
        self.assertEqual(
            snapshot["first_teardown_failure"], "worker exceeded its stop bound"
        )

    def test_forced_harness_cleanup_can_never_pass_qualification(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        harness = self.cleanup_harness(process)
        harness.command.side_effect = QualificationFailure("quit failed")
        harness.close()
        process.terminate.assert_called_once_with()
        with self.assertRaisesRegex(QualificationFailure, "graceful.*shutdown failed"):
            harness.require_clean()

        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("harness", 8),
            0,
        ]
        harness = self.cleanup_harness(process)
        harness.command.return_value = {"event": "stopped"}
        harness.close()
        process.kill.assert_called_once_with()
        with self.assertRaisesRegex(QualificationFailure, "graceful stop bound"):
            harness.require_clean()

    def test_bridge_socket_ownership_defaults_to_the_fixture_creator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            socket_path = Path(temporary) / "bridge.sock"
            bridge = SyntheticNativeBridge(BridgeEvidence(), socket_path=socket_path)
            bridge.start()
            try:
                self.assertEqual(socket_path.stat().st_uid, os.geteuid())
                self.assertEqual(bridge.socket_owner_uid, None)
            finally:
                bridge.stop()

    def test_package_contracts_reuse_only_the_exact_qualified_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*arguments: str) -> None:
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
                    cwd=root,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            git("init", "-q")
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            git("add", ".gitignore")
            git("commit", "-q", "-m", "source")
            workspace = root / "build" / "package-contracts"
            manifest = workspace / "artifacts" / "package-build-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            (workspace / "openrgb-sdk-harness").mkdir()
            (workspace / "external-full-root").mkdir()
            binding = {EXTERNAL_OPENRGB_QUALIFICATION_BINDING: "build/binding.json"}

            with patch.dict(os.environ, binding):
                self.assertIsNone(_reusable_package_build(root, workspace))
            _record_package_build_reuse(root, workspace, None)
            self.assertFalse((workspace / PACKAGE_BUILD_REUSE_NAME).exists())

            _record_package_build_reuse(root, workspace, _clean_source_identity(root))
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(_reusable_package_build(root, workspace))
            with patch.dict(os.environ, binding):
                self.assertEqual(_reusable_package_build(root, workspace), manifest)
            self.assertEqual(
                sorted(entry.name for entry in workspace.iterdir()),
                ["artifacts", "openrgb-sdk-harness", PACKAGE_BUILD_REUSE_NAME],
            )

            (root / "local-change.txt").write_text("drift\n", encoding="utf-8")
            with patch.dict(os.environ, binding):
                self.assertIsNone(_reusable_package_build(root, workspace))
            git("add", "local-change.txt")
            git("commit", "-q", "-m", "next")
            with patch.dict(os.environ, binding):
                self.assertIsNone(_reusable_package_build(root, workspace))


if __name__ == "__main__":
    unittest.main()
