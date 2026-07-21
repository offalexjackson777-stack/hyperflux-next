# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import json
import types
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from .errors import CodecError
from .generated import protocol_v5_types as v5


_TAG_ATTRIBUTES = (
    ("METHOD", "method", "request"),
    ("TYPE", "type", "response"),
    ("OUTCOME", "outcome", "detail"),
    ("STATE", "state", "detail"),
)


def _domain_wrapper(value_type: type[Any]) -> bool:
    return (
        is_dataclass(value_type)
        and value_type.__module__.endswith(".domain_types")
        and len(fields(value_type)) == 1
        and fields(value_type)[0].name == "value"
    )


def _tag(value_type: type[Any]) -> tuple[str, str, str] | None:
    for attribute, wire_name, content_name in _TAG_ATTRIBUTES:
        if hasattr(value_type, attribute):
            value = getattr(value_type, attribute)
            if isinstance(value, str):
                return wire_name, value, content_name
    return None


def _field_limits(value_type: type[Any]) -> dict[tuple[str, str], int]:
    module = value_type.__module__
    if module.endswith("protocol_v5_types") or module.endswith("protocol_types"):
        return v5.FIELD_LIMITS
    return {}


def to_wire(value: Any) -> Any:
    """Encode one generated SDK value into strict JSON-compatible data."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, int):
        return value
    if isinstance(value, tuple):
        return [to_wire(item) for item in value]
    value_type = type(value)
    if _domain_wrapper(value_type):
        raw = value.value
        if getattr(value_type, "WIRE_ENCODING", None) == "decimal-string":
            return str(raw)
        return raw
    if not is_dataclass(value):
        raise CodecError(f"unsupported SDK value: {value_type.__name__}")
    if value_type is v5.ErrorEnvelope:
        return {"type": "error", "response": _record_to_wire(value)}
    marker = _tag(value_type)
    if marker is not None:
        tag_name, tag_value, content_name = marker
        return {tag_name: tag_value, content_name: to_wire(getattr(value, content_name))}
    return _record_to_wire(value)


def _record_to_wire(value: Any) -> dict[str, Any]:
    value_type = type(value)
    limits = _field_limits(value_type)
    result: dict[str, Any] = {}
    for field in fields(value):
        item = getattr(value, field.name)
        limit = limits.get((value_type.__name__, field.name))
        if limit is not None and isinstance(item, tuple) and len(item) > limit:
            raise CodecError(f"{value_type.__name__}.{field.name} exceeds {limit} items")
        result[field.name] = to_wire(item)
    return result


def _replace_typevars(value_type: Any, substitutions: dict[TypeVar, Any]) -> Any:
    if isinstance(value_type, TypeVar):
        return substitutions.get(value_type, value_type)
    return value_type


def _dataclass_origin(value_type: Any) -> tuple[type[Any] | None, dict[TypeVar, Any]]:
    origin = get_origin(value_type)
    if origin is None:
        return (value_type if isinstance(value_type, type) else None), {}
    if not isinstance(origin, type):
        return None, {}
    parameters = getattr(origin, "__parameters__", ())
    return origin, dict(zip(parameters, get_args(value_type), strict=True))


def _decode_union(value_type: Any, value: Any, substitutions: dict[TypeVar, Any]) -> Any:
    alternatives = get_args(value_type)
    if value is None and type(None) in alternatives:
        return None
    candidates = [candidate for candidate in alternatives if candidate is not type(None)]
    if isinstance(value, dict):
        for candidate in candidates:
            candidate = _replace_typevars(candidate, substitutions)
            candidate_type, _ = _dataclass_origin(candidate)
            if candidate_type is None:
                continue
            if candidate_type is v5.ErrorEnvelope and value.get("type") == "error":
                if set(value) != {"type", "response"}:
                    raise CodecError("ErrorEnvelope contains unknown fields")
                return from_wire(candidate_type, value["response"], substitutions)
            marker = _tag(candidate_type)
            if marker is None:
                continue
            tag_name, tag_value, content_name = marker
            if value.get(tag_name) != tag_value:
                continue
            if set(value) != {tag_name, content_name}:
                raise CodecError(f"{candidate_type.__name__} contains unknown fields")
            hints = get_type_hints(candidate_type)
            detail_type = hints[content_name]
            return candidate_type(
                **{content_name: from_wire(detail_type, value[content_name], substitutions)}
            )
    if len(candidates) == 1:
        return from_wire(candidates[0], value, substitutions)
    raise CodecError("wire value does not match a tagged SDK union")


def from_wire(
    value_type: Any,
    value: Any,
    substitutions: dict[TypeVar, Any] | None = None,
) -> Any:
    """Decode strict JSON-compatible data into one generated SDK value."""

    substitutions = {} if substitutions is None else substitutions
    value_type = _replace_typevars(value_type, substitutions)
    origin = get_origin(value_type)
    if origin in {types.UnionType, Union}:
        return _decode_union(value_type, value, substitutions)
    if origin is tuple:
        if not isinstance(value, list):
            raise CodecError("wire array expected")
        arguments = get_args(value_type)
        if len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise CodecError("only homogeneous generated tuples are supported")
        return tuple(from_wire(arguments[0], item, substitutions) for item in value)
    value_class, generic_substitutions = _dataclass_origin(value_type)
    if value_class is not None and is_dataclass(value_class):
        merged = {**substitutions, **generic_substitutions}
        if _domain_wrapper(value_class):
            raw = value
            if getattr(value_class, "WIRE_ENCODING", None) == "decimal-string":
                if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                    raise CodecError(f"{value_class.__name__} requires a decimal string")
                raw = int(value)
            try:
                return value_class(raw)
            except (TypeError, ValueError) as error:
                raise CodecError(f"invalid {value_class.__name__}") from error
        if not isinstance(value, dict):
            raise CodecError(f"{value_class.__name__} requires an object")
        expected = {field.name for field in fields(value_class)}
        if set(value) != expected:
            missing = sorted(expected - set(value))
            extras = sorted(set(value) - expected)
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if extras:
                detail.append(f"unknown {', '.join(extras)}")
            raise CodecError(f"{value_class.__name__}: {'; '.join(detail)}")
        hints = get_type_hints(value_class)
        limits = _field_limits(value_class)
        decoded: dict[str, Any] = {}
        for field in fields(value_class):
            raw = value[field.name]
            limit = limits.get((value_class.__name__, field.name))
            if limit is not None and isinstance(raw, list) and len(raw) > limit:
                raise CodecError(f"{value_class.__name__}.{field.name} exceeds {limit} items")
            decoded[field.name] = from_wire(hints[field.name], raw, merged)
        try:
            return value_class(**decoded)
        except (TypeError, ValueError) as error:
            raise CodecError(f"invalid {value_class.__name__}") from error
    if isinstance(value_type, type) and issubclass(value_type, Enum):
        try:
            return value_type(value)
        except (TypeError, ValueError) as error:
            raise CodecError(f"invalid {value_type.__name__}") from error
    if value_type is bool:
        if not isinstance(value, bool):
            raise CodecError("boolean expected")
        return value
    if value_type is str:
        if not isinstance(value, str):
            raise CodecError("string expected")
        return value
    if value_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError("integer expected")
        return value
    if value_type is type(None) and value is None:
        return None
    raise CodecError(f"unsupported SDK type: {value_type!r}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodecError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _bounded_depth(value: Any, depth: int = 0) -> None:
    if depth > v5.MAX_JSON_DEPTH:
        raise CodecError("JSON nesting exceeds the protocol bound")
    if isinstance(value, dict):
        for item in value.values():
            _bounded_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded_depth(item, depth + 1)


def encode_message(value: Any) -> bytes:
    payload = json.dumps(
        to_wire(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > v5.MAX_WIRE_MESSAGE_BYTES:
        raise CodecError("encoded SDK message exceeds the protocol bound")
    return payload


def decode_message(value_type: Any, payload: bytes) -> Any:
    if not payload or len(payload) > v5.MAX_WIRE_MESSAGE_BYTES:
        raise CodecError("SDK message length is outside the protocol bound")
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodecError("SDK message is not canonical JSON") from error
    _bounded_depth(document)
    return from_wire(value_type, document)
