# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import socket
import struct
from threading import Lock
from typing import Protocol

from .codec import decode_message, encode_message
from .errors import ConnectionClosed, FramingError, PeerCredentialError
from .generated.protocol_v5_types import MAX_WIRE_MESSAGE_BYTES, RpcRequest, RpcResponse


class RpcChannel(Protocol):
    def exchange(self, request: RpcRequest) -> RpcResponse: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UnixChannelConfig:
    socket_path: Path = Path("/run/hyperflux-next/bridge.sock")
    timeout_seconds: float = 5.0
    expected_peer_uid: int | None = None
    expected_peer_user: str | None = "hyperflux-next"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("socket timeout must be greater than zero")
        if self.expected_peer_uid is not None and self.expected_peer_user is not None:
            raise ValueError("select an expected peer UID or account, not both")


class UnixRpcChannel:
    """Bounded, credential-checked local bridge request/response transport."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection: socket.socket | None = connection
        self._lock = Lock()

    @classmethod
    def connect(cls, config: UnixChannelConfig) -> UnixRpcChannel:
        encoded_path = os.fsencode(config.socket_path)
        if not encoded_path or len(encoded_path) >= 108:
            raise ValueError("bridge socket path is empty or exceeds the Unix socket bound")
        expected_uid = config.expected_peer_uid
        if config.expected_peer_user is not None:
            try:
                expected_uid = pwd.getpwnam(config.expected_peer_user).pw_uid
            except KeyError as error:
                raise PeerCredentialError("configured bridge service account is unavailable") from error
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(config.timeout_seconds)
            connection.connect(str(config.socket_path))
            if expected_uid is not None:
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                )
                _, uid, _ = struct.unpack("3i", credentials)
                if uid != expected_uid:
                    raise PeerCredentialError(
                        "local bridge peer UID does not match the configured authority"
                    )
            return cls(connection)
        except BaseException:
            connection.close()
            raise

    @classmethod
    def adopt_connected_socket(cls, connection: socket.socket) -> UnixRpcChannel:
        if connection.family != socket.AF_UNIX:
            raise ValueError("SDK channels require a connected Unix socket")
        return cls(connection)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def exchange(self, request: RpcRequest) -> RpcResponse:
        with self._lock:
            connection = self._connection
            if connection is None:
                raise ConnectionClosed("bridge SDK channel is closed")
            payload = encode_message(request)
            try:
                connection.sendall(struct.pack("!I", len(payload)) + payload)
                declared = struct.unpack("!I", self._read_exact(4))[0]
                if declared == 0 or declared > MAX_WIRE_MESSAGE_BYTES:
                    raise FramingError("bridge response length is outside the protocol bound")
                response = self._read_exact(declared)
            except (TimeoutError, socket.timeout) as error:
                raise FramingError("bridge SDK exchange exceeded its bounded timeout") from error
            except OSError as error:
                raise FramingError("bridge SDK exchange failed") from error
            return decode_message(RpcResponse, response)

    def _read_exact(self, size: int) -> bytes:
        connection = self._connection
        if connection is None:
            raise ConnectionClosed("bridge SDK channel is closed")
        output = bytearray()
        while len(output) < size:
            chunk = connection.recv(size - len(output))
            if not chunk:
                raise ConnectionClosed("bridge closed an incomplete SDK frame")
            output.extend(chunk)
        return bytes(output)

    def __enter__(self) -> UnixRpcChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
