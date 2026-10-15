# Project Journey

HyperFlux Next began with a practical Linux problem. A keyboard and mouse paired
through one HyperFlux receiver worked as ordinary input devices, but the controls
that made them useful were invisible to the applications I already wanted to
use. I did not want a one-machine binding or a replacement RGB desktop. I wanted
the receiver to reveal its real paired children automatically while OpenRazer,
Polychromatic, and OpenRGB remained themselves.

That goal survived several prototypes and one important correction of scope. The
result is a bridge with narrow ownership: HyperFlux handles receiver transport,
route authority, and packaged connectors; stock applications keep device
semantics and the user experience. This is how that boundary took shape.

## Era 1: The Receiver Problem

At first the missing behavior looked like ordinary device discovery. Linux
already received keys, pointer movement, clicks, and scrolling from the physical
receiver. What applications lacked was a trustworthy identity and feature-report
route for each paired child.

Several shortcuts would have made one workstation look successful. I could have
bound a USB path, copied a serial, treated the receiver as a keyboard or mouse,
or admitted every Razer product ID. Each shortcut would have confused local
observation with product authority.

I chose a capability-based model instead. The receiver is a transport surface,
not a fake controller. Each child remains independent. Pairing inventory says an
identity is known; it does not say that the child is awake, current, or writable.
Unknown children can remain visible in diagnostics without receiving a command.
A mouse can work without a keyboard sibling, and a keyboard can work without a
mouse sibling.

The Basilisk V3 Pro 35K and DeathStalker V2 Pro TKL became the two end-to-end
physical fixtures. They are not privileged runtime branches. The wider inventory
enters through the same reviewed capabilities and source identities described in
the [hardware guide](../user/hardware.md).

## Era 2: Learning The Transport

The next lesson was that identity, presence, and transport authority are separate
facts. Replaying a cached product ID as an available device produced convincing
snapshots and unsafe state. A current route also needs a qualified profile, the
active receiver generation, and a writer that still owns that exact route.

I moved the receiver mechanics into a bounded kernel interface while leaving
ordinary input on the physical HID path. The module binds only reviewed
interfaces, preserves normal input delivery, and exposes fixed-size operations.
It does not decide product names, lighting effects, application ownership, or
presentation.

Above it, the bridge protocol grew from version 1 through version 7 without
rewriting earlier wire contracts. Generated Rust, Python, and C++ clients derive
from the same catalogs. Negotiation makes missing meaning explicit: an older
client keeps the contract it understands, while an unsafe downgrade fails.

This also established the no-duplicate-input rule. The OpenRazer endpoint is
feature-only. It carries settings and lighting reports but cannot become a
second desktop keyboard or mouse. The retained boundaries are documented in the
[kernel architecture](../architecture/kernel.md) and the
[protocol reference](../generated/bridge-protocol.md).

## Era 3: From Prototype To Bridge

A successful exchange was not yet a product. The system also had to behave when
a receiver disappeared, a child slept, a process crashed, a lease expired, or a
write result became uncertain.

Receiver generations became opaque authority epochs. Replacing a generation
revokes the old one before new routes publish. Leases bind a client, resource,
profile, generation, and expiry. Transactions retain whether a request was not
sent, may have been sent, or reached a confirmed result. An uncertain write is
never retried merely because an acknowledgement was lost. Sibling routes remain
independent, so one unavailable child cannot hide or restart another.

Persistence needed the same discipline. Stable intent is distinct from
transient effects and ownership. State replacement, compare-and-swap, bounded
history, and the persistence lock have explicit tests. Restoration is separate
from normal operation and ships disabled.

One defect made that separation especially clear: an incompatible stable profile
could enter repeated restoration work. Compatibility now runs before claim
creation. The regression proves that a stale profile creates no claim,
transaction, completion, or hardware write while retaining intent for a future
compatible route.

The design became a bridge when failure paths were as deliberate as the happy
path. See the [bridge core](../architecture/bridge-core.md) and
[persistence contract](../architecture/persistence.md).

## Era 4: Preserving Application Ownership

Application integration was where the project came closest to losing its
purpose. An early approach copied behavior that belonged to OpenRazer and
Polychromatic. It made isolated testing convenient, but it made HyperFlux own an
application stack instead of supporting one.

I removed that approach. The OpenRazer daemon and Python API remain stock.
Polychromatic remains the distribution application and sees receiver-backed
children through the ordinary OpenRazer D-Bus API. HyperFlux adapts only the
kernel transport needed for receiver feature reports, then supplies a bounded
relay and read-only preflight.

OpenRGB follows the same rule. The application remains stock. HyperFlux ships a
plugin built against pinned upstream source, and the plugin transports standard
Razer reports over the bridge SDK. OpenRGB still owns controllers, modes, effects,
layouts, brightness behavior, and profiles. The receiver never appears as a
pseudo-controller.

The package boundary now mirrors those responsibilities:

- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI,
  and base services.
- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility
  transport and OpenRGB integration for optional stock applications.
- OpenRazer userspace, Python OpenRazer, Polychromatic, and OpenRGB remain stock,
  optional applications.

The [application guide](../user/applications.md) and
[integration architecture](../architecture/integrations.md) describe the final
ownership line.

## Era 5: Evidence And Recovery

Offline success taught me a useful limit: a correct model is not a physical
observation. Software can prove route policy and still miss a publication timing
problem. A healthy bridge can coexist with an application boundary that has not
yet appeared.

I began testing one boundary at a time. Reproducible package bytes, service
identity, route state, and rollback inputs were recorded before any live change.
Physical and graphical results remained human observations. A failed test stopped
at the first missing layer instead of escalating into unrelated changes.

That approach uncovered defects that broad demonstrations had hidden. Cached
identity once granted availability without route evidence. Readiness once
required every remembered sibling instead of one valid available child. The
relay once published an endpoint before stock OpenRazer could finish its
synchronous constructor requests. Publication now requires stable,
source-derived identity on one unchanged route epoch, and one failing child does
not hide the other.

Physical testing also corrected assumptions about battery zero, charging state,
device mode, direct USB precedence, application handoff, and special controls.
Keyboard and mouse power cycles now withdraw and republish only the affected
endpoint. Normal input stays on the physical receiver throughout.

The 2.0.0 Arch release completed that bounded parity work with both physical
fixtures. The verification layers and what each one can claim are explained in
the [verification guide](../developer/verification.md).

## Era 6: Building A Smaller 2.0

Once the behavior was reliable, I made the source easier to review. Tests moved
out of production modules. Large files were split by responsibility where the
split clarified ownership. Public crate paths remained available through
compatibility exports. Generated contracts stayed generated, while human guides
became shorter and task-focused.

The two-package design became explicit rather than accidental. Core is useful on
its own as the receiver bridge and SDK boundary. Full is the optional integration
layer for stock OpenRazer and OpenRGB application paths. Arch is the reference
backend; other distributions must earn their own build, lifecycle, and hardware
evidence instead of inheriting an Arch claim.

Quality work followed the same rule. Coverage has a ratchet. Dependencies and
licenses are inventoried. Parsers and lifecycle models have fuzz, property, and
fault tests. Performance changes require measurements. The documentation catalog
assigns each public page an owner and canonical source so factual projections do
not drift away from the product.

Version 2.0 is the result of those choices: a smaller public surface around the
same protocol, UAPI, capability model, and stock-application boundary. Version
2.0.0 was published for Arch Linux x86_64 on 2026-08-04.

## Technical Appendix

### Technical source map

| Era | Public authorities | Representative tests |
| --- | --- | --- |
| Receiver problem | [constitution](../../architecture/constitution.json), [candidate inventory](../../profiles/candidates/razer-hyperflux-v2-2026-07-21.json) | [profiles](../../tests/test_profiles.py), [universality](../../tests/test_source_validated_universality.py) |
| Learning the transport | [protocol registry](../../protocol/registry.json), [UAPI registry](../../uapi/registry.json) | [protocol](../../tests/test_protocol.py), [kernel UAPI](../../tests/test_kernel_uapi.py) |
| Bridge foundation | [protected behavior](../../assurance/protected-behavior.json), [runtime authority](../../runtime/linux.json) | [restoration](../../crates/hfx-core/tests/restoration.rs), [generations](../../crates/hfx-bridge/tests/generation_orchestration.rs) |
| Application ownership | [integration catalog](../../integrations/catalog.json), [package authority](../../packaging/distributions.json) | [stock applications](../../tests/stock_application_compatibility.py), [packaged OpenRGB](../../tests/openrgb_packaged_plugin_sdk_qualification.py) |
| Evidence and recovery | [protected behavior](../../assurance/protected-behavior.json), [performance budgets](../../assurance/performance-budgets.json) | [native parity](../../tests/test_native_parity.py), [release qualifiers](../../tests/test_bridge_only_release_qualifiers.py) |
| Building 2.0 | [package authority](../../packaging/distributions.json), [quality audit](../../assurance/quality-audit.json) | [package pipeline](../../tests/test_package_pipeline.py), [quality](../../tests/test_quality.py) |

The machine-readable [journey source map](../../assurance/journey-source-coverage.json)
checks that every era is grounded in public repository authorities and tests.

### Product and package boundary

The canonical package descriptions above come from
[`packaging/distributions.json`](../../packaging/distributions.json). Neither
package owns stock OpenRGB, OpenRazer userspace, Python OpenRazer,
Polychromatic, their settings, or their lifecycles.

Installation remains non-activating, and restoration remains disabled by
default.

### Accepted limitations

The limits users can see today, such as Ripple showing no reactive lighting and
the Quartz label in OpenRGB, are listed with what to do about each in
[known limitations](../user/limitations.md).

### Prior-art and novelty boundary

The project reviewed its pinned OpenRazer and OpenRGB sources, device facts,
integration catalog, and receiver inventory. That review explains which behavior
HyperFlux adopts, adapts, or leaves upstream. It is not a survey of every receiver
bridge or control project, and this account makes no absolute novelty claim.
