# Design Book Chapter VII: Schemas, SDKs And Reuse

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

The bridge, the SDK, the connectors, and the tests are written in several languages, and all
of them must agree on every message. Hand-written copies of the same message would drift
apart. This chapter describes how versioned schemas, generated code, and explicit state
machines keep them in step.

## 28. Schema-First Development

Requests, responses, events, profiles, capabilities, errors, diagnostics,
evidence, and test definitions use versioned schemas. Schemas define required
fields, bounds, compatibility, privacy classifications, and extension points
before language-specific implementations diverge.

## 29. Generated Code

The generators produce Rust types, C++ SDK structures, Python models, kernel
tables, validators, documentation tables, and fixtures from canonical sources.
Generated files identify their authority, and verification fails on drift.

## 30. Stable HyperFlux SDK

The SDK is the supported contract for integrations. Protocol v5 preserves
logical-device discovery, semantic migration APIs, telemetry, ownership, and
diagnostics. Protocol v7 adds qualified native-route enumeration, bounded
availability events, NativeControl ownership, and exact fixed-size Feature
exchanges while still hiding socket framing, credentials, receiver carrier
frames, and bridge-internal actors. C++, Rust, and Python clients share
generated protocol types and language-appropriate convenience APIs.

## 31. Feature Negotiation

When a client connects, it announces its protocol range and optional features.
The bridge responds with the mutually supported version and capabilities. A
newer bridge can continue serving an older OpenRGB plugin when the required
contract remains compatible. A genuinely incompatible client receives a clear
structured error explaining which component needs updating. This prevents a
package upgrade from producing unexplained missing devices because two binaries
silently disagree about a field.

## 32. Interfaces And Dependency Injection

Core logic depends on interfaces such as `ReceiverTransport`, `Clock`,
`ProfileRegistry`, `LeaseManager`, `PersistenceStore`, and `EventSink`.
Production supplies real implementations; tests supply deterministic substitutes
for time, storage failures, and transport lifecycle cut points.

## 33. Explicit State Machines

Receiver lifecycle, native-route availability, application ownership, and
transactions have declared states and valid transitions. A transaction cannot
move from created directly to delivered; it is validated, ownership-bound,
generation-bound, queued, sent, and terminally recorded. A stale generation
cannot return to active, and a released lease cannot authorize another write.
Restoration is disabled by default and remains an independent policy.

## 34. Event Subscriptions

Applications subscribe to typed events such as RouteAvailable,
RouteTemporarilyUnavailable, RouteWithdrawn, BatteryUpdated, OwnershipChanged,
GenerationReplaced, and TransactionCompleted. Availability events do not claim
sleep, power-off, or child application state. Events contain sequence numbers
and current-state references so a client can detect gaps and request a fresh
snapshot.

## 35. Bounded Concurrency And Backpressure

Every queue, journal, event buffer, and native route must have a defined limit
and overflow policy. Native exchanges retain route-local FIFO ordering, fair
sibling service, deadlines, and explicit certainty; once SET starts, an
uncertain operation is never coalesced or automatically repeated. Slow clients
must not block the receiver actor, and logging must never wait forever for an
unread pipe. Backpressure becomes a structured not-sent result before dispatch.

---

- Previous: [Chapter VI: Application Ownership](06-application-ownership.md)
- [Design Book index](../design-book.md)
- Next:
  [Chapter VIII: Simulation And Verification](08-simulation-and-verification.md)
