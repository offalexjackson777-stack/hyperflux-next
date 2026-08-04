# HyperFlux Next

HyperFlux Next 2.0 makes supported Razer devices paired through HyperFlux V2
available to ordinary Linux applications without replacing those applications.

[![Verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
[![License: GPL-2.0-only](docs/assets/badge-license.svg)](LICENSE)

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **Arch 2.0.0 release.** Arch Linux x86_64 Core and Full 2.0.0-1 packages are published through the signed HyperFlux Next third-party Pacman repository backed by GitHub Release v2.0.0.
<!-- HFX-GENERATED-END: release-status -->

## What It Does

- **Discovers paired children naturally.** Capability-based routing needs no
  serial, USB-port, or machine-specific binding.
- **Preserves stock applications.** OpenRazer, Polychromatic, and OpenRGB keep
  their public APIs, controls, and user experience.
- **Fails closed.** Stale, unknown, or unavailable routes stay non-writable and
  `hyperfluxctl doctor` reports the first missing boundary.

## Packages

<!-- HFX-GENERATED-BEGIN: recommended-package -->
**Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI, and base services.

**Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility transport and OpenRGB integration for optional stock applications.

The Arch Linux x86_64 packages are published in
[GitHub Release v2.0.0](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.0).
The signed project-operated Pacman repository resolves Full and its exact Core
dependency with `sudo pacman -S hyperflux-next-full`. It is a third-party
repository, not an official Arch Linux repository. See
[installation](docs/user/installation.md) for the one-time key and repository setup.
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

Use the [documentation hub](docs/README.md), [installation guide](docs/user/installation.md),
[first-run guide](docs/user/first-run.md), and [troubleshooting guide](docs/user/troubleshooting.md).
Project links: [Support](SUPPORT.md), [Contributing](CONTRIBUTING.md),
[Journey](docs/project/journey.md), [Security](SECURITY.md), and [License](LICENSE).
