# Applications

HyperFlux transports qualified device exchanges while stock applications keep
their existing device models, settings, effects, profiles, and persistence.

## OpenRazer And Polychromatic

Full provides the narrow driver and relay integration needed for stock
OpenRazer to publish receiver-backed child devices through its ordinary public
API. Polychromatic remains the distribution application and is neither bundled
nor patched by HyperFlux.

## OpenRGB

Full packages a standalone HyperFlux plugin for the stock OpenRGB application.
The plugin publishes qualified child controllers; it does not replace OpenRGB
or write global OpenRGB configuration.

## Known Limitations

- Polychromatic offers Ripple, but over HyperFlux it shows no reactive lighting.
- OpenRGB may call a black keyboard Quartz. Only the description is wrong.
- Fn lighting shortcuts change the keyboard without the applications noticing.
- Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
- Polychromatic needs F5 after a device switches off and back on.
- Some devices do not report their current brightness.

See [known limitations](limitations.md) for what to do about each one.
