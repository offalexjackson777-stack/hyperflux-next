# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .model import ModelError, load_json, require_unique, sha256_file


NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PRIMITIVES: dict[str, tuple[int, int]] = {
    "u8": (1, 1),
    "u16": (2, 2),
    "u32": (4, 4),
    "i32": (4, 4),
    "u64": (8, 8),
}
TOP_LEVEL_KEYS = {
    "$schema",
    "schema",
    "abi_version",
    "ioctl_magic",
    "limits",
    "enums",
    "structs",
    "ioctls",
}
MAX_IOCTL_STRUCT_BYTES = 1 << 14


@dataclass(frozen=True)
class UapiEnumValue:
    name: str
    value: int


@dataclass(frozen=True)
class UapiEnum:
    name: str
    values: tuple[UapiEnumValue, ...]


@dataclass(frozen=True)
class UapiField:
    name: str
    type_name: str
    count: int | str | None


@dataclass(frozen=True)
class UapiStruct:
    name: str
    fields: tuple[UapiField, ...]
    size: int
    alignment: int


@dataclass(frozen=True)
class UapiIoctl:
    name: str
    number: int
    direction: str
    struct_name: str


@dataclass(frozen=True)
class KernelUapi:
    abi_version: int
    ioctl_magic: int
    limits: dict[str, int]
    enums: tuple[UapiEnum, ...]
    structs: tuple[UapiStruct, ...]
    ioctls: tuple[UapiIoctl, ...]
    source_sha256: str

    def struct(self, name: str) -> UapiStruct:
        return next(item for item in self.structs if item.name == name)


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or NAME.fullmatch(value) is None:
        raise ModelError(f"kernel UAPI {label} must be a canonical snake-case name")
    return value


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelError(f"kernel UAPI {label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ModelError(f"kernel UAPI {label} must be an integer <= {maximum}")
    return value


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ModelError(f"kernel UAPI {label}: {'; '.join(detail)}")


def _align(offset: int, alignment: int) -> int:
    return (offset + alignment - 1) // alignment * alignment


def load_kernel_uapi(root: Path) -> KernelUapi:
    path = root / "uapi" / "kernel-uapi.json"
    value = load_json(path)
    _exact_keys(value, TOP_LEVEL_KEYS, "catalog")
    if value["$schema"] != "../schemas/kernel-uapi.schema.json":
        raise ModelError("kernel UAPI catalog has an unexpected schema reference")
    if value["schema"] != "hyperflux-kernel-uapi-v1":
        raise ModelError("unsupported kernel UAPI catalog schema")

    abi_version = _integer(value["abi_version"], "ABI version", minimum=1, maximum=0xFFFFFFFF)
    ioctl_magic = _integer(value["ioctl_magic"], "ioctl magic", minimum=1, maximum=0xFF)

    raw_limits = value["limits"]
    if not isinstance(raw_limits, dict) or not raw_limits:
        raise ModelError("kernel UAPI limits must be a nonempty object")
    limits: dict[str, int] = {}
    for raw_name, raw_limit in raw_limits.items():
        name = _name(raw_name, "limit")
        limits[name] = _integer(
            raw_limit,
            f"limit {name}",
            minimum=1,
            maximum=0xFFFFFFFFFFFFFFFF,
        )

    raw_enums = value["enums"]
    if not isinstance(raw_enums, list) or not raw_enums:
        raise ModelError("kernel UAPI enums must be a nonempty array")
    enums: list[UapiEnum] = []
    for raw_enum in raw_enums:
        if not isinstance(raw_enum, dict):
            raise ModelError("kernel UAPI enum must be an object")
        _exact_keys(raw_enum, {"name", "values"}, "enum")
        name = _name(raw_enum["name"], "enum")
        raw_values = raw_enum["values"]
        if not isinstance(raw_values, list) or not raw_values:
            raise ModelError(f"kernel UAPI enum {name} has no values")
        enum_values: list[UapiEnumValue] = []
        for raw_enum_value in raw_values:
            if not isinstance(raw_enum_value, dict):
                raise ModelError(f"kernel UAPI enum {name} value must be an object")
            _exact_keys(raw_enum_value, {"name", "value"}, f"enum {name} value")
            enum_values.append(
                UapiEnumValue(
                    name=_name(raw_enum_value["name"], f"enum {name} value"),
                    value=_integer(
                        raw_enum_value["value"],
                        f"enum {name} value",
                        maximum=0xFFFFFFFF,
                    ),
                )
            )
        require_unique([item.name for item in enum_values], f"kernel UAPI {name} value name")
        require_unique([str(item.value) for item in enum_values], f"kernel UAPI {name} value")
        enums.append(UapiEnum(name=name, values=tuple(enum_values)))
    require_unique([item.name for item in enums], "kernel UAPI enum")

    raw_structs = value["structs"]
    if not isinstance(raw_structs, list) or not raw_structs:
        raise ModelError("kernel UAPI structs must be a nonempty array")
    structs: list[UapiStruct] = []
    layouts: dict[str, tuple[int, int]] = {}
    for raw_struct in raw_structs:
        if not isinstance(raw_struct, dict):
            raise ModelError("kernel UAPI struct must be an object")
        _exact_keys(raw_struct, {"name", "fields"}, "struct")
        name = _name(raw_struct["name"], "struct")
        raw_fields = raw_struct["fields"]
        if not isinstance(raw_fields, list) or not raw_fields:
            raise ModelError(f"kernel UAPI struct {name} has no fields")
        fields: list[UapiField] = []
        offset = 0
        struct_alignment = 1
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                raise ModelError(f"kernel UAPI struct {name} field must be an object")
            if set(raw_field) not in ({"name", "type"}, {"name", "type", "count"}):
                raise ModelError(f"kernel UAPI struct {name} field has invalid keys")
            field_name = _name(raw_field["name"], f"struct {name} field")
            type_name = _name(raw_field["type"], f"struct {name} field type")
            if type_name in PRIMITIVES:
                element_size, alignment = PRIMITIVES[type_name]
            elif type_name in layouts:
                element_size, alignment = layouts[type_name]
            else:
                raise ModelError(
                    f"kernel UAPI struct {name} field {field_name} references "
                    "an unknown or forward-declared type"
                )
            count = raw_field.get("count")
            if isinstance(count, str):
                count = _name(count, f"struct {name} field count")
                if count not in limits:
                    raise ModelError(f"kernel UAPI struct {name} references unknown limit {count}")
                resolved_count = limits[count]
            elif count is None:
                resolved_count = 1
            else:
                resolved_count = _integer(count, f"struct {name} field count", minimum=1)
            if resolved_count > 4096:
                raise ModelError(f"kernel UAPI struct {name} field {field_name} is unbounded")
            offset = _align(offset, alignment)
            offset += element_size * resolved_count
            struct_alignment = max(struct_alignment, alignment)
            fields.append(UapiField(field_name, type_name, count))
        require_unique([item.name for item in fields], f"kernel UAPI {name} field")
        size = _align(offset, struct_alignment)
        if size >= MAX_IOCTL_STRUCT_BYTES:
            raise ModelError(f"kernel UAPI struct {name} exceeds the Linux ioctl size field")
        layouts[name] = (size, struct_alignment)
        structs.append(UapiStruct(name, tuple(fields), size, struct_alignment))
    require_unique([item.name for item in structs], "kernel UAPI struct")

    raw_ioctls = value["ioctls"]
    if not isinstance(raw_ioctls, list) or not raw_ioctls:
        raise ModelError("kernel UAPI ioctls must be a nonempty array")
    struct_names = set(layouts)
    ioctls: list[UapiIoctl] = []
    for raw_ioctl in raw_ioctls:
        if not isinstance(raw_ioctl, dict):
            raise ModelError("kernel UAPI ioctl must be an object")
        _exact_keys(raw_ioctl, {"name", "number", "direction", "struct"}, "ioctl")
        name = _name(raw_ioctl["name"], "ioctl")
        number = _integer(raw_ioctl["number"], f"ioctl {name} number", maximum=0xFF)
        direction = raw_ioctl["direction"]
        if direction not in {"read", "write", "read_write"}:
            raise ModelError(f"kernel UAPI ioctl {name} has invalid direction")
        struct_name = _name(raw_ioctl["struct"], f"ioctl {name} struct")
        if struct_name not in struct_names:
            raise ModelError(f"kernel UAPI ioctl {name} references unknown struct {struct_name}")
        ioctls.append(UapiIoctl(name, number, direction, struct_name))
    require_unique([item.name for item in ioctls], "kernel UAPI ioctl")
    require_unique([str(item.number) for item in ioctls], "kernel UAPI ioctl number")

    return KernelUapi(
        abi_version=abi_version,
        ioctl_magic=ioctl_magic,
        limits=limits,
        enums=tuple(enums),
        structs=tuple(structs),
        ioctls=tuple(ioctls),
        source_sha256=sha256_file(path),
    )
