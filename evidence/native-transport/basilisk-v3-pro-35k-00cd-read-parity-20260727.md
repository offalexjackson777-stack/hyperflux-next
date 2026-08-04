# Basilisk V3 Pro 35K Native Transport Parity

## Result

`PASSED`

A supervised, restoration-isolated comparison reproduced all sixteen required
read-only OpenRazer semantics through the HyperFlux receiver route. The direct
USB reference used Razer PID `0x00cc`; the receiver-observed wireless child used
PID `0x00cd`. Requests retained the standard 90-byte Razer Feature report
shape. Where required, the receiver profile translated the application
selector to pointer lane `0x1f` and restored the application selector in the
response.

## Source Binding

- HyperFlux source: `ceb4f913751e16fd742d55559ff0b46660a20813`
- Candidate package SHA-256: `54bd8d92ad86a59911a898aebb20576269e08ff32682fefb327de8fd49951b68`
- OpenRazer source: `1ef8a91712906a89fd332a2ed62bbd105315d6d1`
- Native probe catalog SHA-256: `8b1b4121c3de190ff579d8737ce361a2e39b46a659318393370799b0726d81a9`
- Canonical parity artifact SHA-256: `8620cdf7fa5acbcfec127ef0131277a3280174dd10eaa29a48940abd9805857c`
- Source-bound run record SHA-256: `d89cb90ce0a91345d1e50942e63ed06042069fc1bc45d952034bdba902023209`

## Covered Semantics

- firmware, redacted serial equality, and device mode;
- battery and charging state;
- DPI, DPI stages, polling rate, idle time, and low-battery threshold;
- scroll mode, acceleration, and Smart-Reel state;
- whole-device, logo, and scroll-wheel brightness.

Every required method passed. The qualifier used no input nodes, changed no
setting or lighting value, retained no serial or raw report, performed no
network upload, and deleted its ephemeral capture before completion. Ordinary
mouse and keyboard controls remained functional during the watched session.

## Scope

This evidence qualifies the exact PID `0x00cd` profile for the reviewed
90-byte native Feature-report route and its generated command policy. It does
not qualify another PID or report format. Reversible writes, real OpenRazer
D-Bus and Polychromatic enumeration, application-visible lighting, lifecycle,
rollback, and release readiness remain separate gates.
