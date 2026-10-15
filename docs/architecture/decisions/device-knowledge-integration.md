# Device-Knowledge Integration

**Status:** Accepted

## Context

Hardware facts arrive from receiver observations, profiles, and pinned upstream
projects. These sources describe different kinds of truth and cannot safely be
merged into one support list or application-specific database.

## Decision

HyperFlux Next keeps one owner for each kind of hardware knowledge:

- reviewed product facts and explicit gaps live under `knowledge/`;
- dated candidate membership lives under `profiles/candidates/`;
- immutable upstream revisions live in `integrations/catalog.json`;
- normalized upstream catalogs are reproducible generated imports;
- profile identity plus physical evidence is the only source of receiver write authority.

## Public Surfaces

The same canonical data has three deliberately different projections:

| Surface | Purpose | Hardware access |
| --- | --- | --- |
| GitHub Markdown | Architecture, support state, and contribution guidance | None |
| Repository Atlas | Ownership, dependencies, generated outputs, and change impact | None |
| Source-only qualification console | Exact local PID/profile/generation and guided evidence collection | Loopback companion only |

There is no separate documentation website or database. The browser never opens
HID or USB devices directly, and completion in the local console does not publish
support automatically.

## Evidence Boundary

An official model name, OpenRazer method, OpenRGB registry entry, or candidate-list
match can describe a device. None can grant a receiver write. Unknown and
conflicting records, and every route that is not `physically-proven`, remain
visible without child-profile controls. Route tiers come from the source-validated
route catalog, so Device Knowledge and Supported Hardware report the same counts.

## Consequences

- Upstream refreshes remain bound to exact commits, paths, digests, and licenses.
- Device research is useful before physical qualification without becoming a write shortcut.
- Presentation can evolve without moving hardware truth into UI code.
- Updating a candidate snapshot is a reviewed authority change; it does not
  silently promote write support.

## Alternatives Rejected

- A PID allowlist cannot establish transport capability or route authority.
- UI-owned hardware facts would couple presentation to safety policy.
- A public device database cannot inspect or authorize a visitor's local route.
