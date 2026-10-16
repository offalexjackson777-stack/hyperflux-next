# Project Journey

HyperFlux Next began with a practical Linux problem. A keyboard and mouse paired
through one HyperFlux receiver worked as ordinary input devices, but the controls
that made them useful were invisible to the applications I already wanted to
use. I did not want a fix that only worked on my machine, or a replacement RGB
desktop. I wanted the receiver to reveal the devices actually paired with it,
while OpenRazer, Polychromatic, and OpenRGB stayed exactly as they are.

That goal survived several prototypes and one important correction of scope. The
result is a bridge with a narrow job: HyperFlux carries reports through the
receiver, decides which device may be written, and ships the connectors; the
stock applications keep the device behaviour and the user experience. This is
how that line was drawn.

## Era 1: The Receiver Problem

At first the missing behaviour looked like ordinary device discovery. Linux
already received keys, pointer movement, clicks, and scrolling from the
receiver. What applications lacked was a trustworthy identity, and a path for
feature reports, for each device paired with it.

Several shortcuts would have made one workstation look finished. I could have
bound a USB path, copied a serial, treated the receiver as a keyboard or mouse,
or accepted every Razer product ID. Each one would have mistaken what I saw on
my desk for what the product could promise.

I chose a capability-based model instead. The receiver is a transport, not a
fake controller. Each paired device stays independent. The receiver's pairing
list says a device is known; it does not say that the device is awake, current,
or safe to write to. Unknown devices can stay visible in diagnostics without
receiving a command. A mouse works without a keyboard beside it, and a keyboard
works without a mouse.

The Basilisk V3 Pro 35K and DeathStalker V2 Pro TKL became the two devices I
tested end to end. They get no special code paths. The wider inventory enters
through the same reviewed capabilities and source identities described in the
[hardware guide](../user/hardware.md).

## Era 2: Learning The Transport

The next lesson was that identity, presence, and permission to write are
separate facts. Replaying a cached product ID as an available device produced
convincing snapshots and unsafe state. A device is writable only with a reviewed
profile, on the receiver's current connection, through a writer that still owns
that exact route. (A route is the bridge's handle for one device on one receiver
connection.)

I moved the receiver mechanics into a small kernel interface and left ordinary
input on the physical HID path. The module binds only reviewed interfaces,
keeps normal input working, and exposes fixed-size operations. It does not
decide product names, lighting effects, which application is in control, or
presentation.

Above it, the bridge protocol grew from version 1 to version 7 without
rewriting earlier wire contracts. Generated Rust, Python, and C++ clients come
from the same catalogs. Negotiation makes a missing feature explicit: an older
client keeps the contract it understands, and an unsafe downgrade fails. In
2.2 the bridge stopped serving versions 1 to 4 and 6, keeping 5 for lighting
and 7 for the native tunnel; older clients now get a clear incompatible-version
answer.

This also set the no-duplicate-input rule. The OpenRazer endpoint is
feature-only. It carries settings and lighting reports but cannot become a
second desktop keyboard or mouse. The details are in the
[kernel architecture](../architecture/kernel.md) and the
[protocol reference](../generated/bridge-protocol.md).

## Era 3: From Prototype To Bridge

A successful exchange was not yet a product. The system also had to behave when
a receiver disappeared, a device slept, a process crashed, a lease expired, or
nobody could tell whether a write had happened.

Each physical connection of the receiver became a generation. Replacing a
generation revokes the old one before new routes are published. Leases bind a
client, resource, profile, generation, and expiry. Transactions record whether a
request was not sent, may have been sent, or reached a confirmed result. An
uncertain write is never retried just because an acknowledgement was lost.
Sibling routes stay independent, so one unavailable device cannot hide or
restart another.

Persistence needed the same discipline. Saved lighting intent is kept apart
from transient effects and ownership. State replacement, compare-and-swap,
bounded history, and the persistence lock have explicit tests. Restoration is
separate from normal operation and ships disabled.

One defect made that separation especially clear: an incompatible saved profile
could start restoration work over and over. Compatibility is now checked before
any restoration claim is created. The regression test proves that a stale
profile creates no claim, transaction, completion, or hardware write, while its
intent is kept for a future compatible route.

The design became a bridge when the failure paths were as deliberate as the
happy path. See the [bridge core](../architecture/bridge-core.md) and the
[persistence contract](../architecture/persistence.md).

## Era 4: Preserving Application Ownership

Application integration was where the project came closest to losing its
purpose. An early approach copied behaviour that belonged to OpenRazer and
Polychromatic. It made isolated testing convenient, but it made HyperFlux own an
application stack instead of supporting one.

I removed that approach. The OpenRazer daemon and Python API remain stock.
Polychromatic remains the distribution's application and sees the receiver's
devices through the ordinary OpenRazer D-Bus API. HyperFlux adapts only the
kernel transport needed for receiver feature reports, and supplies a bounded
relay and a read-only preflight check.

OpenRGB follows the same rule. The application remains stock. HyperFlux ships a
plugin built against pinned upstream source, and the plugin carries standard
Razer reports over the bridge SDK. OpenRGB still owns controllers, modes,
effects, layouts, brightness behaviour, and profiles. The receiver never appears
as a pseudo-controller.

The packages now mirror those responsibilities:

- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI,
  and base services.
- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility
  transport and OpenRGB integration for optional stock applications.
- OpenRazer userspace, Python OpenRazer, Polychromatic, and OpenRGB remain stock,
  optional applications.

The [application guide](../user/applications.md) and
[integration architecture](../architecture/integrations.md) describe where the
line now sits.

## Era 5: Evidence And Recovery

Offline success taught me a useful limit: a correct model is not a physical
observation. Software can prove the rules for a route and still miss a
publication timing problem. A healthy bridge can sit beside an application that
has not yet seen the device.

I began testing one layer at a time. Reproducible package bytes, service
identity, route state, and rollback inputs were recorded before any live change.
Physical and on-screen results stayed human observations. A failed test stopped
at the first missing layer instead of growing into unrelated changes.

That approach found defects that broad demonstrations had hidden. Cached
identity once made a device available without route evidence. Readiness once
required every remembered sibling instead of one valid available device. The
relay once published an endpoint before stock OpenRazer could finish its
synchronous constructor requests. Publication now requires a stable identity,
derived from the device's own source records, on one unchanged route epoch, and
one failing device does not hide the other.

Physical testing also corrected my assumptions about battery zero, charging
state, device mode, direct USB precedence, application handoff, and special
controls. Keyboard and mouse power cycles now withdraw and republish only the
affected endpoint. Normal input stays on the physical receiver throughout.

The 2.0.0 Arch release completed that parity work on both tested devices. The
verification layers, and what each one can claim, are explained in the
[verification guide](../developer/verification.md).

## Era 6: Building A Smaller 2.0

Once the behaviour was reliable, I made the source easier to review. Tests moved
out of production modules. Large files were split by responsibility where the
split made ownership clearer. Public crate paths stayed available through
compatibility exports. Generated contracts stayed generated, and the guides
became shorter and task-focused.

The two-package design became deliberate. Core is useful on its own as the
receiver bridge and SDK. Full is the optional layer for the stock OpenRazer and
OpenRGB applications. Arch is the reference platform; other distributions must
earn their own build, lifecycle, and hardware evidence instead of inheriting an
Arch claim.

Quality work followed the same rule. Coverage has a ratchet. Dependencies and
licences are inventoried. Parsers and lifecycle models have fuzz, property, and
fault tests. Performance changes require measurements. The documentation catalog
gives each public page an owner and a canonical source, so its facts do not
drift away from the product.

Version 2.0 is the result of those choices: a smaller public surface around the
same protocol, kernel interface, capability model, and stock-application
boundary. Version 2.0.0 was published for Arch Linux x86_64 on 2026-08-04.

After the release, real systems showed problems the tests had not: a shutdown
hang, a retired driver taking the receiver, a mouse missing after boot, slow
returns after sleep, and battery readings that jumped. Some were fixed in 2.0.1;
the rest have fixes waiting for 2.0.2. I wrote each one up, with its cause, its
fix, and what is still unproven, in [Investigations](../investigations/README.md).

## Technical Appendix

### Technical source map

Each era lists its main data files and representative tests.

- **Receiver problem**
  - Data: [constitution](../../architecture/constitution.json)
  - Data: [candidate inventory](../../profiles/candidates/razer-hyperflux-v2-2026-07-21.json)
  - Test: [profiles](../../tests/test_profiles.py)
  - Test: [universality](../../tests/test_source_validated_universality.py)
- **Learning the transport**
  - Data: [protocol registry](../../protocol/registry.json)
  - Data: [UAPI registry](../../uapi/registry.json)
  - Test: [protocol](../../tests/test_protocol.py)
  - Test: [kernel UAPI](../../tests/test_kernel_uapi.py)
- **Bridge foundation**
  - Data: [protected behavior](../../assurance/protected-behavior.json)
  - Data: [runtime authority](../../runtime/linux.json)
  - Test: [restoration](../../crates/hfx-core/tests/restoration.rs)
  - Test: [generations](../../crates/hfx-bridge/tests/generation_orchestration.rs)
- **Application ownership**
  - Data: [integration catalog](../../integrations/catalog.json)
  - Data: [package authority](../../packaging/distributions.json)
  - Test: [stock applications](../../tests/stock_application_compatibility.py)
  - Test: [packaged OpenRGB](../../tests/openrgb_packaged_plugin_sdk_qualification.py)
- **Evidence and recovery**
  - Data: [protected behavior](../../assurance/protected-behavior.json)
  - Data: [performance budgets](../../assurance/performance-budgets.json)
  - Test: [native parity](../../tests/test_native_parity.py)
  - Test: [release qualifiers](../../tests/test_bridge_only_release_qualifiers.py)
- **Building 2.0**
  - Data: [package authority](../../packaging/distributions.json)
  - Data: [quality audit](../../assurance/quality-audit.json)
  - Test: [package pipeline](../../tests/test_package_pipeline.py)
  - Test: [quality](../../tests/test_quality.py)

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
