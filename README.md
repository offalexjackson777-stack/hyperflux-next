# HyperFlux Next

HyperFlux Next 2.0 makes supported Razer devices paired through HyperFlux V2
available to ordinary Linux applications without replacing those applications.

[![Full verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/full-verification.yml/badge.svg?branch=main)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/full-verification.yml?query=branch%3Amain)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
[![License: GPL-2.0-only](docs/assets/badge-license.svg)](LICENSE)

<!-- HFX-GENERATED-BEGIN: release-summary -->
**HyperFlux Next 2.0.1 is available for Arch Linux x86_64.** Read the
[v2.0.1 release notes](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.1).
<!-- HFX-GENERATED-END: release-summary -->

## What It Does

- **Discovers paired children naturally.** Capability-based routing needs no
  serial, USB-port, or machine-specific binding.
- **Preserves stock applications.** OpenRazer, Polychromatic, and OpenRGB keep
  their public APIs, controls, and user experience.
- **Fails closed.** Stale, unknown, or unavailable routes stay non-writable and
  `hyperfluxctl doctor` reports the first missing boundary.

## Packages

<!-- HFX-GENERATED-BEGIN: recommended-package -->
Choose one edition. Full is recommended for desktops; Core is an advanced choice.

- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility
  transport and OpenRGB integration for optional stock applications.
- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK,
  CLI, and base services.

Stock OpenRGB, OpenRazer, and Polychromatic are separate Arch packages. Install
the ones you want to use; HyperFlux does not install or replace them.

### Install

1. Add the signed HyperFlux Pacman repository once. Follow the
   [one-time setup](docs/user/installation.md#1-add-the-hyperflux-repository---one-time-only).
   It is a signed project-operated Arch repository, not an official Arch Linux repository.
2. Install Full:

   ```bash
   sudo pacman -Syu hyperflux-next-full
   ```

   Full pulls in the matching Core package. To install Core alone instead, run
   `sudo pacman -Syu hyperflux-next-linux`.
3. Enable the services and reboot. Keep the receiver plugged in.

   ```bash
   sudo systemctl enable hyperflux-next-bridge.service \
     "hyperflux-next-client@$(id -u).socket"
   sudo reboot
   ```

4. After you log in, check the result:

   ```bash
   hyperfluxctl status
   hyperfluxctl doctor
   ```

   Doctor prints `HyperFlux Next is ready.` when everything works. Otherwise it
   names the first problem and one thing to do about it.

Later updates arrive with your normal `sudo pacman -Syu`. The
[first-run guide](docs/user/first-run.md) shows how to open your applications,
and the [installation guide](docs/user/installation.md) covers key checks and
the offline package fallback.
<!-- HFX-GENERATED-END: recommended-package -->

## Hardware And Applications

<!-- HFX-GENERATED-BEGIN: compatibility-summary -->
- Stock OpenRazer `3.12.4` userspace,
  stock Polychromatic, and stock OpenRGB remain application owners.
- 2 devices are physically proven,
  8 are source-qualified operational
  candidates, and 2 are blocked or
  research-only. The physical fixtures are not an allowlist.
- Restoration ships disabled.
<!-- HFX-GENERATED-END: compatibility-summary -->

See the [hardware tiers](docs/user/hardware.md) and
[application guide](docs/user/applications.md) for the exact support boundary.

## Start Here

Use the [documentation hub](docs/README.md),
[installation guide](docs/user/installation.md),
[first-run guide](docs/user/first-run.md), and
[troubleshooting guide](docs/user/troubleshooting.md).
Project links: [Support](SUPPORT.md), [Contributing](CONTRIBUTING.md),
[Journey](docs/project/journey.md), [Security](SECURITY.md), and [License](LICENSE).
