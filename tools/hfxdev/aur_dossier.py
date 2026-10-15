# SPDX-License-Identifier: GPL-2.0-only

"""Local, review-ready dossier for a possible official Arch adoption.

The dossier states, for every requirement an Arch Package Maintainer would
check, what the project can show today. Each claim is computed from the
repository's own authorities at generation time, is backed by a retained
receipt written next to the dossier, and cites the official package snapshot
it was evaluated against. A claim that cannot be evidenced locally says so
rather than borrowing confidence from a neighbouring claim.

Producing the dossier contacts nobody. Publication to the AUR and any
approach to an Arch Package Maintainer stay external decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .arch_official_snapshot import evaluate_provider_alignment
from .arch_split import ArchSplit, relation_name
from .arch_transition import (
    PRIOR_RELEASES,
    analyse_transition,
    check_pacman_safety,
    load_prior_topology,
    payload_items,
)
from .aur_install import install_plans
from .aur_recipe import RECIPE_FILES
from .channel_parity import SCHEMA as CHANNEL_SCHEMA
from .channel_parity import recipe_source
from .model import ModelError


SCHEMA = "hyperflux-arch-adoption-dossier-v1"
SNAPSHOT_PATH = "packaging/arch-official-snapshot.json"
SNAPSHOT_RECEIPT = "arch-official-snapshot.json"
STATUSES = {
    "met": "evidenced by the cited receipts",
    "static-only": "statically proven; the physical receipt is missing",
    "not-met": "checked, and the check fails",
    "not-evidenced": "no receipt exists for this requirement yet",
    "external": "outside the project's control; nothing was done",
}


@dataclass(frozen=True)
class Claim:
    id: str
    requirement: str
    status: str
    statement: str
    gaps: tuple[str, ...]
    sources: tuple[str, ...]
    receipts: tuple[str, ...]


@dataclass(frozen=True)
class Dossier:
    candidate: str
    snapshot_sha256: str
    sources: dict[str, str]
    receipts: dict[str, bytes]
    claims: tuple[Claim, ...]

    def summary(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for claim in self.claims:
            counts[claim.status] += 1
        return counts

    def as_json(self) -> str:
        value = {
            "schema": SCHEMA,
            "candidate": self.candidate,
            "maintainer_contact": "none",
            "snapshot": {"path": SNAPSHOT_PATH, "sha256": self.snapshot_sha256},
            "summary": self.summary(),
            "sources": dict(sorted(self.sources.items())),
            "receipts": {
                name: hashlib.sha256(content).hexdigest()
                for name, content in sorted(self.receipts.items())
            },
            "claims": [
                {
                    "id": claim.id,
                    "requirement": claim.requirement,
                    "status": claim.status,
                    "statement": claim.statement,
                    "gaps": list(claim.gaps),
                    "sources": list(claim.sources),
                    "receipts": list(claim.receipts),
                }
                for claim in self.claims
            ],
        }
        return json.dumps(value, indent=2) + "\n"


def _receipt(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


class _Builder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources: dict[str, str] = {}
        self.receipts: dict[str, bytes] = {}
        self.claims: list[Claim] = []

    def source(self, path: str) -> str:
        file = self.root / path
        if not file.is_file():
            raise ModelError(f"dossier source {path} does not exist")
        self.sources[path] = hashlib.sha256(file.read_bytes()).hexdigest()
        return path

    def claim(
        self,
        claim_id: str,
        requirement: str,
        status: str,
        statement: str,
        *,
        record: Any,
        sources: tuple[str, ...] = (),
        gaps: tuple[str, ...] = (),
    ) -> None:
        name = f"{claim_id}.json"
        self.receipts[name] = _receipt(record)
        self.claims.append(
            Claim(
                id=claim_id,
                requirement=requirement,
                status=status,
                statement=statement,
                gaps=gaps,
                sources=tuple(self.source(path) for path in sources),
                receipts=(name, SNAPSHOT_RECEIPT),
            )
        )


def _recipe(recipe_dir: Path | None) -> dict[str, str] | None:
    if recipe_dir is None:
        return None
    files: dict[str, str] = {}
    for name in RECIPE_FILES:
        try:
            files[name] = (recipe_dir / name).read_text(encoding="utf-8")
        except OSError as error:
            raise ModelError(f"AUR recipe is incomplete: {error}") from error
    return files


def _srcinfo_licenses(srcinfo: str) -> dict[str, list[str]]:
    licenses: dict[str, list[str]] = {}
    current: str | None = None
    for line in srcinfo.splitlines():
        key, _, value = line.strip().partition(" = ")
        if key == "pkgname":
            current = value
            licenses[current] = []
        elif key == "license" and current is not None:
            licenses[current].append(value)
    return licenses


def _channel_report(path: Path | None, candidate: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModelError(f"cannot read channel parity report: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != CHANNEL_SCHEMA:
        raise ModelError(f"channel parity report is not {CHANNEL_SCHEMA}")
    if value.get("candidate") != candidate:
        raise ModelError(f"channel parity report is for {value.get('candidate')}, not {candidate}")
    return value


def build_dossier(
    root: Path,
    split: ArchSplit,
    *,
    recipe_dir: Path | None = None,
    channel_report: Path | None = None,
) -> Dossier:
    if not split.active:
        raise ModelError(f"the adoption dossier starts with the four-output split at {split.first_release}")
    builder = _Builder(root)
    snapshot = (root / SNAPSHOT_PATH).read_bytes()
    builder.receipts[SNAPSHOT_RECEIPT] = snapshot
    builder.source(SNAPSHOT_PATH)
    recipe = _recipe(recipe_dir)
    report = _channel_report(channel_report, split.full_version)
    candidate = split.full_version
    limitations: list[str] = []

    # Stable AUR and maintainer contact are external by construction.
    builder.claim(
        "stable-aur",
        "A stable AUR package exists",
        "external",
        "No AUR package has been published. Publishing needs explicit authorization and an AUR account action.",
        record={"published": False},
    )

    # Four-output graph.
    core, full = install_plans(split)
    builder.claim(
        "four-output-graph",
        "Four outputs with exact Core and Full install sets",
        "met",
        f"Core installs {', '.join(core.packages)}; Full installs {', '.join(full.packages)}. "
        "Core is a strict subset of Full and contains no Full output.",
        record={
            "outputs": [
                {
                    "package": output.package_name,
                    "edition": output.edition,
                    "depends": list(output.dependencies),
                    "conflicts": list(output.conflicts),
                    "provides": list(output.provides),
                    "replaces": list(output.replaces),
                }
                for output in split.outputs
            ],
            "core": list(core.packages),
            "full": list(full.packages),
        },
        sources=("packaging/arch-split.json",),
    )

    # OpenRazer provider and conflict behaviour, gated on the official snapshot.
    alignment = evaluate_provider_alignment(root, split=split)
    broad = [
        output.package_name
        for output in split.outputs
        if any(relation_name(item) == "openrazer-driver-dkms" for item in output.replaces)
    ]
    provider_gaps = (*alignment.problems, *(f"{name} replaces openrazer-driver-dkms" for name in broad))
    builder.claim(
        "openrazer-provider",
        "Correct OpenRazer provider and conflict behaviour",
        "met" if not provider_gaps else "not-met",
        "The OpenRazer driver output conflicts with and provides openrazer-driver-dkms at the "
        "transformed version and replaces nothing; the gate compares it with official Arch.",
        record=json.loads(alignment.as_json()) | {"broad_replaces": broad},
        sources=("packaging/arch-split.json", SNAPSHOT_PATH),
        gaps=provider_gaps,
    )

    # Current OpenRazer/OpenRGB matrix.
    official = json.loads(alignment.as_json())["official_versions"]
    openrgb = [item.purpose for item in split.output("full").optional_dependencies if item.package == "openrgb"]
    builder.claim(
        "upstream-matrix",
        "Current OpenRazer and OpenRGB matrix",
        "met" if alignment.passed else "not-met",
        f"Provided {', '.join(f'{n}={v}' for n, v in alignment.provided)} from OpenRazer "
        f"{alignment.transformed_commit}; official versions are recorded per repository.",
        record={
            "provided": [f"{name}={version}" for name, version in alignment.provided],
            "transformed_commit": alignment.transformed_commit,
            "official_versions": official,
            "openrgb_plugin": openrgb,
        },
        sources=(SNAPSHOT_PATH, "packaging/arch-split.json"),
        gaps=alignment.problems,
    )

    # Packaging licence, licence map and source bindings come from a recipe.
    if recipe is None:
        for claim_id, requirement in (
            ("packaging-license", "0BSD packaging metadata"),
            ("license-map", "Complete license map"),
            ("source-binding", "Source, signature and checksum bindings"),
        ):
            builder.claim(
                claim_id,
                requirement,
                "not-evidenced",
                "No generated AUR recipe was supplied to this dossier.",
                record={"recipe": None},
                sources=("packaging/aur.json",),
                gaps=("generate one with ./hfx package aur and pass --aur-recipe",),
            )
    else:
        pkgbuild = recipe["PKGBUILD"]
        zero_bsd = "# SPDX-License-Identifier: 0BSD" in pkgbuild.splitlines()[:3]
        reuse = 'SPDX-License-Identifier = "0BSD"' in recipe["REUSE.toml"]
        builder.claim(
            "packaging-license",
            "0BSD packaging metadata",
            "met" if zero_bsd and reuse else "not-met",
            "The recipe files are 0BSD so they can move into official packaging unchanged.",
            record={"pkgbuild_0bsd": zero_bsd, "reuse_0bsd": reuse},
            sources=("packaging/aur.json",),
            gaps=() if zero_bsd and reuse else ("recipe files are not marked 0BSD",),
        )
        licenses = _srcinfo_licenses(recipe[".SRCINFO"])
        missing_outputs = [o.package_name for o in split.outputs if not licenses.get(o.package_name)]
        missing_texts = sorted(
            {
                identifier
                for values in licenses.values()
                for identifier in values
                if not (root / "LICENSES" / f"{identifier}.txt").is_file()
            }
        )
        map_gaps = (
            *(f"{name} declares no license" for name in missing_outputs),
            *(f"LICENSES/{identifier}.txt is missing" for identifier in missing_texts),
        )
        builder.claim(
            "license-map",
            "Complete license map",
            "met" if not map_gaps else "not-met",
            "Every output declares SPDX licenses, and each identifier has its text under LICENSES/.",
            record={"licenses": licenses},
            gaps=map_gaps,
        )
        source = recipe_source(recipe_dir)
        disclosures = tuple(
            line.removeprefix("# Qualification only: ")
            for line in pkgbuild.splitlines()
            if line.startswith("# Qualification only: ")
        )
        skips = len(re.findall(r"'SKIP'", pkgbuild))
        binding_gaps = disclosures + ((f"{skips} source checksums are SKIP",) if skips else ())
        if source["version"] != candidate:
            binding_gaps += (f"recipe builds {source['version']}, not {candidate}",)
        builder.claim(
            "source-binding",
            "Source, signature and checksum bindings",
            "met" if source["mode"] == "publication" and not binding_gaps else "not-met",
            f"The {source['mode']} recipe builds {source['version']} from {source['commit']}.",
            record=source | {"skip_checksums": skips, "disclosures": list(disclosures)},
            sources=("packaging/aur.json",),
            gaps=binding_gaps,
        )
        limitations.extend(disclosures)

    builder.claim(
        "clean-chroot",
        "Clean chroot build and reproducibility",
        "not-evidenced",
        "No clean-chroot (pkgctl or makechrootpkg) build or reproducibility receipt exists.",
        record={"receipts": []},
        gaps=("build in a clean chroot twice and compare with ./hfx package compare-channels",),
    )

    if report is None:
        builder.claim(
            "channel-parity",
            "AUR and signed repository deliver one product",
            "not-evidenced",
            "No channel parity report from independent builds was supplied.",
            record={"report": None},
            gaps=("run ./hfx package compare-channels on both channels and pass --channel-parity",),
        )
    else:
        builder.claim(
            "channel-parity",
            "AUR and signed repository deliver one product",
            "met" if report.get("passed") is True else "not-met",
            f"Independent builds compared over {report.get('compared_paths')} paths.",
            record=report,
            gaps=tuple(report.get("problems", ())),
        )
        limitations.extend(report.get("disclosures", ()))

    # Lifecycle, rollback and removal.
    transitions = [
        analyse_transition(split, load_prior_topology(root, prior)) for prior in PRIOR_RELEASES
    ]
    safety = check_pacman_safety(payload_items(root), recipe["PKGBUILD"] if recipe else None)
    static_gaps = tuple(p for t in transitions for p in t.problems) + safety
    builder.claim(
        "lifecycle",
        "Upgrade, rollback and removal lifecycle",
        "not-met" if static_gaps else "static-only",
        "In-place upgrades from "
        + ", ".join(t.source_release for t in transitions)
        + " are statically proven, the repository-to-AUR switch and rollback scripts are "
        "exercised against temporary Pacman configurations by the test suite, and no output "
        "ships an install script, a Pacman hook or Pacman configuration.",
        record={
            "transitions": [t.as_json() for t in transitions],
            "pacman_safety_problems": list(safety),
        },
        sources=("packaging/arch-split.json", "packaging/install.json", "tests/test_arch_transition.py"),
        gaps=static_gaps + ("no physical upgrade, switch, rollback or removal receipt",),
    )
    limitations.extend(d for t in transitions for d in t.disclosures)

    snapshot_value = json.loads(snapshot)
    app_receipts = snapshot_value.get("app_behavior_receipts", [])
    builder.claim(
        "physical-evidence",
        "Physical evidence for the candidate",
        "not-evidenced" if not app_receipts else "met",
        f"{len(app_receipts)} physical application receipts are bound to the snapshot.",
        record={"app_behavior_receipts": app_receipts},
        sources=(SNAPSHOT_PATH,),
        gaps=() if app_receipts else ("no physical receipt for the four-output candidate",),
    )

    builder.claim(
        "supported-kernels",
        "Supported kernels",
        "not-evidenced",
        "The DKMS configuration builds against any installed kernel headers; no authority "
        "records a tested kernel range.",
        record={"tested_kernels": []},
        sources=("packaging/generated/dkms.conf",),
        gaps=("record tested kernels (linux, linux-lts) with build and load receipts",),
    )

    security = (root / "SECURITY.md").read_text(encoding="utf-8")
    private = "private vulnerability reporting" in security
    acknowledgement = re.search(r"acknowledged within (\d+) days", security)
    builder.claim(
        "security-contact",
        "Security and maintenance contacts with a response commitment",
        "met" if private and acknowledgement else "not-met",
        "Security reports go through GitHub private vulnerability reporting"
        + (f"; acknowledgement is targeted within {acknowledgement.group(1)} days" if acknowledgement else "")
        + ", subject to maintainer availability.",
        record={"private_reporting": private, "acknowledgement_days": acknowledgement and int(acknowledgement.group(1))},
        sources=("SECURITY.md", "SUPPORT.md"),
        gaps=("the acknowledgement target is best-effort, not a guarantee",),
    )

    builder.claim(
        "maintainer-contact",
        "Approach to an Arch Package Maintainer",
        "external",
        "No Arch Package Maintainer has been contacted. Any approach (ARCHDIST-002) needs explicit authorization.",
        record={"contacted": False},
    )

    gaps = [f"{claim.id}: {gap}" for claim in builder.claims for gap in claim.gaps]
    builder.claim(
        "limitations",
        "Limitations are disclosed",
        "met",
        f"{len(gaps)} open gaps and {len(limitations)} disclosures are listed.",
        record={"gaps": gaps, "disclosures": limitations},
    )
    dossier = Dossier(
        candidate=candidate,
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        sources=builder.sources,
        receipts=builder.receipts,
        claims=tuple(builder.claims),
    )
    validate_dossier(dossier)
    return dossier


def validate_dossier(dossier: Dossier) -> None:
    """Every claim cites the snapshot and at least one receipt of its own."""

    seen: set[str] = set()
    for claim in dossier.claims:
        if claim.id in seen:
            raise ModelError(f"dossier claim {claim.id} is duplicated")
        seen.add(claim.id)
        if claim.status not in STATUSES:
            raise ModelError(f"dossier claim {claim.id} has unknown status {claim.status}")
        if SNAPSHOT_RECEIPT not in claim.receipts:
            raise ModelError(f"dossier claim {claim.id} does not cite the official snapshot")
        if not set(claim.receipts) - {SNAPSHOT_RECEIPT}:
            raise ModelError(f"dossier claim {claim.id} has no receipt of its own")
        for name in claim.receipts:
            if name not in dossier.receipts:
                raise ModelError(f"dossier claim {claim.id} cites missing receipt {name}")
        for path in claim.sources:
            if path not in dossier.sources:
                raise ModelError(f"dossier claim {claim.id} cites unrecorded source {path}")
        if claim.status != "met" and claim.status != "external" and not claim.gaps:
            raise ModelError(f"dossier claim {claim.id} is {claim.status} without a stated gap")


def render_markdown(dossier: Dossier) -> str:
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in dossier.receipts.items()}
    lines = [
        f"# HyperFlux Next {dossier.candidate}: official Arch readiness dossier",
        "",
        "Generated by ./hfx package aur-dossier. Local review copy: no Arch Package "
        "Maintainer has been contacted and nothing has been published.",
        "",
        f"Official package snapshot: [{SNAPSHOT_PATH}](receipts/{SNAPSHOT_RECEIPT}) "
        f"`sha256:{dossier.snapshot_sha256}`",
        "",
        "| Status | Claims |",
        "|---|---|",
    ]
    summary = dossier.summary()
    for status, meaning in STATUSES.items():
        lines.append(f"| `{status}` ({meaning}) | {summary[status]} |")
    for claim in dossier.claims:
        lines += ["", f"## {claim.requirement}", "", f"- Status: `{claim.status}`", f"- {claim.statement}"]
        for gap in claim.gaps:
            lines.append(f"- Gap: {gap}")
        for name in claim.receipts:
            lines.append(f"- Receipt: [receipts/{name}](receipts/{name}) `sha256:{digests[name]}`")
        for path in claim.sources:
            lines.append(f"- Source: `{path}` `sha256:{dossier.sources[path]}`")
    return "\n".join(lines) + "\n"


def write_dossier(dossier: Dossier, output: Path) -> tuple[Path, ...]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ModelError(f"dossier output must be new or empty: {output}")
    receipts = output / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in sorted(dossier.receipts.items()):
        path = receipts / name
        path.write_bytes(content)
        written.append(path)
    for name, content in (("dossier.json", dossier.as_json()), ("DOSSIER.md", render_markdown(dossier))):
        path = output / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return tuple(written)
