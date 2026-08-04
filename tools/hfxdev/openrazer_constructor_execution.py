# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from .model import ModelError
from .native_probe import c_function_source, simple_c_defines
from .openrazer_constructor_parsing import (
    REPORT_BYTES,
    SAFE_EXPRESSION,
    _transitive_constructor_functions,
)


def _constructor_harness(
    source_root: Path,
    expressions: list[str],
) -> str:
    chroma_source = (source_root / "driver" / "razerchromacommon.c").read_text(
        encoding="utf-8"
    )
    common_source = (source_root / "driver" / "razercommon.c").read_text(
        encoding="utf-8"
    )
    common_header = (source_root / "driver" / "razercommon.h").read_text(
        encoding="utf-8"
    )
    roots = {
        match.group(1)
        for expression in expressions
        if (
            match := re.match(
                r"(razer_chroma_[a-z0-9_]+|get_razer_report)\s*\(", expression
            )
        )
    }
    functions = _transitive_constructor_functions(
        chroma_source, {root for root in roots if root.startswith("razer_chroma_")}
    )
    function_sources = [c_function_source(chroma_source, name)[0] for name in functions]
    helper_sources = [c_function_source(common_source, "get_razer_report")[0]]
    helper_sources.extend(
        [
        c_function_source(common_source, name)[0]
        for name in ("clamp_u8", "clamp_u16")
        if any(re.search(rf"\b{re.escape(name)}\s*\(", source) for source in function_sources)
        ]
    )
    constants = simple_c_defines(common_header)
    constants.update(simple_c_defines(chroma_source))
    required_constants = sorted(
        {
            token
            for text in [*expressions, *function_sources]
            for token in re.findall(r"\b[A-Z][A-Z0-9_]*\b", text)
            if token in constants
        }
    )
    constant_lines = "\n".join(
        f"#define {name} {constants[name]}" for name in required_constants
    )
    emit_lines: list[str] = []
    for expression in expressions:
        if SAFE_EXPRESSION.fullmatch(expression) is None:
            raise ModelError(f"unsafe OpenRazer constructor witness: {expression}")
        emit_lines.append(
            "    report = "
            + expression
            + ";\n"
            "    for(size_t index = 0; index < sizeof(report); ++index)\n"
            '        printf("%02x", ((const unsigned char*)&report)[index]);\n'
            "    putchar('\\n');"
        )
    return f"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t __be16;
union transaction_id_union {{
    u8 id;
    struct {{ u8 device : 3; u8 id : 5; }} parts;
}};
union command_id_union {{
    u8 id;
    struct {{ u8 direction : 1; u8 id : 7; }} parts;
}};
struct razer_report {{
    u8 status;
    union transaction_id_union transaction_id;
    __be16 remaining_packets;
    u8 protocol_type;
    u8 data_size;
    u8 command_class;
    union command_id_union command_id;
    u8 arguments[80];
    u8 crc;
    u8 reserved;
}};
struct razer_rgb {{ u8 r; u8 g; u8 b; }};
_Static_assert(sizeof(struct razer_report) == 90, "unexpected report layout");
{constant_lines}
#define KERN_ALERT ""
#define printk(...) ((void)0)

{os.linesep.join(helper_sources)}
{os.linesep.join(function_sources)}

int main(void)
{{
    struct razer_report report;
    unsigned char buf[80] = {{0, 0, 0, 0x11, 0x22, 0x33}};
    unsigned short dpi[10] = {{800, 800, 1600, 1600}};
    struct razer_rgb rgb = {{0x11, 0x22, 0x33}};
    unsigned char brightness = 127;
    unsigned char enabled = 1;
    unsigned char mode = 3;
    unsigned char param = 0;
    unsigned char direction = 1;
    unsigned char speed = 1;
    unsigned char row_id = 0;
    unsigned char row_index = 0;
    unsigned char start_col = 0;
    unsigned char stop_col = 0;
    unsigned char stages_count = 1;
    unsigned char active_stage = 1;
    unsigned char threshold = 25;
    unsigned char led_id = 0;
    unsigned int scroll_mode = 1;
    unsigned short dpi_x = 800;
    unsigned short dpi_y = 800;
    unsigned short polling_rate = 1000;
    unsigned short idle_time = 60;
    size_t offset = 3;
    bool acceleration = true;
    bool smart_reel = true;
    (void)buf; (void)dpi; (void)rgb; (void)brightness; (void)enabled;
    (void)mode; (void)param; (void)direction; (void)speed; (void)row_id;
    (void)row_index; (void)start_col; (void)stop_col; (void)stages_count;
    (void)active_stage; (void)threshold; (void)led_id; (void)dpi_x;
    (void)dpi_y; (void)polling_rate; (void)idle_time; (void)acceleration;
    (void)smart_reel; (void)scroll_mode; (void)offset;
{os.linesep.join(emit_lines)}
    return 0;
}}
"""


@lru_cache(maxsize=1024)
def _compile_constructor_reports_cached(
    root_text: str,
    source_root_text: str,
    source_digest: str,
    compiler: str,
    expressions: tuple[str, ...],
) -> tuple[bytes, ...]:
    del source_digest
    root = Path(root_text)
    source_root = Path(source_root_text)
    harness = _constructor_harness(source_root, list(expressions))
    scratch_root = root / "build"
    scratch_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="hyperflux-openrazer-write-", dir=scratch_root) as directory:
        executable = Path(directory) / "witness"
        try:
            compiled = subprocess.run(
                [
                    compiler,
                    "-x",
                    "c",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-Wno-unused-parameter",
                    "-o",
                    str(executable),
                    "-",
                ],
                input=harness,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelError(f"cannot compile OpenRazer write constructors: {error}") from error
        if compiled.returncode != 0:
            raise ModelError(
                "pinned OpenRazer write constructor compilation failed: "
                + compiled.stderr.strip()
            )
        rendered = subprocess.run(
            [str(executable)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    if rendered.returncode != 0 or rendered.stderr:
        raise ModelError("pinned OpenRazer write constructor harness failed")
    lines = rendered.stdout.splitlines()
    if len(lines) != len(expressions):
        raise ModelError("pinned OpenRazer write constructor output is incomplete")
    reports: list[bytes] = []
    for expression, line in zip(expressions, lines, strict=True):
        try:
            report = bytes.fromhex(line)
        except ValueError as error:
            raise ModelError("OpenRazer write constructor emitted invalid hex") from error
        if len(report) != REPORT_BYTES:
            raise ModelError("OpenRazer write constructor emitted a non-90-byte report")
        reports.append(report)
    return tuple(reports)


def _compile_constructor_reports(
    root: Path,
    source_root: Path,
    expressions: list[str],
) -> dict[str, bytes]:
    if not expressions:
        raise ModelError("OpenRazer constructor witness set is empty")
    normalized = tuple(expressions)
    digest = hashlib.sha256()
    for relative in (
        "driver/razerchromacommon.c",
        "driver/razercommon.c",
        "driver/razercommon.h",
    ):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((source_root / relative).read_bytes())
        digest.update(b"\0")
    reports = _compile_constructor_reports_cached(
        str(root.resolve()),
        str(source_root.resolve()),
        digest.hexdigest(),
        os.environ.get("CC", "cc"),
        normalized,
    )
    return dict(zip(normalized, reports, strict=True))
