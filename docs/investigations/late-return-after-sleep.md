# Late Return After Sleep Or Power-Off

A keyboard or mouse that came back from sleep, or was switched off and on, could take up to
about 30 seconds to reappear in OpenRazer, and Polychromatic could keep showing a stale or
empty device. Two gaps in how the relay waits before publishing a returning device caused
the delay. One fix shipped in 2.0.1; the second, and a documentation fix for Polychromatic,
are on `dev` for 2.0.2.

Issues:
[#38](https://github.com/offalexjackson777-stack/hyperflux-next/issues/38) (return from
sleep) and
[#39](https://github.com/offalexjackson777-stack/hyperflux-next/issues/39) (keyboard off and
on).

## What People Saw

- After sleep, a device was usable again on the receiver but OpenRazer did not see it for a
  while.
- After the keyboard was switched off and on, Polychromatic could show it as stale or show
  no device at all.

## How It Was Tracked Down

When a device goes away, the relay withdraws its virtual device and retries publication
with a backoff: 100 ms five times, then 1, 2, 4, 8, and 16 seconds, then every 30 seconds.
Before publishing again it requires two matching serial reads, so a different device cannot
take over the old one's identity. The backoff stops a missing device from flooding the
receiver with probes. The question in both issues was what resets that timer when the device
comes back.

For #38 the answer was: nothing, when the route never changed state. A sleeping device can
answer with terminal status `0x04`, which sends the relay's publication into backoff, while
the bridge keeps the route marked available. When the device woke, the route journal saw
no state change and recorded nothing, so the relay waited out whatever was left of its
timer.

For #39 the device did leave, but the receiver did not report the route as gone. The relay
learned of the departure only from a status-timeout reply. When the keyboard came back, a
keypress could not open a return epoch, the signal that a device is back, until the
device's return quiescence period had passed. Again nothing reset the backoff.

#39 was opened to settle first whether HyperFlux owned the Polychromatic symptom at all.
Reading both code bases gave a mixed answer:

- Polychromatic and the OpenRazer client library read the device list once and do not
  follow devices that are removed and added again. Only a refresh picks up the change.
- HyperFlux turns a child power-off into exactly that remove and add. This is deliberate:
  a returning device must pass the serial check again before it is published.
- The delay above could keep the device absent for up to about 30 seconds, and during that
  time even a refresh shows an empty list.

HyperFlux owns the delay; the missing refresh is upstream behaviour.

## The Cause

The relay's publication backoff ignored fresh evidence that a device had returned. In the
worst case a returning device waited for the 30-second backoff ceiling.

## The Fix

**Act on a fresh return epoch (#38, 2.0.1).** The daemon now forwards a device's
`EpochOpened` signal to the bridge. For a strictly newer epoch, the bridge's route journal
appends one ordinary `Available` refresh for that exact device and generation. The relay
supervisor classifies that refresh as fresh evidence instead of restarting the endpoint,
and the relay resets a publication that is in retryable backoff, once, so the next tick
probes.

[e86e124](https://github.com/offalexjackson777-stack/hyperflux-next/commit/e86e124b8d184d73f412bd7f1514010b41cebf22)

Duplicate and older epochs, sibling devices, other generations, and unavailable routes are
ignored. Healthy, blocked, and terminal publications are untouched, a failed retry goes
back to the normal backoff, and the two matching serial reads are still required. There is
no protocol or schema change.

**Re-probe quickly after a departure (#39, 2.0.2).** When the relay recycles a retained
route after its device departs, it now opens a short fast window. The window lasts as long
as the longest generated return quiescence, and within it retryable identity failures are
re-probed at least every 2 seconds. Blocked failures still block, publishing clears the
window, and the first publication and kernel-binding recovery keep their usual schedule.

[fdf3635](https://github.com/offalexjackson777-stack/hyperflux-next/commit/fdf36352d8c31deed442d7119af3052c9daedcac)

We chose a bounded window over a shorter backoff everywhere. The cost is one more relay
state to reason about. The benefit is that a device that is really gone still settles into
the slow 30-second probe, so it does not keep the receiver busy.

**Tell Polychromatic users to refresh (#39).** The user documentation now says to wait a
few seconds after switching the keyboard on, then press F5 on Polychromatic's Devices tab.

[d664c59](https://github.com/offalexjackson777-stack/hyperflux-next/commit/d664c599e63db85196e94ef78ca6df93cd34140a)

## How It Was Checked

- Bridge and relay tests for #38 check that a strictly newer epoch refreshes an unchanged
  route exactly once, that unavailable routes ignore epochs, and that only a publication in
  backoff is reset, once.
- Relay runtime tests for #39 check the 2-second re-probe inside the window, that the
  window covers every generated return quiescence, that blocked failures stay blocked, and
  that publication and kernel-binding recovery do not use the window.

## What Is Still Open

- #38 shipped in 2.0.1. The issue is done when fresh return evidence bypasses a stale
  backoff exactly once on hardware, with no retry storm.
- #39 ships in 2.0.2. It stays open until a keyboard off and on cycle is checked on
  hardware as part of the 2.0.2 release checks.
- Polychromatic not following a returning device is upstream behaviour. It is listed in
  [Known limitations](../user/limitations.md).
