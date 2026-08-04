# Roadmap

HyperFlux Next 2.0 keeps a lean Core package and an integration-complete Full
package while preserving stock OpenRazer, Polychromatic, and OpenRGB ownership.

## Current Release

HyperFlux Next 2.0.0-1 is published for Arch Linux x86_64 as reproducible Core
and Full packages. A signed project-operated third-party Pacman repository is
the primary package channel, backed by stable GitHub Release assets.

## Future Targets

1. Maintain the verified Arch reference backend.
2. Evaluate other native distribution backends independently.
3. Advance each distribution only through evidence it actually has.
4. Rotate repository signing material through the documented fingerprint-bound
   release procedure before key expiry.

## Continuing Boundaries

- Restoration remains disabled.
- Device support stays capability-based; physical fixtures do not become an
  allowlist.
- Native package managers remain the installation path.
- A blocked distribution does not demote another independently verified target.
