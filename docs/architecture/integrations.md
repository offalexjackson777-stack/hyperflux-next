# Integration Boundary

Application adapters consume SDK snapshots through one tested projection. They
do not decide receiver qualification, infer routes from product names, or turn
paired inventory into write authority on their own.

## Two Views Of Hardware

The integration model deliberately exposes two related collections:

- **Inventory** preserves paired, sleeping, unavailable, unknown, and
  unqualified children so diagnostics remain truthful.
- **Controllers** contains only devices with exact receiver and child profile
  bindings plus one fresh, usable HyperFlux wireless endpoint.

A direct USB route can make a physical device available without making its
HyperFlux route writable. The device therefore remains in inventory while the
HyperFlux controller is absent. Native application detectors remain responsible
for the direct USB controller; HyperFlux never suppresses unrelated devices.

Sleeping is different from power-off or route loss. One fresh sleeping receiver
route retains controller identity so an effect can resume when the device
returns, but its view disallows immediate submission. Stale, ambiguous, unknown,
or explicitly unavailable routes grant no controller.

## Exact Authority

Every controller carries generation-scoped receiver and child profile bindings,
the application slot count, semantic capabilities, presentation provenance, and
one lighting resource key. Ownership is rendered relative to the viewing SDK
client as unowned, owned by that client, or owned by another client. Available
actions are derived from those facts; widgets do not recreate the policy.

Unknown PIDs remain visible with no inherited model, layout, or writable
capability. Model names and PIDs alone never merge a wired device with a
receiver-backed route, because two identical physical devices may legitimately
exist.

## Application Responsibilities

OpenRGB owns its model registry, LED labels, geometry, profiles, and effect
engine. Polychromatic owns its native presentation. The isolated OpenRazer
compatibility service translates only qualified methods. All three consume the
same inventory, controller, ownership, and capability semantics while the bridge
remains the sole userspace hardware writer.

Native application integrations are the normal path. In particular,
Polychromatic should load the native HyperFlux backend beside its existing
OpenRazer backend. HyperFlux does not replace or hide the official OpenRazer
daemon. The optional [OpenRazer compatibility boundary](openrazer-compatibility.md)
exists only for clients that cannot yet consume a native HyperFlux SDK backend.
