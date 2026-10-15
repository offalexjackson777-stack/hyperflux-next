# Design Book Chapter IX: User Experience And Diagnostics

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 48. Declarative Application UI

Application integrations consume tested view models rather than interpreting raw
bridge responses inside widgets. The view model determines labels, actions,
ownership, battery freshness, route availability, and qualification level; the
stock application renders those facts without taking over transport policy.

## 49. Central Error Catalog

Each product error has a stable identifier, severity, technical cause, user
explanation, safe action, verification method, privacy classification, and
documentation link. Doctor, service logs, integrations, and support bundles
project the same underlying finding.

## 50. Doctor

Doctor answers what works, what requires attention, and what the user should do
next. It is read-only: it does not restart services, unload modules, reconnect
hardware, change pairing, or enable writes. Detailed evidence remains available
through bounded diagnostics while the default output stays understandable.

## 51. Privacy-Safe Support Bundles

A support preview describes exactly what will be collected. Bundles include
bounded package, kernel, bridge, capability, and typed-error evidence. They
exclude hardware serials, stable host identifiers, private paths, terminal
history, raw reports, captures, and memory dumps.

---

- Previous:
  [Chapter VIII: Simulation And Verification](08-simulation-and-verification.md)
- [Design Book index](../design-book.md)
- Next:
  [Chapter X: Updates, Packaging And Operations](10-updates-packaging-and-operations.md)
