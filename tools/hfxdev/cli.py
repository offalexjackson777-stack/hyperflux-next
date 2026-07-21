# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .migration import capture_inventory, summary
from .model import ModelError
from .openrazer import write_imported_metadata
from .render import write_generated
from .testgraph import format_plan, load_test_catalog
from .verify import verify_all


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hfx", description="HyperFlux Next repository tooling")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="run repository verification")
    verify.add_argument("--all", action="store_true", required=True)

    generate = commands.add_parser("generate", help="regenerate canonical derived files")
    generate.add_argument("--check", action="store_true", help="reserved for compatibility; verify performs the check")

    test = commands.add_parser("test", help="inspect typed verification metadata")
    test_commands = test.add_subparsers(dest="test_command", required=True)
    plan = test_commands.add_parser("plan", help="show the dependency-ordered verification plan")
    plan.add_argument("--all", action="store_true", required=True)

    migration = commands.add_parser("migration", help="inspect or capture migration inputs")
    migration_commands = migration.add_subparsers(dest="migration_command", required=True)
    migration_commands.add_parser("summary", help="show reviewed migration progress")
    capture = migration_commands.add_parser("capture", help="capture an immutable git source inventory")
    capture.add_argument("--source", required=True)
    capture.add_argument("--path", required=True, type=Path)

    imports = commands.add_parser("import", help="transform pinned upstream metadata")
    imports.add_argument("upstream", choices=["openrazer"])
    imports.add_argument("--source", required=True, type=Path)
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    root = _root()
    try:
        if args.command == "verify":
            checks = verify_all(root)
            print("HyperFlux Next verification: PASS")
            for check in checks:
                print(f"  PASS  {check}")
            return 0
        if args.command == "generate":
            write_generated(root)
            print("Generated repository views are current.")
            return 0
        if args.command == "test":
            print(format_plan(load_test_catalog(root)))
            return 0
        if args.command == "import":
            destination = write_imported_metadata(root, args.source.resolve())
            print(f"Imported {args.upstream}: {destination.relative_to(root)}")
            return 0
        if args.migration_command == "summary":
            print(summary(root))
            return 0
        destination = capture_inventory(root, args.source, args.path.resolve())
        print(f"Captured {args.source}: {destination.relative_to(root)}")
        return 0
    except ModelError as error:
        print(f"hfx: {error}", file=sys.stderr)
        return 1
