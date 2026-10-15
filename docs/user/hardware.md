# Hardware

HyperFlux Next works with a Razer HyperFlux V2 receiver and the keyboards and
mice paired to it. A device works when HyperFlux knows exactly how to talk to it,
not because its name looks similar to a known device. There is no fixed list of
allowed devices on your machine.

## Tested On Real Hardware

These two devices have been tested end to end on real hardware, through the
receiver and with both stock applications:

- **Razer Basilisk V3 Pro 35K** (mouse): `1532:00cd` through the receiver,
  `1532:00cc` over a USB cable.
- **Razer DeathStalker V2 Pro Tenkeyless** (keyboard): `1532:0296` through the
  receiver, `1532:0298` over a USB cable.

They are the devices the project tests on. They are not the only devices that
work.

## Expected To Work

Eight more devices are expected to work. Their support in stock OpenRazer or
OpenRGB has been checked in the upstream source code, but they have not been
tested on real hardware with HyperFlux. Some work in only one of the two
applications.

## Waiting For Upstream Support

Two devices are known but cannot be used yet, because stock OpenRazer does not
support them. HyperFlux leaves them alone until it does.

The [supported-hardware reference](../generated/supported-hardware.md) lists
every device in each group with its USB IDs and applications. If your device is
expected to work but does not, or is not listed, please
[ask for device support](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new?template=device_support.yml).

## A Device Plugged In By Cable

If you plug a device in with a USB cable while it is also paired to the
receiver, applications use the cable connection and hide the receiver copy of
that same device. Other devices on the receiver are not affected.

## Devices HyperFlux Does Not Recognise

A paired device that HyperFlux does not recognise, or that is asleep or out of
reach, stays read-only. No application can change it through HyperFlux, and it
does not show up as a working device. `hyperfluxctl status` and Doctor may
still mention it.

See [known limitations](limitations.md) for what works differently from a
direct USB connection.
