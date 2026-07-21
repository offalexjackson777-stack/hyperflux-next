# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Iterator, cast


STATE_SCHEMA = "hyperflux-polychromatic-state-v1"
MAX_STATE_BYTES = 512 * 1024
MAX_DEVICES = 64
MAX_LEDS = 1024
SERIAL_PATTERN = re.compile(r"^hfx-[0-9a-f]{24}$")
MODE_VALUES = frozenset(("off", "static", "unknown"))

Rgb = tuple[int, int, int]


class StateError(RuntimeError):
    """Polychromatic state is unsafe, malformed, or cannot be committed."""


@dataclass(frozen=True, slots=True)
class StableState:
    mode: str
    brightness: int
    colors: tuple[Rgb, ...]

    def __post_init__(self) -> None:
        if self.mode not in MODE_VALUES:
            raise StateError("stable lighting mode is unsupported")
        if isinstance(self.brightness, bool) or not 0 <= self.brightness <= 100:
            raise StateError("stable lighting brightness must be from 0 through 100")
        if not 1 <= len(self.colors) <= MAX_LEDS:
            raise StateError("stable lighting dimensions are outside the adapter bound")
        for color in self.colors:
            if len(color) != 3 or any(
                isinstance(channel, bool)
                or not isinstance(channel, int)
                or not 0 <= channel <= 255
                for channel in color
            ):
                raise StateError("stable lighting contains an invalid RGB value")

    @classmethod
    def unknown(cls, led_count: int) -> StableState:
        if isinstance(led_count, bool) or not 1 <= led_count <= MAX_LEDS:
            raise StateError("controller LED count is outside the adapter bound")
        return cls("unknown", 100, ((0, 0, 0),) * led_count)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"state contains duplicate field {key!r}")
        result[key] = value
    return result


def _hex_color(color: Rgb) -> str:
    return "".join(f"{channel:02x}" for channel in color)


def _decode_color(value: object) -> Rgb:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{6}", value) is None:
        raise StateError("state contains a non-canonical RGB color")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


class StateStore:
    """Small private, locked, atomic store for application-owned stable state."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Polychromatic state path must be absolute")
        self._path = path
        self._lock_path = path.with_name(path.name + ".lock")

    def load(self, serial: str, led_count: int) -> StableState:
        self._validate_serial(serial)
        with self._locked():
            document = self._read_document()
            devices = cast(dict[str, object], document["devices"])
            raw = devices.get(serial)
            if raw is None:
                return StableState.unknown(led_count)
            state = self._decode_state(raw)
            if len(state.colors) != led_count:
                return StableState.unknown(led_count)
            return state

    def save(self, serial: str, state: StableState) -> None:
        self._validate_serial(serial)
        with self._locked():
            document = self._read_document()
            devices = cast(dict[str, object], document["devices"])
            if serial not in devices and len(devices) >= MAX_DEVICES:
                raise StateError("Polychromatic state reached its bounded device capacity")
            devices[serial] = {
                "brightness": state.brightness,
                "colors": [_hex_color(color) for color in state.colors],
                "mode": state.mode,
            }
            self._write_document(document)

    @staticmethod
    def _validate_serial(serial: str) -> None:
        if SERIAL_PATTERN.fullmatch(serial) is None:
            raise StateError("device identity is not a HyperFlux pseudonymous serial")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory = os.open(self._path.parent, directory_flags)
        except OSError as error:
            raise StateError("private Polychromatic state directory is unsafe") from error
        try:
            os.fchmod(directory, 0o700)
        finally:
            os.close(directory)
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as error:
            raise StateError("cannot open the private Polychromatic state lock") from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read_document(self) -> dict[str, object]:
        if not self._path.exists():
            return {"schema": STATE_SCHEMA, "devices": {}}
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as error:
            raise StateError("cannot open private Polychromatic state") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATE_BYTES:
                raise StateError("private Polychromatic state is not a bounded regular file")
            if metadata.st_uid != os.geteuid():
                raise StateError("private Polychromatic state has an unexpected owner")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise StateError("private Polychromatic state permissions are too broad")
            payload = bytearray()
            while len(payload) <= MAX_STATE_BYTES:
                chunk = os.read(descriptor, min(65_536, MAX_STATE_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_STATE_BYTES:
                raise StateError("private Polychromatic state exceeds its byte bound")
        finally:
            os.close(descriptor)
        try:
            document = json.loads(payload, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateError("private Polychromatic state is malformed") from error
        return self._validate_document(document)

    def _validate_document(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {"devices", "schema"}:
            raise StateError("private Polychromatic state has unsupported fields")
        if value["schema"] != STATE_SCHEMA or not isinstance(value["devices"], dict):
            raise StateError("private Polychromatic state uses an unsupported schema")
        devices = cast(dict[str, object], value["devices"])
        if len(devices) > MAX_DEVICES:
            raise StateError("private Polychromatic state exceeds its device bound")
        for serial, raw in devices.items():
            self._validate_serial(serial)
            self._decode_state(raw)
        return value

    @staticmethod
    def _decode_state(value: object) -> StableState:
        if not isinstance(value, dict) or set(value) != {"brightness", "colors", "mode"}:
            raise StateError("device state has unsupported fields")
        colors = value["colors"]
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_LEDS:
            raise StateError("device state has invalid lighting dimensions")
        brightness = value["brightness"]
        if isinstance(brightness, bool) or not isinstance(brightness, int):
            raise StateError("device state brightness is not an integer")
        mode = value["mode"]
        if not isinstance(mode, str):
            raise StateError("device state mode is not a string")
        return StableState(mode, brightness, tuple(_decode_color(color) for color in colors))

    def _write_document(self, value: dict[str, object]) -> None:
        payload = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
        if len(payload) > MAX_STATE_BYTES:
            raise StateError("private Polychromatic state exceeds its byte bound")
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise StateError("private Polychromatic state write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self._path)
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory = os.open(self._path.parent, directory_flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise StateError("cannot commit private Polychromatic state") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
