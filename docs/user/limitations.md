# Known Limitations

These are the limits of HyperFlux Next today. Each one says what you will see
and what to do.

## Platform And Packages

### Ubuntu, Debian, Fedora, And openSUSE Are Not Yet Confirmed On Hardware

HyperFlux Next is published for Arch Linux, Ubuntu 26.10, Debian 14, Fedora 44,
and openSUSE Tumbleweed, on x86_64 only. The Ubuntu, Debian, Fedora, and
openSUSE packages are built and tested automatically, but not yet confirmed on
real hardware. They carry both editions, Core and Full.

If something does not work on one of them, run `hyperfluxctl doctor` and
[report it](../../SUPPORT.md) with the output. Debian 14 is still Debian's
testing release, so Debian's own packages can change under you. The
[roadmap](../project/roadmap.md) shows what comes next.

### No OpenRGB Plugin On Ubuntu, Fedora, And openSUSE

Full on Ubuntu, Fedora, and openSUSE comes without the OpenRGB plugin. This is
not a HyperFlux limit. It is what those distributions package:

- Ubuntu 26.10 packages a development snapshot of OpenRGB from before 1.0.
  It loads only plugins built for plugin interface 4.
- Fedora 44 and openSUSE Tumbleweed package an OpenRGB 1.0 release candidate.
  It loads only plugins built for plugin interface 4.

The HyperFlux plugin is built for plugin interface 5, which OpenRGB 1.0 final
and OpenRGB 1.1 use, and OpenRGB refuses a plugin built for anything else. Arch
Linux packages OpenRGB 1.1 and Debian 14 packages OpenRGB 1.0 final, so Full
there includes the plugin. Debian builds its OpenRGB with Qt 5 rather than
Qt 6, so Full on Debian carries a copy of the plugin built against Debian's own
Qt 5. OpenRazer apps and Polychromatic still work with Full everywhere, and
`hyperfluxctl doctor` reports Full as ready without the plugin. When a
distribution packages OpenRGB 1.0 final or newer, its Full packages can include
the plugin too.

### Only The Newest Ubuntu And Debian

The packages are built for Ubuntu 26.10 and Debian 14, not for the older
releases many people still run:

- **Ubuntu 26.04 LTS** packages OpenRazer 3.11.0, which does not know the
  BlackWidow V4 Tenkeyless HyperSpeed.
- **Debian 13** packages OpenRazer 3.10.2, which does not know the BlackWidow
  V4 Mini HyperSpeed or the BlackWidow V4 Tenkeyless HyperSpeed, and it does
  not package OpenRGB at all.

Full uses the OpenRazer service that your distribution packages, so on those
releases some supported keyboards would not appear in OpenRazer apps. Ubuntu
26.10 and Debian 14 both package OpenRazer 3.12.4, which knows every device
HyperFlux supports.

Keep in mind:

- **Debian 14 is still Debian's testing release.** It changes more often than
  a stable release until Debian releases it.
- **Ubuntu 26.10 is supported by Ubuntu until July 2027.** The packages move to
  the next Ubuntu release before then.

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
- **The ring can start one key to the right.** OpenRazer uses one key map
  for every keyboard. That map assumes a column of macro keys left of Esc. On
  a keyboard without that column, such as the DeathStalker V2 Pro, OpenRazer
  places each key one column too far right, so Ripple starts at the key next
  to the one you pressed. This comes from OpenRazer's own code, so it happens
  over a cable too. HyperFlux passes OpenRazer's lighting through unchanged
  and does not shift it back, because that would move the lights Polychromatic
  and OpenRGB set for each key.
- **Ripple stops when you change the brightness.** OpenRazer draws Ripple
  itself and stops drawing it as soon as anything else changes the lighting,
  and it counts a brightness change as such a change. Polychromatic still
  shows Ripple as chosen, but the keys stay dark when you press them. Choose
  another effect and then Ripple again, or set the brightness first and choose
  Ripple after. This comes from OpenRazer's own code, so it happens over a
  cable too.
- **The ring skips some keys.** Through the receiver, sending one picture of
  the keyboard takes about 80 ms, and OpenRazer waits another 50 ms before
  the next one. In that time the ring grows about three keys, but OpenRazer
  draws it only two keys wide, so some keys along its path never light. Over
  a cable a picture takes only a few milliseconds, so the ring has no gaps
  there. Polychromatic's Ripple speed setting does not change this, because
  Polychromatic does not pass it on to OpenRazer.
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

### The Mouse Shows A Keyboard Icon

In the desktop battery widget, the mouse shows a keyboard icon. Its name,
battery level, and charging status are right.

The widget learns what kind of device a battery belongs to from the desktop's
battery service, UPower. UPower looks at everything that arrives through the
same USB receiver. When it finds a keyboard there, it treats every battery on
that receiver as a keyboard, because it prefers a keyboard over a mouse. Your
mouse and keyboard share one receiver, so the mouse gets the keyboard icon too.
HyperFlux cannot change this from its side; it needs a change in UPower.

## Hardware

Two devices have been tested end to end on real hardware. Eight more are
expected to work because support for them has been checked in the source code
of OpenRazer and OpenRGB, but they have not been tested on real hardware. Two
more wait for support in OpenRazer; OpenRGB does not support them either. See
[hardware](hardware.md) for the list.

A device that an application can use is not the same as a device that has been
tested on real hardware. If your device is in the second group and something
does not work, please [report it](../../SUPPORT.md).
