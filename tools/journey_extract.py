#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hfxdev.journey import extract_journey_sources


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stream private project records into a review-only journey ledger."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-overlay", type=Path, required=True)
    parser.add_argument("sources", type=Path, nargs="+")
    arguments = parser.parse_args()
    manifest = extract_journey_sources(
        arguments.root,
        arguments.sources,
        arguments.output,
        arguments.private_overlay,
    )
    print(
        f"{manifest['status']}: {manifest['event_count']} private narrative events "
        f"from {len(manifest['sources'])} source files"
    )
    return 0 if manifest["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
