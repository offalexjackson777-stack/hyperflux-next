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

## Accepted Limitations

Polychromatic offers Ripple for keyboards whose stock OpenRazer model supports
it, but over HyperFlux it shows no reactive lighting. Ripple needs a
route-associated key-event source that is not yet available through this
feature-only path. Choose another effect.

OpenRGB may label a black keyboard as Quartz. OpenRGB reads the colour variant
from a byte in the keyboard's own information reply, without checking the reply
or whether the model has variants; HyperFlux passes that reply through
unchanged. The device name is correct; only the description is affected.

The keyboard's own lighting shortcuts, such as Fn brightness and effect keys,
change lighting inside the keyboard firmware. Stock OpenRazer and Polychromatic
keep their last known brightness and effect and do not notice the change, and
HyperFlux does not react to it. To take control back, apply the effect again in
OpenRGB, or in Polychromatic change the brightness and then apply the effect
again.

Polychromatic may list the receiver as an unrecognised `1532:00CF` device.
Polychromatic lists every Razer USB device that OpenRazer does not manage, and
the receiver itself is a transport, not a controllable device. Ignore that
entry; the keyboard and mouse appear under their own names.

Polychromatic does not follow devices that leave and return, such as a
wireless keyboard switched off and on. Wait a few seconds after switching it
on, then press F5 on the Devices tab.

These are visible limitations, not hidden compatibility claims.
