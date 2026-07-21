# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import Final

from hyperflux_sdk.generated.domain_types import ControllerAvailability, DeviceKind
from hyperflux_sdk.generated.openrazer_metadata import OPENRAZER_DEVICES_BY_PROFILE
from hyperflux_sdk.generated import protocol_v5_types as v5


_SERIAL_DOMAIN: Final = b"hyperflux-next-openrazer-compat-v1\0"
_REQUIRED_CAPABILITIES: Final = frozenset(
    (
        "lighting.brightness",
        "lighting.direct-frame",
        "lighting.off",
        "lighting.software-effect-frames",
        "lighting.static",
    )
)


class CompatibilityModelError(RuntimeError):
    """The SDK view cannot be represented by the reviewed compatibility contract."""


def controller_serial(receiver_id: str, device_id: str) -> str:
    material = _SERIAL_DOMAIN + receiver_id.encode("utf-8")
    material += b"\0" + device_id.encode("utf-8")
    return "hfx_" + hashlib.sha256(material).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ControllerRecord:
    serial: str
    controller: v5.ControllerView
    model_name: str
    image_url: str
    vendor_id: int
    product_id: int
    rows: int
    columns: int

    @property
    def led_count(self) -> int:
        return self.controller.lighting.application_slot_count.value

    @property
    def device_kind(self) -> DeviceKind:
        return self.controller.device_kind

    @property
    def available(self) -> bool:
        return self.controller.availability is ControllerAvailability.READY


def records_from_view(
    view: v5.IntegrationView,
    *,
    metadata: Mapping[str, Mapping[str, object]] = OPENRAZER_DEVICES_BY_PROFILE,
) -> tuple[ControllerRecord, ...]:
    records: dict[str, ControllerRecord] = {}
    for receiver in view.receivers:
        for controller in receiver.controllers:
            record = _record(controller, metadata)
            if record.serial in records:
                raise CompatibilityModelError(
                    "the bridge projected duplicate OpenRazer compatibility identity"
                )
            records[record.serial] = record
    return tuple(
        sorted(
            records.values(),
            key=lambda value: (
                value.device_kind.value,
                value.model_name.casefold(),
                value.serial,
            ),
        )
    )


def _record(
    controller: v5.ControllerView,
    metadata: Mapping[str, Mapping[str, object]],
) -> ControllerRecord:
    capabilities = {value.value for value in controller.capabilities}
    if not _REQUIRED_CAPABILITIES <= capabilities:
        raise CompatibilityModelError(
            "a compatibility controller lacks the required qualified lighting capabilities"
        )
    profile_id = controller.device_profile.profile_id.value
    imported = metadata.get(profile_id)
    if imported is None:
        raise CompatibilityModelError(
            "a qualified controller lacks exact pinned OpenRazer metadata"
        )
    identity = imported.get("identity")
    presentation = imported.get("presentation")
    if not isinstance(identity, Mapping) or not isinstance(presentation, Mapping):
        raise CompatibilityModelError("pinned OpenRazer metadata is malformed")
    rows = controller.lighting.rows.value
    columns = controller.lighting.columns.value
    expected = (
        identity.get("product_id"),
        identity.get("device_kind"),
        presentation.get("matrix_rows"),
        presentation.get("matrix_columns"),
        presentation.get("has_matrix"),
    )
    actual = (
        controller.product_id.value,
        controller.device_kind.value,
        rows,
        columns,
        True,
    )
    if expected != actual or rows * columns != controller.lighting.application_slot_count.value:
        raise CompatibilityModelError(
            "pinned OpenRazer metadata does not match qualified bridge authority"
        )
    vendor_id = identity.get("vendor_id")
    model_name = identity.get("model_name")
    image_url = presentation.get("image_url")
    if (
        isinstance(vendor_id, bool)
        or not isinstance(vendor_id, int)
        or not isinstance(model_name, str)
        or not model_name
        or not isinstance(image_url, str)
        or not image_url.startswith("https://")
    ):
        raise CompatibilityModelError("pinned OpenRazer presentation is unsafe")
    return ControllerRecord(
        serial=controller_serial(
            controller.receiver_id.value,
            controller.device_id.value,
        ),
        controller=controller,
        model_name=model_name,
        image_url=image_url,
        vendor_id=vendor_id,
        product_id=controller.product_id.value,
        rows=rows,
        columns=columns,
    )


__all__ = [
    "CompatibilityModelError",
    "ControllerRecord",
    "controller_serial",
    "records_from_view",
]
