# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
from threading import Thread
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from hyperflux_sdk.channel import UnixRpcChannel
from hyperflux_sdk.client import Client, ClientConfig
from hyperflux_sdk.codec import CodecError, decode_message, encode_message, to_wire
from hyperflux_sdk.generated.domain_types import (
    ClientId,
    ClientName,
    ProtocolFeatureId,
    RequestId,
)
from hyperflux_sdk.generated.protocol_v5_types import RpcRequest, RpcResponse, TransactionRequest
from hyperflux_sdk.identity import ProcessIdentitySource


def _read_frame(connection: socket.socket) -> dict:
    prefix = connection.recv(4)
    if len(prefix) != 4:
        raise AssertionError("request frame prefix is incomplete")
    size = struct.unpack("!I", prefix)[0]
    payload = bytearray()
    while len(payload) < size:
        payload.extend(connection.recv(size - len(payload)))
    return json.loads(payload)


def _write_frame(connection: socket.socket, value: dict) -> None:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    connection.sendall(struct.pack("!I", len(payload)) + payload)


class PythonSdkTests(unittest.TestCase):
    def test_transaction_fixture_round_trips_through_generated_types(self) -> None:
        payload = (ROOT / "protocol" / "v5" / "fixtures" / "transaction-request-canonical.json").read_bytes()
        request = decode_message(TransactionRequest, payload)
        self.assertEqual(json.loads(encode_message(request)), json.loads(payload))

    def test_codec_rejects_duplicate_fields_unknown_fields_and_bad_decimal_numbers(self) -> None:
        with self.assertRaisesRegex(CodecError, "duplicate JSON field"):
            decode_message(RpcRequest, b'{"method":"negotiate","method":"snapshot"}')
        with self.assertRaisesRegex(CodecError, "unknown extra"):
            decode_message(
                RpcRequest,
                b'{"method":"snapshot","request":{"request_id":"r","protocol_session_id":"s","negotiation_token":"n","params":{},"extra":1}}',
            )
        response = {
            "type": "integration-view-success",
            "response": {
                "request_id": "r",
                "server_instance_id": "server",
                "result": {
                    "cursor": {
                        "stream_id": "stream",
                        "stream_epoch": 1,
                        "projection_revision": 1,
                        "sequence": "0",
                    },
                    "receivers": [],
                },
            },
        }
        with self.assertRaisesRegex(CodecError, "decimal string"):
            decode_message(RpcResponse, json.dumps(response).encode())

    def test_client_negotiates_and_reads_integration_view_over_bounded_framing(self) -> None:
        client_socket, server_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                negotiate = _read_frame(server_socket)
                self.assertEqual(negotiate["method"], "negotiate")
                request_id = negotiate["request"]["request_id"]
                _write_frame(
                    server_socket,
                    {
                        "type": "negotiate-success",
                        "response": {
                            "request_id": request_id,
                            "server_instance_id": "server-1",
                            "result": {
                                "selected_version": 5,
                                "server_instance_id": "server-1",
                                "protocol_session_id": "session-1",
                                "negotiation_token": "token-1",
                                "bridge_version": "0.1.0",
                                "enabled_features": ["integration-view-projection"],
                                "event_buffer_capacity": 64,
                            },
                        },
                    },
                )
                request = _read_frame(server_socket)
                self.assertEqual(request["method"], "integration-view")
                self.assertEqual(request["request"]["protocol_session_id"], "session-1")
                _write_frame(
                    server_socket,
                    {
                        "type": "integration-view-success",
                        "response": {
                            "request_id": request["request"]["request_id"],
                            "server_instance_id": "server-1",
                            "result": {
                                "cursor": {
                                    "stream_id": "stream-1",
                                    "stream_epoch": "1",
                                    "projection_revision": 1,
                                    "sequence": "0",
                                },
                                "receivers": [],
                            },
                        },
                    },
                )
            except BaseException as error:
                failures.append(error)
            finally:
                server_socket.close()

        thread = Thread(target=serve)
        thread.start()
        channel = UnixRpcChannel.adopt_connected_socket(client_socket)
        sdk = Client.connect(
            channel,
            ClientConfig(
                client_id=ClientId("polychromatic"),
                client_name=ClientName("Polychromatic HyperFlux backend"),
                required_features=(ProtocolFeatureId("integration-view-projection"),),
            ),
            ProcessIdentitySource("test"),
        )
        view = sdk.integration_view()
        self.assertEqual(view.receivers, ())
        sdk.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        if failures:
            raise failures[0]

    def test_error_response_has_the_canonical_wire_wrapper(self) -> None:
        payload = {
            "type": "error",
            "response": {
                "request_id": "request-1",
                "server_instance_id": "server-1",
                "error": {
                    "request_id": "request-1",
                    "kind": "invalid-request",
                    "message": "bad request",
                    "finding_id": "HFX-REQUEST-001",
                },
            },
        }
        response = decode_message(RpcResponse, json.dumps(payload).encode())
        self.assertEqual(to_wire(response), payload)
        self.assertEqual(response.request_id, RequestId("request-1"))


if __name__ == "__main__":
    unittest.main()
