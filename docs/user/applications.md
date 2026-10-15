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

OpenRGB may label a black keyboard as Quartz because its upstream information
query has no trustworthy discriminator.

These are visible limitations, not hidden compatibility claims.
