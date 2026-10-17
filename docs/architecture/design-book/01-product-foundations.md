# Design Book Chapter I: Product Foundations

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

The problem this book answers is simple to state: Razer devices paired through a HyperFlux
V2 receiver are invisible to the Linux applications that already support the same devices
over USB. This chapter says what HyperFlux Next is, what it keeps from earlier
implementations, and what it deliberately leaves behind.

## 1. Purpose

HyperFlux Next is the Linux bridge for devices paired through HyperFlux V2. Core
provides the receiver driver, bridge, SDK, CLI, and base services. Full adds the
OpenRazer compatibility transport and OpenRGB integration used by optional stock
applications. Capability-based profiles allow mouse-only, keyboard-only,
mixed-device, and no-RGB-application installations without machine-specific
binding.

## 2. Relationship To Earlier Implementations

HyperFlux Next keeps verified behavior while replacing accidental coupling
with explicit boundaries, schemas, and generated projections. Earlier
implementation details are not public product authority. Current profiles,
source-bound catalogs, tests, and release evidence are the maintained sources of
truth.

## 3. Knowledge That Must Be Preserved

Maintained claims link to source authority, tests, or privacy-safe physical
evidence. Receiver framing, qualified identities, LED mappings, generation
rules, battery semantics, lifecycle behavior, package behavior, and application
layouts remain reviewable facts rather than unexplained constants.

## 4. Complexity That Must Not Be Preserved

The public product rejects duplicated constants, absolute machine paths, exact
local-device combinations, handwritten copies of upstream inventories, and
hidden coupling between application presentation and transport policy.
Compatibility is preserved through stable interfaces and evidence; accidental
implementation structure is not.

---

- [Design Book index](../design-book.md)
- Next:
  [Chapter II: The Architectural Foundation](02-architectural-foundation.md)
