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
    RELEASE_EVIDENCE_MODES,
    container_invocation,
    run_container,
)
from .documentation_evidence import (
    DEFAULT_CHROMIUM,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    bind as bind_documentation_render,
    check_render as check_documentation_render,
    review as review_documentation_render,
    run_render as run_documentation_render,
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
from .distributions import TARGET_IDS as DISTRIBUTION_TARGET_IDS
from .github_sync import COMPONENTS as GITHUB_COMPONENTS
from .github_sync import apply as apply_github
from .github_sync import plan as plan_github
from .github_sync import verify as verify_github
from .knowledge import import_upstream_catalogs
from .openrazer import write_imported_metadata
from .owner_reports import CONFIRMATION_LABELS as OWNER_CONFIRMATION_LABELS
from .owner_reports import build_owner_record, write_owner_record
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
from .documentation_preview import RELEASE_EVIDENCE_ENVIRONMENT


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
    verify.add_argument(
        "--release-evidence",
        choices=RELEASE_EVIDENCE_MODES,
        help=(
            "how to check the committed documentation render record: structure "
            "(default, every pull request) or required (release pull requests, which "
            "must carry a render of these exact documents)"
        ),
    )

    docs = commands.add_parser(
        "docs", help="render documentation and manage the release render record"
    )
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    docs_render = docs_commands.add_parser(
        "render",
        help="render every cataloged document offline and fail on any layout or network issue",
    )
    docs_render.add_argument("--output", required=True, type=Path, help="new directory")
    docs_render.add_argument(
        "--changed-from",
        metavar="REVISION",
        help="skip the render when no render input changed since this revision",
    )
    docs_render.add_argument("--chromium", default=DEFAULT_CHROMIUM)
    docs_render.add_argument("--node-modules", type=Path)
    docs_render.add_argument(
        "--timeout", type=int, default=DEFAULT_RENDER_TIMEOUT_SECONDS, help="seconds"
    )
    docs_check = docs_commands.add_parser(
        "check-render", help="prove a render matches this tree and found no issue"
    )
    docs_check.add_argument("--render", required=True, type=Path)
    docs_bind = docs_commands.add_parser(
        "bind",
        help="record a checked render as this release's documentation render evidence",
    )
    docs_bind.add_argument("--render", required=True, type=Path)
    docs_bind.add_argument("--revision", help="source revision (default: HEAD)")
    docs_review = docs_commands.add_parser(
        "review",
        help="list changed critical screenshots and record the ones you have looked at",
    )
    docs_review.add_argument("--render", required=True, type=Path)
    docs_review.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="screenshots/FILE.png",
        help="a screenshot you have looked at; repeat for each one",
    )

    generate = commands.add_parser("generate", help="regenerate canonical derived files")
    generate.add_argument("--check", action="store_true", help="reserved for compatibility; verify performs the check")

    coverage = commands.add_parser("coverage", help="maintain the Rust coverage ratchet")
    coverage_commands = coverage.add_subparsers(dest="coverage_command", required=True)
    coverage_tighten = coverage_commands.add_parser(
        "tighten",
        help="lower the committed not-covered limits to a measured llvm-cov summary",
    )
    coverage_tighten.add_argument(
        "--report",
        type=Path,
        help=(
            "llvm-cov JSON summary to read, for example from the "
            "hyperflux-full-evidence CI artifact (default: build/rust-coverage-current.json)"
        ),
    )
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

    qualification = commands.add_parser(
        "qualification", help="accept hardware reports from device owners"
    )
    qualification_commands = qualification.add_subparsers(
        dest="qualification_command", required=True
    )
    qualification_ingest = qualification_commands.add_parser(
        "ingest",
        help="check one owner hardware report and write its public record",
    )
    qualification_ingest.add_argument(
        "report",
        type=Path,
        help="saved device support issue body, or the raw report JSON; - reads standard input",
    )
    qualification_ingest.add_argument("--issue", required=True, type=int)
    qualification_ingest.add_argument(
        "--device", help="device-N from the report, when it shows more than one candidate"
    )
    qualification_ingest.add_argument(
        "--confirmed",
        action="append",
        choices=sorted(OWNER_CONFIRMATION_LABELS),
        help="what the owner checked by hand; only for a raw report, repeat as needed",
    )
    qualification_ingest.add_argument("--output-dir", type=Path)
    qualification_ingest.add_argument(
        "--check", action="store_true", help="check the report and print the record without writing it"
    )

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

    kernel_canary = commands.add_parser(
        "kernel-canary",
        help="build the kernel drivers against newer kernel headers (never gates merges)",
    )
    kernel_canary.add_argument("--output", required=True, type=Path)
    kernel_canary.add_argument(
        "--header-dir",
        action="append",
        default=[],
        type=Path,
        help="prepared kernel build directory; repeat for each kernel",
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
    ci_verify.add_argument("--release-evidence", choices=RELEASE_EVIDENCE_MODES)
    ci_docs_prepare = ci_commands.add_parser(
        "docs-prepare",
        help="install the locked documentation renderer packages with bounded network access",
    )
    ci_docs_prepare.add_argument("--image", required=True)
    ci_docs_render = ci_commands.add_parser(
        "docs-render",
        help="render the documentation offline in the pinned container",
    )
    ci_docs_render.add_argument("--image", required=True)
    ci_docs_render.add_argument("--output", required=True, type=Path)
    ci_docs_render.add_argument("--changed-from")
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
    ci_package = ci_commands.add_parser(
        "package",
        help="build source-bound package artifacts offline in the pinned container",
    )
    ci_package.add_argument("--image", required=True)
    ci_package.add_argument("--output", required=True, type=Path)
    ci_package_artifacts = ci_commands.add_parser(
        "package-artifacts", help=argparse.SUPPRESS
    )
    ci_package_artifacts.add_argument("--output", required=True, type=Path)
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
    package_distro.add_argument(
        "--distribution", required=True, choices=list(DISTRIBUTION_TARGET_IDS)
    )
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
        help="qualify a future split release such as 2.1.0-1 (commit recipes only)",
    )

    package_aur_install = package_commands.add_parser(
        "aur-install", help="print the exact AUR install commands for Core and Full"
    )
    package_aur_install.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="print the plan for a future split release such as 2.1.0-1",
    )

    package_compare = package_commands.add_parser(
        "compare-channels",
        help="compare signed-repository and AUR packages of one split release",
    )
    package_compare.add_argument("--repository", required=True, type=Path)
    package_compare.add_argument("--aur", required=True, type=Path)
    package_compare.add_argument("--aur-recipe", type=Path)
    package_compare.add_argument("--repository-commit")
    package_compare.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="compare a future split release such as 2.1.0-1",
    )

    package_dossier = package_commands.add_parser(
        "aur-dossier",
        help="write the local official-Arch readiness dossier with its receipts",
    )
    package_dossier.add_argument("--output", required=True, type=Path)
    package_dossier.add_argument("--aur-recipe", type=Path)
    package_dossier.add_argument("--channel-parity", type=Path, metavar="REPORT")
    package_dossier.add_argument(
        "--candidate-release",
        metavar="VERSION-RELEASE",
        help="the split release such as 2.1.0-1",
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
        help="check a future split release such as 2.1.0-1",
    )

    arch_transition = commands.add_parser(
        "arch-transition",
        help="prove the two-to-four output upgrade and render the optional AUR switch",
    )
    arch_transition_commands = arch_transition.add_subparsers(
        dest="arch_transition_command", required=True
    )
    for name, summary in (
        ("check", "fail unless every prior release upgrades in place to the split"),
        ("switch-script", "print the script that disables the repository for the AUR"),
        ("rollback-script", "print the script that re-enables the repository"),
    ):
        arch_transition_commands.add_parser(name, help=summary).add_argument(
            "--candidate-release",
            metavar="VERSION-RELEASE",
            help="the split release such as 2.1.0-1",
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
    arch_repository_build.add_argument(
        "--package",
        dest="packages",
        action="append",
        required=True,
        type=Path,
        help="one frozen release package; repeat for every package of the release",
    )
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


def _arch_transition(root: Path, args: argparse.Namespace) -> int:
    from .arch_repository import load_arch_repository_policy
    from .arch_transition import (
        PRIOR_RELEASES,
        SCHEMA,
        analyse_transition,
        check_pacman_safety,
        load_prior_topology,
        payload_items,
        rollback_script,
        switch_script,
    )

    split = _candidate_split(root, args.candidate_release)
    if not split.active:
        raise ModelError(f"the four-output topology starts at {split.first_release}")
    policy = load_arch_repository_policy(root)
    if args.arch_transition_command == "switch-script":
        print(switch_script(policy), end="")
        return 0
    if args.arch_transition_command == "rollback-script":
        print(rollback_script(policy, split), end="")
        return 0
    reports = [
        analyse_transition(split, load_prior_topology(root, prior)).as_json()
        for prior in PRIOR_RELEASES
    ]
    safety = check_pacman_safety(payload_items(root))
    passed = all(report["passed"] for report in reports) and not safety
    value = {
        "schema": SCHEMA,
        "candidate": split.full_version,
        "passed": passed,
        "transitions": reports,
        "pacman_safety_problems": list(safety),
    }
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0 if passed else 1


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


def _docs(root: Path, args: argparse.Namespace) -> int:
    if args.docs_command == "render":
        output = args.output if args.output.is_absolute() else Path.cwd() / args.output
        run_documentation_render(
            root,
            output,
            changed_from=args.changed_from,
            chromium=args.chromium,
            node_modules=args.node_modules.resolve() if args.node_modules else None,
            timeout_seconds=args.timeout,
        )
        print((output / "summary.md").read_text(encoding="utf-8"), end="")
        return 0
    if args.docs_command == "check-render":
        checked = check_documentation_render(root, args.render)
        counts = checked.result["counts"]
        print(
            f"Documentation render: PASS ({counts['documents']} documents, "
            f"{counts['checks']} checks, {counts['screenshots']} screenshots, 0 issues)"
        )
        return 0
    if args.docs_command == "bind":
        outcome = bind_documentation_render(root, args.render, args.revision)
        render = args.render.resolve()
        print(f"Bound {render} as the documentation render evidence.")
        print(f"Screenshots that changed: {len(outcome.changed_screenshots)}")
        for file in outcome.changed_screenshots:
            print(f"  {file}")
        return _report_pending_review(render, outcome.pending_review)
    remaining = review_documentation_render(root, args.render, tuple(args.confirm))
    if args.confirm:
        print(f"Recorded review of {len(args.confirm)} screenshot(s).")
    return _report_pending_review(args.render.resolve(), remaining)


def _report_pending_review(render: Path, pending: tuple[str, ...]) -> int:
    if not pending:
        print("Visual review: complete; every critical screenshot matches its review.")
        return 0
    print(f"Visual review: {len(pending)} critical screenshot(s) changed and need a person to look:")
    for file in pending:
        print(f"  {render / file}")
    print(
        "Open each image, then record it with: ./hfx docs review --render "
        f"{render} --confirm screenshots/NAME.png [--confirm ...]"
    )
    return 1


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    root = _root()
    try:
        if args.command == "verify":
            from .verify import RUNNERS
            from .toolchains import verify_current_toolchain

            lane = "fast" if args.fast else "full-software"
            if args.release_evidence is not None:
                os.environ[RELEASE_EVIDENCE_ENVIRONMENT] = args.release_evidence
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
                        release_evidence=args.release_evidence,
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
        if args.command == "docs":
            return _docs(root, args)
        if args.command == "generate":
            from .render import write_generated

            write_generated(root)
            print("Generated repository views are current.")
            return 0
        if args.command == "coverage":
            from .coverage import POLICY_PATH, tighten_rust_coverage_policy

            report = None if args.report is None else args.report.expanduser().resolve()
            result = tighten_rust_coverage_policy(root, report)
            print(result.table())
            if result.written:
                print(f"Rust coverage limits tightened: commit {POLICY_PATH}")
            else:
                print("Rust coverage limits are already tight; nothing was written.")
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
        if args.command == "qualification":
            try:
                submission = (
                    sys.stdin.read()
                    if str(args.report) == "-"
                    else args.report.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError) as error:
                raise ModelError(f"cannot read the report: {error}") from error
            record = build_owner_record(
                root,
                submission,
                issue=args.issue,
                device_id=args.device,
                confirmations=args.confirmed,
            )
            print(f"Owner report accepted: {record['model_name']} ({record['route_id']})")
            print(f"Confirmed: {', '.join(record['confirmations'])}")
            if args.check:
                print(json.dumps(record, indent=2))
                return 0
            destination = write_owner_record(root, record, args.output_dir)
            print(f"Public record: {destination}")
            print("Run ./hfx generate to update the supported-hardware page.")
            return 0
        if args.command == "upstream":
            prepared = prepare_upstreams(root, args.output)
            for identifier in prepared.fetched:
                print(f"Fetched or extended {identifier} source cache.")
            for identifier in prepared.reused:
                print(f"Verified existing {identifier} source cache.")
            print(f"Upstream lock: {prepared.manifest}")
            return 0
        if args.command == "kernel-canary":
            from .kernel_canary import run_kernel_canary

            result = run_kernel_canary(root, args.output, args.header_dir)
            print(f"Kernel canary: {'PASS' if result == 0 else 'FAIL'} ({args.output / 'summary.md'})")
            return result
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
            elif args.ci_command == "package-artifacts":
                if os.environ.get(PINNED_VERIFICATION_ENVIRONMENT) != "1":
                    raise ModelError("CI package artifacts require the pinned container")
                from .verify_integration import _openrazer_source, _openrgb_source

                manifest = build_artifacts(
                    root,
                    args.output,
                    capabilities={
                        "openrazer-source": _openrazer_source(root),
                        "openrgb-source": _openrgb_source(root),
                    },
                )
                print(f"Package build manifest: {manifest}")
                return 0
            elif args.ci_command == "package":
                invocation = container_invocation(
                    root,
                    image=args.image,
                    operation="package-artifacts",
                    output=args.output,
                )
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
                    release_evidence=args.release_evidence,
                )
            elif args.ci_command == "docs-prepare":
                invocation = container_invocation(
                    root, image=args.image, operation="docs-prepare"
                )
            elif args.ci_command == "docs-render":
                invocation = container_invocation(
                    root,
                    image=args.image,
                    operation="docs-render",
                    output=args.output,
                    changed_from=args.changed_from,
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
            if args.package_command == "compare-channels":
                from .channel_parity import compare_channels

                comparison = compare_channels(
                    _candidate_split(root, args.candidate_release),
                    repository=args.repository.resolve(),
                    aur=args.aur.resolve(),
                    aur_recipe=args.aur_recipe.resolve() if args.aur_recipe else None,
                    repository_commit=args.repository_commit,
                )
                print(comparison.as_json(), end="")
                return 0 if comparison.passed else 1
            if args.package_command == "aur-dossier":
                from .aur_dossier import build_dossier, write_dossier

                dossier = build_dossier(
                    root,
                    _candidate_split(root, args.candidate_release),
                    recipe_dir=args.aur_recipe.resolve() if args.aur_recipe else None,
                    channel_report=args.channel_parity.resolve() if args.channel_parity else None,
                )
                write_dossier(dossier, args.output.resolve())
                print(f"Dossier: {args.output.resolve() / 'DOSSIER.md'}")
                for status, count in dossier.summary().items():
                    print(f"{status}: {count}")
                return 0
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
        if args.command == "arch-transition":
            return _arch_transition(root, args)
        if args.command == "arch-repository":
            if args.arch_repository_command == "build":
                manifest = build_arch_repository(
                    root,
                    packages=tuple(package.resolve() for package in args.packages),
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
