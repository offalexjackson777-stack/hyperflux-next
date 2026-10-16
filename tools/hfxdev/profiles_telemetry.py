# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from typing import Any

from .model import ModelError
from .profiles_common import (
    NATIVE_BYTE_RANGE_KEYS,
    NATIVE_BYTE_VALUE_KEYS,
    NATIVE_CHECKSUM_KEYS,
    NATIVE_DATA_SIZE_KEYS,
    NATIVE_EXCHANGE_KEYS,
    NATIVE_EXCHANGE_REQUIRED_KEYS,
    NATIVE_MATCH_RANGE_KEYS,
    NATIVE_REPORT_FORMAT_KEYS,
    NATIVE_ROUTE_SELECTOR_KEYS,
    NATIVE_TRANSACTION_ID_KEYS,
    OPTIONAL_PASSIVE_KEYS,
    PASSIVE_KEYS,
    PASSIVE_TELEMETRY_CAPABILITIES,
    RECEIVER_TRANSPORT_KEYS,
    _expect_keys,
)
from .profiles_lifecycle import _validate_lifecycle_policy


def _validate_lighting(profile: dict[str, Any]) -> None:
    write_capabilities = [capability for capability in profile["capabilities"] if capability["id"].startswith("lighting.")]
    if profile["kind"] != "child":
        return
    lighting = profile.get("transport", {}).get("lighting")
    if write_capabilities and not isinstance(lighting, dict):
        raise ModelError(f"{profile['profile_id']}: writable lighting requires a transport map")
    if not isinstance(lighting, dict):
        return
    required = {"physical_led_count", "application_slot_count", "carrier_count", "rows", "columns", "application_index_to_carrier"}
    missing = sorted(required - set(lighting))
    if missing:
        raise ModelError(f"{profile['profile_id']}: incomplete lighting map: {', '.join(missing)}")
    counts = {key: lighting[key] for key in required - {"application_index_to_carrier"}}
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in counts.values()):
        raise ModelError(f"{profile['profile_id']}: lighting dimensions must be positive integers")
    if lighting["rows"] * lighting["columns"] != lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting rows and columns do not cover application slots")
    if lighting["physical_led_count"] > lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: physical LED count exceeds application slots")
    mapping = lighting["application_index_to_carrier"]
    if not isinstance(mapping, list) or len(mapping) != lighting["application_slot_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting map length mismatch")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in mapping):
        raise ModelError(f"{profile['profile_id']}: lighting carriers must be non-negative integers")
    if len(set(mapping)) != len(mapping):
        raise ModelError(f"{profile['profile_id']}: lighting map repeats a receiver carrier")
    if max(mapping) >= lighting["carrier_count"]:
        raise ModelError(f"{profile['profile_id']}: lighting map exceeds carrier count")


def _raw_values(value: object, label: str) -> list[int]:
    if not isinstance(value, list):
        raise ModelError(f"{label} must be a list")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 0xFFFF_FFFF
        for item in value
    ):
        raise ModelError(f"{label} contains an invalid raw value")
    if value != sorted(set(value)):
        raise ModelError(f"{label} must be sorted and unique")
    return value


def _validate_native_exchange(profile_id: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ModelError(f"{profile_id}: native exchange must be an object")
    _expect_keys(value, NATIVE_EXCHANGE_KEYS, f"{profile_id} native exchange")
    missing = sorted(NATIVE_EXCHANGE_REQUIRED_KEYS - set(value))
    if missing:
        raise ModelError(f"{profile_id}: incomplete native exchange: {', '.join(missing)}")
    if value["protocol_family"] != "razer-feature-report-v1":
        raise ModelError(f"{profile_id}: unsupported native exchange protocol")
    if value["report_bytes"] != 90:
        raise ModelError(f"{profile_id}: native exchange report must be exactly 90 bytes")
    integer_bounds = {
        "set_report_index": (0, 255),
        "response_report_index": (0, 255),
        "initial_response_delay_us": (0, 100_000),
        "response_retry_delay_us": (1, 100_000),
        "max_response_attempts": (1, 16),
    }
    for key, (minimum, maximum) in integer_bounds.items():
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ModelError(f"{profile_id}: native exchange {key} is out of bounds")
    report_format = value["report_format"]
    if not isinstance(report_format, dict):
        raise ModelError(f"{profile_id}: native report format must be an object")
    _expect_keys(report_format, NATIVE_REPORT_FORMAT_KEYS, f"{profile_id} native report format")
    if set(report_format) != NATIVE_REPORT_FORMAT_KEYS:
        raise ModelError(f"{profile_id}: native report format is incomplete")

    def byte_value(name: str) -> None:
        item = report_format[name]
        if not isinstance(item, dict):
            raise ModelError(f"{profile_id}: native {name} must be an object")
        _expect_keys(item, NATIVE_BYTE_VALUE_KEYS, f"{profile_id} native {name}")
        if set(item) != NATIVE_BYTE_VALUE_KEYS:
            raise ModelError(f"{profile_id}: native {name} is incomplete")
        if any(
            isinstance(item[key], bool)
            or not isinstance(item[key], int)
            or not 0 <= item[key] <= maximum
            for key, maximum in (("offset", value["report_bytes"] - 1), ("value", 255))
        ):
            raise ModelError(f"{profile_id}: native {name} is out of bounds")

    byte_value("request_status")
    byte_value("protocol_type")
    byte_value("trailer")
    response_status = report_format["response_status"]
    if not isinstance(response_status, dict):
        raise ModelError(f"{profile_id}: native response status must be an object")
    _expect_keys(
        response_status,
        NATIVE_BYTE_RANGE_KEYS,
        f"{profile_id} native response status",
    )
    if set(response_status) != NATIVE_BYTE_RANGE_KEYS or any(
        isinstance(response_status[key], bool)
        or not isinstance(response_status[key], int)
        or not 0 <= response_status[key] <= maximum
        for key, maximum in (
            ("offset", value["report_bytes"] - 1),
            ("minimum", 255),
            ("maximum", 255),
        )
    ):
        raise ModelError(f"{profile_id}: native response status is out of bounds")
    if response_status["minimum"] > response_status["maximum"]:
        raise ModelError(f"{profile_id}: native response status range is reversed")
    data_size = report_format["data_size"]
    if not isinstance(data_size, dict):
        raise ModelError(f"{profile_id}: native data size must be an object")
    _expect_keys(data_size, NATIVE_DATA_SIZE_KEYS, f"{profile_id} native data size")
    if set(data_size) != NATIVE_DATA_SIZE_KEYS or any(
        isinstance(data_size[key], bool)
        or not isinstance(data_size[key], int)
        or not 0 <= data_size[key] <= maximum
        for key, maximum in (("offset", value["report_bytes"] - 1), ("maximum", 255))
    ):
        raise ModelError(f"{profile_id}: native data size is out of bounds")
    checksum = report_format["checksum"]
    if not isinstance(checksum, dict):
        raise ModelError(f"{profile_id}: native checksum must be an object")
    _expect_keys(checksum, NATIVE_CHECKSUM_KEYS, f"{profile_id} native checksum")
    if set(checksum) != NATIVE_CHECKSUM_KEYS or checksum["algorithm"] != "xor8":
        raise ModelError(f"{profile_id}: native checksum is incomplete or unsupported")
    for key in ("start_offset", "end_offset_exclusive", "result_offset"):
        if isinstance(checksum[key], bool) or not isinstance(checksum[key], int):
            raise ModelError(f"{profile_id}: native checksum {key} is invalid")
    if not (
        0 <= checksum["start_offset"] < checksum["end_offset_exclusive"] <= value["report_bytes"]
        and 0 <= checksum["result_offset"] < value["report_bytes"]
        and not checksum["start_offset"]
        <= checksum["result_offset"]
        < checksum["end_offset_exclusive"]
    ):
        raise ModelError(f"{profile_id}: native checksum range is out of bounds")
    transaction_id = report_format["transaction_id"]
    if not isinstance(transaction_id, dict):
        raise ModelError(f"{profile_id}: native transaction id must be an object")
    _expect_keys(
        transaction_id,
        NATIVE_TRANSACTION_ID_KEYS,
        f"{profile_id} native transaction id",
    )
    if set(transaction_id) != NATIVE_TRANSACTION_ID_KEYS or any(
        isinstance(transaction_id[key], bool)
        or not isinstance(transaction_id[key], int)
        or not 0 <= transaction_id[key] <= maximum
        for key, maximum in (("offset", value["report_bytes"] - 1), ("match_mask", 255))
    ):
        raise ModelError(f"{profile_id}: native transaction id is out of bounds")
    route_selector = value.get("route_selector")
    if route_selector is not None:
        if not isinstance(route_selector, dict):
            raise ModelError(f"{profile_id}: native route selector must be an object")
        _expect_keys(
            route_selector,
            NATIVE_ROUTE_SELECTOR_KEYS,
            f"{profile_id} native route selector",
        )
        if set(route_selector) != NATIVE_ROUTE_SELECTOR_KEYS:
            raise ModelError(f"{profile_id}: native route selector is incomplete")
        if route_selector["field"] != "transaction-id":
            raise ModelError(f"{profile_id}: native route selector field is unsupported")
        for lane in ("pointer_lane", "keyboard_lane"):
            selector = route_selector[lane]
            if (
                isinstance(selector, bool)
                or not isinstance(selector, int)
                or not 1 <= selector <= 255
            ):
                raise ModelError(f"{profile_id}: native route selector {lane} is invalid")
        if route_selector["pointer_lane"] == route_selector["keyboard_lane"]:
            raise ModelError(f"{profile_id}: native route selectors must isolate endpoint lanes")
        if route_selector["restore_original_response"] is not True:
            raise ModelError(
                f"{profile_id}: native route selector must restore the application response"
            )
    ranges = value["match_ranges"]
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 4:
        raise ModelError(f"{profile_id}: native exchange needs one to four match ranges")
    previous_end = 0
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise ModelError(f"{profile_id}: native match range must be an object")
        _expect_keys(item, NATIVE_MATCH_RANGE_KEYS, f"{profile_id} native match range")
        if set(item) != NATIVE_MATCH_RANGE_KEYS:
            raise ModelError(f"{profile_id}: native match range is incomplete")
        offset = item["offset"]
        length = item["length"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or offset < 0
            or length <= 0
            or offset + length > value["report_bytes"]
        ):
            raise ModelError(f"{profile_id}: native match range is out of bounds")
        if index and offset < previous_end:
            raise ModelError(f"{profile_id}: native match ranges must be sorted and disjoint")
        previous_end = offset + length


def _validate_passive(
    profile: dict[str, Any], lifecycle_policy_index: dict[str, dict[str, Any]]
) -> None:
    transport = profile.get("transport", {})
    if not isinstance(transport, dict):
        raise ModelError(f"{profile['profile_id']}: transport must be an object")
    kind = profile["kind"]
    if kind == "receiver":
        _expect_keys(
            transport,
            RECEIVER_TRANSPORT_KEYS,
            f"{profile['profile_id']} receiver transport",
        )
        inventory = transport.get("pairing_inventory")
        if inventory is not None:
            if not isinstance(inventory, dict):
                raise ModelError(
                    f"{profile['profile_id']}: pairing inventory must be an object"
                )
            _expect_keys(
                inventory,
                {"decoder"},
                f"{profile['profile_id']} pairing inventory",
            )
            if inventory.get("decoder") != "razer-hyperflux-v2-feature-01-v1":
                raise ModelError(
                    f"{profile['profile_id']}: unsupported pairing inventory decoder"
                )
        native_exchange = transport.get("native_exchange")
        if native_exchange is not None:
            _validate_native_exchange(profile["profile_id"], native_exchange)
        return
    if kind != "child":
        if transport:
            raise ModelError(f"{profile['profile_id']}: surface transport is not allowed")
        return
    _expect_keys(
        transport,
        {"lighting", "passive", "lifecycle"},
        f"{profile['profile_id']} child transport",
    )
    passive = transport.get("passive")
    capability_ids = {item["id"] for item in profile["capabilities"]}
    needs_passive = bool(capability_ids & PASSIVE_TELEMETRY_CAPABILITIES)
    if passive is None:
        if needs_passive:
            raise ModelError(f"{profile['profile_id']}: passive telemetry requires a decoder")
        _validate_lifecycle_policy(profile, transport, passive, lifecycle_policy_index)
        return
    if not isinstance(passive, dict):
        raise ModelError(f"{profile['profile_id']}: passive decoder must be an object")
    _expect_keys(
        passive,
        PASSIVE_KEYS | OPTIONAL_PASSIVE_KEYS,
        f"{profile['profile_id']} passive decoder",
    )
    missing = sorted(PASSIVE_KEYS - set(passive))
    if missing:
        raise ModelError(f"{profile['profile_id']}: incomplete passive decoder: {', '.join(missing)}")
    expected_lane = "pointer" if profile["device_kind"] == "mouse" else "keyboard"
    if passive["endpoint_lane"] != expected_lane:
        raise ModelError(f"{profile['profile_id']}: passive lane contradicts the device kind")
    if passive["battery_encoding"] != "linear-255":
        raise ModelError(f"{profile['profile_id']}: unsupported battery encoding")
    if not isinstance(passive["report_implies_route_available"], bool):
        raise ModelError(f"{profile['profile_id']}: route implication must be boolean")

    contact = passive["contact"]
    if profile["device_kind"] == "mouse":
        if not isinstance(contact, dict) or set(contact) != {"off_mat", "on_mat"}:
            raise ModelError(f"{profile['profile_id']}: mouse contact decoder is incomplete")
        off_mat = _raw_values(contact["off_mat"], f"{profile['profile_id']} off-mat values")
        on_mat = _raw_values(contact["on_mat"], f"{profile['profile_id']} on-mat values")
        if not off_mat or not on_mat or set(off_mat) & set(on_mat):
            raise ModelError(f"{profile['profile_id']}: contact values must be nonempty and disjoint")
    elif contact is not None:
        raise ModelError(f"{profile['profile_id']}: keyboard cannot declare mouse contact")

    _validate_charge(profile, passive, capability_ids)

    route = passive["route"]
    route_keys = {"available", "unavailable", "indeterminate"}
    if (
        not isinstance(route, dict)
        or not {"available", "unavailable"}.issubset(route)
        or not set(route).issubset(route_keys)
    ):
        raise ModelError(f"{profile['profile_id']}: route decoder is incomplete")
    available = _raw_values(route["available"], f"{profile['profile_id']} available routes")
    unavailable = _raw_values(route["unavailable"], f"{profile['profile_id']} unavailable routes")
    indeterminate = _raw_values(
        route.get("indeterminate", []),
        f"{profile['profile_id']} indeterminate routes",
    )
    if (
        set(available) & set(unavailable)
        or set(available) & set(indeterminate)
        or set(unavailable) & set(indeterminate)
    ):
        raise ModelError(f"{profile['profile_id']}: route values must be disjoint")
    if "telemetry.connection-evidence" in capability_ids and not (
        available or unavailable or passive["report_implies_route_available"]
    ):
        raise ModelError(f"{profile['profile_id']}: connection evidence has no route decoder")
    _validate_lifecycle_policy(profile, transport, passive, lifecycle_policy_index)


def _validate_charge(
    profile: dict[str, Any], passive: dict[str, Any], capability_ids: set[str]
) -> None:
    charge = passive.get("charge")
    qualified = "telemetry.charging-state" in capability_ids
    if charge is None:
        if qualified:
            raise ModelError(f"{profile['profile_id']}: charging state requires a charge decoder")
        return
    if profile["device_kind"] != "mouse":
        raise ModelError(f"{profile['profile_id']}: only a mouse may declare a charge decoder")
    if not isinstance(charge, dict) or set(charge) != {"charging", "not_charging"}:
        raise ModelError(f"{profile['profile_id']}: charge decoder is incomplete")
    charging = _raw_values(charge["charging"], f"{profile['profile_id']} charging values")
    not_charging = _raw_values(
        charge["not_charging"], f"{profile['profile_id']} not-charging values"
    )
    if not charging or not not_charging or set(charging) & set(not_charging):
        raise ModelError(f"{profile['profile_id']}: charge values must be nonempty and disjoint")
    if not qualified:
        raise ModelError(f"{profile['profile_id']}: charge decoder requires the charging-state capability")
