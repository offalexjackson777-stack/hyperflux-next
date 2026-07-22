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
    canvas = Canvas()
    canvas.rectangle(0, 0, WIDTH, 8, PALETTE["teal"])
    canvas.rectangle(64, 54, 72, 72, PALETTE["surface"])
    canvas.rectangle(64, 54, 72, 3, PALETTE["teal"])
    canvas.rectangle(64, 123, 72, 3, PALETTE["teal"])
    canvas.rectangle(64, 54, 3, 72, PALETTE["teal"])
    canvas.rectangle(133, 54, 3, 72, PALETTE["teal"])
    canvas.text("HF", 79, 75, 5, PALETTE["teal"])
    canvas.text("HYPERFLUX NEXT", 166, 54, 7, PALETTE["ink"])
    canvas.text("LINUX RECEIVER FOUNDATION", 170, 110, 3, PALETTE["muted"])

    tags = (
        ("SCHEMA FIRST", PALETTE["cyan"]),
        ("EVIDENCE BOUND", PALETTE["lime"]),
        ("PRODUCT UNRELEASED", PALETTE["coral"]),
    )
    tag_x = 780
    for value, color in tags:
        width = _label_width(value, 1) + 20
        canvas.rectangle(tag_x, 65, width, 30, PALETTE["surface"])
        canvas.rectangle(tag_x, 65, width, 2, color)
        canvas.text(value, tag_x + 10, 76, 1, color)
        tag_x += width + 10

    canvas.rectangle(64, 170, 1152, 1, PALETTE["line"])
    canvas.text("ONE DIRECTION OF RESPONSIBILITY", 64, 195, 3, PALETTE["yellow"])
    canvas.text(
        "APPLICATION INTENT FLOWS THROUGH TYPED BOUNDARIES TO ONE RECEIVER WRITER",
        64,
        230,
        2,
        PALETTE["muted"],
    )

    nodes = (
        ("APPS", PALETTE["cyan"]),
        ("SDK", PALETTE["lime"]),
        ("BRIDGE", PALETTE["teal"]),
        ("KERNEL", PALETTE["yellow"]),
        ("RECEIVER", PALETTE["coral"]),
    )
    node_width = 190
    gap = 42
    start_x = 64
    for index, (label, color) in enumerate(nodes):
        x = start_x + index * (node_width + gap)
        canvas.rectangle(x, 285, node_width, 92, PALETTE["surface"])
        canvas.rectangle(x, 285, node_width, 4, color)
        _centered_text(canvas, label, x + node_width // 2, 319, 3, PALETTE["ink"])
        if index < len(nodes) - 1:
            canvas.rectangle(x + node_width, 329, gap - 8, 3, PALETTE["line"])
            canvas.rectangle(x + node_width + gap - 14, 323, 3, 15, color)
            canvas.rectangle(x + node_width + gap - 11, 326, 3, 9, color)
            canvas.rectangle(x + node_width + gap - 8, 329, 3, 3, color)

    canvas.rectangle(64, 425, 1152, 1, PALETTE["line"])
    metrics = (
        (str(candidates), "REVIEWED CANDIDATES", PALETTE["cyan"]),
        (str(subsystems), "ATLAS SUBSYSTEMS", PALETTE["lime"]),
        (str(verification_nodes), "VERIFICATION NODES", PALETTE["teal"]),
        (str(hardware_writes), "UNSUPERVISED WRITES", PALETTE["coral"]),
    )
    metric_width = 276
    for index, (value, label, color) in enumerate(metrics):
        x = 64 + index * 288
        canvas.text(value, x, 466, 6, color)
        canvas.text(label, x, 526, 2, PALETTE["muted"])
        if index < len(metrics) - 1:
            canvas.rectangle(x + metric_width, 456, 1, 104, PALETTE["line"])

    canvas.text("PUBLIC PRE-RELEASE", 64, 594, 2, PALETTE["yellow"])
    canvas.text("QUALIFICATION  ATLAS  RELEASE EVIDENCE", 750, 594, 2, PALETTE["muted"])
    return canvas.png()
