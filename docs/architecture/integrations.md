# Integration Boundary

Application adapters consume exact, generation-bound routes through the SDK.
They do not decide receiver qualification, infer routes from product names, or
turn paired inventory into write authority on their own.

## Inventory And Native Routes

The integration model deliberately exposes two related collections:

- **Inventory** preserves pairing and passive observation facts, including
  unqualified children, so diagnostics remain truthful. Inventory does not
  grant a route or claim that a child is asleep or powered off.
- **Native routes** contain only devices with exact receiver and child profile
  bindings, a qualified report family, an opaque route token, and one of the
  protocol-v7 states `available`, `temporarily-unavailable`, or `withdrawn`.

A direct data route wins only when its nonempty, physically queried serial
matches the receiver-backed child. HyperFlux then withdraws that one virtual
route while the upstream application's ordinary detector owns the direct
controller. A PID, model name, device kind, or wall-power cable is never enough
to merge routes, and unrelated devices are never suppressed.

Availability events report only route authority. They do not assert sleep,
power-off, child readiness, visible lighting, or application state. Applications
may retain their own controller and profile state while a route is temporarily
unavailable, then continue through a newer qualified event.

## Exact Authority

Every native route carries generation-scoped receiver and child profile
bindings, an opaque route token, exact wireless identity, protocol family, and
one native-control resource. Device semantics do not cross this boundary:
applications submit an upstream-generated 90-byte Feature report and receive
the matching response unchanged.

Native control excludes the same child's retained semantic lighting, settings,
and pairing resources while a native lease is active. Sibling devices and newer
receiver generations remain independent. This keeps migration fallbacks
available without letting them compete with an upstream-native session.

Unknown PIDs remain visible with no inherited model, layout, or writable
capability. Model names and PIDs alone never merge a wired device with a
receiver-backed route, because two identical physical devices may legitimately
exist.

## Application Responsibilities

OpenRGB keeps its pinned Razer registry, controller modes, LED labels, geometry,
profiles, and effect engine. Its HyperFlux plugin replaces only the Feature
transport for qualified receiver routes; ordinary hidapi controllers are
unchanged.

OpenRazer keeps its daemon, D-Bus API, Python classes, methods, images, and
persistence. A source-pinned driver patch accepts trusted feature-only UHID
devices and sends their reports through protocol v7. Polychromatic therefore
uses its ordinary OpenRazer backend rather than learning HyperFlux device
semantics.

## OpenRazer Source And Package Ownership

The exact OpenRazer source, transform digest, DKMS identity, capability marker,
and relay unit are rendered from one canonical deployment model in the
[OpenRazer native-route deployment](../generated/openrazer-native-deployment.md).
The transform changes only the trusted HyperFlux transport boundary and its
capability markers. It preserves upstream direct USB tables, command building,
daemon behavior, Python-client behavior, and kernel compatibility fixes.

The HyperFlux distribution package owns the transformed OpenRazer DKMS source
and satisfies the upstream driver package dependency. Native package metadata
conflicts with and replaces the ordinary OpenRazer DKMS driver so `pacman -Syu`
cannot install a second source-tree owner. The official daemon and Python
client remain separate upstream packages. Distribution release suffixes on the
verified source line are ignored; a different upstream source line blocks the
native relay until a new source-bound transform and verification exist.

Loaded modules are trusted by a declared transport capability, not by parsing a
package version string. Source lineage is checked separately so a newer daemon
cannot silently use an older patched driver. The generated
[installation contract](../generated/installation.md) records update,
activation, Doctor, and rollback behavior.

The private OpenRazer service and SDK-native Polychromatic backend are retained
only as migration fallbacks until physical parity and a separate retirement gate
pass. The bridge remains the sole userspace receiver writer; applications own
the commands and user experience.
