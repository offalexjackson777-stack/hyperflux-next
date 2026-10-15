# Roadmap

This page lists what has shipped, what comes next, and what is planned for
later. Nothing under Next or Later is available yet.

## Shipped

- **2.0.0:** the first release, with the Core and Full packages for Arch Linux
  x86_64.
- **2.0.1:** a reliability update. Shutdown no longer stalls on the OpenRazer
  relay or the desktop socket. The relay recovers when the driver fails to bind.
  Leftover drivers from HyperFlux pre-release builds that take over the receiver
  are now detected, and Doctor explains how to move them aside. The OpenRazer
  drivers HyperFlux needs load at boot, and the OpenRGB plugin works with
  OpenRGB 1.0.

Install the current release from the signed HyperFlux Pacman repository, as
shown in the [installation guide](../user/installation.md). See the
[release status](release-status.md) for details.

## Next

- **A 2.0.x update with reconnect fixes.** Devices that are switched off and on
  come back faster, battery readings are matched to the right request, and
  OpenRazer stops reporting errors when a device sleeps and returns. These fixes
  are in the source code but not yet in a published package.
- **An AUR package.** Publishing HyperFlux Next on the Arch User Repository is
  planned. It is not published yet; until it is, use the
  [installation guide](../user/installation.md).

## Later

- **Other distributions.** Ubuntu, Debian, and Fedora packages are planned as
  separate pieces of work. Each will be published only when it works on its own;
  a delay on one does not hold back another.
- **Signing key rotation.** The repository signing key will be replaced before
  it expires, following the published fingerprint procedure.

## What Will Not Change

- HyperFlux's own lighting restore stays off by default.
- Device support depends on what HyperFlux knows about a device, not on a fixed
  list of tested devices.
- You install and update HyperFlux with your distribution's package manager.
- Stock OpenRazer, Polychromatic, and OpenRGB stay separate projects that
  HyperFlux does not own.
