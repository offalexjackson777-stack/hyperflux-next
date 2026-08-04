# Contributing

Thanks for helping improve HyperFlux Next. Contributions can cover bugs,
documentation, device knowledge, packaging, integrations, or the bridge itself.

## Find The Right Starting Point

- Start from the [structured issue form chooser](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new/choose)
  when reporting a bug, proposing a feature, or contributing documentation.
- Use [Device support](.github/ISSUE_TEMPLATE/device_support.yml) for public
  facts, capability questions, and support requests. An issue never authorizes
  a physical test.
- Use [Discussions](https://github.com/offalexjackson777-stack/hyperflux-next/discussions)
  for help or early design exploration.
- Follow [SECURITY.md](SECURITY.md) for private vulnerability reports.

## Change Workflow

1. Fork the repository and create a focused branch.
2. Find the owning area in the [Repository Atlas](docs/generated/repository-atlas.md).
3. Change canonical data or source rather than a generated projection.
4. Add focused tests for changed behavior or contracts.
5. Run `./hfx generate` and confirm a second run produces no diff.
6. Run `./hfx verify --changed-from <commit>` while iterating.
7. Open a pull request describing the reason, validation, compatibility, and risk.

## Generated Files

Generated files identify their source at the top. Run `./hfx generate` after
changing an authority; do not hand-edit generated workflows, issue forms,
CODEOWNERS, folder READMEs, bindings, catalogs, or repository projections.

Generated documentation links back to its source or to the Repository Atlas.
If a generated page is wrong, fix the authority named there.

## Safety And Privacy

- Hosted CI has no device access. Opening an issue or pull request does not
  authorize receiver queries, writes, suspend, pairing, or other physical work.
- Unknown devices may contribute identity and passive observations but inherit
  no writable capability.
- Imported facts require an exact public source, provenance, license review,
  and an explicit distinction between stated fact, inference, conflict, and
  unknown.
- Raw captures, private paths, serials, stable host identifiers, arbitrary
  logs, and research senders are not accepted into this repository.

## Review

Changes are reviewed for ownership, compatibility, bounded resource use,
privacy, focused tests, and generated freshness. See the [building](docs/developer/building.md)
and [verification](docs/developer/verification.md) guides for deeper maintainer
workflows.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
