# Installation

HyperFlux Next has exactly two user-facing packages.

- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI, and base services.
- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility transport and OpenRGB integration for optional stock applications.

Full is the ordinary desktop choice. Core is useful when only the receiver
transport and SDK are needed. Stock OpenRazer userspace, Polychromatic, and
OpenRGB remain separate, optional distribution packages.

## Availability

Arch Linux x86_64 Core and Full packages are published in
[GitHub Release v2.0.0](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.0).
There is no pacman repository yet, so `pacman -S hyperflux-next-full` is not
available. Download both package files and `SHA256SUMS`, then verify and install
the exact local files:

```bash
sha256sum -c SHA256SUMS
sudo pacman -U -- \
  ./hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst
```

The assets are unsigned. Check the published SHA-256 values; no private signing
key is published.

## Safe Desktop Lifecycle

The normal receiver-only desktop path is:

1. Keep the receiver and devices connected.
2. Install Core and Full together with the command above.
3. Enable the bridge and per-user socket services:
   `sudo systemctl enable hyperflux-next-bridge.service "hyperflux-next-client@$(id -u).socket"`.
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
