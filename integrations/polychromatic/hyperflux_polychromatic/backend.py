# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import atexit
from pathlib import Path
from threading import RLock
from typing import Callable

from polychromatic.backends._backend import Backend

from hyperflux_sdk.generated.domain_types import DeviceKind

from .runtime import ControllerRecord, HyperFluxRuntime, IntegrationError
from .state import Rgb


VERSION = "0.0.0-dev.1"


def _rgb_hex(color: Rgb) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in color)


def _parse_hex(value: object) -> Rgb:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        raise IntegrationError("Polychromatic supplied an invalid static color")
    try:
        color = (
            int(value[1:3], 16),
            int(value[3:5], 16),
            int(value[5:7], 16),
        )
    except ValueError as error:
        raise IntegrationError("Polychromatic supplied an invalid static color") from error
    return color


class HyperFluxBattery(Backend.DeviceItem.Battery):
    def __init__(
        self,
        runtime: HyperFluxRuntime,
        record: ControllerRecord,
    ) -> None:
        super().__init__()
        self._runtime = runtime
        self._serial = record.serial
        self._apply(record)

    def refresh(self, record: ControllerRecord | None = None) -> None:
        current = self._runtime.record(self._serial) if record is None else record
        self._apply(current)

    def _apply(self, record: ControllerRecord) -> None:
        percentage = self._runtime.battery_from_record(record)
        self.percentage = -1 if percentage is None else percentage
        # Receiver telemetry does not currently qualify charging semantics.
        self.is_charging = False


class HyperFluxMatrix(Backend.DeviceItem.Matrix):
    def __init__(self, runtime: HyperFluxRuntime, record: ControllerRecord) -> None:
        super().__init__()
        self._runtime = runtime
        self._serial = record.serial
        self.name = record.model_name
        self.form_factor_id = record.device_kind.value
        self.rows = record.rows
        self.cols = record.columns
        self._colors: list[Rgb] = [(0, 0, 0)] * record.led_count
        self._brightness = 100
        self._lock = RLock()

    def set(self, x=0, y=0, red=255, green=255, blue=255) -> None:
        values = (x, y, red, green, blue)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise IntegrationError("Polychromatic matrix values must be integers")
        if not 0 <= x < self.cols or not 0 <= y < self.rows:
            raise IntegrationError("Polychromatic matrix coordinate is outside the device")
        if any(not 0 <= channel <= 255 for channel in (red, green, blue)):
            raise IntegrationError("Polychromatic matrix color is outside the RGB range")
        with self._lock:
            self._colors[y * self.cols + x] = (red, green, blue)

    def draw(self) -> None:
        with self._lock:
            colors = tuple(self._colors)
            brightness = self._brightness
        self._runtime.effect_frame(self._serial, colors, brightness)

    def clear(self) -> None:
        with self._lock:
            self._colors[:] = [(0, 0, 0)] * len(self._colors)

    def brightness(self, percent) -> None:
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            raise IntegrationError("Polychromatic effect brightness must be from 0 through 100")
        with self._lock:
            self._brightness = percent


class HyperFluxStaticOption(Backend.EffectOption):
    def __init__(
        self,
        runtime: HyperFluxRuntime,
        serial: str,
        indexes: tuple[int, ...],
    ) -> None:
        super().__init__()
        self.uid = "static"
        self.colours_required = 1
        self._runtime = runtime
        self._serial = serial
        self._indexes = indexes
        self.refresh()

    def refresh(self) -> None:
        state = self._runtime.stable_state(self._serial)
        colors = {state.colors[index] for index in self._indexes}
        self.active = state.mode != "unknown" and len(colors) == 1
        self.colours = [_rgb_hex(next(iter(colors)))] if len(colors) == 1 else ["#000000"]

    def apply(self, _parameter=None) -> None:
        self._runtime.apply_static(
            self._serial,
            self._indexes,
            _parse_hex(self.colours[0]),
        )
        self.refresh()


class HyperFluxOffOption(Backend.EffectOption):
    def __init__(
        self,
        runtime: HyperFluxRuntime,
        serial: str,
        indexes: tuple[int, ...],
    ) -> None:
        super().__init__()
        self.uid = "none"
        self._runtime = runtime
        self._serial = serial
        self._indexes = indexes
        self.refresh()

    def refresh(self) -> None:
        state = self._runtime.stable_state(self._serial)
        self.active = state.mode != "unknown" and all(
            not any(state.colors[index]) for index in self._indexes
        )

    def apply(self, _parameter=None) -> None:
        self._runtime.apply_off(self._serial, self._indexes)
        self.refresh()


class HyperFluxBrightness(Backend.SliderOption):
    def __init__(self, runtime: HyperFluxRuntime, serial: str) -> None:
        super().__init__()
        self.uid = "brightness"
        self.min = 0
        self.max = 100
        self.step = 5
        self.suffix = "%"
        self.suffix_plural = "%"
        self._runtime = runtime
        self._serial = serial
        self.refresh()

    def refresh(self) -> None:
        self.value = self._runtime.stable_state(self._serial).brightness

    def apply(self, value=0) -> None:
        self._runtime.apply_brightness(self._serial, value)
        self.value = value


class HyperFluxDevice(Backend.DeviceItem):
    def __init__(self, backend: HyperFluxBackend, record: ControllerRecord) -> None:
        super().__init__()
        self.backend = backend
        self.backend_id = backend.backend_id
        self._runtime = backend.runtime
        self.name = record.model_name
        self.form_factor = backend.get_form_factor(record.device_kind.value)
        self.real_image = record.image_url
        self.serial = record.serial
        self.vid = f"{record.vendor_id:04X}"
        self.pid = f"{record.product_id:04X}"
        self.monochromatic = False
        self.keyboard_layout = ""
        self.has_programmable_keys = False
        self.has_macro_keys = False
        capabilities = {value.value for value in record.controller.capabilities}
        if "telemetry.battery-percent" in capabilities:
            self.battery = HyperFluxBattery(self._runtime, record)
        if {
            "lighting.direct-frame",
            "lighting.software-effect-frames",
        } <= capabilities:
            self.matrix = HyperFluxMatrix(self._runtime, record)
        self.zones = self._zones(backend, record)

    def refresh(self) -> None:
        record = self._runtime.record(self.serial)
        if self.battery is not None:
            self.battery.refresh(record)
        for zone in self.zones:
            for option in zone.options:
                option.refresh()

    def _zones(self, backend: HyperFluxBackend, record: ControllerRecord) -> list[Backend.DeviceItem.Zone]:
        main = self._zone(
            backend,
            "main",
            self.form_factor["label"],
            tuple(range(record.led_count)),
            brightness=True,
        )
        zones = [main]
        if record.device_kind is DeviceKind.MOUSE and record.led_count == 13:
            zones.extend(
                (
                    self._zone(backend, "scroll", backend._("Scroll Wheel"), (0,)),
                    self._zone(backend, "logo", backend._("Logo"), (1,)),
                    self._zone(
                        backend,
                        "led-strip",
                        backend._("LED Strip"),
                        tuple(range(2, 13)),
                    ),
                )
            )
        return zones

    def _zone(
        self,
        backend: HyperFluxBackend,
        zone_id: str,
        label: str,
        indexes: tuple[int, ...],
        *,
        brightness: bool = False,
    ) -> Backend.DeviceItem.Zone:
        zone = Backend.DeviceItem.Zone()
        zone.zone_id = zone_id
        zone.label = label
        zone.icon = self.form_factor["icon"] if zone_id == "main" else ""
        off = HyperFluxOffOption(self._runtime, self.serial, indexes)
        off.label = backend._("Off")
        off.icon = backend.get_icon("params", "0")
        static = HyperFluxStaticOption(self._runtime, self.serial, indexes)
        static.label = backend._("Static")
        static.icon = backend.get_icon("options", "static")
        zone.options = [off, static]
        if brightness:
            slider = HyperFluxBrightness(self._runtime, self.serial)
            slider.label = backend._("Brightness")
            slider.icon = backend.get_icon("options", "brightness")
            zone.options.insert(0, slider)
        return zone


RuntimeFactory = Callable[[Path], HyperFluxRuntime]


class HyperFluxBackend(Backend):
    """Native HyperFlux backend loaded beside Polychromatic's OpenRazer backend."""

    def __init__(
        self,
        base,
        *,
        runtime: HyperFluxRuntime | None = None,
        runtime_factory: RuntimeFactory = HyperFluxRuntime.production,
    ) -> None:
        super().__init__(base)
        self.backend_id = "hyperflux"
        self.name = "HyperFlux Next"
        self.logo = ""
        self.version = VERSION
        # Publication endpoints remain empty until the repository interlock is released.
        self.project_url = ""
        self.bug_url = ""
        self.releases_url = ""
        self.license = "GPLv3"
        self._runtime_factory = runtime_factory
        self._runtime = runtime
        self._registered_close = False

    @property
    def runtime(self) -> HyperFluxRuntime:
        if self._runtime is None:
            storage = Path(self.get_backend_storage_path())
            self._runtime = self._runtime_factory(storage)
        return self._runtime

    def init(self):
        try:
            self.runtime.refresh()
            if not self._registered_close:
                atexit.register(self.runtime.close)
                self._registered_close = True
            return True
        except Exception as error:
            self.debug(f"Initialization failed: {error}")
            return self.get_exception_as_string(error)

    def get_devices(self):
        try:
            return [HyperFluxDevice(self, record) for record in self.runtime.records()]
        except Exception as error:
            self.debug(f"Device refresh failed: {error}")
            return []

    def get_device_by_name(self, name):
        for device in self.get_devices():
            if device.name == name:
                return device
        return None

    def get_device_by_serial(self, serial):
        try:
            record = self.runtime.record(serial)
        except Exception:
            return None
        return HyperFluxDevice(self, record)

    def get_unsupported_devices(self):
        # This backend never scans or suppresses unrelated Razer USB devices.
        return []

    def restart(self):
        try:
            self.runtime.close()
            storage = Path(self.get_backend_storage_path())
            self._runtime = self._runtime_factory(storage)
            self.runtime.refresh()
            return True
        except Exception as error:
            self.debug(f"Restart failed: {error}")
            return False


__all__ = ["HyperFluxBackend", "HyperFluxDevice"]
