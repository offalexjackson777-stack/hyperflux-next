# Installation

HyperFlux Next 2.0 uses two Arch packages. Core provides the receiver driver,
bridge, SDK endpoint, and operations CLI. Full adds packaged integration for
stock OpenRazer, Polychromatic, and OpenRGB.

## Choose A Package

Use Full for the ordinary desktop experience. Core is intended for transport
and connector development where application integration is deliberately
absent. Neither package replaces stock OpenRGB, OpenRazer userspace, or
Polychromatic.

## Availability

No public package channel is authorized yet. The generated
[installation contract](../generated/installation.md) records exact ownership,
dependencies, and lifecycle behavior for candidate qualification.

## Safety Boundary

Package registration does not start services, alter hardware, or authorize a
device write. Activation is a separate reviewed step. Restoration remains
disabled by default.
