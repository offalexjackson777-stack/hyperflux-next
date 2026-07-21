# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from collections.abc import Callable
import json
import signal
import sys
from threading import RLock
from typing import Protocol

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from .contract import ServiceIdentity
from .matrix import MatrixError
from .model import ControllerRecord
from .runtime import CompatibilityRuntimeError


ADAPTER_VERSION = "0.0.0.dev1"


class ServiceRuntime(Protocol):
    def refresh(self) -> tuple[ControllerRecord, ...]: ...

    def record(self, serial: str, *, refresh: bool = True) -> ControllerRecord: ...

    def brightness(self, serial: str) -> float: ...

    def apply_static(self, serial: str, color: tuple[int, int, int]) -> None: ...

    def apply_off(self, serial: str) -> None: ...

    def apply_brightness(self, serial: str, brightness: float) -> None: ...

    def stage_matrix(self, serial: str, payload: bytes) -> None: ...

    def commit_matrix(self, serial: str) -> None: ...

    def close(self) -> None: ...


class _NotSupported(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.NotSupported"


class _InvalidArgs(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class _OperationFailed(dbus.DBusException):
    _dbus_error_name = "dev.hyperflux.OpenRazer1.Error.OperationFailed"


class RootObject(dbus.service.Object):
    def __init__(self, bus_name: dbus.service.BusName, object_path: str) -> None:
        self._serials: tuple[str, ...] = ()
        self._lock = RLock()
        super().__init__(bus_name, object_path)

    def replace_serials(self, serials: tuple[str, ...]) -> None:
        with self._lock:
            self._serials = serials

    @dbus.service.method("razer.daemon", out_signature="s")
    def version(self) -> str:
        return ADAPTER_VERSION

    @dbus.service.method("razer.devices", out_signature="as")
    def getDevices(self) -> list[str]:  # noqa: N802 - exact upstream D-Bus vocabulary
        with self._lock:
            return list(self._serials)

    @dbus.service.method("razer.devices", out_signature="b")
    def getSyncEffects(self) -> bool:  # noqa: N802 - exact upstream D-Bus vocabulary
        return False

    @dbus.service.method("razer.devices", in_signature="b")
    def syncEffects(self, enabled: bool) -> None:  # noqa: N802 - exact upstream D-Bus vocabulary
        if bool(enabled):
            raise _NotSupported(
                "HyperFlux does not emulate OpenRazer native hardware-effect synchronization"
            )

    @dbus.service.signal("razer.devices")
    def device_added(self) -> None:
        return None

    @dbus.service.signal("razer.devices")
    def device_removed(self) -> None:
        return None


class DeviceObject(dbus.service.Object):
    def __init__(
        self,
        bus_name: dbus.service.BusName,
        object_path: str,
        runtime: ServiceRuntime,
        record: ControllerRecord,
    ) -> None:
        self._runtime = runtime
        self._record = record
        self._lock = RLock()
        super().__init__(bus_name, object_path)

    def update(self, record: ControllerRecord) -> None:
        with self._lock:
            self._record = record

    def snapshot(self) -> ControllerRecord:
        with self._lock:
            return self._record

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getSerial(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return self.snapshot().serial

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getDeviceName(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return self.snapshot().model_name

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getDeviceType(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return self.snapshot().device_kind.value

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getDriverVersion(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return ADAPTER_VERSION

    @dbus.service.method("razer.device.misc", out_signature="ai")
    def getVidPid(self) -> list[int]:  # noqa: N802 - exact upstream D-Bus vocabulary
        record = self.snapshot()
        return [record.vendor_id, record.product_id]

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getFirmware(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return "unavailable"

    @dbus.service.method("razer.device.misc", out_signature="s")
    def getDeviceImage(self) -> str:  # noqa: N802 - exact upstream D-Bus vocabulary
        return self.snapshot().image_url

    @dbus.service.method("razer.device.misc", out_signature="b")
    def hasMatrix(self) -> bool:  # noqa: N802 - exact upstream D-Bus vocabulary
        return True

    @dbus.service.method("razer.device.misc", out_signature="ai")
    def getMatrixDimensions(self) -> list[int]:  # noqa: N802 - exact upstream D-Bus vocabulary
        record = self.snapshot()
        return [record.rows, record.columns]

    @dbus.service.method("razer.device.lighting.brightness", out_signature="d")
    def getBrightness(self) -> float:  # noqa: N802 - exact upstream D-Bus vocabulary
        return self._call(self._runtime.brightness, self.snapshot().serial)

    @dbus.service.method("razer.device.lighting.brightness", in_signature="d")
    def setBrightness(self, brightness: float) -> None:  # noqa: N802
        self._call(
            self._runtime.apply_brightness,
            self.snapshot().serial,
            float(brightness),
        )

    @dbus.service.method("razer.device.lighting.chroma", in_signature="yyy")
    def setStatic(self, red: int, green: int, blue: int) -> None:  # noqa: N802
        self._call(
            self._runtime.apply_static,
            self.snapshot().serial,
            (int(red), int(green), int(blue)),
        )

    @dbus.service.method("razer.device.lighting.chroma")
    def setNone(self) -> None:  # noqa: N802 - exact upstream D-Bus vocabulary
        self._call(self._runtime.apply_off, self.snapshot().serial)

    @dbus.service.method(
        "razer.device.lighting.chroma",
        in_signature="ay",
        byte_arrays=True,
    )
    def setKeyRow(self, payload: object) -> None:  # noqa: N802
        try:
            encoded = bytes(payload)
        except (TypeError, ValueError) as error:
            raise _InvalidArgs("OpenRazer matrix data is not a byte array") from error
        self._call(self._runtime.stage_matrix, self.snapshot().serial, encoded)

    @dbus.service.method("razer.device.lighting.chroma")
    def setCustom(self) -> None:  # noqa: N802 - exact upstream D-Bus vocabulary
        self._call(self._runtime.commit_matrix, self.snapshot().serial)

    @staticmethod
    def _call(function: Callable[..., object], *arguments: object) -> object:
        try:
            return function(*arguments)
        except MatrixError as error:
            raise _InvalidArgs(str(error)) from error
        except CompatibilityRuntimeError as error:
            raise _OperationFailed(str(error)) from error


class ServiceController:
    def __init__(
        self,
        runtime: ServiceRuntime,
        identity: ServiceIdentity,
        reconcile_interval_ms: int,
        initial_records: tuple[ControllerRecord, ...],
        *,
        diagnostic: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self.identity = identity
        self._reconcile_interval_ms = reconcile_interval_ms
        self._diagnostic = diagnostic or emit_diagnostic
        self._closed = False
        self._bus = dbus.SessionBus()
        self._bus_name = dbus.service.BusName(
            identity.bus_name,
            bus=self._bus,
            allow_replacement=False,
            replace_existing=False,
            do_not_queue=True,
        )
        self._root = RootObject(self._bus_name, identity.root_path)
        self._devices: dict[str, DeviceObject] = {}
        self._apply(initial_records, initial=True)
        self._timer = GLib.timeout_add(reconcile_interval_ms, self.reconcile)

    @property
    def controller_count(self) -> int:
        return len(self._devices)

    def reconcile(self) -> bool:
        if self._closed:
            return False
        try:
            records = self._runtime.refresh()
            self._apply(records, initial=False)
        except BaseException as error:
            self._apply((), initial=False)
            self._diagnostic(
                {
                    "schema": "hyperflux-openrazer-diagnostic-v1",
                    "state": "bridge-unavailable",
                    "controller_count": 0,
                    "error_type": type(error).__name__,
                }
            )
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        for serial in tuple(self._devices):
            self._remove(serial)
        self._root.replace_serials(())
        self._root.remove_from_connection()
        self._runtime.close()

    def _apply(self, records: tuple[ControllerRecord, ...], *, initial: bool) -> None:
        desired = {record.serial: record for record in records}
        if len(desired) != len(records):
            raise RuntimeError("OpenRazer reconciliation received duplicate controller identity")
        removed = sorted(set(self._devices) - set(desired))
        added = sorted(set(desired) - set(self._devices))
        retained = sorted(set(desired) & set(self._devices))
        for serial in removed:
            self._remove(serial)
        for serial in retained:
            self._devices[serial].update(desired[serial])
        for serial in added:
            path = f"{self.identity.device_path_prefix}/{serial}"
            self._devices[serial] = DeviceObject(
                self._bus_name,
                path,
                self._runtime,
                desired[serial],
            )
        self._root.replace_serials(tuple(sorted(desired)))
        if not initial:
            for _ in removed:
                self._root.device_removed()
            for _ in added:
                self._root.device_added()
        if added or removed:
            self._diagnostic(
                {
                    "schema": "hyperflux-openrazer-diagnostic-v1",
                    "state": "topology-reconciled",
                    "controller_count": len(desired),
                    "added": len(added),
                    "removed": len(removed),
                }
            )

    def _remove(self, serial: str) -> None:
        device = self._devices.pop(serial, None)
        if device is not None:
            device.remove_from_connection()


def ready_record(controller: ServiceController) -> dict[str, object]:
    return {
        "schema": "hyperflux-openrazer-ready-v1",
        "adapter_version": ADAPTER_VERSION,
        "identity_mode": controller.identity.mode.value,
        "bus_name": controller.identity.bus_name,
        "root_path": controller.identity.root_path,
        "controller_count": controller.controller_count,
        "official_name_claimed": controller.identity.claims_official_name,
        "isolated_session_required": controller.identity.requires_isolated_session,
        "transport_access": "sdk-only",
        "hardware_write_executed": False,
    }


def emit_diagnostic(record: dict[str, object]) -> None:
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)


def run_loop(controller: ServiceController) -> int:
    loop = GLib.MainLoop()

    def stop(_signal: int, _frame: object) -> None:
        loop.quit()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        print(
            json.dumps(ready_record(controller), sort_keys=True, separators=(",", ":")),
            flush=True,
        )
        loop.run()
    finally:
        controller.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


def initialize_dbus_mainloop() -> None:
    DBusGMainLoop(set_as_default=True)


__all__ = [
    "DeviceObject",
    "RootObject",
    "ServiceController",
    "ServiceRuntime",
    "emit_diagnostic",
    "initialize_dbus_mainloop",
    "ready_record",
    "run_loop",
]
