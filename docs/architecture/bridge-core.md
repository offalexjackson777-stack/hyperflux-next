# Bridge Core Boundaries

Several applications may want the same receiver, and each could get the details wrong in its
own way: write to a device that has gone, repeat a command that may already have run, or
fight another application for the same device. The bridge solves this once for all of them.
It runs as the `hyperflux-next-bridge` service, and every write to the receiver goes through
it.

The HyperFlux bridge is the sole userspace route, arbitration, and
receiver-write authority between application SDKs and a generation-bound
transport. Its primary protocol-v7 path carries qualified upstream Feature
reports without owning device commands, settings, presentation, layouts,
effects, persistence, or physical qualification claims.

## Terms Used Here

- **Route**: the bridge's handle for one device on one receiver generation. Applications see
  it only as an opaque route token.
- **Generation**: one physical connection of the receiver; a replug starts a new one.
- **NativeControl lease**: permission for one application to send native exchanges to one
  device. It is held per device, so sibling devices stay free.
- **Protocol v7**: the current version of the local protocol between the SDK and the bridge.
- **Migration path**: the protocol-v6 semantic lighting path, kept for rollback.

## Dependency Direction

```mermaid
flowchart LR
    Apps["OpenRazer and OpenRGB"] --> Integrations["Thin transport adapters"]
    Integrations --> SDK["Generated SDK"]
    SDK --> Protocol["Versioned protocol"]
    Bridge["Bridge service"] --> Protocol
    Bridge --> Core["Pure bridge core"]
    Core --> Profiles["Composable profiles"]
    Bridge --> Transport["ReceiverTransport"]
    Transport --> Kernel["Kernel-backed adapter"]
    Transport --> Simulator["Virtual receiver adapter"]
```

The protocol, core state machines, and profile registry point inward.
Application integrations never become bridge dependencies. Linux ioctl details
and raw receiver reports remain behind the concrete kernel adapter.

## Native Tunnel Path

Protocol v7 exposes three narrow operations: enumerate qualified routes, watch
bounded availability transitions, and exchange one exact 90-byte Feature report
through a generation-bound opaque route. The bridge validates route authority,
profile qualification, NativeControl ownership, report shape, deadlines, queue
admission, and response correlation. It treats the report's device command and
status as upstream-owned opaque data.

OpenRGB may hold NativeControl for an explicit controller session. The
feature-only OpenRazer UHID relay acquires the same child-local resource when a
burst of Feature exchanges starts and keeps it while the exchanges keep
matching. OpenRazer writes one lighting frame as several back-to-back
exchanges, one per row, so acquiring and releasing around every exchange would
add two bridge round trips to each of them. The relay gives the lease back 25
ms after the last matched exchange, and at once when an exchange is not sent,
may have been sent, or cannot be decoded; the next exchange then acquires a
fresh lease. It never repeats an exchange to recover a lease. A lease held for
half its 30-second duration is renewed before the next exchange uses it.
Teardown, route recycling, kernel-binding recovery, and relay shutdown release
the lease explicitly, so an idle virtual endpoint cannot exclude another
application for longer than one burst plus 25 ms. NativeControl conflicts with
the retained semantic lighting, settings, and pairing resources for that child
only. Siblings, other receivers, and newer generations remain independent.

Protocol v6, UAPI v1, semantic carrier encoding, stable-state persistence, and
restoration remain installed as a noncompeting compatibility fallback. They are
not the target architecture and cannot be removed before physical parity,
rollback, and maintainer review of the retirement.

## Distinct Safety Bindings

The following identities solve different problems and must not be merged:

- receiver generation rejects state from a previous physical connection;
- client identity owns connection-scoped resources;
- lease identity proves current application ownership;
- transaction identity provides idempotent outcome lookup;
- request identity deduplicates one method invocation;
- event stream identity and epoch detect service restart;
- event sequence detects a missed bounded event.

Each qualified profile also has a profile-local runtime digest. It covers
identity authority, compatibility, transport mappings, and capability
contracts, while excluding presentation prose. Updating one device profile
therefore invalidates work bound to that profile without needlessly revoking
unrelated devices; the separate catalog digest still proves whole-catalog
provenance.

A reconnect creates a newer generation and invalidates generation-scoped
observations, leases, queued work, and partial restoration. An event from an
older generation cannot reactivate itself.

## Connection Sessions

Every accepted bridge connection begins unnegotiated. The only legal first
method is `negotiate`, which selects one protocol version and the intersection
of offered bridge features. The bridge then issues a protocol session ID and an
opaque negotiation token. Every later request carries both values, and methods
that contain a client or nested request identity must agree with the negotiated
client and outer request envelope.

The exact initial negotiation request is replayable and returns the same server
hello without minting new credentials. A different second negotiation on the
same connection is rejected. Closing or revoking the connection invalidates its
independent internal session ID and authorization epoch, which makes queued work
fail its authority recheck even if a client retained old protocol credentials.

Protocol session IDs, negotiation tokens, internal session IDs, and
authorization epochs are generated from operating-system entropy. Tokens are
treated as credentials: mismatch errors never echo either the supplied or
expected value. The connection layer must additionally enforce peer credentials
and bounded concurrent sessions; a token does not replace local socket
authorization.

The bridge-wide session registry has a fixed capacity and permits one active
connection for each client ID. This prevents two connections that chose the same
client identity from operating one another's leases. Registration rejects
capacity, session-ID collision, and duplicate-client cases without changing
existing authority. Disconnect revokes the internal session before releasing
client leases and invalidating queued work.

The generated protocol catalog owns request method names, request IDs, session
credential access, and feature requirements. Bridge code consumes those
generated accessors instead of maintaining a parallel method table. This keeps a
future protocol method addition compile-visible and generation-driven.

The connection dispatcher is the only layer that turns a typed RPC request into
a method call. It performs negotiation and registry admission, verifies every
session-bound envelope, routes only authorized method parameters to an
application-neutral backend, and converts failures through the central error
catalog. Backend debug text, credentials, payloads, and private paths never
become protocol error messages.

Disconnect is ordered deliberately: internal session authority is revoked first,
then the backend releases client leases and terminally accounts for queued work.
A cleanup failure therefore cannot leave a disconnected client authorized to
reach transport. Repeated disconnect is a no-op.

## Local RPC Framing

The Unix transport uses a four-byte unsigned big-endian payload length followed
by one JSON protocol document. A clean EOF before any prefix byte ends the
connection. A partial prefix, empty frame, partial payload, malformed document,
or payload above 1 MiB is a terminal framing error.

The length bound is checked before allocation. Requests and responses are
validated and serialized through a bounded writer before any bytes are emitted,
then written with complete-write semantics and flushed. Framing errors preserve
the I/O stage without including payloads, tokens, or private paths. The framing
implementation belongs to the protocol crate and is shared by the bridge and
every native SDK; neither side maintains a private interpretation of the byte
stream.

Framing is not the SDK contract; generated protocol records are. The initial
`negotiate` exchange is the deliberately stable base handshake and has one
invariant shape across every retained version. A successful response selects one
exact frozen version; every later request and response is encoded and decoded
only through that version. Versions 1 through 6 preserve the additive semantic
migration contract. Version 7 preserves those records and independently adds
qualified native-route enumeration, availability events, NativeControl
ownership, and fixed-size Feature exchanges. A payload or method from an
adjacent version is rejected instead of being guessed from fields.

One connection runtime owns the dispatcher lifetime. Clean EOF and every
terminal framing or write failure converge on the same disconnect path, which
revokes session authority before backend cleanup. If transport and cleanup both
fail, the typed result preserves both failures rather than hiding one.

## Native Exchange Meaning

A native request is admitted only after its route, generation, profile, owner,
fixed report shape, and deadline agree. The bridge and kernel produce exactly
one of three terminal certainty classes:

- `not-sent` means SET did not begin;
- `may-have-been-sent` means SET may have begun and no layer may repeat it
  automatically;
- `matched-response` returns the complete correlated 90-byte response without
  interpreting its device status.

Route-local FIFO ordering, fair sibling dispatch, a capacity of 128 admitted
native transactions, and one active exchange per receiver keep the path bounded.
A generation replacement cancels queued work and prevents results from crossing
routes or generations.

## Semantic Transaction Meaning (Migration Fallback)

A transaction moves through declared states:

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Validated
    Validated --> OwnershipBound
    OwnershipBound --> GenerationBound
    GenerationBound --> Queued
    Queued --> Sent
    Sent --> HealthPending
    HealthPending --> Succeeded
    HealthPending --> Failed
    OwnershipBound --> Revoked
    GenerationBound --> Revoked
    Queued --> Revoked
```

`Succeeded` means every declared frame received one terminal receiver-transport
delivery and required finalization completed. It does not claim that a child
applied the frame or that a person observed the intended result. The protocol
therefore reports delivered frame count, transport side-effect certainty,
whether live transport began, automatic-retry policy, and separate
child-application confirmation.

Partial or uncertain transport is never retried automatically. A client resolves
an ambiguous response through transaction outcome lookup, not by submitting the
write again. A later visible result does not rewrite a terminal timeout history.

## Passive Inventory And Migration Projection

The bridge accepts passive lifecycle evidence through one typed,
generation-checked ingress. Receiver identity, child identity, endpoint
registration, pairing, raw route fields, activity, mat contact, freshness, and
battery values remain distinct observations. Unsupported children remain visible
but receive no write authority. A malformed, stale, conflicting, or
wrong-generation observation changes neither canonical state nor the event
stream.

Native route availability is a deliberately smaller protocol-v7 projection:
`available`, `temporarily-unavailable`, or `withdrawn`. It is generation-bound
and does not claim sleep, power-off, child readiness, or visible application
state. Older semantic snapshots retain their version-6 presence vocabulary for
compatibility. The native projection consumes only explicit route evidence or
endpoint-local traffic qualified as route evidence; it never converts pairing,
silence, sleep, power, or aggregate retail presence into authority.

Generation activation stages lifecycle, profile qualification, old lease
revocation, queued transaction revocation, durable restoration reconciliation,
and canonical events before committing any volatile owner. Initial discovery
emits `receiver-available`. A replacement emits `generation-replaced`; reconnect
after a completed removal additionally emits `receiver-available`. Physical
disconnect is two-stage: the first transport-confirmed absence moves the
generation to `disconnecting`, revokes live authority, terminally reconciles
every durable claim for that generation, and emits `receiver-unavailable`; a
later observation retires the generation and its profile binding. Ordinary
lifecycle observations cannot enter `disconnecting`, so no adapter can bypass
revocation.

## Ownership And Atomicity

Resources are keyed by receiver, generation, logical child, and generic domain:
NativeControl, lighting, settings, or pairing. NativeControl conflicts with all
three semantic write domains for the same child. This supports arbitrary
receivers and multiple children without fixed mouse/keyboard slots while
leaving siblings independent.

Lease acquisition is atomic. If any requested resource is owned by another
client, none are granted. Forced takeover is not part of protocol version 1.
Read-only snapshots, telemetry, and diagnostics remain shared.

Battery telemetry is a generation-bound lifecycle fact, not adapter-owned UI
state. Reported zero percent remains a real value; unavailable and never
observed remain distinct; freshness can become stale without erasing the last
value. A receiver-generation replacement clears all battery evidence so a new
connection can never inherit telemetry from its predecessor.

Acquire, renew, and release share one bounded client-and-request idempotency
history. Exact replay returns the retained result; reuse across methods or with
changed content is rejected. Conflict outcomes consume the same bounded history
budget as grants, so a stream of losing acquisition requests cannot create
unbounded bridge state.

Atomicity describes admission, ownership, ordering, and complete transaction
accounting. It does not invent physical rollback or simultaneous visible child
application where hardware cannot prove either claim.

## Backpressure

Backpressure is policy-specific:

- native exchanges preserve route-local FIFO ordering, fair sibling service,
  and immutable deadlines; rejection before SET is `not-sent`, while any
  uncertainty after SET forbids automatic retry;
- current unsent effect frames may be coalesced per resource because an older frame is obsolete;
- static lighting, settings, pairing, and restoration preserve strict order and
  return busy when their bounded queue cannot accept work;
- one logical device's outage must not stall its sibling;
- event and diagnostic journals are bounded history rings, not work queues;
- logging output is best effort through a bounded nonblocking sink and can never stop transport.

Every deadline uses an injected monotonic clock. Kernel session expiration may
use Linux boottime inside the concrete kernel boundary so suspend remains
meaningful. Durable capture timestamps use a separate wall-clock boundary.

## Semantic Restoration (Migration Fallback)

Native adapters do not use this subsystem. OpenRazer and OpenRGB own settings,
effects, profiles, and persistence, and resume them as they do for direct
devices. HyperFlux reports route availability and transports the first upstream
request without a wake-color timer or child-application claim.

Persistence stores semantic stable intent and exact profile identity. It never
stores a live lease, session authorization, route observation, raw frame, or
software-effect phase.

Restoration requires a fresh qualified generation, current routes, a matching
profile digest, new ownership, and one durable lifecycle claim. A surviving
claim, partial checkpoint, or failed target blocks a false complete result.
Software effects remain application computations and restart through the
application's saved startup profile.

Transporting completed application frames does not make an input-reactive
effect work. Stock OpenRazer Ripple draws from key presses it reads from an evdev
file, which it finds once, when it adds the keyboard, by matching
`/dev/input/by-id` names against a per-model pattern. Through the receiver the
keyboard's key presses arrive on the receiver's own input interface, whose name
matches no keyboard pattern. The Full component's udev rules therefore add a
second by-id name on that interface that matches the paired keyboard's pattern
and no other presented device's, and tag the node `uaccess` so only the
logged-in seat user can open it. We chose this over a daemon-side change because
it leaves OpenRazer untouched; the cost is that any program in that user's
session can read the receiver's key presses, which the
[known limitations](../user/limitations.md#ripple-and-other-effects-that-react-to-keys)
state plainly. The matcher is still global per model, not bound to a child route
or generation: with two same-model receivers, udev keeps one link and only one
keyboard reacts. The links are listed in the
[deployment reference](../generated/openrazer-native-deployment.md#key-presses-for-reactive-effects).
Doctor reports `HFX-INTEGRATION-007` when an attached receiver's link is
missing. The shared matrix-row and custom-mode Feature commands remain valid
transport operations.

`DurableRestorationRuntime` is the bridge's one persistence owner and snapshot
source. Its generation-retirement operation first consumes an exact retained
transaction terminal when one exists, otherwise reconciles the persisted
dispatch nonce with transport. Confirmed or possible writes retain their outcome
facts. Only a transport `NotObserved` result permits stale invalidation. Sibling
claims change through one atomic batch compare-and-set, so a mouse claim cannot
retire while its keyboard sibling remains accidentally active because a later
file operation failed.

After a definitive version 3 Static transaction succeeds, the bridge captures
its explicit Static or Off declarations automatically. An exact terminal replay
is idempotent: it neither advances the intent revision nor replaces the original
capture time. Capture happens after immutable hardware completion is recorded. A
persistence failure is therefore reported as a separate capture result and can
never rewrite successful transport truth or authorize another hardware dispatch.

The production persistence adapter keeps one strict, schema-versioned document
in a private service-owned directory. It holds an advisory writer lock, rejects
symlinks and broadly readable files, bounds bytes and receiver-scoped records,
and writes through a same-directory temporary file followed by file sync, atomic
replacement, and directory sync. A failure before replacement changes neither
disk nor the in-process compare-and-set view. A directory-sync failure reports
uncertain durability after advancing the in-process view to the already-visible
replacement, so a stale retry conflicts instead of silently replaying an old
revision.

## Protocol Evolution

Clients offer a protocol range and optional feature identifiers. The bridge
selects one exact version and emits that version's exact record shapes. Hardware
capability discovery is separate from protocol feature negotiation.

Values that may exceed IEEE-754's exact integer range, including receiver
generations, monotonic instants, event sequences, and stream epochs, use
canonical decimal strings on JSON-compatible wire surfaces. They remain bounded
integer types inside native implementations.
