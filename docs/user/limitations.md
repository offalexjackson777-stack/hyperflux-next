# Known Limitations

These are the limits of HyperFlux Next today. Each one says what you will see
and what to do.

## Platform

HyperFlux Next is published only for Arch Linux on x86_64. Other distributions
are on the [roadmap](../project/roadmap.md), but no packages exist for them yet.

## Applications

Stock OpenRazer, Polychromatic, and OpenRGB are optional. They are separate
projects with their own packages, settings, and bugs. HyperFlux does not install,
patch, or own them. It lets them reach the devices paired to your receiver.

### Ripple Shows No Reactive Lighting

Polychromatic offers Ripple for keyboards whose stock OpenRazer model supports
it, but over HyperFlux it shows no reactive lighting. Ripple needs to see your
key presses through the receiver, and stock OpenRazer cannot do that yet. Choose
another effect.

### OpenRGB May Call A Black Keyboard Quartz

OpenRGB reads the colour variant from a byte in the keyboard's own information
reply, without checking the reply or whether the model has variants; HyperFlux
passes that reply through unchanged. The device name is correct and lighting
works. Only the description is affected.

### Keyboard Lighting Shortcuts

The keyboard's own lighting shortcuts, such as Fn brightness and effect keys,
change lighting inside the keyboard firmware. Stock OpenRazer and Polychromatic
keep their last known brightness and effect and do not notice the change, and
HyperFlux does not react to it. To take control back, apply the effect again in
OpenRGB, or in Polychromatic change the brightness and then apply the effect
again.

### Polychromatic Lists The Receiver

Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
Polychromatic lists every Razer USB device that OpenRazer does not manage, and
the receiver itself is a transport, not a controllable device. Ignore that
entry; the keyboard and mouse appear under their own names.

### Polychromatic Needs A Refresh After A Device Returns

Polychromatic does not follow devices that leave and return, such as a
wireless keyboard switched off and on. Wait a few seconds after switching it
on, then press F5 on the Devices tab.

### Brightness Readback

Some devices do not report their current brightness, even over a direct USB
cable. HyperFlux passes the device's answer through unchanged and never makes up
a value, so an application may show a default brightness. The device still
appears in your applications.

## Hardware

Two devices have been tested end to end on real hardware. Eight more are
expected to work because their stock application support has been checked in
the upstream source, but they have not been tested on real hardware. Two more
wait for upstream OpenRazer support. See [hardware](hardware.md) for the list.

A device that an application can use is not the same as a device that has been
tested on real hardware. If your device is in the second group and something
does not work, please [report it](../../SUPPORT.md).
