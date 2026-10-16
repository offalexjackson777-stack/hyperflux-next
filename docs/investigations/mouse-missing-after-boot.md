# Mouse Missing After Boot

On 2.0.0, a mouse that was already switched on when the computer started could stay
missing until it was turned off and on again. The cause was a race between two kernel
drivers over the first virtual device after boot. 2.0.1 loads the right drivers early and
lets the relay recover if the race happens anyway.

Reported in
[Discussion #19](https://github.com/offalexjackson777-stack/hyperflux-next/discussions/19)
and tracked in [#32](https://github.com/offalexjackson777-stack/hyperflux-next/issues/32).
Doctor's misleading advice in the same report is covered in
[Doctor restart advice](doctor-restart-advice.md).

## What People Saw

A user installed the Full package, enabled the bridge, and rebooted. Status read:

```text
HyperFlux Next status
Driver: active
Bridge: unavailable
Receivers: 0 generation(s), 0 qualified
Next: Start or restart the compatible HyperFlux bridge service.
```

The user then found that the bridge journal logged the receiver-available event with
`device_id` set to `null`: the bridge had never seen the mouse, which had been on the whole
time. Switching the mouse off, waiting a few seconds, and switching it back on made
everything work.

## How It Was Tracked Down

#32 started from two guesses about how a device that is present could go unseen, because
each needed a different fix:

- mouse activity arrives before the mouse's identity is known, and is dropped;
- the mouse's route already exists before the bridge consumer starts, and startup replays
  the identity but not the current route state.

The plan was to fix only a cause proven on hardware. Capturing the real boot order across
two reboots showed that neither guess was right. The timeline from one boot:

| Time after boot | Event |
| --- | --- |
| 52.53 s | Relay creates the virtual device for the mouse |
| 52.54 s | `hid-generic` binds the virtual device |
| 52.56 s | Relay marks the route blocked |
| 52.567 s | `razerkbd` finishes loading |

The OpenRazer drivers were loading after the device they were meant to drive.

## The Cause

The OpenRazer drivers that serve the relay's virtual devices (`razermouse`, `razerkbd`,
and the others) were loaded on demand, when the first virtual Razer device appeared. The
generic HID driver was already loaded, so it bound that first device. When the Razer
drivers finished loading a few milliseconds later, the HID core re-probed the device and
stopped `hid-generic` to hand it over.

The relay saw that stop as a failed kernel binding and treated it as terminal. It dropped
the route's bridge connection and blocked the route until the device was power-cycled. Only
the first device to wake after boot lost the race; every later device found the Razer
drivers already loaded.

## The Fix

**Load the drivers at boot.** The Full package now installs
`/usr/lib/modules-load.d/hyperflux-next-openrazer.conf`, which lists `razeraccessory`,
`razerkbd`, `razerkraken`, and `razermouse`. `systemd-modules-load` loads them early in
boot, before the bridge or relay can publish a virtual device. The list is generated from
the deployment model's driver module list, not from a list of products: these drivers claim
any Razer device on the virtual bus.

[fc51a83](https://github.com/offalexjackson777-stack/hyperflux-next/commit/fc51a83d1ba6aa2993a7397f20eff73394735c5b)

**Recover if a binding fails anyway.** A kernel-binding failure is now retryable. The relay
withdraws the stopped virtual device, keeps the route's bridge connection, and tries again
with the normal backoff and the usual check that two serial reads match. We bounded this to
three attempts per route lifetime so a device that can never bind cannot churn forever.
Protocol, CRC, serial, and UHID I/O failures still block the route, because retrying them
would hide a real fault.

[93b7ec8](https://github.com/offalexjackson777-stack/hyperflux-next/commit/93b7ec87277b6413929ed5bb3716e62a91609ad1)

The first change removes the race. The second is defence in depth, so that a later change
in boot order cannot strand a device again.

If you added `/etc/modules-load.d/hyperflux-next-openrazer.conf` by hand as a workaround,
remove it after updating; the package now ships its own file.

## How It Was Checked

- The cause was proven on hardware across two reboots before the fix was written.
- Install and integration tests check that the Full package installs
  `/usr/lib/modules-load.d/hyperflux-next-openrazer.conf` and that the generator emits the
  expected driver list.
- New relay runtime tests cover the retryable kernel-binding failure.

## What Is Still Open

Nothing. The mouse appeared without being power-cycled on eight boots in a row between
14 and 16 October 2026, on 2.0.2 and 2.1.0. The last one was a full power-off with the
mouse already on, followed by a sleep and wake in which both devices came back. There was
no ghost or duplicate device, and #32 is closed.
