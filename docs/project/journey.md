# Project Journey

HyperFlux Next began with a practical frustration: a receiver-paired keyboard and
mouse could work as ordinary input devices on Linux while the controls that made
them useful remained trapped behind a transport the desktop applications could
not see. I did not want a one-machine workaround. I wanted the receiver to reveal
its real paired children automatically, and I wanted existing applications to
recognize those children without being replaced by a private desktop stack.

That goal survived several prototypes, failed live gates, and one major scope
correction. The result is a bridge with deliberately narrow ownership. HyperFlux
owns receiver transport, generation and lease authority, safe publication, and
thin packaged integrations. Stock OpenRazer, Polychromatic, and OpenRGB retain
their application semantics and user experience. The current reconstruction is
turning the accepted private release 8 behavior into a smaller, reviewable 2.0
tree without changing the protocol or hardware contract underneath it.

This account is written from privacy-reviewed private development records.
Those records provide motivation and sequence, not technical authority. Every
technical claim below is grounded in repository history and canonical evidence,
with the neutral source map collected in the [technical appendix](#technical-appendix).

## Era 1: The Receiver Problem

At first, the problem looked deceptively small. Linux already received ordinary
keyboard and pointer input from the receiver. What was missing was the device
identity and control path that stock applications expected. A quick solution
could have bound one USB path, copied one local serial, or treated the receiver as
the device. Each shortcut would have made the development machine work at the
expense of the product.

I set a different boundary. The receiver would be the transport surface, never a
fake keyboard, mouse, or lighting controller. Its paired children would remain
independent devices. Eligibility would come from reviewed capabilities and exact
source identities, not from a broad vendor match or a list of whatever happened
to be attached during development. The Basilisk V3 Pro 35K and DeathStalker V2
Pro TKL became physical assurance fixtures, but not privileged runtime branches.

That distinction changed how support had to be represented. A pairing slot could
say that a child identity existed, but it could not prove that the child was
awake, current, or writable. An unknown child still needed to remain visible
without receiving authority. A supported mouse had to work without its keyboard
sibling, and a supported keyboard had to work without its mouse sibling. Direct
USB also had to take precedence without creating duplicate controllers when a
wireless route was still remembered.

The early architecture constitution and profile compiler made those constraints
machine-readable. The official inventory became a release boundary with honest
operational and backend-required distinctions. A deterministic virtual receiver
graph provided software evidence while keeping physical assurance separate. That
separation remains the basis of [supported hardware](../generated/supported-hardware.md)
and the [native onboarding process](../architecture/native-device-onboarding.md).

## Era 2: Learning The Transport

The next lesson was that discovery and transport authority are not the same
event. Receiver inventory can expose product identifiers, but application-facing
control requires a current route, a qualified profile, and a writer that still
owns the exact generation. Turning a cached identity directly into an available
device created convincing snapshots and incorrect authority.

I moved the transport boundary downward and made it smaller. The kernel module
learned the receiver-specific mechanics while preserving the ordinary HID input
path. It does not decide application names, lighting semantics, or product
presentation. It exposes fixed-width, bounded operations through a versioned
UAPI. User space owns policy and can reject malformed, stale, oversized, or
ambiguous input before it reaches hardware.

Above that boundary, the bridge protocol grew through versions 1 to 7 without
rewriting earlier wire shapes. Generated Rust, Python, and C++ SDKs all derive
from the same protocol catalogs. Newer clients can request route events, native
exchange, and integration views, while older clients keep the behavior their
negotiated version can represent. Unsafe downgrades fail instead of silently
dropping meaning.

This was also where I learned to treat input preservation as a first-class
contract. The receiver already delivers ordinary key and pointer reports through
the physical HID path. A feature-only virtual endpoint must not create a second
desktop input source. The OpenRazer route can expose the feature reports needed
for settings and lighting while leaving normal input delivery exactly where the
kernel and device already put it. The boundaries are described in the
[kernel architecture](../architecture/kernel.md), and the retained wire surface
is indexed by the [protocol registry](../generated/bridge-protocol.md).

## Era 3: From Prototype To Bridge

A working exchange was not yet a bridge. The product needed to remain correct
when a receiver disappeared, a child slept, a process crashed, a lease expired,
or a write result became uncertain. Those cases forced the design away from
optimistic callbacks and toward explicit state machines.

Receiver generations became opaque authority epochs. A replacement generation
revokes the old one before new routes are published. Leases bind a client,
resource, profile, generation, and expiry. Transactions preserve whether a
request was not sent, may have been sent, or reached a confirmed terminal result.
An uncertain write is never replayed merely because a process did not receive its
acknowledgement. Sibling routes remain independent, so one blocked child cannot
hide or restart another.

Persistence required the same discipline. Stable lighting intent is recorded
separately from transient effects and ownership. File replacement, compare and
swap, terminal history compaction, and the persistence lock all became tested
boundaries. Restoration is deliberately independent of normal qualified-live
operation and remains disabled by default. The bridge can therefore serve stock
applications without granting itself automatic replay authority.

One failure made that separation concrete. A stale receiver profile could enter
a repeated restoration schedule even though its stable intent was incompatible
with the active profile. The correction moved compatibility ahead of claim
creation. The exact twelve-attempt regression now proves that an incompatible
receiver digest creates no claim, transaction, completion event, or hardware
write while preserving the stable intent for a future compatible authority.

The prototype became a bridge when these failure paths were as deliberate as the
happy path. The current [bridge core](../architecture/bridge-core.md) and
[persistence contract](../architecture/persistence.md) describe that ownership,
and the protected-behavior authority prevents later cleanup from quietly
weakening it.

## Era 4: Application Compatibility

Application integration was the point where the project came closest to losing
its mission. An early branch bundled private OpenRazer behavior and a custom
Polychromatic path. It made integration tests easier to control, but it also made
HyperFlux responsible for applications it was supposed to support. That was not
compatibility. It was takeover.

I removed those boundaries and rebuilt around stock applications. OpenRazer keeps
its public daemon and Python API. HyperFlux adapts only the kernel-driver route
needed to carry receiver-backed feature reports, plus a bounded relay and
read-only preflight. Polychromatic remains the distribution package and sees the
children through the ordinary OpenRazer D-Bus API. HyperFlux owns none of its UI,
state files, effects, or lifecycle.

OpenRGB follows the same rule. The application stays stock. HyperFlux ships a
plugin built against pinned upstream source and publishes controller identities
through the bridge SDK. The plugin transports standard reports and retains
OpenRGB's own modes, effects, layouts, and brightness behavior. It does not patch
the installed application or register the receiver as a pseudo-controller.

The package boundary became equally important. Core contains the bridge, CLI,
relay, DKMS content, narrowly adapted OpenRazer driver sources, units, and runtime
configuration. Full adds optional stock-application integration, including the
OpenRGB plugin and dependency relationships. It does not contain replacement
application code. Installation registers files and reloads package-manager
metadata but does not silently activate hardware or services.

This correction also made support more universal. Generated native-route
authority comes from exact upstream methods, profile evidence, and declared
transport capabilities. The two local devices prove the physical path without
becoming a device-model switch in the bridge or relay. The final boundary is
summarized in [application integrations](../architecture/integrations.md) and the
[user application guide](../user/applications.md).

## Era 5: Evidence And Recovery

The most expensive lesson was that a green offline graph is not physical proof.
Synthetic qualification could show a correct profile while the real publication
path still exposed no child endpoint. Doctor could report a healthy bridge while
an optional application boundary was missing. A package could be reproducible
and still arrive at the wrong live assumption.

I began treating each live attempt as a bounded evidence gate rather than a
demonstration. Candidate bytes, package ownership, service identity, routes,
restart counts, and rollback inputs were bound before mutation. Physical and GUI
observations remained operator evidence rather than something a script could
infer. A failed gate stopped at the first missing boundary, preserved what was
known, and used the smallest recovery action available.

That process uncovered real defects. Cached identity replay initially granted
availability without child-specific route evidence. Readiness once required all
recorded siblings instead of at least one qualified available route. A relay
published a feature endpoint before stock OpenRazer could complete its synchronous
constructor exchanges, poisoning the daemon's only hot-add worker after an I/O
failure. Publication was narrowed to two matching source-derived serial reads on
the same route epoch, followed by a fresh direct-device check and a bounded UHID
start. One failing child no longer hides its sibling.

Physical testing also corrected several assumptions rather than merely confirming
code. Battery zero and unavailable had to remain distinct. Charging state needed
to follow the actual cable state. Device mode used by stock OpenRazer had to be
virtualized so the physical keyboard and mouse remained in their normal onboard
mode. Keyboard and mouse off/on cycles had to withdraw and republish one endpoint
without restarting the other. Direct USB precedence, application handoff, and
special input controls all needed their own observations.

Release 8 became the accepted private behavioral reference only after a normal
boot reached Doctor Ready, both routes and UHID endpoints appeared, stock
OpenRazer and Polychromatic saw exactly two children, OpenRGB published exactly
two controllers, and reversible controls worked. Its evidence also records what
did not become a claim. Ripple remains unsupported because stock OpenRazer lacks
a route-associated KeyWatcher event source. The OpenRGB Quartz color label is an
upstream cosmetic limitation also seen over direct USB. Neither limitation is
hidden by a machine-specific override.

The release evidence is summarized in [release status](release-status.md), while
the verification layers and their authority limits are explained in the
[verification guide](../developer/verification.md).

## Era 6: Reconstructing 2.0

Once release 8 worked, I chose reconstruction over publication. The private tree
contained the right behavior but carried years of accumulated structure,
generated surfaces, oversized modules, and historical packaging assumptions. A
public release needed a tree that another person could review without replaying
the entire investigation.

The reconstruction began by sealing release 8 as immutable behavioral evidence.
Every source area then received an explicit disposition: keep, move, split,
extract, retire, or defer. Public protocol versions 1 through 7, the kernel UAPI,
SDK paths, stock application interfaces, and package ownership stayed protected.
Embedded tests moved out of production modules. Large handwritten files were
split by responsibility or given a measured justification. Compatibility
re-exports preserved existing Rust paths while internal crates gained clearer
ownership.

The target package boundary is lean Core plus integration-complete Full. Core is
the bridge product and the necessary driver/runtime foundation. Full adds the
optional stock-application integrations without becoming a third ecosystem or a
replacement application. Arch remains the first native backend. Other
distributions are deferred until each can earn its own build, lifecycle, and
hardware evidence rather than inheriting an Arch claim.

Quality work followed the structural work. The repository now carries explicit
coverage ratchets, dependency and license inventories, bounded formal models,
fuzz and mutation surfaces, performance budgets, and privacy checks. Optimization
is measured rather than assumed; one expensive build-profile experiment was
rejected before it could become a production change. Documentation has the same
authority model as code: concise human guidance, generated factual projections,
protected hybrid blocks, and a catalog that assigns every page an owner and
source.

This journey is part of that reconstruction. Private development records were
streamed through a bounded, fail-closed extractor, filtered to product context,
deduplicated, curated into six eras, and reduced to aggregate public coverage.
No raw project-record text, local path, session identifier, or direct quotation
appears in the repository. Technical statements remain attached to commits and
canonical files.

The next milestone is not publication. It is a reproducible `2.0.0-1` Arch
candidate whose independent Core and Full assemblies are byte-identical and
whose complete software graph is clean. A final physical parity gate still needs
separate authorization. Remote cutover, tags, release channels, and public
support remain locked to a later user decision, as shown on the
[roadmap](roadmap.md).

## Technical Appendix

### Evidence model

The journey uses two distinct evidence classes. Private narrative records provide
motivation, sequence, and first-person voice. They are publication-authorized
only as paraphrase. Technical claims require repository commits, canonical
authorities, or sanitized evidence. The public
[source-coverage record](../../assurance/journey-source-coverage.json) binds the
private snapshot, extraction ledger, curation plan, and curated ledger by SHA-256
while revealing only counts and hashes.

The extraction selected 1,018 product-context narrative events from the bounded
source snapshot. Sixteen user-origin motivation records were curated across the
six eras with counts of 2, 2, 2, 3, 3, and 4. No direct quotation was approved.
The public privacy review rejects local paths, session identifiers, source text,
and any attempt to convert chat into technical authority.

### Technical source map

| Era | Principal commits | Canonical evidence |
| --- | --- | --- |
| Receiver problem | `da4b63a`, `3ba2f51`, `5d8ab87` | architecture constitution, candidate inventory |
| Transport discovery | `2dc409b`, `4c89108`, `baf01ea`, `8487cc5` | protocol and UAPI registries |
| Bridge foundation | `3cc3392`, `b1bb3a5`, `db22e4a`, `d9a9b6d` | protected behavior, persistence contract |
| Application compatibility | `6dca5c1`, `719ee9c`, `6598d19`, `ea04da7`, `7a6a473`, `6aa101a` | integration catalog and pinned transforms |
| Evidence and recovery | `5902a19`, `75d666b`, `f9d4598`, `5fa98e4`, `c348305` | installed baseline and release gates |
| Reconstructing 2.0 | `ea5b2de`, `62af0fb`, `cd0c02e`, `afa0b93`, `01041eb`, `5856ea1` | reconstruction ledger and quality audit |

The full 40-character revisions, individual supported claims, and repository
paths are validated by the source-coverage contract rather than duplicated in
this prose.

### Product and package boundary

HyperFlux owns receiver transport, bridge policy, qualified route publication,
UHID relay, its kernel module, the necessary OpenRazer driver adaptation, stable
SDKs, Doctor/status tooling, and packaged integration connectors. It does not own
stock OpenRGB, the OpenRazer daemon or Python API, Polychromatic, their settings,
or their application lifecycles.

Core and Full are the two user-facing Arch packages. Core supplies the bridge and
runtime foundation. Full adds optional integration dependencies and the packaged
OpenRGB plugin. Installation remains non-activating, restoration remains disabled
by default, and no passing software test grants publication authority.

### Accepted limitations

Ripple is not advertised as supported over HyperFlux because unchanged stock
OpenRazer cannot associate its KeyWatcher with the receiver-routed physical event
stream. A bounded negative run did not reproduce an earlier input latch, but it
did not prove universal safety or causal attribution. HyperFlux does not invent a
second input device, synthesize releases, or fork the application to conceal that
gap.

OpenRGB may label the black keyboard color variant as Quartz. The same label was
observed through stock OpenRGB on a direct USB path, so HyperFlux does not override
identity or colorway data for one machine. The limitation is cosmetic and remains
reported as upstream behavior.

### Prior-art and novelty boundary

The project performed a bounded review of its pinned OpenRazer and OpenRGB source
authorities, integration catalog, reviewed device facts, and official candidate
inventory. That review is sufficient to explain which behavior HyperFlux adopts,
adapts, or deliberately leaves upstream. It is not a global survey of every
receiver bridge or device-control project. This journey therefore makes no
absolute novelty claim. Its claims are about the design and evidence present in
this repository.
