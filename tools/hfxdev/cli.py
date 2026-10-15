# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time

from .actions_summary import write_actions_summary
from .arch_repository import build_arch_repository, verify_arch_repository
from .arch_repository_public import verify_public_arch_repository
from .ci import (
    PINNED_VERIFICATION_ENVIRONMENT,
    container_invocation,
    run_container,
)
from .ci_openrgb import run_external_openrgb_qualification
from .governance import load_github_governance
from .model import ModelError
from .native_command import write_native_command_catalog
from .native_control import write_native_method_inventory
from .native_direct_reference import run_staged_live_parity
from .native_live_parity import run_live_parity
from .native_onboarding import (
    build_draft_from_file,
    build_promotion_from_files,
    write_onboarding_draft,
    write_promotion_assessment,
)
from .native_parity import build_offline_parity_evidence, write_parity_evidence
from .native_probe import write_native_probe_catalog
from .distribution_package import build_distribution_package
from .github_sync import COMPONENTS as GITHUB_COMPONENTS
from .github_sync import apply as apply_github
from .github_sync import plan as plan_github
from .github_sync import verify as verify_github
from .knowledge import import_upstream_catalogs
from .openrazer import write_imported_metadata
from .openrazer_capability import write_userspace_capability_catalog
from .openrazer_native_route import (
    prepare_openrazer_native_source,
    write_native_route_catalog,
)
from .openrgb_native_route import prepare_openrgb_native_source
from .package_pipeline import build_artifacts, stage_rootfs
from .source_validated import write_source_validated_catalog
from .testgraph import format_plan, load_test_catalog
from .upstreams import prepare_upstreams
from .verification_run import git_changed_paths, run_verification


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
    verify.add_argument(
        "--host-toolchain",
        action="store_true",
        help="require the host toolchain instead of selecting the pinned container",
    )

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

    native = commands.add_parser(
        "native", help="inspect source-bound native receiver qualification evidence"
    )
    native_commands = native.add_subparsers(dest="native_command", required=True)
    parity = native_commands.add_parser(
        "parity", help="sanitize existing direct and receiver usbmon captures"
    )
    parity.add_argument("--profile", required=True)
    parity.add_argument("--direct-capture", required=True, type=Path)
    parity.add_argument("--receiver-capture", type=Path)
    parity.add_argument("--receiver-result", type=Path)
    live_parity = native_commands.add_parser(
        "live-parity",
        help="run one restoration-isolated read-only candidate parity session",
    )
    live_parity.add_argument("--profile", required=True)
    live_parity.add_argument("--direct-evidence", required=True, type=Path)
    live_parity.add_argument("--package", required=True, type=Path)
    live_parity.add_argument("--package-sha256", required=True)
    live_parity.add_argument("--expected-generation", required=True, type=int)
    live_parity.add_argument("--approval-text", required=True)
    live_parity.add_argument("--user", required=True)
    live_parity.add_argument("--timeout-seconds", type=int, default=90)
    staged_parity = native_commands.add_parser(
        "staged-parity",
        help="compare one exact device directly and through the receiver in one private session",
    )
    staged_parity.add_argument("--profile", required=True)
    staged_parity.add_argument("--package", required=True, type=Path)
    staged_parity.add_argument("--package-sha256", required=True)
    staged_parity.add_argument("--expected-generation", required=True, type=int)
    staged_parity.add_argument("--approval-text", required=True)
    staged_parity.add_argument("--user", required=True)
    staged_parity.add_argument("--timeout-seconds", type=int, default=90)
    staged_parity.add_argument("--transition-timeout-seconds", type=int, default=300)
    native_openrazer = native_commands.add_parser(
        "prepare-openrazer",
        help="prepare the source-bound OpenRazer native transport tree",
    )
    native_openrazer.add_argument("--source", required=True, type=Path)
    native_openrazer.add_argument("--output", required=True, type=Path)
    native_openrgb = native_commands.add_parser(
        "prepare-openrgb",
        help="prepare the source-bound OpenRGB native transport tree",
    )
    native_openrgb.add_argument("--source", required=True, type=Path)
    native_openrgb.add_argument("--output", required=True, type=Path)
    native_operational = native_commands.add_parser(
        "generate-operational-catalog",
        help="generate exact source-validated receiver device routes",
    )
    native_operational.add_argument("--openrazer-source", required=True, type=Path)
    native_operational.add_argument("--openrgb-source", required=True, type=Path)
    native_draft = native_commands.add_parser(
        "draft-profile",
        help="create a non-writable profile draft from a privacy-safe route observation",
    )
    native_draft.add_argument("--observation", required=True, type=Path)
    native_draft.add_argument("--output-dir", type=Path)
    native_promote = native_commands.add_parser(
        "assess-promotion",
        help="assess whether exact parity evidence permits a profile-only promotion",
    )
    native_promote.add_argument("--draft", required=True, type=Path)
    native_promote.add_argument("--parity", required=True, type=Path)
    native_promote.add_argument("--output-dir", type=Path)

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
    ci_verify.add_argument("--openrgb-qualification", type=Path)
    ci_openrgb = ci_commands.add_parser(
        "qualify-openrgb",
        help="qualify exact packaged OpenRGB bytes in a dedicated OCI sandbox",
    )
    ci_openrgb.add_argument("--image", required=True)
    ci_openrgb.add_argument("--output", required=True, type=Path)
    ci_openrgb_prepare = ci_commands.add_parser(
        "openrgb-prepare", help=argparse.SUPPRESS
    )
    ci_openrgb_prepare.add_argument("--output", required=True, type=Path)
    ci_summary = ci_commands.add_parser(
        "summary", help="write a bounded GitHub Actions summary from structured evidence"
    )
    ci_summary.add_argument("--result", required=True, type=Path)
    ci_summary.add_argument("--output", required=True, type=Path)
    ci_summary.add_argument("--source-revision")

    github = commands.add_parser(
        "github", help="plan, apply, or verify canonical public repository state"
    )
    github_commands = github.add_subparsers(dest="github_command", required=True)
    for name in ("plan", "apply", "verify"):
        command = github_commands.add_parser(name)
        command.add_argument(
            "--component",
            action="append",
            choices=GITHUB_COMPONENTS,
            help="limit synchronization to one component; repeat as needed",
        )
        command.add_argument("--output", type=Path)

    package = commands.add_parser("package", help="build and stage canonical package payloads")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    package_build = package_commands.add_parser(
        "build", help="build source-bound package artifacts"
    )
    package_build.add_argument("--output", required=True, type=Path)
    package_build.add_argument("--openrazer-source", required=True, type=Path)
    package_build.add_argument("--openrgb-source", type=Path)
    package_build.add_argument("--source-revision")
    package_stage = package_commands.add_parser(
        "stage", help="create one deterministic component root filesystem"
    )
    package_stage.add_argument("--build-manifest", required=True, type=Path)
    package_stage.add_argument("--root", required=True, type=Path)
    package_stage.add_argument(
        "--component", required=True, choices=["linux", "full"]
    )
    package_distro = package_commands.add_parser(
        "distro", help="build the native split package set from canonical roots"
    )
    package_distro.add_argument("--distribution", required=True, choices=["arch"])
    package_distro.add_argument("--build-manifest", required=True, type=Path)
    package_distro.add_argument("--output", required=True, type=Path)
    package_aur = package_commands.add_parser(
        "aur", help="render the source-built AUR recipe for one exact revision"
    )
    package_aur.add_argument("--output", required=True, type=Path)
    aur_reference = package_aur.add_mutually_exclusive_group(required=True)
    aur_reference.add_argument("--tag", help="signed annotated tag for a publication recipe")
    aur_reference.add_argument(
        "--commit", help="full commit for a local qualification recipe"
    )
    package_aur.add_argument(
        "--upstream-checkout",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="local Git checkout containing a pinned upstream commit (openrazer, openrgb)",
    )
    package_aur.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="qualify a future split release such as 2.0.1-2 (commit recipes only)",
    )

    package_aur_install = package_commands.add_parser(
        "aur-install", help="print the exact AUR install commands for Core and Full"
    )
    package_aur_install.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="print the plan for a future split release such as 2.0.1-2",
    )

    arch_snapshot = commands.add_parser(
        "arch-snapshot",
        help="import official Arch package state and check OpenRazer provider alignment",
    )
    arch_snapshot_commands = arch_snapshot.add_subparsers(
        dest="arch_snapshot_command", required=True
    )
    arch_snapshot_import = arch_snapshot_commands.add_parser(
        "import", help="record official repositories from local pacman sync databases"
    )
    arch_snapshot_import.add_argument("--candidate", required=True, metavar="VERSION-RELEASE")
    arch_snapshot_import.add_argument(
        "--database",
        action="append",
        default=[],
        metavar="REPOSITORY=PATH",
        help="sync database of extra, extra-testing or extra-staging; omitted ones are unobserved",
    )
    arch_snapshot_check = arch_snapshot_commands.add_parser(
        "check", help="fail unless the OpenRazer provider matches official Arch"
    )
    arch_snapshot_check.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="check a future split release such as 2.0.1-2",
    )

    arch_repository = commands.add_parser(
        "arch-repository",
        help="build or verify the signed third-party Arch repository",
    )
    arch_repository_commands = arch_repository.add_subparsers(
        dest="arch_repository_command", required=True
    )
    arch_repository_build = arch_repository_commands.add_parser(
        "build", help="sign exact frozen packages and create repository metadata"
    )
    arch_repository_build.add_argument("--core", required=True, type=Path)
    arch_repository_build.add_argument("--full", required=True, type=Path)
    arch_repository_build.add_argument("--gnupg-home", required=True, type=Path)
    arch_repository_build.add_argument("--key-fingerprint", required=True)
    arch_repository_build.add_argument("--output", required=True, type=Path)
    arch_repository_verify = arch_repository_commands.add_parser(
        "verify", help="verify one complete signed repository asset directory"
    )
    arch_repository_verify.add_argument("--repository", required=True, type=Path)
    arch_repository_verify_public = arch_repository_commands.add_parser(
        "verify-public",
        help="prove the anonymous HTTPS repository with an isolated Pacman state",
    )
    arch_repository_verify_public.add_argument("--output", required=True, type=Path)
    return parser


def _candidate_split(root: Path, value: str | None):
    from .arch_split import load_arch_split

    if value is None:
        return load_arch_split(root)
    version, separator, release = value.rpartition("-")
    if not separator or not version or not release.isdecimal():
        raise ModelError("candidate release must be VERSION-RELEASE")
    return load_arch_split(root, release=(version, int(release)))


def _arch_snapshot(root: Path, args: argparse.Namespace) -> int:
    from .arch_official_snapshot import (
        evaluate_provider_alignment,
        import_snapshot,
        write_snapshot,
    )

    if args.arch_snapshot_command == "import":
        databases: dict[str, Path] = {}
        for item in args.database:
            name, separator, value = item.partition("=")
            if not separator or not value or name in databases:
                raise ModelError("--database must be one unique REPOSITORY=PATH")
            databases[name] = Path(value).expanduser().resolve()
        split = _candidate_split(root, args.candidate)
        value = import_snapshot(root, databases, candidate=args.candidate, split=split)
        print(f"Official Arch snapshot: {write_snapshot(root, value)}")
        for repository in value["repositories"]:
            print(f"{repository['name']}: {repository['state']}")
        return 0
    alignment = evaluate_provider_alignment(
        root, split=_candidate_split(root, args.candidate_release)
    )
    print(alignment.as_json(), end="")
    return 0 if alignment.passed else 1


def _package_aur(root: Path, args: argparse.Namespace) -> int:
    from .aur_recipe import generate_aur_recipe, write_aur_recipe

    checkouts: dict[str, Path] = {}
    for item in args.upstream_checkout:
        name, separator, value = item.partition("=")
        if not separator or not value or name in checkouts:
            raise ModelError("--upstream-checkout must be one unique ID=PATH")
        checkouts[name] = Path(value).expanduser().resolve()
    if args.candidate_release is not None and args.tag is not None:
        raise ModelError("a publication recipe uses the current release, not a candidate")
    split = _candidate_split(root, args.candidate_release)
    recipe = generate_aur_recipe(
        root,
        tag=args.tag,
        commit=args.commit,
        upstream_checkouts=checkouts,
        split=split,
    )
    for path in write_aur_recipe(recipe, args.output):
        print(f"AUR recipe file: {path}")
    print(f"Recipe mode: {recipe.mode}")
    print(f"Package version: {recipe.version}")
    print(f"Source commit: {recipe.source.commit}")
    for disclosure in recipe.disclosures:
        print(f"Qualification only: {disclosure}")
    return 0


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    root = _root()
    try:
        if args.command == "verify":
            from .verify import RUNNERS
            from .toolchains import verify_current_toolchain

            lane = "fast" if args.fast else "full-software"
            running_pinned = os.environ.get(PINNED_VERIFICATION_ENVIRONMENT) == "1"
            if not args.host_toolchain and not running_pinned:
                try:
                    verify_current_toolchain(root)
                except ModelError:
                    output = args.output
                    if output is None:
                        output = Path("build") / "verification" / (
                            f"pinned-{os.getpid()}-{time.time_ns()}"
                        )
                    elif output.is_absolute():
                        try:
                            output = output.resolve().relative_to(root.resolve())
                        except ValueError as error:
                            raise ModelError(
                                "container verification output must stay below the repository"
                            ) from error
                    changed_from = None
                    if args.changed_from is not None:
                        changed_from, _ = git_changed_paths(root, args.changed_from)
                    invocation = container_invocation(
                        root,
                        image=load_github_governance(root).development_image,
                        operation="verify",
                        lane="fast" if args.fast else "full",
                        output=output,
                        changed_from=changed_from,
                    )
                    print(
                        "HyperFlux Next verification environment: pinned container "
                        "(host package versions are independent)",
                        flush=True,
                    )
                    return run_container(invocation)
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
            from .render import write_generated

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
            native_destination = write_native_method_inventory(
                root, args.source.resolve()
            )
            print(
                "Imported OpenRazer native control: "
                f"{native_destination.relative_to(root)}"
            )
            probe_destination = write_native_probe_catalog(
                root, args.source.resolve()
            )
            print(
                "Imported OpenRazer native probes: "
                f"{probe_destination.relative_to(root)}"
            )
            command_destination = write_native_command_catalog(
                root, args.source.resolve()
            )
            print(
                "Imported OpenRazer native command policy: "
                f"{command_destination.relative_to(root)}"
            )
            route_destination = write_native_route_catalog(
                root, args.source.resolve()
            )
            print(
                "Imported OpenRazer native routes: "
                f"{route_destination.relative_to(root)}"
            )
            capability_destination = write_userspace_capability_catalog(
                root, args.source.resolve()
            )
            print(
                "Imported OpenRazer userspace capabilities: "
                f"{capability_destination.relative_to(root)}"
            )
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
        if args.command == "native":
            if args.native_command == "parity":
                evidence = build_offline_parity_evidence(
                    root,
                    profile_id=args.profile,
                    direct_capture=args.direct_capture,
                    receiver_capture=args.receiver_capture,
                    receiver_result=args.receiver_result,
                )
                destination = write_parity_evidence(evidence)
                coverage = evidence["coverage"]
                print(f"Native transport parity: {evidence['conclusion']}")
                print(
                    "Coverage: "
                    f"{len(coverage['passed'])} passed, "
                    f"{len(coverage['missing'])} incomplete, "
                    f"{len(coverage['failed'])} failed"
                )
                print(f"Privacy-safe evidence: {destination}")
                return 0
            if args.native_command == "live-parity":
                result = run_live_parity(
                    root,
                    profile_id=args.profile,
                    direct_evidence_path=args.direct_evidence,
                    package=args.package,
                    package_sha256=args.package_sha256,
                    expected_generation=args.expected_generation,
                    approval_text=args.approval_text,
                    user=args.user,
                    timeout_seconds=args.timeout_seconds,
                )
                print(f"Native transport parity: {result.conclusion}")
                print(
                    "Coverage: "
                    f"{len(result.passed)} passed, "
                    f"{len(result.missing)} incomplete, "
                    f"{len(result.failed)} failed"
                )
                print(f"Privacy-safe evidence: {result.evidence_path}")
                print(f"Source-bound run: {result.run_path}")
                return 0
            if args.native_command == "staged-parity":
                result = run_staged_live_parity(
                    root,
                    profile_id=args.profile,
                    package=args.package,
                    package_sha256=args.package_sha256,
                    expected_generation=args.expected_generation,
                    approval_text=args.approval_text,
                    user=args.user,
                    timeout_seconds=args.timeout_seconds,
                    transition_timeout_seconds=args.transition_timeout_seconds,
                )
                print(f"Native transport parity: {result.conclusion}")
                print(
                    "Coverage: "
                    f"{len(result.passed)} passed, "
                    f"{len(result.missing)} incomplete, "
                    f"{len(result.failed)} failed"
                )
                print(f"Privacy-safe evidence: {result.evidence_path}")
                print(f"Source-bound run: {result.run_path}")
                return 0
            if args.native_command == "prepare-openrazer":
                prepared = prepare_openrazer_native_source(
                    root, args.source, args.output
                )
                print(f"Prepared OpenRazer source: {prepared.root}")
                print(f"Source binding: {prepared.result}")
                return 0
            if args.native_command == "prepare-openrgb":
                prepared = prepare_openrgb_native_source(
                    root, args.source, args.output
                )
                print(f"Prepared OpenRGB source: {prepared.root}")
                print(f"Source binding: {prepared.result}")
                return 0
            if args.native_command == "generate-operational-catalog":
                destination = write_source_validated_catalog(
                    root,
                    args.openrazer_source,
                    args.openrgb_source,
                )
                print(f"Source-validated device catalog: {destination}")
                return 0
            timestamp = (
                datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            if args.native_command == "draft-profile":
                draft = build_draft_from_file(
                    root,
                    args.observation,
                    created_at=timestamp,
                )
                destination = write_onboarding_draft(
                    draft,
                    cache_root=args.output_dir,
                )
                print(f"Native onboarding: {draft['assessment']['state']}")
                print(f"Candidate profile: {draft['candidate_profile']['profile_id']}")
                print("Write authority: not granted")
                print(f"Privacy-safe draft: {destination}")
                return 0
            if args.native_command == "assess-promotion":
                assessment = build_promotion_from_files(
                    root,
                    args.draft,
                    args.parity,
                    created_at=timestamp,
                )
                destination = write_promotion_assessment(
                    assessment,
                    cache_root=args.output_dir,
                )
                print(f"Native promotion: {assessment['state']}")
                print("Write authority: not granted automatically")
                print(f"Privacy-safe assessment: {destination}")
                return 0
        if args.command == "upstream":
            prepared = prepare_upstreams(root, args.output)
            for identifier in prepared.fetched:
                print(f"Fetched or extended {identifier} source cache.")
            for identifier in prepared.reused:
                print(f"Verified existing {identifier} source cache.")
            print(f"Upstream lock: {prepared.manifest}")
            return 0
        if args.command == "ci":
            if args.ci_command == "summary":
                write_actions_summary(
                    root,
                    args.result,
                    args.output,
                    expected_revision=args.source_revision,
                )
                print(f"Actions summary: {args.output}")
                return 0
            if args.ci_command == "prepare":
                invocation = container_invocation(
                    root, image=args.image, operation="prepare"
                )
            elif args.ci_command == "openrgb-prepare":
                if os.environ.get(PINNED_VERIFICATION_ENVIRONMENT) != "1":
                    raise ModelError(
                        "OpenRGB qualification preparation requires the pinned container"
                    )
                from .verify_packaging import prepare_external_openrgb_qualification

                request = prepare_external_openrgb_qualification(root, args.output)
                print(f"OpenRGB qualification request: {request}")
                return 0
            elif args.ci_command == "qualify-openrgb":
                result = run_external_openrgb_qualification(
                    root,
                    image=args.image,
                    output=args.output,
                )
                if result == 0:
                    print(
                        "External packaged OpenRGB qualification: PASS "
                        f"({args.output / 'binding.json'})"
                    )
                return result
            elif args.ci_command == "verify":
                invocation = container_invocation(
                    root,
                    image=args.image,
                    operation="verify",
                    lane=args.lane,
                    output=args.output,
                    changed_from=args.changed_from,
                    openrgb_qualification=args.openrgb_qualification,
                )
            print(
                f"HyperFlux CI: {invocation.operation} "
                f"(container network: {invocation.network})"
            )
            return run_container(invocation)
        if args.command == "github":
            operation = {
                "plan": plan_github,
                "apply": apply_github,
                "verify": verify_github,
            }[args.github_command]
            result = operation(root, args.component)
            rendered = result.as_json()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"GitHub {args.github_command}: {args.output}")
            else:
                print(rendered, end="")
            return 0
        if args.command == "package":
            if args.package_command == "build":
                capabilities = {}
                capabilities["openrazer-source"] = args.openrazer_source.resolve()
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
                result = stage_rootfs(
                    root, args.build_manifest, args.root, args.component
                )
                print(f"Staged package root: {result.root}")
                print(f"Distribution component: {result.distribution_component}")
                print(f"Installed files: {result.file_count}")
                print(f"Payload SHA-256: {result.payload_sha256}")
                print(f"Inventory: {result.inventory}")
                return 0
            if args.package_command == "aur":
                return _package_aur(root, args)
            if args.package_command == "aur-install":
                from .aur_install import render_guide

                print(render_guide(_candidate_split(root, args.candidate_release)), end="")
                return 0
            result = build_distribution_package(
                root,
                args.build_manifest,
                args.distribution,
                args.output,
            )
            for package in result.packages:
                print(
                    f"Distribution package ({package.component}): {package.package}"
                )
            print(f"Build manifest: {result.manifest}")
            return 0
        if args.command == "arch-snapshot":
            return _arch_snapshot(root, args)
        if args.command == "arch-repository":
            if args.arch_repository_command == "build":
                manifest = build_arch_repository(
                    root,
                    core_package=args.core.resolve(),
                    full_package=args.full.resolve(),
                    gnupg_home=args.gnupg_home.resolve(),
                    key_fingerprint=args.key_fingerprint,
                    output=args.output.resolve(),
                )
                print(f"Signed Arch repository manifest: {manifest}")
                return 0
            if args.arch_repository_command == "verify":
                result = verify_arch_repository(root, args.repository.resolve())
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            receipt = verify_public_arch_repository(root, args.output.resolve())
            print(f"Public Arch repository receipt: {receipt}")
            return 0
    except ModelError as error:
        print(f"hfx: {error}", file=sys.stderr)
        return 1
