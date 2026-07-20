# Bridge Core Boundaries

The HyperFlux bridge is the sole userspace policy and write authority between application SDKs and a generation-bound receiver transport. It does not own application presentation, effects, hardware report layouts, or physical qualification claims.

## Dependency Direction

```mermaid
flowchart LR
    Apps["Applications"] --> Integrations["Application integrations"]
    Integrations --> SDK["Generated SDK"]
    SDK --> Protocol["Versioned protocol"]
    Bridge["Bridge service"] --> Protocol
    Bridge --> Core["Pure bridge core"]
    Core --> Profiles["Composable profiles"]
    Bridge --> Transport["ReceiverTransport"]
    Transport --> Kernel["Kernel-backed adapter"]
    Transport --> Simulator["Virtual receiver adapter"]
```

The protocol, core state machines, and profile registry point inward. Application integrations never become bridge dependencies. Linux ioctl details and raw receiver reports remain behind the concrete kernel adapter.

## Distinct Safety Bindings

The following identities solve different problems and must not be merged:

- receiver generation rejects state from a previous physical connection;
- client identity owns connection-scoped resources;
- lease identity proves current application ownership;
- transaction identity provides idempotent outcome lookup;
- request identity deduplicates one method invocation;
- event stream identity and epoch detect service restart;
- event sequence detects a missed bounded event.

A reconnect creates a newer generation and invalidates generation-scoped observations, leases, queued work, and partial restoration. An event from an older generation cannot reactivate itself.

## Connection Sessions

Every accepted bridge connection begins unnegotiated. The only legal first method is `negotiate`, which selects one protocol version and the intersection of offered bridge features. The bridge then issues a protocol session ID and an opaque negotiation token. Every later request carries both values, and methods that contain a client or nested request identity must agree with the negotiated client and outer request envelope.

The exact initial negotiation request is replayable and returns the same server hello without minting new credentials. A different second negotiation on the same connection is rejected. Closing or revoking the connection invalidates its independent internal session ID and authorization epoch, which makes queued work fail its authority recheck even if a client retained old protocol credentials.

Protocol session IDs, negotiation tokens, internal session IDs, and authorization epochs are generated from operating-system entropy. Tokens are treated as credentials: mismatch errors never echo either the supplied or expected value. The connection layer must additionally enforce peer credentials and bounded concurrent sessions; a token does not replace local socket authorization.

The generated protocol catalog owns request method names, request IDs, session credential access, and feature requirements. Bridge code consumes those generated accessors instead of maintaining a parallel method table. This keeps a future protocol method addition compile-visible and generation-driven.

## Local RPC Framing

The Unix transport uses a four-byte unsigned big-endian payload length followed by one JSON protocol document. A clean EOF before any prefix byte ends the connection. A partial prefix, empty frame, partial payload, malformed document, or payload above 1 MiB is a terminal framing error.

The length bound is checked before allocation. Responses are validated and serialized through a bounded writer before any bytes are emitted, then written with complete-write semantics and flushed. Framing errors preserve the I/O stage without including payloads, tokens, or private paths.

Framing is not the SDK contract; generated protocol records are. Protocol v1 and v2 currently share the same request shape, so the first bridge framing implementation normalizes that common shape. The production dispatcher must still select and encode the exact negotiated version before this boundary can claim support for divergent future versions.

## Transaction Meaning

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

`Succeeded` means every declared frame received one terminal receiver-transport delivery and required finalization completed. It does not claim that a child applied the frame or that a person observed the intended result. The protocol therefore reports delivered frame count, transport side-effect certainty, whether live transport began, automatic-retry policy, and separate child-application confirmation.

Partial or uncertain transport is never retried automatically. A client resolves an ambiguous response through transaction outcome lookup, not by submitting the write again. A later visible result does not rewrite a terminal timeout history.

## Ownership And Atomicity

Resources are keyed by logical device and generic domain: lighting, settings, or pairing. This supports arbitrary receivers and multiple children without fixed mouse/keyboard slots.

Lease acquisition is atomic. If any requested resource is owned by another client, none are granted. Forced takeover is not part of protocol version 1. Read-only snapshots, telemetry, and diagnostics remain shared.

Atomicity describes admission, ownership, ordering, and complete transaction accounting. It does not invent physical rollback or simultaneous visible child application where hardware cannot prove either claim.

## Backpressure

Backpressure is policy-specific:

- current unsent effect frames may be coalesced per resource because an older frame is obsolete;
- static lighting, settings, pairing, and restoration preserve strict order and return busy when their bounded queue cannot accept work;
- one logical device's outage must not stall its sibling;
- event and diagnostic journals are bounded history rings, not work queues;
- logging output is best effort through a bounded nonblocking sink and can never stop transport.

Every deadline uses an injected monotonic clock. Kernel session expiration may use Linux boottime inside the concrete kernel boundary so suspend remains meaningful. Durable capture timestamps use a separate wall-clock boundary.

## Restoration

Persistence stores semantic stable intent and exact profile identity. It never stores a live lease, session authorization, route observation, raw frame, or software-effect phase.

Restoration requires a fresh qualified generation, current routes, a matching profile digest, new ownership, and one durable lifecycle claim. A surviving claim, partial checkpoint, or failed target blocks a false complete result. Software effects remain application computations and restart through the application's saved startup profile.

The production persistence adapter keeps one strict, schema-versioned document in a private service-owned directory. It holds an advisory writer lock, rejects symlinks and broadly readable files, bounds bytes and receiver-scoped records, and writes through a same-directory temporary file followed by file sync, atomic replacement, and directory sync. A failure before replacement changes neither disk nor the in-process compare-and-set view. A directory-sync failure reports uncertain durability after advancing the in-process view to the already-visible replacement, so a stale retry conflicts instead of silently replaying an old revision.

## Protocol Evolution

Clients offer a protocol range and optional feature identifiers. The bridge selects one exact version and emits that version's exact record shapes. Hardware capability discovery is separate from protocol feature negotiation.

Values that may exceed IEEE-754's exact integer range, including receiver generations, monotonic instants, event sequences, and stream epochs, use canonical decimal strings on JSON-compatible wire surfaces. They remain bounded integer types inside native implementations.
