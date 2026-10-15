# Design Book Chapter V: Application Integrations

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

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

Spectrum Cycling, Rainbow Wave, Breathing, and the broader Effects catalog
belong to OpenRGB’s official effect engine. HyperFlux does not recreate or claim
those effects. It carries the same upstream-generated Feature reports through a
bounded, fair native exchange and returns a matching application-form response
after any declared receiver selector is restored. Tests verify animation
continuity, sibling-device independence, rescan behavior, reconnect recovery,
and transport latency rather than reimplementing effect mathematics.

## 20. Application-Owned Persistence

Application lighting persistence belongs to OpenRGB. Stable colors, Off,
hardware modes, and software-effect startup profiles are represented and resumed
by the upstream application exactly as they are for direct devices. HyperFlux
reports route loss and return without inventing sleep state, retaining colors,
replaying wake frames, or promising a wake-color deadline. Its contract is that
the first valid upstream transaction after a writable return is transported
without an intentional restoration timer.

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

Lighting is only one part of the upstream experience. DPI stages, independent
X/Y DPI, polling rate, acceleration, scroll behavior, Smart Reel, idle timeout,
low-battery threshold, gaming mode, macros, brightness, charging state, pairing,
and future model behavior remain owned by OpenRazer or OpenRGB. HyperFlux
qualifies whether application-owned command bytes, declarative receiver routing,
and matching responses cross a route; it does not add one semantic SDK method or
command translation per setting. A different report format is an explicit
backend requirement rather than a guessed rewrite.

---

- Previous:
  [Chapter IV: Universality And Qualification](04-universality-and-qualification.md)
- [Design Book index](../design-book.md)
- Next: [Chapter VI: Application Ownership](06-application-ownership.md)
