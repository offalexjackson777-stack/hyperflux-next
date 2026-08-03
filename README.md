# HyperFlux Next

HyperFlux Next 2.0 makes supported receiver-paired Razer devices available to
ordinary Linux applications without replacing those applications.

[![Verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
![License](docs/assets/badge-license.svg)

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **Unreleased.** Release 8 is an accepted private baseline; reconstructed 2.0 artifacts and publication remain unauthorized. Exact installed release 8 is frozen, verified, and accepted as the private reconstruction oracle.
<!-- HFX-GENERATED-END: release-status -->

## Why HyperFlux Next

- **Plug and play:** capability-based discovery publishes each qualified child
  route without serial-, port-, or machine-specific bindings.
- **Application friendly:** stock OpenRazer, Polychromatic, and OpenRGB keep
  ownership of device controls and user experience.
- **Fail closed:** bounded transport, exact route generations, and Doctor make
  missing authority visible instead of guessing.

## Install

<!-- HFX-GENERATED-BEGIN: recommended-package -->
The recommended Arch experience is **Full `2.0.0-1`**, which keeps the lean
Core transport and adds packaged integration for stock OpenRazer, Polychromatic,
and OpenRGB. No public package channel is authorized yet. After publication,
the ordinary command will be:

```sh
sudo pacman -S hyperflux-next-full
```

See [Arch installation](docs/user/installation.md) and the
[distribution roadmap](docs/project/roadmap.md).
<!-- HFX-GENERATED-END: recommended-package -->

## First Run

After installation and explicit service activation, run `hyperfluxctl doctor`.
A Ready result means the required Core and installed integration boundaries are
healthy; any missing route or stock-application boundary is reported with a
specific next step. Start with the [first-run guide](docs/user/first-run.md).

## Compatibility

<!-- HFX-GENERATED-BEGIN: compatibility-summary -->
- Stock OpenRazer `3.12.4` userspace,
  stock Polychromatic, and stock OpenRGB remain application owners.
- The source-validated catalog contains 10 operational routes and
  2 research-only candidates; physical fixtures do not form an allowlist.
- Restoration ships disabled. Input-reactive Ripple and OpenRGB's false Quartz
  colorway label remain accepted external limitations.
- Current 2.0 software status: 6 of 10 release gates are ready in software.
<!-- HFX-GENERATED-END: compatibility-summary -->

## Learn And Contribute

Read the [documentation hub](docs/README.md), get help through
[Support](SUPPORT.md), review [Contributing](CONTRIBUTING.md), follow the
[project journey](docs/project/journey.md), and see the
[licensing policy](docs/legal/licensing.md).
