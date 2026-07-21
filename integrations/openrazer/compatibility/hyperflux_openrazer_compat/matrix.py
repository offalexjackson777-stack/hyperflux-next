# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from dataclasses import dataclass

from .model import ControllerRecord


Rgb = tuple[int, int, int]


class MatrixError(ValueError):
    """An OpenRazer matrix payload is malformed, incomplete, or stale."""


@dataclass(slots=True)
class _Frame:
    authority: tuple[str, int, str, int, int]
    colors: list[Rgb | None]


class MatrixBuffer:
    def __init__(self) -> None:
        self._frame: _Frame | None = None

    def clear(self) -> None:
        self._frame = None

    def stage(self, record: ControllerRecord, payload: bytes) -> None:
        authority = _authority(record)
        updates = _parse_payload(record.rows, record.columns, payload)
        if self._frame is None or self._frame.authority != authority:
            self._frame = _Frame(authority, [None] * record.led_count)
        for index, color in updates:
            self._frame.colors[index] = color

    def complete(self, record: ControllerRecord) -> tuple[Rgb, ...]:
        frame = self._frame
        if frame is None:
            raise MatrixError("no OpenRazer matrix frame has been staged")
        if frame.authority != _authority(record):
            raise MatrixError("the staged matrix frame belongs to a stale controller generation")
        if any(color is None for color in frame.colors):
            present = sum(color is not None for color in frame.colors)
            raise MatrixError(
                f"the OpenRazer matrix frame is incomplete: {present} of {record.led_count} LEDs"
            )
        return tuple(color for color in frame.colors if color is not None)


def _authority(record: ControllerRecord) -> tuple[str, int, str, int, int]:
    controller = record.controller
    return (
        controller.receiver_id.value,
        controller.generation_id.value,
        controller.device_id.value,
        record.rows,
        record.columns,
    )


def _parse_payload(rows: int, columns: int, payload: bytes) -> tuple[tuple[int, Rgb], ...]:
    if not payload:
        raise MatrixError("the OpenRazer matrix row payload is empty")
    offset = 0
    seen: set[int] = set()
    updates: list[tuple[int, Rgb]] = []
    while offset < len(payload):
        if len(payload) - offset < 3:
            raise MatrixError("the OpenRazer matrix row header is truncated")
        row, start, end = payload[offset : offset + 3]
        offset += 3
        if row >= rows or start > end or end >= columns:
            raise MatrixError("the OpenRazer matrix coordinates are outside the device")
        color_count = end - start + 1
        color_bytes = color_count * 3
        if len(payload) - offset < color_bytes:
            raise MatrixError("the OpenRazer matrix RGB data is truncated")
        for column in range(start, end + 1):
            index = row * columns + column
            if index in seen:
                raise MatrixError("the OpenRazer payload addresses one LED more than once")
            seen.add(index)
            updates.append((index, tuple(payload[offset : offset + 3])))
            offset += 3
    return tuple(updates)


__all__ = ["MatrixBuffer", "MatrixError", "Rgb"]
