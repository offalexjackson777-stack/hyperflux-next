# Doctor Blamed A Healthy Bridge

When the bridge was running but a paired device had not come online, Doctor and status
reported a service failure and told people to restart the bridge. A restart could not help,
and the advice hid the real state. 2.0.1 names the layer that is actually not ready.

Issue: [#33](https://github.com/offalexjackson777-stack/hyperflux-next/issues/33).
The same advice appeared in
[Discussion #19](https://github.com/offalexjackson777-stack/hyperflux-next/discussions/19),
whose real cause is described in [Mouse missing after boot](mouse-missing-after-boot.md).

## What People Saw

On 2.0.0, a user whose mouse had not appeared after boot got this status:

```text
Driver: active
Bridge: unavailable
Receivers: 0 generation(s), 0 qualified
Next: Start or restart the compatible HyperFlux bridge service.
```

The bridge was in fact running: its journal logged the receiver as available. The mouse
was the only thing missing.

## How It Was Tracked Down

Doctor's assessment raised a single finding, `HFX-SERVICE-001`, for anything short of a
fully ready bridge. When the bridge answered but was not ready, the finding carried the
value `active-unready`, and its remediation was the generic one for a service failure:
start or restart the bridge. The status line used the same readiness flag, so a bridge
that answered but had no live device was printed as "unavailable".

Restarting a working bridge cannot create receiver or device evidence. The finding had to
say which layer was missing, not just that something was.

## The Cause

Doctor collapsed three different states into one service failure:

- the bridge is stopped or does not answer;
- the bridge answers, but the receiver is not registered or not qualified;
- the bridge and receiver are ready, but no paired device has a live route.

Only the first is a service problem.

## The Fix

[a43582c](https://github.com/offalexjackson777-stack/hyperflux-next/commit/a43582c809223d9f73ea930bb4ea8cc7630be20d)

Doctor now reports one of three findings, each with remediation that matches its cause:

| Finding | When | What Doctor suggests |
| --- | --- | --- |
| `HFX-SERVICE-001` | The bridge is stopped or unreachable | Start or restart the bridge |
| `HFX-SERVICE-002` | The bridge answers; the receiver is `not-registered` or `unqualified` | Reconnect the receiver or reboot, then collect a support bundle |
| `HFX-SERVICE-003` | Bridge and receiver are ready; the route state is `no-paired-device` or `not-live` | Move or click the device; power-cycle it only if it does not appear within 30 seconds |

The status line now says "Bridge: running" whenever the bridge answers, and the next step
for a missing device asks the user to move or click it. The JSON output contract is
unchanged; whether the bridge answered is tracked internally.

The new findings and their remediation text are defined in `errors/catalog.json`, and the
error catalog, SDK constants, and [error reference](../generated/error-catalog.md) are
generated from it.

## How It Was Checked

New assessment tests cover each state:

- `paired_mouse_without_a_live_route_is_not_a_bridge_failure`
- `qualified_receiver_without_paired_devices_names_the_route_state`
- `unqualified_receiver_is_named_without_blaming_the_bridge`
- `unreachable_bridge_still_reports_the_service_and_unavailable_status`

## What Is Still Open

#33 also listed a temporarily unavailable route and a receiver bound to an unexpected
kernel driver. A paired device without a live route is reported as `not-live` under
`HFX-SERVICE-003`. The kernel-driver case is reported by `HFX-KERNEL-003`, added by the
[retired DKMS driver](retired-dkms-driver.md) fix. The 2.0.1
checklist, [#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36),
includes checking on hardware that Doctor's output matches the real state.
