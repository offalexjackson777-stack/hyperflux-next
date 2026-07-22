# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .ci import container_invocation, run_container
from .migration import capture_inventory, run_shadow_comparison, summary
from .model import ModelError
from .distribution_package import build_distribution_package
from .knowledge import import_upstream_catalogs
from .openrazer import write_imported_metadata
from .package_pipeline import build_artifacts, stage_rootfs
from .portal import build_portal, verify_portal
from .render import write_generated
from .testgraph import format_plan, load_test_catalog
from .upstreams import prepare_upstreams
from .verification_run import git_changed_paths, run_verification
from .verify import RUNNERS


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hfx", description="HyperFlux Next repository tooling")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="run repository verification")
    verify_lane = verify.add_mutually_exclusive_group(required=True)
    verify_lane.add_argument("--fast", action="store_true", help="run the fast software lane")
    verify_lane.add_argument("--full", action="store_true", help="run the complete software lane")
    verify_lane.add_argument(
        "--all",
        action="store_true",
        help="compatibility alias for the complete software lane",
    )
    verify.add_argument(
        "--changed-from",
        metavar="REVISION",
        help="select tests from changes since a Git revision; unknown paths fail closed",
    )
    verify.add_argument("--output", type=Path, help="empty directory for structured evidence")

    generate = commands.add_parser("generate", help="regenerate canonical derived files")
    generate.add_argument("--check", action="store_true", help="reserved for compatibility; verify performs the check")

    test = commands.add_parser("test", help="inspect typed verification metadata")
    test_commands = test.add_subparsers(dest="test_command", required=True)
    plan = test_commands.add_parser("plan", help="show the dependency-ordered verification plan")
    plan_lane = plan.add_mutually_exclusive_group(required=True)
    plan_lane.add_argument("--fast", action="store_true", help="plan the fast software lane")
    plan_lane.add_argument("--full", action="store_true", help="plan the complete software lane")
    plan_lane.add_argument(
        "--all",
        action="store_true",
        help="compatibility alias for the complete software lane",
    )
    plan.add_argument(
        "--changed-from",
        metavar="REVISION",
        help="select tests from changes since a Git revision; unknown paths fail closed",
    )

    migration = commands.add_parser("migration", help="inspect or capture migration inputs")
    migration_commands = migration.add_subparsers(dest="migration_command", required=True)
    migration_commands.add_parser("summary", help="show reviewed migration progress")
    capture = migration_commands.add_parser("capture", help="capture an immutable git source inventory")
    capture.add_argument("--source", required=True)
    capture.add_argument("--path", required=True, type=Path)
    compare = migration_commands.add_parser(
        "compare", help="compare frozen legacy decisions with the new simulator"
    )
    compare.add_argument("--fixture", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)

    imports = commands.add_parser("import", help="transform pinned upstream metadata")
    imports.add_argument("upstream", choices=["openrazer"])
    imports.add_argument("--source", required=True, type=Path)

    knowledge = commands.add_parser(
        "knowledge", help="manage provenance-bound device knowledge"
    )
    knowledge_commands = knowledge.add_subparsers(
        dest="knowledge_command", required=True
    )
    knowledge_import = knowledge_commands.add_parser(
        "import", help="rebuild normalized catalogs from exact upstream checkouts"
    )
    knowledge_import.add_argument("--openrazer-source", required=True, type=Path)
    knowledge_import.add_argument("--openrgb-source", required=True, type=Path)

    upstream = commands.add_parser("upstream", help="manage immutable upstream checkouts")
    upstream_commands = upstream.add_subparsers(dest="upstream_command", required=True)
    upstream_prepare = upstream_commands.add_parser(
        "prepare", help="fetch or verify every exact cataloged upstream commit"
    )
    upstream_prepare.add_argument(
        "--output",
        type=Path,
        help="checkout root; defaults to .hfx/upstreams",
    )

    docs = commands.add_parser("docs", help="build or verify the static documentation portal")
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    docs_build = docs_commands.add_parser("build", help="build a deterministic local portal")
    docs_build.add_argument("--output", required=True, type=Path)
    docs_verify = docs_commands.add_parser("verify", help="verify a built portal artifact")
    docs_verify.add_argument("--site", required=True, type=Path)

    ci = commands.add_parser("ci", help="run bounded repository jobs in the pinned container")
    ci_commands = ci.add_subparsers(dest="ci_command", required=True)
    ci_prepare = ci_commands.add_parser(
        "prepare", help="prepare exact upstream sources with bounded network access"
    )
    ci_prepare.add_argument("--image", required=True)
    ci_verify = ci_commands.add_parser(
        "verify", help="run a software verification lane without network or devices"
    )
    ci_verify.add_argument("--image", required=True)
    ci_verify.add_argument("--lane", required=True, choices=["fast", "full"])
    ci_verify.add_argument("--output", required=True, type=Path)
    ci_verify.add_argument("--changed-from")
    ci_docs = ci_commands.add_parser(
        "docs", help="build and verify the portal without network or devices"
    )
    ci_docs.add_argument("--image", required=True)
    ci_docs.add_argument("--output", required=True, type=Path)

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
            lane = "fast" if args.fast else "full-software"
            outcome = run_verification(
                root,
                load_test_catalog(root),
                RUNNERS,
                lane=lane,
                output=args.output,
                changed_from=args.changed_from,
            )
            print(f"HyperFlux Next verification: {outcome.status.upper()}")
            for check in outcome.passed_titles:
                print(f"  PASS  {check}")
            for node_id in outcome.failed_nodes:
                print(f"  FAIL  {node_id}")
            print(f"Evidence: {outcome.output / 'evidence.json'}")
            return 0 if outcome.status == "passed" else 1
        if args.command == "generate":
            write_generated(root)
            print("Generated repository views are current.")
            return 0
        if args.command == "test":
            lane = "fast" if args.fast else "full-software"
            changed_paths = None
            if args.changed_from is not None:
                _, changed_paths = git_changed_paths(root, args.changed_from)
            print(
                format_plan(
                    load_test_catalog(root),
                    lane=lane,
                    changed_paths=changed_paths,
                )
            )
            return 0
        if args.command == "import":
            destination = write_imported_metadata(root, args.source.resolve())
            print(f"Imported {args.upstream}: {destination.relative_to(root)}")
            return 0
        if args.command == "knowledge":
            paths = import_upstream_catalogs(
                root,
                args.openrazer_source,
                args.openrgb_source,
            )
            for path in paths:
                print(f"Generated {path.relative_to(root)}")
            return 0
        if args.command == "upstream":
            prepared = prepare_upstreams(root, args.output)
            for identifier in prepared.fetched:
                print(f"Fetched {identifier} at its catalog commit.")
            for identifier in prepared.reused:
                print(f"Verified existing {identifier} checkout.")
            print(f"Upstream lock: {prepared.manifest}")
            return 0
        if args.command == "docs":
            if args.docs_command == "build":
                portal = build_portal(root, args.output)
                print(f"Documentation portal: {portal.output}")
                print(f"Pages: {portal.pages} | Files: {portal.files}")
                print(f"Manifest: {portal.manifest}")
                return 0
            result = verify_portal(root, args.site)
            print(
                "Documentation portal verified: "
                f"{result['pages']} pages, {len(result['files'])} files"
            )
            return 0
        if args.command == "ci":
            if args.ci_command == "prepare":
                invocation = container_invocation(
                    root, image=args.image, operation="prepare"
                )
            elif args.ci_command == "verify":
                invocation = container_invocation(
                    root,
                    image=args.image,
                    operation="verify",
                    lane=args.lane,
                    output=args.output,
                    changed_from=args.changed_from,
                )
            else:
                invocation = container_invocation(
                    root,
                    image=args.image,
                    operation="docs",
                    output=args.output,
                )
            print(
                f"HyperFlux CI: {invocation.operation} "
                f"(container network: {invocation.network})"
            )
            return run_container(invocation)
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
        if args.migration_command == "compare":
            result = run_shadow_comparison(root, args.fixture, args.output)
            print(f"HyperFlux Next shadow comparison: {result.status.upper()}")
            print(f"Comparison: {result.comparison}")
            print(f"Evidence: {result.evidence}")
            return 0 if result.status == "matched" else 1
        destination = capture_inventory(root, args.source, args.path.resolve())
        print(f"Captured {args.source}: {destination.relative_to(root)}")
        return 0
    except ModelError as error:
        print(f"hfx: {error}", file=sys.stderr)
        return 1
