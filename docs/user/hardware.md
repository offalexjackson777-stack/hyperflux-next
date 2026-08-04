# Hardware

HyperFlux support is capability-based and plug-and-play. A route needs an exact
source-qualified identity, report family, active generation, and transport
authority. Similar names, broad vendor matches, serials, USB ports, and local
machine bindings never grant support.

## Three Support Tiers

1. **Physically proven:** two devices completed end-to-end hardware,
   application, and lifecycle checks.
2. **Source-qualified operational candidates:** eight additional devices have
   at least one validated stock-application path in pinned upstream source, but
   not the same physical assurance.
3. **Upstream-blocked or research-only:** two receiver-scope candidates have no
   currently validated usable application path and are not operational claims.

See the generated [supported-hardware reference](../generated/supported-hardware.md)
for the device-by-device classification. The two physical fixtures prove the
path; they are not a runtime allowlist.

## Direct Devices

An exact matching direct USB device suppresses only its own receiver-backed
duplicate. Unrelated devices and sibling routes remain independent.

## Unknown Devices

Pairing inventory is identity, not presence or write authority. Unknown,
unqualified, stale, and unavailable children remain non-writable and may appear
only in diagnostics that can describe them truthfully.
