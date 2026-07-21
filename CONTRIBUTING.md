# Contributing

HyperFlux Next is schema-first and evidence-bound.

Before submitting a change:

1. Read the [Design Book](docs/architecture/design-book.md) and generated
   [Architecture Constitution](docs/generated/architecture.md).
2. Change canonical data before generated views.
3. Add or update tests for the behavior, state transition, or invariant being
   changed.
4. Run `./hfx verify --all`.
5. Record provenance for imported facts, metadata, fixtures, or code.

Repository automation is schema-owned. Change
[`governance/github.json`](governance/github.json), then run `./hfx generate`;
do not edit generated workflows, issue forms, CODEOWNERS, labels, or protection
plans directly. Hosted verification never grants hardware-write or publication
authority.

Research captures, raw hardware payloads, mission transcripts, private device
identifiers, and live sender experiments belong in the existing research or
engineering repositories. They are not accepted here.

Unknown hardware begins with identity and passive observations. It receives no
writable capability until its exact route and operation are independently
qualified.
