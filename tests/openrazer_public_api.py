# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OPENRAZER_USERSPACE_CAPABILITIES = json.loads(
    (ROOT / "integrations" / "openrazer" / "userspace-capability-catalog.json").read_text(
        encoding="utf-8"
    )
)


INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"
ACCESS_ERRORS = {
    "org.freedesktop.DBus.Error.AccessDenied",
    "org.freedesktop.DBus.Error.AuthFailed",
}
ABSENT_ERRORS = {
    "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.ServiceUnknown",
}
TEMPORARY_ERRORS = {
    "org.freedesktop.DBus.Error.Disconnected",
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.NoServer",
    "org.freedesktop.DBus.Error.Timeout",
    "org.freedesktop.DBus.Error.UnknownObject",
}


class CapabilityBus(Protocol):
    def owner(self, bus_name: str) -> str | None: ...

    def introspect(self, bus_name: str, object_path: str) -> str: ...

    def call(
        self, bus_name: str, object_path: str, interface: str, member: str
    ) -> Any: ...


@dataclass(frozen=True)
class MethodSignature:
    in_signature: str
    out_signature: str


@dataclass(frozen=True)
class IntrospectionSurface:
    methods: dict[tuple[str, str], MethodSignature]
    signals: dict[tuple[str, str], str]


def _error_name(error: BaseException) -> str:
    method = getattr(error, "get_dbus_name", None)
    if callable(method):
        try:
            value = method()
        except Exception:  # pragma: no cover - defensive boundary around dbus-python
            value = None
        if isinstance(value, str):
            return value
    value = getattr(error, "dbus_name", None)
    return value if isinstance(value, str) else ""


def classify_failure(error: BaseException) -> str:
    name = _error_name(error)
    if name in ACCESS_ERRORS or isinstance(error, PermissionError):
        return "permission-failure"
    if name in ABSENT_ERRORS:
        return "daemon-unavailable"
    if name in TEMPORARY_ERRORS or isinstance(error, (ConnectionError, TimeoutError)):
        return "temporary-transport-failure"
    if name == "org.freedesktop.DBus.Error.UnknownMethod":
        return "missing-required-capability"
    return "temporary-transport-failure"


def parse_introspection(xml: str) -> IntrospectionSurface:
    if not isinstance(xml, str) or len(xml) > 1_048_576:
        raise ValueError("OpenRazer introspection document exceeds its bound")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError("OpenRazer introspection document is invalid") from error
    methods: dict[tuple[str, str], MethodSignature] = {}
    signals: dict[tuple[str, str], str] = {}
    for interface in root.findall("interface"):
        interface_name = interface.get("name", "")
        if not interface_name or len(interface_name) > 255:
            continue
        for method in interface.findall("method"):
            member = method.get("name", "")
            if not member:
                continue
            inputs: list[str] = []
            outputs: list[str] = []
            for argument in method.findall("arg"):
                signature = argument.get("type", "")
                if argument.get("direction", "in") == "out":
                    outputs.append(signature)
                else:
                    inputs.append(signature)
            methods[(interface_name, member)] = MethodSignature(
                "".join(inputs), "".join(outputs)
            )
        for signal in interface.findall("signal"):
            member = signal.get("name", "")
            if member:
                signals[(interface_name, member)] = "".join(
                    argument.get("type", "") for argument in signal.findall("arg")
                )
    return IntrospectionSurface(methods, signals)


def _method_problem(
    surface: IntrospectionSurface, method: dict[str, str]
) -> str | None:
    actual = surface.methods.get((method["interface"], method["member"]))
    if actual is None:
        return "missing"
    if (
        actual.in_signature != method["in_signature"]
        or actual.out_signature != method["out_signature"]
    ):
        return "signature-mismatch"
    return None


def _signal_problem(
    surface: IntrospectionSurface, signal: dict[str, str]
) -> str | None:
    actual = surface.signals.get((signal["interface"], signal["member"]))
    if actual is None:
        return "missing"
    if actual != signal["signature"]:
        return "signature-mismatch"
    return None


def _surface_problems(
    surface: IntrospectionSurface, methods: list[dict[str, str]]
) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for method in methods:
        problem = _method_problem(surface, method)
        if problem is not None:
            problems.append(
                {
                    "id": method["id"],
                    "interface": method["interface"],
                    "member": method["member"],
                    "problem": problem,
                }
            )
    return problems


def _bounded_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512


def _semantic_probe_passed(method_id: str, value: Any, identity: dict[str, Any]) -> bool:
    if method_id == "get_vid_pid":
        try:
            values = [int(item) for item in value]
        except (TypeError, ValueError):
            return False
        return values == [identity["vendor_id"], identity["product_id"]]
    if method_id in {"get_device_name", "get_firmware", "get_serial"}:
        return value is not None and _bounded_text(str(value))
    return value is not None


def _device_result(
    catalog: dict[str, Any],
    bus: CapabilityBus,
    object_path: str,
    surface: IntrospectionSurface,
    identity: tuple[int, int],
) -> dict[str, Any]:
    device = next(
        (
            value
            for value in catalog["devices"]
            if (value["identity"]["vendor_id"], value["identity"]["product_id"])
            == identity
        ),
        None,
    )
    if device is None:
        return {
            "profile_id": None,
            "device_kind": None,
            "vendor_id": identity[0],
            "product_id": identity[1],
            "state": "unsupported-device",
            "required_problems": [],
            "features": [],
            "semantic_probes": [],
        }
    required_problems = _surface_problems(surface, device["required"])
    feature_results: list[dict[str, Any]] = []
    for feature in device["features"]:
        problems = _surface_problems(surface, feature["methods"])
        feature_results.append(
            {
                "id": feature["id"],
                "requirement": feature["requirement"],
                "state": "available" if not problems else "unavailable",
                "problems": problems,
            }
        )
        if feature["requirement"] == "required":
            required_problems.extend(problems)

    probe_results: list[dict[str, str]] = []
    if not required_problems:
        for method in device["semantic_probes"]:
            try:
                value = bus.call(
                    catalog["service"]["bus_name"],
                    object_path,
                    method["interface"],
                    method["member"],
                )
                state = (
                    "passed"
                    if _semantic_probe_passed(method["id"], value, device["identity"])
                    else "semantic-mismatch"
                )
            except Exception as error:  # dbus-python exceptions share no narrower base
                state = classify_failure(error)
            probe_results.append({"id": method["id"], "state": state})

    failed_probes = [value for value in probe_results if value["state"] != "passed"]
    required_signature_mismatch = any(
        value["problem"] == "signature-mismatch" for value in required_problems
    )
    if required_signature_mismatch:
        state = "signature-mismatch"
    elif required_problems:
        state = "missing-required-capability"
    elif failed_probes:
        states = {value["state"] for value in failed_probes}
        state = (
            "permission-failure"
            if "permission-failure" in states
            else "temporary-transport-failure"
            if "temporary-transport-failure" in states
            else "semantic-probe-failed"
        )
    elif any(value["state"] == "unavailable" for value in feature_results):
        state = "limited"
    else:
        state = "compatible"
    return {
        "profile_id": device["profile_id"],
        "device_kind": device["identity"]["device_kind"],
        "vendor_id": identity[0],
        "product_id": identity[1],
        "state": state,
        "required_problems": required_problems,
        "features": feature_results,
        "semantic_probes": probe_results,
    }


def probe_capabilities(
    bus: CapabilityBus,
    catalog: dict[str, Any] = OPENRAZER_USERSPACE_CAPABILITIES,
) -> dict[str, Any]:
    service = catalog["service"]
    owner = bus.owner(service["bus_name"])
    base = {
        "schema": "hyperflux-openrazer-capability-result-v1",
        "policy_upstream_commit": catalog["upstream"]["commit"],
        "observed_version": None,
        "version_probe_state": "not-attempted",
        "owner": owner,
        "state": "daemon-unavailable",
        "root_problems": [],
        "devices": [],
    }
    if owner is None:
        return base
    try:
        root_surface = parse_introspection(
            bus.introspect(service["bus_name"], service["root_path"])
        )
    except Exception as error:
        base["state"] = classify_failure(error)
        return base
    root_problems = _surface_problems(root_surface, catalog["root"]["methods"])
    for signal in catalog["root"]["signals"]:
        problem = _signal_problem(root_surface, signal)
        if problem is not None:
            root_problems.append(
                {
                    "id": signal["id"],
                    "interface": signal["interface"],
                    "member": signal["member"],
                    "problem": problem,
                }
            )
    base["root_problems"] = root_problems
    if root_problems:
        base["state"] = (
            "signature-mismatch"
            if any(value["problem"] == "signature-mismatch" for value in root_problems)
            else "missing-required-capability"
        )
        return base
    root_methods = {method["id"]: method for method in catalog["root"]["methods"]}
    diagnostics = {
        method["id"]: method for method in catalog["root"]["diagnostic_methods"]
    }
    version_method = diagnostics.get("version")
    if version_method is not None:
        problem = _method_problem(root_surface, version_method)
        if problem is not None:
            base["version_probe_state"] = problem
        else:
            try:
                version = bus.call(
                    service["bus_name"],
                    service["root_path"],
                    version_method["interface"],
                    version_method["member"],
                )
                if version is not None:
                    base["observed_version"] = str(version)[:128]
                    base["version_probe_state"] = "reported"
                else:
                    base["version_probe_state"] = "unavailable"
            except Exception as error:
                base["version_probe_state"] = classify_failure(error)
    try:
        enumerate_method = root_methods["get_serial_list"]
        serials = list(
            bus.call(
                service["bus_name"],
                service["root_path"],
                enumerate_method["interface"],
                enumerate_method["member"],
            )
        )
    except Exception as error:
        base["state"] = classify_failure(error)
        return base
    if len(serials) > 256:
        base["state"] = "temporary-transport-failure"
        return base
    devices: list[dict[str, Any]] = []
    for serial in serials:
        serial_text = str(serial)
        if not serial_text or len(serial_text) > 128 or "/" in serial_text:
            devices.append(
                {
                    "profile_id": None,
                    "device_kind": None,
                    "vendor_id": 0,
                    "product_id": 0,
                    "state": "unsupported-device",
                    "required_problems": [],
                    "features": [],
                    "semantic_probes": [],
                }
            )
            continue
        path = service["device_path_prefix"] + serial_text
        try:
            surface = parse_introspection(bus.introspect(service["bus_name"], path))
            vid_pid = bus.call(
                service["bus_name"], path, "razer.device.misc", "getVidPid"
            )
            identity_values = tuple(int(value) for value in vid_pid)
            if len(identity_values) != 2:
                raise ValueError("invalid OpenRazer VID/PID result")
            result = _device_result(catalog, bus, path, surface, identity_values)
        except Exception as error:
            result = {
                "profile_id": None,
                "device_kind": None,
                "vendor_id": 0,
                "product_id": 0,
                "state": classify_failure(error),
                "required_problems": [],
                "features": [],
                "semantic_probes": [],
            }
        devices.append(result)
    devices.sort(
        key=lambda value: (
            value["profile_id"] is None,
            value["profile_id"] or "",
            value["vendor_id"],
            value["product_id"],
        )
    )
    base["devices"] = devices
    recognized = [value for value in devices if value["profile_id"] is not None]
    unclassified_failures = [
        value
        for value in devices
        if value["profile_id"] is None
        and value["state"]
        in {
            "daemon-unavailable",
            "permission-failure",
            "temporary-transport-failure",
        }
    ]
    failures = [
        value
        for value in recognized
        if value["state"] not in {"compatible", "limited"}
    ]
    usable = [
        value for value in recognized if value["state"] in {"compatible", "limited"}
    ]
    if failures and usable:
        base["state"] = "limited"
    elif failures:
        base["state"] = "incompatible"
    elif recognized and unclassified_failures:
        base["state"] = "limited"
    elif recognized and any(value["state"] == "limited" for value in recognized):
        base["state"] = "limited"
    elif recognized:
        base["state"] = "compatible"
    elif unclassified_failures:
        states = {value["state"] for value in unclassified_failures}
        base["state"] = (
            "permission-failure"
            if "permission-failure" in states
            else "daemon-unavailable"
            if "daemon-unavailable" in states
            else "temporary-transport-failure"
        )
    else:
        base["state"] = "no-supported-devices"
    return base


class DBusPythonBus:
    def __init__(self) -> None:
        import dbus

        self._dbus = dbus
        self._bus = dbus.SessionBus()

    def owner(self, bus_name: str) -> str | None:
        try:
            return str(self._bus.get_name_owner(bus_name))
        except Exception as error:
            if _error_name(error) in ABSENT_ERRORS:
                return None
            raise

    def _object(self, bus_name: str, object_path: str) -> Any:
        return self._bus.get_object(bus_name, object_path, introspect=False)

    def introspect(self, bus_name: str, object_path: str) -> str:
        interface = self._dbus.Interface(
            self._object(bus_name, object_path), INTROSPECTABLE
        )
        return str(interface.Introspect())

    def call(
        self, bus_name: str, object_path: str, interface: str, member: str
    ) -> Any:
        proxy = self._dbus.Interface(self._object(bus_name, object_path), interface)
        return getattr(proxy, member)()


__all__ = [
    "CapabilityBus",
    "DBusPythonBus",
    "IntrospectionSurface",
    "MethodSignature",
    "classify_failure",
    "parse_introspection",
    "probe_capabilities",
]
