# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass


class HyperFluxSdkError(RuntimeError):
    """Base class for local and bridge-originated SDK failures."""


class CodecError(HyperFluxSdkError):
    """A value cannot be represented by the negotiated wire contract."""


class FramingError(HyperFluxSdkError):
    """A bounded bridge frame is malformed, incomplete, or unavailable."""


class ConnectionClosed(FramingError):
    """The bridge closed the SDK connection before returning a response."""


class PeerCredentialError(HyperFluxSdkError):
    """The connected Unix peer is not the configured bridge authority."""


class NegotiationError(HyperFluxSdkError):
    """The bridge and client could not establish the required SDK contract."""


class ResponseMismatch(HyperFluxSdkError):
    """A response is not bound to the request or bridge instance that issued it."""


class UnexpectedResponse(HyperFluxSdkError):
    """The bridge returned a valid response variant for another method."""


@dataclass(frozen=True, slots=True)
class BridgeError(HyperFluxSdkError):
    """One structured bridge rejection rendered without losing its stable finding."""

    message: str
    finding_id: str
    kind: str

    def __str__(self) -> str:
        return f"{self.message} [{self.finding_id}; {self.kind}]"


class OwnershipConflict(HyperFluxSdkError):
    """Another application owns at least one requested resource."""


class InvalidController(HyperFluxSdkError):
    """An application controller lacks exact generation/profile lighting authority."""


class InvalidLightingFrame(HyperFluxSdkError):
    """A lighting frame does not match its qualified logical topology."""


class SessionInactive(HyperFluxSdkError):
    """A lighting operation was attempted without a current lease."""
