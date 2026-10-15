# Release Status

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **HyperFlux Next 2.0.1 is the current release.** It is available for Arch
> Linux x86_64 from the signed HyperFlux package repository.
> Read the [v2.0.1 release notes](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.1).
<!-- HFX-GENERATED-END: release-status -->

## What Is Out Now

The current release runs on Arch Linux on x86_64 computers. It has two
packages:

- **Full (`hyperflux-next-full`):** the recommended package. Your keyboard and
  mouse work in OpenRazer, Polychromatic, and OpenRGB.
- **Core (`hyperflux-next-linux`):** the receiver driver, background service,
  `hyperfluxctl`, and SDK on their own.

The release notes page linked above also carries both package files, their
checksums, and a software bill of materials.

## Where Packages Come From

Pacman installs and updates HyperFlux from the signed HyperFlux package
repository. It is a signed project-operated Arch repository, not an official
Arch Linux repository. Its key fingerprint is
`8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8`.

The [installation guide](../user/installation.md) shows how to add the
repository, and how to install the package files directly if you cannot.

## What Comes Next

Every 2.0.x update, including 2.0.2, keeps the same two packages. Version 2.1.0
will split them into four smaller packages. It ships only after moving from two
packages to four has been tested on real hardware. The [roadmap](roadmap.md)
lists the planned changes.
