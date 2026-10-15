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

Changes reduce uncertainty before touching hardware.

```mermaid
flowchart LR
    Rules["Architecture boundaries"] --> Schemas["Schemas and domain types"]
    Schemas --> Sim["Simulator and replay"]
    Sim --> Core["Native exchange core"]
    Core --> Razer["OpenRazer native route"]
    Razer --> RGB["OpenRGB native route"]
    RGB --> Own["Native control arbitration"]
    Own --> Parity["Physical parity"]
    Parity --> Package["Packaging and diagnostics"]
    Package --> HW["Targeted hardware qualification"]
    HW --> RC["Release candidate"]
```

An upstream adapter advances only after the native exchange core and its
application-specific transport boundary pass software contracts. Physical
qualification is reserved for claims that software cannot establish, and package
publication remains separately authorized.

## 65. Release Channels

Development builds may contain incomplete migrations. Nightly builds require
complete software verification. Release candidates require installation,
upgrade, lifecycle, application, and selected physical evidence. Stable releases
require approved evidence, reproducible artifacts, documentation, known-limit
declarations, and no unresolved release-blocking findings. A successful compile
is never treated as publication authorization.

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
