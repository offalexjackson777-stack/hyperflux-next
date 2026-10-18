# Hardware

HyperFlux Next works with a Razer HyperFlux V2 receiver and the keyboards and
mice paired to it. This page tells you whether your device works and what to do
if it is not listed.

HyperFlux decides per device: a device works when HyperFlux knows exactly how to
talk to it, not because its name looks like a known device.

## Tested On Real Hardware

These two devices have been tested end to end on real hardware, through the
receiver and with both kinds of app: OpenRazer (with Polychromatic) and
OpenRGB:

- **Razer Basilisk V3 Pro 35K** (mouse): `1532:00cd` through the receiver,
  `1532:00cc` over a USB cable.
- **Razer DeathStalker V2 Pro Tenkeyless** (keyboard): `1532:0296` through the
  receiver, `1532:0298` over a USB cable.

They are the devices the project tests on. They are not the only devices that
work.

## Expected To Work

Eight more devices are expected to work. Their support has been checked in the
source code of OpenRazer or OpenRGB, but they have not been tested on real
hardware with HyperFlux. Some work in only one of the two
applications.

If you own one of them, you can
[confirm that it works](#confirm-that-your-device-works). The
[supported-hardware reference](../generated/supported-hardware.md) then lists it
as confirmed by owners.

## Waiting For Upstream Support

Two devices are known but cannot be used yet, because OpenRazer does not
support them yet, and OpenRGB does not either. HyperFlux leaves them alone until
OpenRazer does.

## Check Whether Your Device Is Listed

1. Find your keyboard or mouse in the
   [supported-hardware reference](../generated/supported-hardware.md). It lists
   every device in each group with its USB IDs and the apps it works in.
2. If it is expected to work but does not, or it is not listed at all,
   [ask for device support](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml).
   Include a [hardware report](#share-a-hardware-report) and the output of
   `hyperfluxctl support-bundle --preview`.

## Share A Hardware Report

A hardware report tells the project which receiver and devices HyperFlux sees
on your computer and how far each one is tested.

1. Connect the receiver and wake your keyboard and mouse.
2. Run:

   ```sh
   hyperfluxctl qualification
   ```

   You should see `State: ready` and a line for each device. If you see another
   state, do what the `Next:` line says.
3. Make the version you will share:

   ```sh
   hyperfluxctl qualification --json --output hyperflux-hardware.json
   ```

   Only you can read the file. The command also prints the report, so you can
   paste it straight away. If a file with that name already exists, the
   command stops without changing it: delete or rename the old file first, or
   choose another name.

   In the report, every test stage, such as `Profile binding` and the lighting
   and reconnect checks, shows as `blocked` or `not-run`. That is expected:
   this version does not include the guided test runner yet, so nothing is
   tested and nothing is written to your devices. The report is still complete.
4. Paste the printed report, unchanged, in the
   [device support form](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml),
   or, if you are reporting something that does not work, in the
   [bug form](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=bug_report.yml).
   Both have a field for it. You can attach the file instead, but a pasted
   report is quicker for the maintainers to check.

The report only reads what HyperFlux already knows. It sends nothing to your
devices, uploads nothing, and does not include firmware versions. The
[privacy guide](privacy.md#hardware-reports) lists what it contains.

## Confirm That Your Device Works

If your device is listed as expected to work and it does work for you, tell
the project. One confirmed report is enough to mark the device as confirmed by
owners, which helps the next person decide.

1. Pair the device with the receiver and wake it. Unplug its USB cable, so it
   really talks through the receiver.
2. Use it for a moment: move the mouse and click, or type a few keys.
3. Change its lighting in an app the
   [supported-hardware reference](../generated/supported-hardware.md) lists for
   it: Polychromatic (or another OpenRazer app), OpenRGB, or both.
4. Run `hyperfluxctl qualification` and check that it says `State: ready` and
   shows your device. If it reports a problem, fix that first; the `Next:` line
   says how.
5. Run `hyperfluxctl qualification --json` and paste what it prints, unchanged,
   in the
   [device support form](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml).
6. In the form, tick only what you saw yourself: that the device worked
   normally, and in which app its lighting changed.

A maintainer checks the report and, if it fits, adds your device to the
confirmed list with a link to your request. A report is turned down if it has
been edited, shows a problem, or holds anything that looks private, such as a
file path or a serial number; you will be asked for a fresh one. Confirming a
device does not change how HyperFlux treats it, and it does not count as the
project testing the device itself.

## A Device Plugged In By Cable

If you plug a device in with a USB cable while it is also paired to the
receiver, applications use the cable connection and hide the receiver copy of
that same device. Other devices on the receiver are not affected.

## Devices HyperFlux Does Not Recognise

A paired device that HyperFlux does not recognise, or that is asleep or out of
reach, stays read-only. No app can change it through HyperFlux, and it does not
show up as a working device. `hyperfluxctl status` and Doctor may
still mention it.

See [known limitations](limitations.md) for what works differently from a
direct USB connection.
