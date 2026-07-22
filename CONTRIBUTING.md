# Contributing

HyperFlux Next is schema-first, evidence-bound, and currently unreleased. A
contribution can improve public source or documentation without implying that a
device, package, or release is supported.

## Find The Right Starting Point

- Read the [Repository Atlas](https://offalexjackson777-stack.github.io/hyperflux-next/atlas/)
  before changing an unfamiliar area. It identifies ownership, canonical
  inputs, generated projections, dependencies, and verification nodes.
- Use [Hardware research](.github/ISSUE_TEMPLATE/hardware_research.yml) for
  public facts and unknowns that do not yet qualify a route.
- Use [Device qualification](.github/ISSUE_TEMPLATE/device_qualification.yml)
  only for an exact device route and bounded evidence proposal.
- Use [Discussions](https://github.com/offalexjackson777-stack/hyperflux-next/discussions)
  for help or early design exploration.
- Follow [SECURITY.md](SECURITY.md) for private vulnerability reports.

## Change Workflow

1. Identify the canonical owner in the Repository Atlas.
2. Change canonical data or source, not a generated projection.
3. Add focused tests for every changed behavior, invariant, state transition,
   schema, UAPI, ABI, or compatibility boundary.
4. Run `./hfx generate` and confirm a second run produces no diff.
5. Run `./hfx verify --changed-from <commit>` while iterating.
6. Run `./hfx verify --all` when the affected graph or release impact requires
   the complete software lane.
7. Complete the pull-request template with affected domains, contract impact,
   generated-file impact, verification, and evidence level.

## Generated Files

Repository automation is owned by
[`governance/github.json`](governance/github.json). Shared device and protocol
facts have their own canonical schemas. Run `./hfx generate` after changing an
authority; do not hand-edit generated workflows, issue forms, CODEOWNERS,
folder READMEs, bindings, catalogs, or portal projections.

Generated documentation links back to its source or to the Repository Atlas.
If a generated page is wrong, fix the authority named there.

## Evidence And Hardware

- Hosted CI has no device access and grants no hardware authority.
- Opening an issue or pull request never authorizes receiver queries, writes,
  suspend, pairing, or any other physical operation.
- Unknown devices may contribute identity and passive observations but inherit
  no writable capability.
- Imported facts require an exact public source, provenance, license review,
  and an explicit distinction between stated fact, inference, conflict, and
  unknown.
- Raw captures, private paths, serials, stable host identifiers, arbitrary
  logs, and research senders are not accepted into this repository.

## Review Standard

Changes are reviewed for architectural ownership, upgrade compatibility,
bounded resource use, privacy, test selection, generated freshness,
performance budgets, and release-gate impact. Main uses linear history,
required checks, code-owner review, conversation resolution, deletion
protection, and force-push protection.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
