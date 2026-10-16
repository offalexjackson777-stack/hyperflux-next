# Release Status

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **HyperFlux Next 2.1.0 is the current release.** It is available for Arch
> Linux x86_64 from the signed HyperFlux package repository.
> Read the [v2.1.0 release notes](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.1.0).
<!-- HFX-GENERATED-END: release-status -->

## What Is Out Now

The current release runs on Arch Linux on x86_64 computers. It comes in two
editions:

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

HyperFlux 2.2 will stop accepting old versions of its local SDK protocol. The
bridge will speak only versions 5 and 7. The OpenRGB integration and the SDK
that ship with HyperFlux already use those versions, so nothing changes for you
if you only use them.

If you run your own program that talks to HyperFlux and it only offers
versions 1 to 4 or 6, it will be refused with error `HFX-PROTOCOL-001`. The
error message ends with the versions HyperFlux accepts, for example
`Supported protocol versions: 5, 7.` Update your program to version 5 or 7. The
[SDK connector guide](../developer/sdk-connectors.md#retired-protocol-versions-22)
lists the details.

## What Comes Next

The [roadmap](roadmap.md) lists the planned changes.
