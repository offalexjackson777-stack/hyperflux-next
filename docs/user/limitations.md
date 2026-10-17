# Known Limitations

These are the limits of HyperFlux Next today. Each one says what you will see
and what to do.

## Platform And Packages

### Published For Arch Linux Only

HyperFlux Next is published only for Arch Linux on x86_64. Packages for Ubuntu,
Debian, Fedora, and openSUSE are built and tested every week, but they are not
published yet, so there is nothing to install from yet. When they are
published, they will carry Core only, without the app connections. The
[roadmap](../project/roadmap.md) shows what comes next.

### Not In The Official Arch Repositories Or The AUR

HyperFlux comes from its own signed package repository, which you add once as
shown in the [installation guide](installation.md). An AUR package is planned
but not published yet.

## Applications

OpenRazer, Polychromatic, and OpenRGB are optional. They are separate
projects with their own packages, settings, and bugs. HyperFlux does not install,
patch, or own them. It lets them reach the devices paired to your receiver.

### Ripple And Other Effects That React To Keys

Ripple, and any other effect that lights up the keys you press, needs to see
your key presses. With the Full package, OpenRazer sees them through the
receiver. Keep these points in mind:

- **Restart once.** OpenRazer looks for the key presses only when it first
  finds the keyboard. After you install Full, restart the computer once. If
  Ripple stays still, run `hyperfluxctl doctor`; it tells you when a restart
  is needed.
- **One keyboard.** With two receivers connected, only one of the two
  keyboards reacts to key presses.
- **The ring looks like two lines.** Ripple draws a ring that grows from the
  key you press. OpenRazer treats the keyboard as just six rows of lights, so
  the ring often looks like two short vertical lines moving apart. This is how OpenRazer
  draws Ripple; it is not a fault.
- **Who can read your key presses.** To make this work, Full lets your user
  account read the keyboard's key presses from the receiver while you are
  logged in at the computer. That means any program you run can read them
  too. Other user accounts on the computer cannot.

If you do not want this, turn it off. Ripple then stays still, and Doctor
does not warn about it:

```sh
sudo ln -s /dev/null /etc/udev/rules.d/70-hyperflux-next-openrazer-keys.rules
```

Then restart the computer. To turn it back on, delete that file and restart:

```sh
sudo rm /etc/udev/rules.d/70-hyperflux-next-openrazer-keys.rules
```

### OpenRGB May Call A Black Keyboard Quartz

OpenRGB may describe a black keyboard as Quartz. This comes from OpenRGB, not
from HyperFlux. The device name is correct and lighting works; only the
description is wrong.

### Keyboard Lighting Shortcuts

The keyboard's own lighting shortcuts, such as Fn brightness and effect keys,
change lighting inside the keyboard firmware. OpenRazer and Polychromatic
keep their last known brightness and effect and do not notice the change, and
HyperFlux does not react to it. To take control back, apply the effect again in
OpenRGB, or in Polychromatic change the brightness and then apply the effect
again.

### Polychromatic Lists The Receiver

Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
Polychromatic lists every Razer USB device that OpenRazer does not manage. The
receiver itself is not something you control there: it only carries the
keyboard's and mouse's signals. Ignore that entry; the keyboard and mouse appear
under their own names.

### Polychromatic Needs A Refresh After A Device Returns

Polychromatic does not follow devices that leave and return, such as a
wireless keyboard switched off and on. Wait a few seconds after switching it
on, then press F5 on the Devices tab.

### Brightness Readback

Some devices do not report their current brightness, even over a direct USB
cable. HyperFlux passes the device's answer through unchanged and never makes up
a value, so an application may show a default brightness. The device still
appears in your applications.

## Battery

### Charging Status Is Partial

The desktop battery widget shows a charging status for the Basilisk V3 Pro 35K
mouse and the DeathStalker V2 Pro Tenkeyless keyboard.

- The mouse shows charging while it rests on the charging mat or is plugged in
  with a USB cable, and discharging when you lift it off the mat.
- The keyboard shows charging while its USB cable is plugged in, and
  discharging when the cable is unplugged.

Keep these points in mind:

- **Charge limit.** If a charge limit is set (for example in Razer Synapse),
  the mouse keeps reporting "Charging" while it holds at that limit, and the
  desktop shows what the mouse reports. What the keyboard reports at a charge
  limit has not been tested. If it reports something HyperFlux has not seen,
  the widget shows unknown.
- **USB cable.** For about two seconds after you plug in the mouse cable, the
  widget may still show discharging. What the mouse reports after you unplug
  its cable has not been tested yet. The keyboard keeps showing charging for
  about four seconds after you unplug its cable.
- **Full.** Neither device reports full by itself. The widget shows full when
  the device is charging and its level reads 100 percent. What the keyboard
  reports once its battery is full on the cable has not been tested. If it
  reports something HyperFlux has not seen, the widget shows unknown.
- **Unknown.** If a device reports something HyperFlux has not seen before,
  the widget shows unknown instead of a guess.

The battery level itself is not affected.

## Hardware

Two devices have been tested end to end on real hardware. Eight more are
expected to work because support for them has been checked in the source code
of OpenRazer and OpenRGB, but they have not been tested on real hardware. Two
more wait for support in OpenRazer; OpenRGB does not support them either. See
[hardware](hardware.md) for the list.

A device that an application can use is not the same as a device that has been
tested on real hardware. If your device is in the second group and something
does not work, please [report it](../../SUPPORT.md).
