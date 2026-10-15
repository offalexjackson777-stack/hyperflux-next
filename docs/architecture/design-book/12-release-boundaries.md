# Design Book Chapter XII: Release Boundaries

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 62. Compatibility Transitions

Compatibility changes receive an explicit owner, rationale, destination test,
and removal condition. Obsolete components do not remain public product
authority merely because they once existed, and stock applications are never
replaced by a transition layer.

## 63. Read-Only Comparison

Recorded observations can be replayed against current logic to compare profiles,
route states, transaction validation, certainty, and diagnostics. Comparison
remains read-only; two implementations never compete for the writer session.

## 64. Safe Change Order

Changes reduce uncertainty before touching hardware. Boundaries, schemas, and
simulation come first, then the native exchange core and the upstream routes,
and physical qualification comes last, only for claims that software cannot
establish. The
[verification guide](../../developer/verification.md#safe-change-order) gives
the full order.

## 65. Release Channels

Work on a branch may be incomplete until it merges. Every pull request runs the
fast and full software lanes, and the full lane also runs weekly. A release
requires every software gate, candidate-bound physical evidence for each
promoted writable capability, maintainer review, and packages signed into the
project-operated Arch repository. The
[release gates](../../generated/release-gates.md) list each criterion. A
successful build alone never makes a release.

## 66. Limits And Epistemic Honesty

No architecture can guarantee that every future Razer device uses the same
receiver commands. No simulator can prove a previously unseen physical effect.
No upstream support list can substitute for transport qualification. HyperFlux
exposes these limits through support tiers and evidence so one unknown device
cannot destabilize proven support.

## 67. Final Definition Of Success

HyperFlux Next succeeds when the foundation is calm even as integrations evolve.
Adding a same-protocol device means adding one composable route profile,
source-bound upstream bindings, and focused qualification evidence without
changing the kernel, bridge engine, SDK transport, relay engine, or application
transport abstractions. OpenRGB and OpenRazer-based clients coexist through
exact route ownership while direct devices remain untouched. Wired and wireless
routes are truthful. Updates preserve configuration and automate everything that
is safe. Most failures are caught by simulation before the receiver is touched,
and physical validation proves only the remaining transport unknowns. The
coverage ledger links these 67 subjects to current implementation evidence and
names any remaining release boundary.

---

- Previous:
  [Chapter XI: Automation, Documentation And GitHub](11-automation-documentation-and-github.md)
- [Design Book index](../design-book.md)
