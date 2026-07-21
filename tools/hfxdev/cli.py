# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .migration import capture_inventory, summary
from .model import ModelError
from .distribution_package import build_distribution_package
from .openrazer import write_imported_metadata
from .package_pipeline import build_artifacts, stage_rootfs
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

    package = commands.add_parser("package", help="build and stage canonical package payloads")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    package_build = package_commands.add_parser(
        "build", help="build source-bound package artifacts"
    )
    package_build.add_argument("--output", required=True, type=Path)
    package_build.add_argument("--openrgb-source", type=Path)
    package_build.add_argument("--source-revision")
    package_stage = package_commands.add_parser(
        "stage", help="create one deterministic package root filesystem"
    )
    package_stage.add_argument("--build-manifest", required=True, type=Path)
    package_stage.add_argument("--root", required=True, type=Path)
    package_distro = package_commands.add_parser(
        "distro", help="build one native distribution package from the canonical root"
    )
    package_distro.add_argument(
        "--distribution", required=True, choices=["arch", "debian", "rpm"]
    )
    package_distro.add_argument("--build-manifest", required=True, type=Path)
    package_distro.add_argument("--output", required=True, type=Path)
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
        if args.command == "package":
            if args.package_command == "build":
                capabilities = {}
                if args.openrgb_source is not None:
                    capabilities["openrgb-source"] = args.openrgb_source.resolve()
                manifest = build_artifacts(
                    root,
                    args.output,
                    capabilities=capabilities,
                    revision=args.source_revision,
                )
                print(f"Package build manifest: {manifest}")
                return 0
            if args.package_command == "stage":
                result = stage_rootfs(root, args.build_manifest, args.root)
                print(f"Staged package root: {result.root}")
                print(f"Installed files: {result.file_count}")
                print(f"Payload SHA-256: {result.payload_sha256}")
                print(f"Inventory: {result.inventory}")
                return 0
            result = build_distribution_package(
                root,
                args.build_manifest,
                args.distribution,
                args.output,
            )
            print(f"Distribution package: {result.package}")
            print(f"Build manifest: {result.manifest}")
            print(f"Common payload SHA-256: {result.common_payload_sha256}")
            print(f"Distribution payload SHA-256: {result.distribution_payload_sha256}")
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
