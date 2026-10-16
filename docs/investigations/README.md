# Investigations

These pages tell the story of problems that reached people using HyperFlux Next. Each one
says what people saw, how the cause was found, what was changed, how the change was
checked, and what is still waiting for proof on real hardware.

They are written for engineers who want to understand why the code looks the way it does,
and for anyone who hit one of these problems and wants to know whether it is fixed.

## Words Used On These Pages

- **Receiver**: the HyperFlux USB dongle. One receiver can serve a keyboard and a mouse at
  the same time; each of these is a **child** of the receiver.
- **Bridge**: the `hyperflux-next-bridge` service. It is the only program that writes to
  the receiver, and it hands each application a route to a child.
- **Route**: the bridge's handle for one child on one receiver connection. A route is
  either available or not, and only a qualified route carries commands.
- **Generation**: one physical connection of the receiver. Unplugging and replugging the
  receiver starts a new generation, so state from the old connection cannot leak into the
  new one.
- **Qualification**: the check that a receiver or device matches a known, tested profile
  before HyperFlux sends it commands.
- **Relay**: the `hyperflux-next-openrazer-relay` service in the Full package. It creates
  one virtual Razer HID device per available child, so OpenRazer's kernel drivers can bind
  to it. HyperFlux ships those drivers with one patch that teaches them this virtual
  transport; the OpenRazer daemon and Polychromatic are unchanged.
- **Doctor**: `hyperfluxctl doctor`, the diagnosis command that names what is not ready
  and what to do next.

The [design book](../architecture/design-book.md) explains these ideas in depth.

## Problems

| Problem | What people saw | Fixed in | Hardware proof |
| --- | --- | --- | --- |
| [Shutdown hang](shutdown-hang.md) | Shutdown waited about 90 seconds | 2.0.1 | Checked in [#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36) |
| [Retired DKMS driver](retired-dkms-driver.md) | After a kernel update, nothing worked but Doctor said "driver active" | 2.0.1 | Checked in [#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36) |
| [Mouse missing after boot](mouse-missing-after-boot.md) | A mouse already on at boot did not appear until power-cycled | 2.0.1 | Checked in [#32](https://github.com/offalexjackson777-stack/hyperflux-next/issues/32) |
| [Doctor restart advice](doctor-restart-advice.md) | Doctor told people to restart a bridge that was working | 2.0.1 | Checked in [#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36) |
| [Late return after sleep or power-off](late-return-after-sleep.md) | A device came back up to 30 seconds late; Polychromatic stayed stale | 2.0.1 and 2.0.2 | Checked in [#38](https://github.com/offalexjackson777-stack/hyperflux-next/issues/38) and [#39](https://github.com/offalexjackson777-stack/hyperflux-next/issues/39) |
| [OSError 131 after sleep](oserror-131-teardown.md) | OpenRazer raised OSError 131 around sleep and wake | 2.0.2 | Checked in [#37](https://github.com/offalexjackson777-stack/hyperflux-next/issues/37) |
| [Battery readings jump](battery-readings-jump.md) | Battery level jumped between unrelated values | 2.0.2 | Checked in [#44](https://github.com/offalexjackson777-stack/hyperflux-next/issues/44) |
| [OpenRGB 1.0 port](openrgb-1-0-port.md) | The OpenRGB plugin would not load on OpenRGB 1.0, then crashed OpenRGB on quit | 2.0.1 | Checked with 2.1.0 |

## Related Decisions

Some problems lead to a design decision instead of a single fix. Those are kept as decision
records:

- [OpenRazer patch removal routes](../architecture/decisions/openrazer-patch-removal.md)
  compares two ways HyperFlux could stop shipping its patched OpenRazer driver.

## Adding A Page

Add a page when a problem reached users and the fix taught something about the design.
Follow the order in the [writing guide](../developer/writing-guide.md#investigations):
what people saw, how it was tracked down, the cause, the fix, how it was checked, and what
is still open. Quote only what the issues, commits, and logs actually say.
