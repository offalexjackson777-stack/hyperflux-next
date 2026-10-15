# Design Book Chapter V: Application Integrations

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

Users already have OpenRGB, OpenRazer, and Polychromatic, and those projects already hold
the commands, layouts, and effects for each Razer model. Rewriting any of that inside
HyperFlux would duplicate work and drift from upstream. This chapter describes how each
application reaches the receiver while keeping its own behaviour.

## 18. OpenRGB Integration

OpenRGB receives qualified native routes through a dedicated integration that
depends only on HyperFlux SDK protocol v7. The integration instantiates the
pinned upstream Razer controller with an alternate Feature transport; OpenRGB
remains responsible for commands, modes, layouts, LED labels, geometry, color
editing, profiles, effects, and application behavior. HyperFlux supplies exact
wireless identity, route availability, generation safety, and an opaque 90-byte
request/response exchange. Ordinary hidapi controllers remain unchanged, and
serial-proven direct USB takes precedence over only the matching receiver route.

## 19. OpenRGB Effects

Software effects such as Spectrum Cycling, Rainbow Wave, and Breathing come from
the optional OpenRGB Effects Plugin, a separate upstream plugin that users
install alongside OpenRGB. HyperFlux does not bundle, recreate, or claim those
effects. The plugin drives a device through Direct-mode frames, and HyperFlux
carries the Feature reports that OpenRGB builds for each frame through a
bounded, fair native exchange. It returns a matching application-form response
after any declared receiver selector is restored. Software contract tests cover
sibling-device independence, rescan, and reconnect recovery rather than effect
mathematics; visible animation continuity and latency on real devices still need
physical evidence.

## 20. Application-Owned Persistence

Application lighting persistence belongs to the application. OpenRGB decides how
stable colors, Off, hardware modes, and software-effect startup profiles are
saved and reapplied, and HyperFlux does not change that behavior for receiver
devices or promise that it matches direct devices. HyperFlux reports route loss
and return without inventing sleep state, retaining colors, replaying wake
frames, or promising a wake-color deadline. Its contract is that the first valid
upstream transaction after a writable return is transported without an
intentional restoration timer. Whether OpenRGB reapplies a startup profile when
a route returns still needs physical qualification. Applications do not read
lighting back from the device, so device lighting can differ from what an
application last set; the
[known limitations](../../user/limitations.md) describe
the known cases.

## 21. OpenRazer Native Route

OpenRazer contains the device classes, method lists, images, settings,
persistence rules, and command construction that its clients already expect.
HyperFlux imports source-bound identity and transport facts but does not
duplicate those semantics. A narrow pinned source transform lets the official
OpenRazer driver send its ordinary 90-byte Feature reports through trusted
feature-only UHID routes while preserving the ordinary USB path for direct
devices. HyperFlux may adapt only a receiver-profile route selector and restores
it before returning the response.

## 22. Stock OpenRazer Userspace Compatibility

The official OpenRazer daemon remains the sole `org.razer` provider. HyperFlux
patches only the incompatible kernel transport boundary and tests stock daemon
and client releases against that ABI. It does not ship a private daemon, Python
API, D-Bus provider, session owner, or lifecycle replacement.

## 23. Stock Polychromatic Through OpenRazer

Polychromatic uses its ordinary OpenRazer backend, official D-Bus API, device
presentation, and controls. Receiver-backed devices appear through the patched
OpenRazer transport like their corresponding wireless classes. HyperFlux ships
no Polychromatic backend, source patch, launcher, state transport, or
application file.

## 24. Upstream Device Settings

Lighting is only one part of the upstream experience. Settings such as DPI
stages, independent X/Y DPI, polling rate, acceleration, scroll behavior, Smart
Reel, idle timeout, low-battery threshold, gaming mode, macros, brightness,
charging state, and pairing remain owned by OpenRazer or OpenRGB. Each one is
available only where the upstream application supports it for that exact model,
and HyperFlux does not add a setting that the upstream model lacks. HyperFlux
qualifies whether application-owned command bytes, declarative receiver routing,
and matching responses cross a route; it does not add one semantic SDK method or
command translation per setting. A different report format is an explicit
backend requirement rather than a guessed rewrite. Native-route parity for these
settings still needs physical evidence, and known differences such as
Polychromatic's Ripple effect showing no reactive lighting are listed in the
[known limitations](../../user/limitations.md).

---

- Previous:
  [Chapter IV: Universality And Qualification](04-universality-and-qualification.md)
- [Design Book index](../design-book.md)
- Next: [Chapter VI: Application Ownership](06-application-ownership.md)
