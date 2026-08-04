# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import os
import secrets
from threading import Lock
from typing import Protocol

from .generated.domain_types import RequestId, TransactionId


class IdentitySource(Protocol):
    def next_request_id(self) -> RequestId: ...

    def next_transaction_id(self) -> TransactionId: ...


class ProcessIdentitySource:
    """Thread-safe process-scoped request and transaction identities."""

    def __init__(self, prefix: str | None = None) -> None:
        self._prefix = prefix or f"py-{os.getpid():x}-{secrets.token_hex(8)}"
        self._request = 0
        self._transaction = 0
        self._lock = Lock()

    def next_request_id(self) -> RequestId:
        with self._lock:
            self._request += 1
            return RequestId(f"{self._prefix}-r-{self._request:x}")

    def next_transaction_id(self) -> TransactionId:
        with self._lock:
            self._transaction += 1
            return TransactionId(f"{self._prefix}-t-{self._transaction:x}")
