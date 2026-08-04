# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import struct
import zlib


WIDTH = 1280
HEIGHT = 640

PALETTE = {
    "background": (17, 21, 26),
    "surface": (28, 34, 41),
    "surface_strong": (37, 45, 53),
    "line": (57, 68, 77),
    "ink": (237, 243, 242),
    "muted": (174, 187, 192),
    "teal": (67, 214, 181),
    "lime": (183, 223, 80),
    "coral": (255, 125, 110),
    "yellow": (255, 209, 102),
    "cyan": (108, 207, 246),
}

FONT = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


class Canvas:
    def __init__(self) -> None:
        self.pixels = bytearray(PALETTE["background"] * (WIDTH * HEIGHT))

    def rectangle(
        self, x: int, y: int, width: int, height: int, color: tuple[int, int, int]
    ) -> None:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(WIDTH, x + width)
        y1 = min(HEIGHT, y + height)
        row = bytes(color) * max(0, x1 - x0)
        for row_index in range(y0, y1):
            start = (row_index * WIDTH + x0) * 3
            self.pixels[start : start + len(row)] = row

    def ellipse(
        self, x: int, y: int, width: int, height: int, color: tuple[int, int, int]
    ) -> None:
        center_x = x + width / 2
        center_y = y + height / 2
        radius_x = width / 2
        radius_y = height / 2
        for row in range(max(0, y), min(HEIGHT, y + height)):
            normalized_y = (row + 0.5 - center_y) / radius_y
            if abs(normalized_y) > 1:
                continue
            half_width = radius_x * (1 - normalized_y * normalized_y) ** 0.5
            left = max(0, int(center_x - half_width))
            right = min(WIDTH, int(center_x + half_width) + 1)
            self.rectangle(left, row, right - left, 1, color)

    def text(
        self,
        value: str,
        x: int,
        y: int,
        scale: int,
        color: tuple[int, int, int],
    ) -> None:
        cursor = x
        for character in value.upper():
            glyph = FONT.get(character)
            if glyph is None:
                raise ValueError(f"unsupported social-preview character: {character}")
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.rectangle(
                            cursor + column_index * scale,
                            y + row_index * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += 6 * scale

    def png(self) -> bytes:
        raw = bytearray()
        stride = WIDTH * 3
        for row in range(HEIGHT):
            raw.append(0)
            start = row * stride
            raw.extend(self.pixels[start : start + stride])
        return b"\x89PNG\r\n\x1a\n" + b"".join(
            (
                _chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)),
                _chunk(b"IDAT", zlib.compress(bytes(raw), level=9)),
                _chunk(b"IEND", b""),
            )
        )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _label_width(value: str, scale: int) -> int:
    return max(0, len(value) * 6 * scale - scale)


def _centered_text(
    canvas: Canvas,
    value: str,
    center_x: int,
    y: int,
    scale: int,
    color: tuple[int, int, int],
) -> None:
    canvas.text(value, center_x - _label_width(value, scale) // 2, y, scale, color)


def render_social_preview(
    *, candidates: int, subsystems: int, verification_nodes: int, hardware_writes: int
) -> bytes:
    _ = (candidates, subsystems, verification_nodes, hardware_writes)
    canvas = Canvas()
    canvas.rectangle(0, 0, WIDTH, 8, PALETTE["teal"])
    canvas.rectangle(64, 54, 64, 64, PALETTE["surface"])
    canvas.rectangle(64, 54, 64, 3, PALETTE["teal"])
    canvas.rectangle(64, 115, 64, 3, PALETTE["teal"])
    canvas.rectangle(64, 54, 3, 64, PALETTE["teal"])
    canvas.rectangle(125, 54, 3, 64, PALETTE["teal"])
    canvas.text("HF", 77, 71, 5, PALETTE["teal"])
    canvas.text("HYPERFLUX NEXT", 158, 58, 7, PALETTE["ink"])
    canvas.text("LINUX SUPPORT FOR RAZER DEVICES PAIRED THROUGH HYPERFLUX V2", 162, 116, 2, PALETTE["muted"])

    canvas.rectangle(64, 164, 1152, 1, PALETTE["line"])

    # Receiver
    canvas.rectangle(88, 258, 184, 156, PALETTE["surface_strong"])
    canvas.rectangle(96, 266, 168, 140, PALETTE["surface"])
    canvas.rectangle(156, 304, 48, 48, PALETTE["teal"])
    canvas.rectangle(169, 317, 22, 22, PALETTE["background"])
    canvas.rectangle(116, 286, 20, 3, PALETTE["cyan"])
    canvas.rectangle(108, 278, 36, 3, PALETTE["line"])
    _centered_text(canvas, "RECEIVER", 180, 438, 2, PALETTE["muted"])

    # Mouse
    canvas.ellipse(352, 220, 206, 250, PALETTE["surface_strong"])
    canvas.ellipse(360, 228, 190, 234, PALETTE["surface"])
    canvas.rectangle(453, 236, 4, 94, PALETTE["line"])
    canvas.rectangle(420, 314, 70, 4, PALETTE["line"])
    canvas.rectangle(442, 270, 26, 50, PALETTE["teal"])
    canvas.rectangle(448, 279, 14, 28, PALETTE["background"])
    _centered_text(canvas, "MOUSE", 455, 492, 2, PALETTE["muted"])

    # Keyboard
    canvas.rectangle(632, 236, 568, 236, PALETTE["surface_strong"])
    canvas.rectangle(642, 246, 548, 216, PALETTE["surface"])
    key_width = 36
    key_height = 30
    for row in range(4):
        for column in range(12):
            key_x = 660 + column * 42
            key_y = 266 + row * 38
            color = PALETTE["teal"] if (row + column) % 5 == 0 else PALETTE["line"]
            canvas.rectangle(key_x, key_y, key_width, key_height, color)
            canvas.rectangle(key_x + 3, key_y + 3, key_width - 6, key_height - 6, PALETTE["surface_strong"])
    canvas.rectangle(816, 420, 220, 24, PALETTE["line"])
    canvas.rectangle(822, 426, 208, 12, PALETTE["surface_strong"])
    _centered_text(canvas, "KEYBOARD", 916, 492, 2, PALETTE["muted"])

    canvas.rectangle(64, 536, 1152, 1, PALETTE["line"])
    _centered_text(
        canvas,
        "OPENRAZER  POLYCHROMATIC  OPENRGB",
        WIDTH // 2,
        574,
        3,
        PALETTE["ink"],
    )
    return canvas.png()
