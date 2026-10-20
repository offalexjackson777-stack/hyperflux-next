# SDK And Connectors

A connector is the code that lets an application, such as OpenRazer or OpenRGB, use
devices behind a HyperFlux receiver. Connectors need to read device state and send
commands without learning how the receiver works, and without being able to break other
applications or send a command twice. This page states what a connector may rely on and
what it must leave to the SDK and the bridge.

Terms used here:

- **SDK**: the published client libraries in `sdk/`, generated from the versioned protocol.
- **Bridge**: the `hyperflux-next-bridge` service, the only program that writes to the
  receiver.
- **Route**: the bridge's opaque handle for one device on one receiver connection.
- **Generation**: one physical connection of the receiver; a replug starts a new one.
- **Qualified**: matched to a known, tested profile. Only qualified routes carry writes.

The [SDK architecture](../architecture/sdk.md) explains how the SDK is built, and the
generated [protocol reference](../generated/bridge-protocol.md) lists every message.

## Stable Boundary

A connector consumes the bridge's decisions through the SDK. It does not copy OpenRazer or
OpenRGB internals, open device files, build receiver frames, or guess hardware identity
from the local machine.

A connector:

- opens the default local SDK channel, whose peer credentials are verified;
- negotiates one supported protocol version and its required features;
- reads typed receiver, child, profile, generation, route, and availability facts from the
  bridge;
- treats route tokens, lease IDs, request IDs, and generation IDs as opaque;
- acquires only the typed resources needed for a bounded operation; and
- closes its SDK client so bridge-owned leases and queued work are revoked.

The SDK owns framing, bounded messages, negotiation credentials, request and
server-instance attribution, and exact-version conversion. The bridge owns capability
discovery, write policy, queueing, transport, persistence, and hardware authority. A
connector must not create a second protocol definition or call bridge implementation
modules.

We drew the line here so that one place decides what may be written to the receiver. The
cost is that a connector cannot add a capability on its own; it has to wait for the
protocol and bridge to support it.

## Capability Rules

Connectors choose behaviour from the negotiated protocol features and the current
source-validated integration view. Product IDs, serials, receiver IDs, USB paths, layouts,
colorways, and the two local assurance devices never decide what a connector may do.
Unknown, unavailable, unqualified, stale-generation, or unsupported routes remain visible
facts but receive no write authority.

Full-package adapters may translate an upstream application's existing commands through
this boundary. They do not move application lifecycle, presentation, effects, or
configuration into HyperFlux Core.

## Compatibility

The bridge serves protocol versions 5 and 7, and both remain immutable compatibility
contracts. Connectors request only the features they need and fail locally when the selected
version cannot represent an operation. New protocol versions use generated types and
conformance fixtures; they do not revise old wire definitions.

### Retired protocol versions (2.2)

Release 2.2 retires protocol versions 1, 2, 3, 4 and 6. What this means for a connector:

- The bridge negotiates only versions 5, 7 and 8. The bundled SDK clients use 5 (lighting)
  and 8 (native tunnel with charging state), so the bundled OpenRGB integration is
  unaffected. Version 7 stays served for clients built against earlier releases.
- A client that offers only versions 1 to 4, or only version 6, receives a structured
  `HFX-PROTOCOL-001` incompatible-version error with the `update-protocol-peer`
  remediation. The connection stays open, so the client can send a new hello with a served
  version.
- The error message ends with the exact list of served versions, for example
  `Supported protocol versions: 5, 7, 8.` Read that list, not a range: version 6 lies
  between 5 and 7 but is not served. The error catalog also defines an optional
  `server_supported_versions` detail (`5,7,8`) next to the existing minimum and maximum
  fields, which keep their meaning (the lowest and highest served version).
- Everything version 6 added (the return-lifecycle projection) is available in versions 7
  and 8.
- Integrations pinned to version 4 or earlier must move to version 5 or later.
- Code that vendored the SDK source and includes `protocol_v1` to `protocol_v4` or
  `protocol_v6` generated headers or Python modules directly must switch to the
  `protocol_v5`, `protocol_v7` or `protocol_v8` ones; the retired files are no longer generated.
- Limitation: the frozen version 5, 7 and 8 error envelope has no detail fields, so the bridge
  cannot send the served list as structured data without a new protocol version. The list
  arrives only inside the message text. Do not parse that text; offer 5 or 8.

An uncertain write result is never replayed automatically. A connector first queries the
original transaction identity, or closes and reconnects as the SDK recovery contract
describes. Clean EOF, malformed framing, peer mismatch, server error, and uncertain
delivery remain distinct failures.

## Minimal Example

[`sdk/python/examples/read_integration_view.py`](../../sdk/python/examples/read_integration_view.py)
connects through the normal desktop proxy, or the system socket when run as root. It
requests only the read-only integration view, prints aggregate route counts, and closes the
client. It contains no device-specific identity and no hardware access.

The example is read-only on purpose. A connector that writes must also use the SDK's typed
lease and transaction APIs, keep generation and profile bindings, and add
integration-specific conformance and failure tests before it can become a packaged
adapter.
