# SPDX-License-Identifier: GPL-2.0-or-later

"""Private, SDK-only OpenRazer compatibility for HyperFlux Next."""

from .contract import CONTRACT, IdentityMode, ServiceIdentity
from .model import ControllerRecord, controller_serial
from ._version import __version__

__all__ = [
    "CONTRACT",
    "ControllerRecord",
    "IdentityMode",
    "ServiceIdentity",
    "controller_serial",
    "__version__",
]
