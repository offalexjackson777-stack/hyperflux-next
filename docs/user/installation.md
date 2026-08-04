# Installation

HyperFlux Next has exactly two user-facing packages.

- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI, and base services.
- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility transport and OpenRGB integration for optional stock applications.

Full is the ordinary desktop choice. Core is useful when only the receiver
transport and SDK are needed. Stock OpenRazer userspace, Polychromatic, and
OpenRGB remain separate, optional distribution packages.

## Availability

No public package channel exists yet, so this repository does not publish a
supported install command. Release artifacts, a version tag, and signing-key
distribution remain separate decisions.

## Safe Desktop Lifecycle

When packages become available, the normal receiver-only desktop path is:

1. Keep the receiver and devices connected.
2. Install Core or Full through the native package manager.
3. Enable the documented bridge and per-user socket services.
4. Reboot normally.
5. Run `hyperfluxctl status` and `hyperfluxctl doctor` after login.

Package registration does not start services, change hardware bindings, or
authorize a device write. Restoration ships disabled and remains independent
of normal device discovery.

> [!WARNING]
> Do not unplug the only keyboard or mouse to complete a normal installation.
> Disconnected activation is an advanced recovery path and requires a built-in
> keyboard and touchpad, a second input pair, or a working SSH session.

The generated [distribution contract](../generated/distributions.md) and
[installation contract](../generated/installation.md) describe exact package
ownership and lifecycle behavior for maintainers.
