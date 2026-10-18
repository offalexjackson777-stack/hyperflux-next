# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import uuid
from typing import Any

from .integrations import upstream_index
from .native_probe import (
    load_native_probe_catalog,
    transformed_native_probe_catalog,
)
from .model import ModelError, load_json, require_unique, sha256_file
from .openrazer import (
    effective_event_regex,
    load_imported_metadata,
    transformed_metadata,
)


TRANSFORM_SCHEMA = "hyperflux-openrazer-native-transform-v1"
DEPLOYMENT_SCHEMA = "hyperflux-openrazer-native-deployment-v2"
CATALOG_SCHEMA = "hyperflux-openrazer-native-route-catalog-v1"
TRANSFORM_KEYS = {"$schema", "schema", "upstream", "patch", "files"}
DEPLOYMENT_KEYS = {
    "$schema",
    "schema",
    "relay",
    "driver",
    "access",
    "key_events",
    "compatibility",
}
KEY_EVENTS_KEYS = {"udev_rules_path", "access", "links"}
KEY_EVENT_LINK_KEYS = {
    "profile_id",
    "receiver_profile_id",
    "receiver_usb_interface",
    "link_name",
}
# udev applies the uaccess tag in 73-seat-late.rules, so the key-event rules
# must sort before it for the logged-in seat user to receive access.
KEY_EVENT_UDEV_RULES_PATH = "/usr/lib/udev/rules.d/70-hyperflux-next-openrazer-keys.rules"
KEY_EVENT_LINK_PREFIX = "hyperflux-next-"
RELAY_KEYS = {
    "executable_path",
    "service_unit",
    "preflight_service_unit",
    "uhid_device",
    "automatic_restart",
}
DRIVER_KEYS = {"dkms_name", "dkms_version", "source_directory", "modules"}
ACCESS_KEYS = {
    "group",
    "udev_rules_source",
    "udev_rules_path",
    "mount_helper_source",
    "mount_helper_path",
    "sysusers_path",
}
COMPATIBILITY_KEYS = {
    "verified_upstream_version",
    "daemon_executable_path",
    "version_argument",
    "package_release_policy",
    "userspace_version_policy",
    "runtime_capability",
}
RUNTIME_CAPABILITY_KEYS = {"modinfo_field", "expected_value", "modules"}
UPSTREAM_KEYS = {"id", "repository", "commit"}
PATCH_KEYS = {"path", "sha256"}
FILE_KEYS = {"path", "source_sha256", "transformed_sha256"}
CATALOG_KEYS = {
    "$schema",
    "schema",
    "upstream_commit",
    "metadata_sha256",
    "transport",
    "routes",
}
CATALOG_TRANSPORT_KEYS = {
    "protocol_family",
    "report_bytes",
    "phys_prefix",
    "descriptor_hex",
    "descriptor_sha256",
}
CATALOG_ROUTE_KEYS = {
    "profile_id",
    "vendor_id",
    "wireless_product_id",
    "model_name",
    "device_kind",
    "driver_family",
    "source",
    "direct_data_product_ids",
    "event_file_regex",
}
CATALOG_SOURCE_KEYS = {"path", "class_name", "sha256"}
NATIVE_DESCRIPTOR_HEX = "0600ff0901a101150026ff007508955ab102c0"
NATIVE_PROTOCOL_FAMILY = "razer-feature-report-v1"
NATIVE_REPORT_BYTES = 90
NATIVE_PHYS_PREFIX = "hyperflux-next/native/v1/"
NATIVE_AUXILIARY_TRANSFORM_PATHS = {
    "install_files/udev/99-razer.rules",
    "install_files/udev/razer_mount",
}


@dataclass(frozen=True)
class PreparedOpenRazerNativeSource:
    root: Path
    result: Path
    source_commit: str
    patch_sha256: str


@dataclass(frozen=True)
class OpenRazerRelayDeployment:
    executable_path: str
    service_unit: str
    preflight_service_unit: str
    uhid_device: str
    automatic_restart: bool


@dataclass(frozen=True)
class OpenRazerDriverDeployment:
    dkms_name: str
    dkms_version: str
    source_directory: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class OpenRazerAccessDeployment:
    group: str
    udev_rules_source: str
    udev_rules_path: str
    mount_helper_source: str
    mount_helper_path: str
    sysusers_path: str


@dataclass(frozen=True)
class OpenRazerKeyEventLink:
    profile_id: str
    receiver_profile_id: str
    receiver_usb_interface: int
    link_name: str


@dataclass(frozen=True)
class OpenRazerKeyEventDeployment:
    udev_rules_path: str
    access: str
    links: tuple[OpenRazerKeyEventLink, ...]


@dataclass(frozen=True)
class OpenRazerRuntimeCapability:
    modinfo_field: str
    expected_value: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class OpenRazerCompatibilityDeployment:
    verified_upstream_version: str
    daemon_executable_path: str
    version_argument: str
    package_release_policy: str
    userspace_version_policy: str
    runtime_capability: OpenRazerRuntimeCapability


@dataclass(frozen=True)
class OpenRazerNativeDeployment:
    source_sha256: str
    upstream_repository: str
    upstream_commit: str
    relay: OpenRazerRelayDeployment
    driver: OpenRazerDriverDeployment
    access: OpenRazerAccessDeployment
    key_events: OpenRazerKeyEventDeployment
    compatibility: OpenRazerCompatibilityDeployment


def _json_payload(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()


def _native_transport() -> dict[str, Any]:
    descriptor = bytes.fromhex(NATIVE_DESCRIPTOR_HEX)
    return {
        "protocol_family": NATIVE_PROTOCOL_FAMILY,
        "report_bytes": NATIVE_REPORT_BYTES,
        "phys_prefix": NATIVE_PHYS_PREFIX,
        "descriptor_hex": NATIVE_DESCRIPTOR_HEX,
        "descriptor_sha256": hashlib.sha256(descriptor).hexdigest(),
    }


def _route_catalog_from_authorities(
    metadata: dict[str, Any],
    probes: dict[str, Any],
    source_root: Path,
) -> dict[str, Any]:
    if metadata["upstream"]["commit"] != probes["upstream"]["commit"]:
        raise ModelError("OpenRazer native route authorities use different commits")
    if probes["report_size"] != NATIVE_REPORT_BYTES:
        raise ModelError("OpenRazer native route report size differs from the transport")
    probes_by_profile = {
        device["profile_id"]: device for device in probes["devices"]
    }
    routes: list[dict[str, Any]] = []
    for device in metadata["devices"]:
        profile_id = device["profile_id"]
        probe = probes_by_profile.get(profile_id)
        if probe is None:
            raise ModelError(f"OpenRazer native route {profile_id} has no probe authority")
        identity = device["identity"]
        direct = probe["direct_usb_reference"]
        if (
            probe["wireless_identity"] != identity
            or direct["vendor_id"] != identity["vendor_id"]
            or direct["device_kind"] != identity["device_kind"]
            or direct["transport_variant"] != "wired"
            or direct["product_id"] == identity["product_id"]
        ):
            raise ModelError(f"OpenRazer native route {profile_id} has invalid direct precedence")
        source = device["source"]
        matcher = effective_event_regex(
            source_root / source["path"], source["class_name"]
        )
        try:
            re.compile(matcher)
        except re.error as error:
            raise ModelError(
                f"OpenRazer native route {profile_id} has an invalid event matcher: {error}"
            ) from error
        routes.append(
            {
                "profile_id": profile_id,
                "vendor_id": identity["vendor_id"],
                "wireless_product_id": identity["product_id"],
                "model_name": identity["model_name"],
                "device_kind": identity["device_kind"],
                "driver_family": (
                    "razermouse" if identity["device_kind"] == "mouse" else "razerkbd"
                ),
                "source": source,
                "direct_data_product_ids": [direct["product_id"]],
                "event_file_regex": matcher,
            }
        )
    routes.sort(key=lambda route: route["profile_id"])
    if set(probes_by_profile) != {route["profile_id"] for route in routes}:
        raise ModelError("OpenRazer native route and probe profile sets differ")
    return {
        "$schema": "../../../schemas/openrazer-native-route-catalog.schema.json",
        "schema": CATALOG_SCHEMA,
        "upstream_commit": metadata["upstream"]["commit"],
        "metadata_sha256": hashlib.sha256(_json_payload(metadata)).hexdigest(),
        "transport": _native_transport(),
        "routes": routes,
    }


def transformed_native_route_catalog(root: Path, source_root: Path) -> dict[str, Any]:
    """Build the relay catalog from one exact upstream checkout and repository data."""

    source_root = source_root.resolve()
    return _route_catalog_from_authorities(
        transformed_metadata(root, source_root),
        transformed_native_probe_catalog(root, source_root),
        source_root,
    )


def load_native_route_catalog(root: Path) -> dict[str, Any]:
    """Load and cross-check the committed source-bound relay catalog."""

    path = root / "integrations" / "openrazer" / "native-route" / "catalog.json"
    value = load_json(path)
    _exact_keys(value, CATALOG_KEYS, "OpenRazer native route catalog")
    if value["$schema"] != "../../../schemas/openrazer-native-route-catalog.schema.json":
        raise ModelError("OpenRazer native route catalog schema reference is not canonical")
    if value["schema"] != CATALOG_SCHEMA:
        raise ModelError("unsupported OpenRazer native route catalog schema")
    metadata = load_imported_metadata(root)
    probes = load_native_probe_catalog(root)
    if value["upstream_commit"] != metadata["upstream"]["commit"]:
        raise ModelError("OpenRazer native route catalog upstream binding drifted")
    if value["metadata_sha256"] != sha256_file(
        root / "integrations" / "openrazer" / "metadata.json"
    ):
        raise ModelError("OpenRazer native route catalog metadata binding drifted")
    transport = value["transport"]
    if not isinstance(transport, dict):
        raise ModelError("OpenRazer native route catalog transport must be an object")
    _exact_keys(transport, CATALOG_TRANSPORT_KEYS, "OpenRazer native route transport")
    if transport != _native_transport():
        raise ModelError("OpenRazer native route transport differs from its canonical profile")
    routes = value["routes"]
    if not isinstance(routes, list) or not 1 <= len(routes) <= 1024:
        raise ModelError("OpenRazer native route catalog is empty or oversized")
    metadata_by_profile = {
        device["profile_id"]: device for device in metadata["devices"]
    }
    probes_by_profile = {
        device["profile_id"]: device for device in probes["devices"]
    }
    profile_ids: list[str] = []
    for index, route in enumerate(routes):
        label = f"OpenRazer native route {index}"
        if not isinstance(route, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(route, CATALOG_ROUTE_KEYS, label)
        profile_id = route["profile_id"]
        if not isinstance(profile_id, str) or not profile_id:
            raise ModelError(f"{label}: invalid profile id")
        profile_ids.append(profile_id)
        device = metadata_by_profile.get(profile_id)
        probe = probes_by_profile.get(profile_id)
        if device is None or probe is None:
            raise ModelError(f"{label}: missing metadata or probe authority")
        identity = device["identity"]
        if route["source"] != device["source"] or any(
            route[field] != identity[field]
            for field in ("vendor_id", "model_name", "device_kind")
        ) or route["wireless_product_id"] != identity["product_id"]:
            raise ModelError(f"{label}: source or identity binding drifted")
        if not isinstance(route["source"], dict):
            raise ModelError(f"{label}: source must be an object")
        _exact_keys(route["source"], CATALOG_SOURCE_KEYS, f"{label} source")
        expected_family = "razermouse" if identity["device_kind"] == "mouse" else "razerkbd"
        if route["driver_family"] != expected_family:
            raise ModelError(f"{label}: driver family drifted")
        direct_product = probe["direct_usb_reference"]["product_id"]
        if route["direct_data_product_ids"] != [direct_product]:
            raise ModelError(f"{label}: direct precedence binding drifted")
        matcher = route["event_file_regex"]
        if not isinstance(matcher, str) or not matcher or len(matcher) > 256:
            raise ModelError(f"{label}: event matcher is invalid")
        try:
            re.compile(matcher)
        except re.error as error:
            raise ModelError(f"{label}: event matcher is invalid: {error}") from error
    require_unique(profile_ids, "OpenRazer native route profile id")
    if profile_ids != sorted(profile_ids) or set(profile_ids) != set(metadata_by_profile):
        raise ModelError("OpenRazer native route catalog profile coverage drifted")
    return value


def write_native_route_catalog(root: Path, source_root: Path) -> Path:
    destination = root / "integrations" / "openrazer" / "native-route" / "catalog.json"
    value = transformed_native_route_catalog(root, source_root)
    destination.write_bytes(_json_payload(value))
    return destination


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing:
        raise ModelError(f"{label}: missing keys: {', '.join(missing)}")
    if extras:
        raise ModelError(f"{label}: unsupported keys: {', '.join(extras)}")


def _safe_relative_path(value: Any, label: str, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ModelError(f"{label}: expected a bounded non-empty path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ModelError(f"{label}: path must be canonical and repository-relative")
    if suffix is not None and path.suffix != suffix:
        raise ModelError(f"{label}: expected a {suffix} file")
    return path


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ModelError(f"{label}: expected a lowercase SHA-256 digest")
    return value


def load_native_transform(root: Path) -> dict[str, Any]:
    transform = load_json(
        root / "integrations" / "openrazer" / "native-route" / "transform.json"
    )
    _exact_keys(transform, TRANSFORM_KEYS, "OpenRazer native transform")
    if transform["$schema"] != "../../../schemas/openrazer-native-transform.schema.json":
        raise ModelError("OpenRazer native transform schema reference is not canonical")
    if transform["schema"] != TRANSFORM_SCHEMA:
        raise ModelError("unsupported OpenRazer native transform schema")

    upstream = transform["upstream"]
    if not isinstance(upstream, dict):
        raise ModelError("OpenRazer native transform upstream must be an object")
    _exact_keys(upstream, UPSTREAM_KEYS, "OpenRazer native transform upstream")
    authority = upstream_index(root).get("openrazer")
    if authority is None or any(
        upstream[field] != authority[field] for field in ("id", "repository", "commit")
    ):
        raise ModelError("OpenRazer native transform differs from the integration pin")

    patch = transform["patch"]
    if not isinstance(patch, dict):
        raise ModelError("OpenRazer native transform patch must be an object")
    _exact_keys(patch, PATCH_KEYS, "OpenRazer native transform patch")
    patch_path = _safe_relative_path(patch["path"], "OpenRazer native patch", suffix=".patch")
    if not patch_path.parts or patch_path.parts[0] != "patches":
        raise ModelError("OpenRazer native patch must remain below patches/")
    _digest(patch["sha256"], "OpenRazer native patch digest")

    files = transform["files"]
    if not isinstance(files, list) or not files or len(files) > 32:
        raise ModelError("OpenRazer native transform files must be a bounded non-empty list")
    paths: list[str] = []
    for index, record in enumerate(files):
        if not isinstance(record, dict):
            raise ModelError(f"OpenRazer native transform file {index}: expected an object")
        _exact_keys(record, FILE_KEYS, f"OpenRazer native transform file {index}")
        path = _safe_relative_path(record["path"], f"OpenRazer native transform file {index}")
        if (
            not path.parts
            or (
                path.parts[0] != "driver"
                and path.as_posix() not in NATIVE_AUXILIARY_TRANSFORM_PATHS
            )
        ):
            raise ModelError(
                f"OpenRazer native transform file {index}: path is outside the approved source set"
            )
        _digest(record["source_sha256"], f"OpenRazer native transform file {index} source")
        _digest(
            record["transformed_sha256"],
            f"OpenRazer native transform file {index} transformed",
        )
        paths.append(path.as_posix())
    require_unique(paths, "OpenRazer native transform path")
    if paths != sorted(paths):
        raise ModelError("OpenRazer native transform files must be sorted by path")
    return transform


def load_native_deployment(root: Path) -> OpenRazerNativeDeployment:
    path = root / "integrations" / "openrazer" / "native-route" / "deployment.json"
    value = load_json(path)
    _exact_keys(value, DEPLOYMENT_KEYS, "OpenRazer native deployment")
    if value["$schema"] != "../../../schemas/openrazer-native-deployment.schema.json":
        raise ModelError("OpenRazer native deployment schema reference is not canonical")
    if value["schema"] != DEPLOYMENT_SCHEMA:
        raise ModelError("unsupported OpenRazer native deployment schema")

    relay = value["relay"]
    if not isinstance(relay, dict):
        raise ModelError("OpenRazer native relay deployment must be an object")
    _exact_keys(relay, RELAY_KEYS, "OpenRazer native relay deployment")
    executable = _deployment_path(
        relay["executable_path"],
        "OpenRazer native relay executable",
        ("/", "usr", "lib", "hyperflux-next"),
    )
    service = relay["service_unit"]
    if not isinstance(service, str) or re.fullmatch(
        r"[a-z][a-z0-9@_.-]{0,95}\.service", service
    ) is None:
        raise ModelError("OpenRazer native relay service unit is invalid")
    if relay["uhid_device"] != "/dev/uhid":
        raise ModelError("OpenRazer native relay must use the kernel UHID device")
    preflight_service = relay["preflight_service_unit"]
    if preflight_service != "hyperflux-next-openrazer-driver-preflight.service":
        raise ModelError("OpenRazer native driver preflight service unit is invalid")
    if preflight_service == service:
        raise ModelError("OpenRazer native driver preflight must be a separate service")
    automatic_restart = relay["automatic_restart"]
    if automatic_restart is not False:
        raise ModelError("OpenRazer native relay automatic restart must remain disabled")

    driver = value["driver"]
    if not isinstance(driver, dict):
        raise ModelError("OpenRazer native driver deployment must be an object")
    _exact_keys(driver, DRIVER_KEYS, "OpenRazer native driver deployment")
    if driver["dkms_name"] != "openrazer-driver":
        raise ModelError("OpenRazer native DKMS name must preserve the upstream identity")
    version = driver["dkms_version"]
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ModelError("OpenRazer native DKMS version is invalid")
    upstream = upstream_index(root).get("openrazer")
    if upstream is None or not str(upstream["version"]).startswith(version):
        raise ModelError("OpenRazer native DKMS version differs from the pinned upstream")
    transform = load_native_transform(root)
    transform_upstream = transform["upstream"]
    if (
        transform_upstream["id"] != "openrazer"
        or transform_upstream["commit"] != upstream["commit"]
    ):
        raise ModelError("OpenRazer native deployment differs from its source transform")
    source_directory = _deployment_path(
        driver["source_directory"],
        "OpenRazer native DKMS source directory",
        ("/", "usr", "src"),
    )
    if source_directory != f"/usr/src/openrazer-driver-{version}":
        raise ModelError("OpenRazer native DKMS source directory does not derive from its identity")
    modules = driver["modules"]
    if (
        not isinstance(modules, list)
        or not 1 <= len(modules) <= 16
        or modules != sorted(modules)
        or len(set(modules)) != len(modules)
        or any(
            not isinstance(module, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", module) is None
            for module in modules
        )
    ):
        raise ModelError("OpenRazer native DKMS module inventory is invalid")

    access = value["access"]
    if not isinstance(access, dict):
        raise ModelError("OpenRazer native access deployment must be an object")
    _exact_keys(access, ACCESS_KEYS, "OpenRazer native access deployment")
    if access["group"] != "openrazer":
        raise ModelError("OpenRazer native access group must match the distribution contract")
    expected_access = {
        "udev_rules_source": "install_files/udev/99-razer.rules",
        "udev_rules_path": "/usr/lib/udev/rules.d/99-razer.rules",
        "mount_helper_source": "install_files/udev/razer_mount",
        "mount_helper_path": "/usr/lib/udev/razer_mount",
        "sysusers_path": "/usr/lib/sysusers.d/openrazer.conf",
    }
    for key, expected in expected_access.items():
        if access[key] != expected:
            raise ModelError(f"OpenRazer native access {key} differs from its deployment contract")

    key_events = _key_event_deployment(root, value["key_events"])

    compatibility = value["compatibility"]
    if not isinstance(compatibility, dict):
        raise ModelError("OpenRazer native compatibility deployment must be an object")
    _exact_keys(
        compatibility,
        COMPATIBILITY_KEYS,
        "OpenRazer native compatibility deployment",
    )
    verified_version = compatibility["verified_upstream_version"]
    if verified_version != upstream["version"] or verified_version != version:
        raise ModelError(
            "OpenRazer native compatibility version differs from its pinned source"
        )
    daemon_executable = _deployment_path(
        compatibility["daemon_executable_path"],
        "OpenRazer daemon executable",
        ("/", "usr", "bin"),
    )
    if daemon_executable != "/usr/bin/openrazer-daemon":
        raise ModelError("OpenRazer daemon executable differs from its runtime contract")
    if compatibility["version_argument"] != "--version":
        raise ModelError("OpenRazer version argument differs from its runtime contract")
    if compatibility["package_release_policy"] != "ignored":
        raise ModelError("OpenRazer package release policy must ignore packaging revisions")
    if compatibility["userspace_version_policy"] != "diagnostic-only":
        raise ModelError("OpenRazer userspace version policy must be diagnostic only")

    capability = compatibility["runtime_capability"]
    if not isinstance(capability, dict):
        raise ModelError("OpenRazer native runtime capability must be an object")
    _exact_keys(
        capability,
        RUNTIME_CAPABILITY_KEYS,
        "OpenRazer native runtime capability",
    )
    field = capability["modinfo_field"]
    expected_value = capability["expected_value"]
    capability_modules = capability["modules"]
    if not isinstance(field, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field) is None:
        raise ModelError("OpenRazer native capability field is invalid")
    if (
        not isinstance(expected_value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", expected_value) is None
    ):
        raise ModelError("OpenRazer native capability value is invalid")
    if (
        not isinstance(capability_modules, list)
        or not capability_modules
        or capability_modules != sorted(capability_modules)
        or len(set(capability_modules)) != len(capability_modules)
        or not set(capability_modules).issubset(modules)
    ):
        raise ModelError("OpenRazer native capability modules are invalid")
    return OpenRazerNativeDeployment(
        source_sha256=sha256_file(path),
        upstream_repository=transform_upstream["repository"],
        upstream_commit=transform_upstream["commit"],
        relay=OpenRazerRelayDeployment(
            executable_path=executable,
            service_unit=service,
            preflight_service_unit=preflight_service,
            uhid_device=relay["uhid_device"],
            automatic_restart=automatic_restart,
        ),
        driver=OpenRazerDriverDeployment(
            dkms_name=driver["dkms_name"],
            dkms_version=version,
            source_directory=source_directory,
            modules=tuple(modules),
        ),
        access=OpenRazerAccessDeployment(
            group=access["group"],
            udev_rules_source=access["udev_rules_source"],
            udev_rules_path=access["udev_rules_path"],
            mount_helper_source=access["mount_helper_source"],
            mount_helper_path=access["mount_helper_path"],
            sysusers_path=access["sysusers_path"],
        ),
        key_events=key_events,
        compatibility=OpenRazerCompatibilityDeployment(
            verified_upstream_version=verified_version,
            daemon_executable_path=daemon_executable,
            version_argument=compatibility["version_argument"],
            package_release_policy=compatibility["package_release_policy"],
            userspace_version_policy=compatibility["userspace_version_policy"],
            runtime_capability=OpenRazerRuntimeCapability(
                modinfo_field=field,
                expected_value=expected_value,
                modules=tuple(capability_modules),
            ),
        ),
    )


def _key_event_deployment(root: Path, value: Any) -> OpenRazerKeyEventDeployment:
    """Validate the by-id names that give OpenRazer a keyboard's key presses.

    OpenRazer opens a device's key-event file once, when it adds the device, by
    matching every /dev/input/by-id name against the device's EVENT_FILE_REGEX
    with Python re.match. Each link name must therefore match the pattern of
    its own keyboard and of no other presented device.
    """

    if not isinstance(value, dict):
        raise ModelError("OpenRazer native key-event deployment must be an object")
    _exact_keys(value, KEY_EVENTS_KEYS, "OpenRazer native key-event deployment")
    if value["udev_rules_path"] != KEY_EVENT_UDEV_RULES_PATH:
        raise ModelError("OpenRazer native key-event udev rules path differs from its contract")
    if value["access"] != "active-seat-user":
        raise ModelError("OpenRazer native key-event access must stay with the seat user")
    links = value["links"]
    if not isinstance(links, list) or not 1 <= len(links) <= 16:
        raise ModelError("OpenRazer native key-event links are empty or oversized")
    matchers = {
        route["profile_id"]: route["event_file_regex"]
        for route in load_native_route_catalog(root)["routes"]
    }
    parsed: list[OpenRazerKeyEventLink] = []
    for index, link in enumerate(links):
        label = f"OpenRazer native key-event link {index}"
        if not isinstance(link, dict):
            raise ModelError(f"{label}: expected an object")
        _exact_keys(link, KEY_EVENT_LINK_KEYS, label)
        profile_id = link["profile_id"]
        if profile_id not in matchers:
            raise ModelError(f"{label}: profile has no native route")
        receiver_profile_id = link["receiver_profile_id"]
        if (
            not isinstance(receiver_profile_id, str)
            or re.fullmatch(r"receiver\.[a-z0-9][a-z0-9.-]{0,127}", receiver_profile_id)
            is None
        ):
            raise ModelError(f"{label}: receiver profile id is invalid")
        interface = link["receiver_usb_interface"]
        if (
            not isinstance(interface, int)
            or isinstance(interface, bool)
            or not 0 <= interface <= 31
        ):
            raise ModelError(f"{label}: receiver USB interface is invalid")
        name = link["link_name"]
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", name) is None
            or not name.startswith(KEY_EVENT_LINK_PREFIX)
        ):
            raise ModelError(f"{label}: link name is invalid")
        if re.match(matchers[profile_id], name) is None:
            raise ModelError(f"{label}: link name does not match the OpenRazer event pattern")
        if any(
            other != profile_id and re.match(matcher, name) is not None
            for other, matcher in matchers.items()
        ):
            raise ModelError(f"{label}: link name also matches another OpenRazer device")
        parsed.append(
            OpenRazerKeyEventLink(
                profile_id=profile_id,
                receiver_profile_id=receiver_profile_id,
                receiver_usb_interface=interface,
                link_name=name,
            )
        )
    require_unique([link.link_name for link in parsed], "OpenRazer key-event link name")
    require_unique([link.profile_id for link in parsed], "OpenRazer key-event link profile")
    return OpenRazerKeyEventDeployment(
        udev_rules_path=value["udev_rules_path"],
        access=value["access"],
        links=tuple(parsed),
    )


def _deployment_path(value: Any, label: str, prefix: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value != value.strip():
        raise ModelError(f"{label}: must be an absolute path")
    path = PurePosixPath(value)
    if path.parts[: len(prefix)] != prefix or ".." in path.parts or str(path) != value:
        raise ModelError(f"{label}: path is outside its deployment boundary")
    if len(value.encode()) > 240 or any(
        ord(character) < 32 or ord(character) > 126 for character in value
    ):
        raise ModelError(f"{label}: path is not bounded printable ASCII")
    return value


def _git(arguments: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ModelError(f"cannot inspect OpenRazer source: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ModelError(f"git {' '.join(arguments)} failed{suffix}")
    return result.stdout.strip()


def _validate_source(root: Path, source: Path, transform: dict[str, Any]) -> None:
    if source.is_symlink() or not source.is_dir() or (source / ".git").is_symlink():
        raise ModelError("OpenRazer native source is missing or uses a symbolic link")
    if _git(["rev-parse", "--is-inside-work-tree"], cwd=source) != "true":
        raise ModelError("OpenRazer native source is not a Git worktree")
    if _git(["rev-parse", "HEAD"], cwd=source) != transform["upstream"]["commit"]:
        raise ModelError("OpenRazer native source does not match the pinned commit")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=source):
        raise ModelError("OpenRazer native source has local modifications")
    if _git(["remote", "get-url", "origin"], cwd=source) != transform["upstream"]["repository"]:
        raise ModelError("OpenRazer native source has an unexpected origin")

    native_root = root / "integrations" / "openrazer" / "native-route"
    patch = native_root / transform["patch"]["path"]
    if patch.is_symlink() or not patch.is_file():
        raise ModelError("OpenRazer native patch is missing or symbolic")
    if sha256_file(patch) != transform["patch"]["sha256"]:
        raise ModelError("OpenRazer native patch differs from its source lock")

    for record in transform["files"]:
        path = source / record["path"]
        if path.is_symlink() or not path.is_file():
            raise ModelError(f"OpenRazer native source file is missing or symbolic: {record['path']}")
        if sha256_file(path) != record["source_sha256"]:
            raise ModelError(f"OpenRazer native source drift: {record['path']}")


def _apply_patch(destination: Path, patch: Path) -> None:
    for check_only in (True, False):
        arguments = [
            "patch",
            "--batch",
            "--forward",
            "--fuzz=0",
            "--no-backup-if-mismatch",
            "-p1",
            "-i",
            str(patch),
        ]
        if check_only:
            arguments.insert(1, "--dry-run")
        try:
            result = subprocess.run(
                arguments,
                cwd=destination,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot apply OpenRazer native patch: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise ModelError(f"OpenRazer native patch did not apply exactly{suffix}")


def prepare_openrazer_native_source(
    root: Path, source: Path, output: Path
) -> PreparedOpenRazerNativeSource:
    root = root.resolve()
    source = source.expanduser().resolve()
    output = output.expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise ModelError("OpenRazer native output must not already exist")
    if output == source or source in output.parents or output in source.parents:
        raise ModelError("OpenRazer native output must be independent from the source checkout")

    transform = load_native_transform(root)
    _validate_source(root, source, transform)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.parent / f".{output.name}.prepare-{uuid.uuid4().hex}"
    native_root = root / "integrations" / "openrazer" / "native-route"
    patch = native_root / transform["patch"]["path"]
    try:
        shutil.copytree(
            source,
            temporary,
            symlinks=False,
            ignore=shutil.ignore_patterns(".git", "*.o", "*.ko", "*.mod", "*.mod.c"),
        )
        _apply_patch(temporary, patch)
        for record in transform["files"]:
            path = temporary / record["path"]
            if sha256_file(path) != record["transformed_sha256"]:
                raise ModelError(f"OpenRazer native transformed output drift: {record['path']}")

        result_path = temporary / "hyperflux-native-transform-result.json"
        result = {
            "schema": "hyperflux-openrazer-native-transform-result-v1",
            "source_commit": transform["upstream"]["commit"],
            "patch_sha256": transform["patch"]["sha256"],
            "transformed_files": [
                {
                    "path": record["path"],
                    "sha256": record["transformed_sha256"],
                }
                for record in transform["files"]
            ],
            "network_access_executed": False,
            "hardware_queried": False,
            "hardware_write_executed": False,
        }
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result_path.chmod(0o600)
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PreparedOpenRazerNativeSource(
        root=output,
        result=output / "hyperflux-native-transform-result.json",
        source_commit=transform["upstream"]["commit"],
        patch_sha256=transform["patch"]["sha256"],
    )
