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

## Qualified Application Routes

The current OpenRazer native route is intentionally narrow. HyperFlux carries
the standard 90-byte Razer Feature exchange for the exact receiver-backed
Razer Basilisk V3 Pro 35K `1532:00cd` and Razer DeathStalker V2 Pro Tenkeyless
`1532:0296` profiles. A source-bound patch lets the official OpenRazer driver
accept those trusted feature-only virtual HID routes; direct USB devices and
all model behavior remain on the ordinary upstream path.

The [generated deployment contract](../generated/openrazer-native-deployment.md)
names the exact reviewed OpenRazer source, runtime capability, and update
policy. Distribution release revisions of that source are compatible. A new
OpenRazer source line fails closed until the patch is rebased and reverified.
Other models in the upstream catalogs remain research candidates with no
HyperFlux write authority; see [Supported hardware](../generated/supported-hardware.md).

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

See [Applications](../generated/integrations.md) for integration state and the
[installation contract](../generated/installation.md) for package ownership,
Doctor, update activation, and rollback boundaries.
