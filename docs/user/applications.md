# Applications

With the Full package installed, stock OpenRGB and stock Polychromatic can
control the keyboards and mice paired to your receiver, just as they control
devices plugged in by cable. Polychromatic does this through the stock OpenRazer
service. Your effects, profiles, and settings stay in those applications.

These applications are optional and belong to their own projects. HyperFlux
does not install, patch, or own them. Full lists them as optional packages:
`openrgb`, `openrazer-daemon`, `python-openrazer`, and `polychromatic`. Install
the ones you want.

With Core alone, these applications cannot reach devices paired to the
receiver.

## OpenRazer And Polychromatic

OpenRazer is the background service that talks to Razer devices.
Polychromatic is the window you use to choose effects and brightness; it talks
to OpenRazer. Full lets the stock OpenRazer service see each device paired to
the receiver as its own device, so Polychromatic shows it by name. You do not
need to change any OpenRazer or Polychromatic setting.

## OpenRGB

Full adds a HyperFlux plugin to the stock OpenRGB application. Your paired
keyboard and mouse then appear in OpenRGB by name. The plugin does not replace
OpenRGB or change its own settings.

## Known Limitations

- Polychromatic offers Ripple, but over HyperFlux it shows no reactive lighting.
- OpenRGB may call a black keyboard Quartz. Only the description is wrong.
- Fn lighting shortcuts change the keyboard without the applications noticing.
- Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
- Polychromatic needs F5 after a device switches off and back on.
- Some devices do not report their current brightness.

See [known limitations](limitations.md) for what to do about each one.
