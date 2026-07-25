# HyperFlux Next

HyperFlux Next is a thin Linux transport bridge for supported devices connected
through the Razer HyperFlux V2 receiver and mat. It gives upstream applications
a qualified route to each paired mouse or keyboard while the receiver and mat
remain the shared physical transport.

The product is deliberately split into layers. The kernel driver preserves HID
input and exposes bounded receiver exchanges. The local bridge owns exact route
identity, qualification, generations, NativeControl arbitration, transaction
certainty, availability, and diagnostics. Thin application adapters use the
versioned SDK to carry one qualified 90-byte Razer Feature report and its
matching response; they cannot submit arbitrary USB setup packets or receiver
carrier frames.

OpenRazer and OpenRGB remain responsible for device commands, capabilities,
images, layouts, DPI, polling, macros, lighting modes, effects, profiles, and
persistence. HyperFlux does not recreate those model semantics.

> **Current state:** this is a public, evidence-bound pre-release source
> repository. It is not an authorized release, package channel, or supported
> product claim.

## What Users Eventually Gain

- truthful discovery of independently paired routes and exact wireless VID/PID;
- upstream names, layouts, zones, settings, and effects through their original
  OpenRazer and OpenRGB implementations;
- bounded bidirectional request/response transport with explicit failure
  certainty and no automatic retry after a possible write;
- generation-safe availability and serial-proven direct-USB handoff without
  suppressing unrelated devices;
- one understandable Doctor result and privacy-safe support evidence;
- updates that distinguish userspace changes from kernel activation.

See [Supported hardware](../generated/supported-hardware.md) for evidence-backed
coverage and [Applications](../generated/integrations.md) for integration state.
