# HyperFlux Next

Your Razer keyboard and mouse on a HyperFlux receiver, working in OpenRazer,
Polychromatic, and OpenRGB on Linux.

[![Full verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/full-verification.yml/badge.svg?branch=main)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/full-verification.yml?query=branch%3Amain)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
[![License: GPL-2.0-only](docs/assets/badge-license.svg)](LICENSE)

<!-- HFX-GENERATED-BEGIN: release-summary -->
**HyperFlux Next 2.0.2 is available for Arch Linux x86_64.** Read the
[v2.0.2 release notes](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.2).
<!-- HFX-GENERATED-END: release-summary -->

With HyperFlux Next 2.0, a keyboard and mouse that share one HyperFlux
receiver show up in your usual lighting and settings apps. You keep the same
apps and use them the usual way.

## Does My Device Work?

<!-- HFX-GENERATED-BEGIN: compatibility-summary -->
Unless a device says otherwise, it works in OpenRazer, Polychromatic, and
OpenRGB.

**Tested on real hardware.** These work through the receiver:

- Razer Basilisk V3 Pro 35K (mouse)
- Razer DeathStalker V2 Pro Tenkeyless (keyboard)

**Expected to work.** Nobody has tested these 8 devices with HyperFlux on real
hardware yet, but they should work:

- Razer Naga V2 Pro (mouse, OpenRGB only)
- Razer Basilisk V3 Pro (mouse)
- Razer Cobra Pro (mouse)
- Razer BlackWidow V3 Pro (keyboard)
- Razer DeathStalker V2 Pro (keyboard)
- Razer BlackWidow V4 Mini HyperSpeed (keyboard, OpenRazer and Polychromatic only)
- Razer BlackWidow V4 Low-profile Tenkeyless HyperSpeed (keyboard, OpenRGB only)
- Razer BlackWidow V4 Tenkeyless HyperSpeed (keyboard)

**Not usable yet.** 2 more devices are known, but they need support in OpenRazer
or OpenRGB first: Razer Cobra HyperSpeed and Razer BlackWidow V4 Low-profile
HyperSpeed.

HyperFlux Next is tested with OpenRazer `3.12.4`.
<!-- HFX-GENERATED-END: compatibility-summary -->

The [hardware guide](docs/user/hardware.md) explains what each group means and
how to check a device that is not listed. The
[known limitations](docs/user/limitations.md) list what does not work yet.

## Install

<!-- HFX-GENERATED-BEGIN: recommended-package -->
You need Arch Linux on an x86_64 computer and the receiver, with your keyboard
and mouse paired to it.

1. **Add the HyperFlux package repository.** You do this once. Follow
   [step 1 of the installation guide](docs/user/installation.md#1-add-the-hyperflux-repository-one-time-only).
   It is a signed project-operated Arch repository,
   not an official Arch Linux repository.
2. **Install HyperFlux Next.**

   ```bash
   sudo pacman -Syu hyperflux-next-full
   ```

   This installs Full, the recommended package. It brings Core with it. If you
   only want the receiver driver and the SDK, without the app connections,
   install Core instead: `sudo pacman -Syu hyperflux-next-linux`.
3. **Turn on HyperFlux and restart.** Keep the receiver plugged in.

   ```bash
   sudo systemctl enable hyperflux-next-bridge.service \
     "hyperflux-next-client@$(id -u).socket"
   sudo reboot
   ```

4. **Check that it works.** After you log in again, run:

   ```bash
   hyperfluxctl doctor
   ```

   You should see `HyperFlux Next is ready.` If you see
   `HyperFlux Next needs attention.` instead, do what its `Do:` line says.

Later updates arrive with your normal `sudo pacman -Syu`.
<!-- HFX-GENERATED-END: recommended-package -->

The [installation guide](docs/user/installation.md) explains each step and the
choice between Full and Core.

## Next Steps

- [First run](docs/user/first-run.md): open your apps and check your devices.
- [Applications](docs/user/applications.md): what OpenRazer, Polychromatic, and
  OpenRGB can do with HyperFlux.
- [Troubleshooting](docs/user/troubleshooting.md): fix a missing device or a
  failed check.
- [Updates and removal](docs/user/updates-removal.md): update, go back, or
  remove HyperFlux.
- [Documentation hub](docs/README.md): every guide, plus how HyperFlux works and
  how it was built.

[Get help](SUPPORT.md) · [Contribute](CONTRIBUTING.md) ·
[Project journey](docs/project/journey.md) · [Security](SECURITY.md) ·
[License](LICENSE)
