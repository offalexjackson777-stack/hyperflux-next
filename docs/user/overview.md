# HyperFlux Next

HyperFlux Next is a clean Linux foundation for supported devices connected
through the Razer HyperFlux V2 receiver and mat. It presents each qualified
mouse or keyboard to applications while the receiver and mat remain the shared
transport layer.

The product is deliberately split into layers. The kernel driver preserves HID
input and exposes a bounded receiver session. The local bridge owns
qualification, application ownership, transactions, restoration, and
diagnostics. Application adapters use the versioned SDK and do not send raw USB
or HID reports.

> **Current state:** this repository is still a local reconstruction. It is not
> an authorized release, package channel, or published support claim.

## What Users Eventually Gain

- truthful discovery of independently paired devices;
- exact qualified names, layouts, zones, and battery observations;
- application lighting through OpenRGB and native compatibility adapters;
- stable lighting restoration after qualified lifecycle events;
- one understandable Doctor result and privacy-safe support evidence;
- updates that distinguish userspace changes from kernel activation.

See [Supported hardware](../generated/supported-hardware.md) for evidence-backed
coverage and [Applications](../generated/integrations.md) for integration state.
