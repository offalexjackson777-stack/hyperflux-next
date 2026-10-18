# Release Status

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **HyperFlux Next 2.1.2 is the current release.** It is available for Arch
> Linux x86_64 from the signed HyperFlux package repository.
> Read the [v2.1.2 release notes](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.1.2).
<!-- HFX-GENERATED-END: release-status -->

## What Is Out Now

The current release runs on Arch Linux on x86_64 computers. Packages for
Ubuntu, Debian, Fedora, and openSUSE are built and tested but not published
yet. The Arch release comes in two editions:

- **Full (`hyperflux-next-full`):** the recommended edition. Your keyboard and
  mouse work in OpenRazer, Polychromatic, and OpenRGB.
- **Core (`hyperflux-next-linux`):** the receiver driver, background service,
  `hyperfluxctl`, and SDK on their own.

Since 2.1.0 the kernel drivers ship in their own packages,
`hyperflux-next-dkms` and, for Full, `hyperflux-next-openrazer-driver-dkms`.
Pacman installs them for you. If you run 2.0.x, `sudo pacman -Syu` moves you to
the new packages; there is nothing else to do.

The release notes page linked above also carries every package file, their
checksums, and a software bill of materials.

## Where Packages Come From

Pacman installs and updates HyperFlux from the signed HyperFlux package
repository. It is a signed project-operated Arch repository, not an official
Arch Linux repository. Its key fingerprint is
`8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8`.

The [installation guide](../user/installation.md) shows how to add the
repository, and how to install the package files directly if you cannot.

## Planned Change In 2.2

HyperFlux 2.2 will stop accepting old versions of its local SDK protocol.
Versions 1 to 4 and 6 are deprecated in 2.1 and will be removed in 2.2. From
2.2 the bridge will speak only versions 5 and 7. The SDK and the OpenRGB
integration that ship with HyperFlux already use those versions, so nothing
changes for you if you only use them.

If you run your own program that talks to HyperFlux, make sure it offers
version 5 or 7. Everything version 6 added is also in version 7. The
[SDK connector guide](../developer/sdk-connectors.md#deprecated-protocol-versions)
lists the details.

## What Comes Next

The [roadmap](roadmap.md) lists the planned changes.
