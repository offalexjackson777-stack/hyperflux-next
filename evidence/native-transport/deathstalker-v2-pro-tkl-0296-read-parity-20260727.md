# DeathStalker V2 Pro TKL Native Transport Parity

## Result

`PASSED`

A supervised, restoration-isolated comparison reproduced all nine required
read-only OpenRazer semantics through the HyperFlux receiver route. The direct
USB reference used Razer PID `0x0298`; the receiver-observed wireless child used
PID `0x0296`. Requests retained the standard 90-byte Razer Feature report
shape. Where required, the receiver profile translated the application
selector to keyboard lane `0x9f` and restored the application selector in the
response.

## Source Binding

- HyperFlux source: `ceb4f913751e16fd742d55559ff0b46660a20813`
- Candidate package SHA-256: `54bd8d92ad86a59911a898aebb20576269e08ff32682fefb327de8fd49951b68`
- OpenRazer source: `1ef8a91712906a89fd332a2ed62bbd105315d6d1`
- Native probe catalog SHA-256: `8b1b4121c3de190ff579d8737ce361a2e39b46a659318393370799b0726d81a9`
- Canonical parity artifact SHA-256: `f6b93f931e5a6b242f156b2d2c2fac759aa4083b5d281448582c0d64395f126e`
- Source-bound run record SHA-256: `ab986bfd459f09f7e8db6b7b3fc1484fcb76cd96e98a716c5e3cfc469ae84ed6`

## Covered Semantics

- firmware, redacted serial equality, and device mode;
- keyboard layout;
- battery and charging state;
- game mode, macro mode, and macro effect.

Every required method passed. The qualifier used no input nodes, changed no
setting or lighting value, retained no serial or raw report, performed no
network upload, and deleted its ephemeral capture before completion. Ordinary
mouse and keyboard controls remained functional during the watched session.

## Scope

This evidence qualifies the exact PID `0x0296` profile for the reviewed
90-byte native Feature-report route and its generated command policy. It does
not qualify another PID, wired PID `0x0298`, or another report format.
Reversible writes, real OpenRazer D-Bus and Polychromatic enumeration,
application-visible lighting, lifecycle, rollback, and release readiness remain
separate gates.
