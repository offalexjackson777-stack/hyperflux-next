# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from types import MappingProxyType
from typing import Final, Mapping

from .generated_contract import OPENRAZER_COMPATIBILITY_CONTRACT


CONTRACT: Final[Mapping[str, object]] = MappingProxyType(
    OPENRAZER_COMPATIBILITY_CONTRACT
)
ISOLATED_SESSION_MARKER: Final = "HYPERFLUX_OPENRAZER_ISOLATED_SESSION"


class IdentityMode(Enum):
    PRIVATE = "private"
    ORG_RAZER_PRIVATE_SESSION = "org-razer-private-session"


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    mode: IdentityMode
    bus_name: str
    root_path: str
    requires_isolated_session: bool

    @property
    def device_path_prefix(self) -> str:
        return f"{self.root_path}/device"

    @property
    def claims_official_name(self) -> bool:
        return self.bus_name == "org.razer"


def identity_for_mode(
    mode: IdentityMode,
    *,
    environment: Mapping[str, str] = os.environ,
) -> ServiceIdentity:
    service = CONTRACT["service"]
    if not isinstance(service, Mapping):
        raise RuntimeError("generated OpenRazer service contract is malformed")
    key = "private_identity" if mode is IdentityMode.PRIVATE else "legacy_identity"
    value = service[key]
    if not isinstance(value, Mapping):
        raise RuntimeError("generated OpenRazer identity contract is malformed")
    identity = ServiceIdentity(
        mode=mode,
        bus_name=str(value["bus_name"]),
        root_path=str(value["root_path"]),
        requires_isolated_session=bool(value["requires_isolated_session"]),
    )
    if identity.requires_isolated_session:
        if environment.get(ISOLATED_SESSION_MARKER) != "1":
            raise ValueError(
                "the org.razer identity is allowed only inside the HyperFlux isolated session launcher"
            )
        if not environment.get("DBUS_SESSION_BUS_ADDRESS"):
            raise ValueError("the isolated compatibility mode requires a session bus")
    return identity


def reconcile_bounds() -> tuple[int, int, int]:
    service = CONTRACT["service"]
    if not isinstance(service, Mapping):
        raise RuntimeError("generated OpenRazer service contract is malformed")
    values = service["reconcile_interval_ms"]
    if not isinstance(values, Mapping):
        raise RuntimeError("generated OpenRazer interval contract is malformed")
    return int(values["minimum"]), int(values["default"]), int(values["maximum"])


__all__ = [
    "CONTRACT",
    "ISOLATED_SESSION_MARKER",
    "IdentityMode",
    "ServiceIdentity",
    "identity_for_mode",
    "reconcile_bounds",
]
