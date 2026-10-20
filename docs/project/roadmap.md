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
- **2.0.2:** a reconnect update. A keyboard switched off and on comes back
  within seconds, each device shows only its own battery level, and OpenRazer
  no longer reports errors after the computer wakes from sleep.
- **2.1.0:** smaller packages. The kernel drivers ship in their own DKMS
  packages, so Full is now four packages and Core two. `sudo pacman -Syu` moves
  a 2.0.x system to the new packages. The guides were rewritten to be shorter
  and plainer.
- **2.1.1:** battery level and charging status for the mouse and keyboard in
  the desktop battery widget, such as the one in KDE Plasma, and a
  `hyperfluxctl qualification` command that writes a hardware report. A few
  charging states show as unknown; see
  [known limitations](../user/limitations.md#charging-status-is-partial).
- **2.1.2:** Ripple in Polychromatic reacts to key presses through the
  receiver, per-key lighting reaches the keyboard about three times faster,
  each battery shows under its own device name, and Doctor finds two more
  problems that hide OpenRazer devices or keep Ripple still.
- **2.2.0:** OpenRGB gets a HyperFlux Next page in its Information list with
  a card for each paired keyboard and mouse that shows its battery level and
  charging state. A mouse resting on its charging mat shows as charging right
  after login. Every device uses the name OpenRazer gives it. Old versions of
  the local SDK protocol are retired; HyperFlux now accepts versions 5, 7 and
  8, which the bundled SDK and the OpenRGB integration already use.

Install the current release from the signed HyperFlux package repository.
The [installation guide](../user/installation.md) covers Arch Linux, and there
are guides for [Ubuntu](../user/install-ubuntu.md),
[Debian](../user/install-debian.md), [Fedora](../user/install-fedora.md), and
[openSUSE](../user/install-opensuse.md). See the
[release status](release-status.md) for details.

## Next

- **Real-hardware checks on other distributions.** The Ubuntu 26.10, Debian
  14, Fedora 44, and openSUSE Tumbleweed packages are published. They are
  built and tested automatically, but not yet confirmed on real hardware. Each
  one is checked on its own; a delay on one does not hold back another.

## Later

- **More reactive effects.** Reactive and Fire lighting for devices on the
  receiver, and Ripple on both keyboards when two receivers are connected
  ([#54](https://github.com/offalexjackson777-stack/hyperflux-next/issues/54)).
  Today only one of two keyboards reacts; see
  [known limitations](../user/limitations.md#ripple-and-other-effects-that-react-to-keys).
- **Signing key rotation.** The repository signing key will be replaced before
  it expires, following the published fingerprint procedure.

## What Will Not Change

- HyperFlux's own lighting restore stays off by default.
- Device support depends on what HyperFlux knows about a device, not on a fixed
  list of tested devices.
- You install and update HyperFlux with your distribution's package manager.
- OpenRazer, Polychromatic, and OpenRGB stay separate projects that
  HyperFlux does not own.
