# Device-Knowledge Integration

Status: accepted for local reconstruction

Reviewed destination: `78bd60106f9ce026d21895f2c1baefefb3519a5d`

Reviewed donor: `665f33bb2868358a34bf1940fca170d59f54fff4`

## Decision

HyperFlux Next keeps one owner for each kind of hardware knowledge:

- reviewed product facts and explicit gaps live under `knowledge/`;
- dated candidate membership lives under `profiles/candidates/`;
- immutable upstream revisions live in `integrations/catalog.json`;
- normalized upstream catalogs are reproducible generated imports;
- profile identity plus physical evidence is the only source of receiver write authority.

The donor was treated as evidence, not as code authority. Useful facts and bounded
parsers were adapted through the current schemas and generators. Donor-generated
files, presentation code, and duplicate managers were not copied.

## User Surfaces

The same canonical data has three deliberately different projections:

| Surface | Purpose | Hardware access |
| --- | --- | --- |
| GitHub Markdown | Research, architecture, support state, and contribution guidance | None |
| Repository Atlas | Ownership, dependencies, generated outputs, and change impact | None |
| Installed qualification console | Exact local PID/profile/generation and guided evidence collection | Loopback companion only |

There is no separate documentation website or database. The browser never opens
HID or USB devices directly, and completion in the local console does not publish
support automatically.

## Evidence Boundary

An official model name, OpenRazer method, OpenRGB registry entry, or candidate-list
match can describe a device. None can authorize a receiver write. Unknown,
conflicting, and candidate-only records remain visible and non-writable.

The 2026-07-13 candidate snapshot remains immutable historical evidence. The
2026-07-21 snapshot is a distinct successor selected explicitly by the
knowledge-link authority; repeated candidate IDs never rewrite history.

## Migration Rules

1. Adapt canonical facts through the current schemas.
2. Regenerate every language binding, profile catalog, kernel table, and document.
3. Keep test fixtures sanitized and non-authoritative.
4. Reject duplicate application state, generated donor files, and alternate upstream managers.
5. Run knowledge, profile, generation, repository-documentation, and affected integration contracts.

## Consequences

- Upstream refreshes remain bound to exact commits, paths, digests, and licenses.
- Device research is useful before physical qualification without becoming a write shortcut.
- Presentation can evolve without moving hardware truth into UI code.
- Updating a candidate snapshot preserves previous evidence and requires an explicit selection change.
