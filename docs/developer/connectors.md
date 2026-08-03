# Connector Contract

HyperFlux connectors use a published SDK to consume Core authority. They do not
copy OpenRazer or OpenRGB internals, open device files, construct receiver
carrier frames, or infer hardware identity from a local machine.

## Stable Boundary

A connector:

- opens the default local SDK channel, whose peer credentials are verified;
- negotiates one supported protocol version and its required features;
- reads typed receiver, child, profile, generation, route, and availability
  facts from the bridge;
- treats route tokens, lease IDs, request IDs, and generation IDs as opaque;
- acquires only the typed resources needed for a bounded operation; and
- closes its SDK client so bridge-owned leases and queued work are revoked.

The SDK owns framing, bounded messages, negotiation credentials, request and
server-instance attribution, and exact-version conversion. The bridge owns
capability discovery, write policy, queueing, transport, persistence, and
hardware authority. A connector must not create a second protocol definition
or call bridge implementation modules.

## Capability Rules

Connectors select behavior from negotiated protocol features and the current
source-validated integration view. Product IDs, serials, receiver IDs, USB
paths, layouts, colorways, and the two local assurance devices are never
connector authority. Unknown, unavailable, unqualified, stale-generation, or
unsupported routes remain visible facts but receive no write authority.

Full-package adapters may translate an upstream application's already-defined
commands through this boundary. They do not transfer application lifecycle,
presentation, effects, or configuration ownership into Core.

## Compatibility

Protocol versions 1 through 7 remain immutable compatibility contracts.
Connectors request only features they require and fail locally when a selected
version cannot represent an operation. New protocol versions use generated
types and conformance fixtures; they do not revise old wire definitions.

An uncertain write result is never replayed automatically. A connector first
queries the original transaction identity or closes and reconnects according
to the SDK recovery contract. Clean EOF, malformed framing, peer mismatch,
server error, and uncertain delivery remain distinct failures.

## Minimal Example

[`sdk/python/examples/read_integration_view.py`](../../sdk/python/examples/read_integration_view.py)
connects through the ordinary desktop proxy (or the system socket when run as
root), requests only the read-only integration projection, prints aggregate
route counts, and closes the client. It contains no device-specific identity
or hardware access.

The example is intentionally read-only. A write-capable connector must also use
the SDK's typed lease and transaction APIs, preserve generation/profile
bindings, and add integration-specific conformance and failure tests before it
can become a packaged adapter.
