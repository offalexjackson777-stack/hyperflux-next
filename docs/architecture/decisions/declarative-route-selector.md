# Declarative Native Route Selector

**Status:** Accepted

## Context

The direct DeathStalker V2 Pro TKL reference was captured from wired PID
`1532:0298` with the wireless selector off. Its generic upstream requests use
transaction selector `0xff`. The receiver exposes the paired wireless child as
PID `1532:0296`; preserved receiver evidence routes that keyboard lane with
selector `0x9f`. The pointer lane uses selector `0x1f`.

The transaction selector is receiver routing metadata, not part of a device
command's semantic payload. Requiring every on-wire byte to remain identical
would incorrectly classify standard receiver addressing as a model-specific
backend.

## Decision

Receiver profiles may declare one optional native route selector:

- the selected report field;
- one value for the pointer endpoint lane;
- one value for the keyboard endpoint lane;
- mandatory restoration of the application's original response value.

The kernel validates the original upstream report, clones it, replaces only the
declared selector, and recomputes the checksum only when that field lies inside
the declared checksum range. It correlates the receiver response against the
routed clone, requires the response to echo the selected receiver lane, restores
the caller's selector, and returns the repaired standard response. Request
digests, replay identity, certainty, deadlines, and the single-SET rule remain
bound to the unmodified application request.

No child PID, model name, command class, command identifier, delay, or setting
appears in the transport engine. A same-protocol child continues to use this
receiver-level rule through profile composition.

## Evidence

Native parity distinguishes two claims:

1. application-owned upstream command fields remain unchanged;
2. receiver routing may translate a separately declared selector.

Privacy-safe evidence records both selector values, whether translation occurred,
whether the application response was restored, and whether the selector value
came from a captured USB exchange or the canonical receiver profile. It never
describes a translated on-wire request as byte-for-byte unchanged.

## Consequences

- OpenRazer and OpenRGB still own complete device commands and semantics.
- HyperFlux owns only the receiver-addressing adaptation required by the route.
- Pointer and keyboard lanes remain isolated without PID branches.
- A different report format or a rewrite outside the declared selector remains
  `backend-required`.
- Existing lighting maps, input forwarding, timing, UAPI structures, and
  restoration behavior are unaffected.

## Alternatives Rejected

- PID-specific command rewriting would make transport authority model-specific.
- Returning the receiver selector to the application would violate the upstream
  controller contract.
- Treating all selector differences as unsupported would reject a proven
  receiver-addressing boundary.
