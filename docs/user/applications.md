# Applications

With the Full package, OpenRGB and Polychromatic control the keyboard and mouse
paired to your receiver, just as they control a device plugged in by cable. Your
effects, profiles, and settings stay in those apps.

The apps belong to their own projects.
HyperFlux does not install, patch, or own them. Full lists them as optional
packages: `openrgb`, `openrazer-daemon`, `python-openrazer`, and `polychromatic`.
The [first-run guide](first-run.md) shows how to install them.

With Core alone, these apps cannot reach devices paired to the receiver.

## Use Polychromatic

OpenRazer is the background service that talks to Razer devices. Polychromatic
is the window where you choose effects and brightness, and it talks to
OpenRazer.

1. Open Polychromatic.
2. Pick your keyboard or mouse from the list. Each paired device appears by its
   own name.
3. Choose an effect or a brightness.

You do not need to change any OpenRazer or Polychromatic setting for HyperFlux.

## Use OpenRGB

Full adds a HyperFlux plugin to OpenRGB. The plugin does not replace OpenRGB or
change its settings.

1. Open OpenRGB.
2. Pick your keyboard or mouse from the device list. Each paired device appears
   by its own name.
3. Choose a colour or a mode.

OpenRGB also gets a **HyperFlux Next** tab at the top of its window. It shows
one card per paired device with its battery level, whether it is charging, and
whether it is connected wirelessly or by cable. If HyperFlux cannot reach your
devices, the tab tells you what to run. The **GitHub** button opens the project
page.

## See Battery Level On Your Desktop

While HyperFlux is running, your mouse and keyboard show their battery level in
the desktop battery widget, for example the battery icon in KDE Plasma. Each
device appears under its own name, such as Basilisk V3 Pro 35K. You do not
need to set anything up.

- A device appears once its battery level is known. With Full installed,
  the OpenRazer service asks for it about half a minute after you start the
  computer, and then about every ten minutes. Without Full, the device has to
  send its level by itself, which can take a few minutes.
- A device that is switched off or out of range leaves the widget until it
  reports again.
- The widget also shows whether each device is charging, discharging, or
  full. Sometimes it shows the charging status as unknown instead; see
  [Charging Status Is Partial](limitations.md#charging-status-is-partial).
- The mouse shows a keyboard icon. Its name and level are right; see
  [The Mouse Shows A Keyboard Icon](limitations.md#the-mouse-shows-a-keyboard-icon).

## What Does Not Work Yet

- Ripple needs one restart after you install Full, and with two receivers
  only one keyboard reacts to key presses.
- Ripple stops after a brightness change until you choose it again, and
  through the receiver its ring skips some keys.
- OpenRGB may call a black keyboard Quartz. Only the description is wrong.
- Fn lighting shortcuts change the keyboard without the apps noticing.
- Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
- Polychromatic needs F5 after a device switches off and back on.
- Some devices do not report their current brightness.
- The mouse shows a keyboard icon in the desktop battery widget.

See [known limitations](limitations.md) for what to do about each one.
