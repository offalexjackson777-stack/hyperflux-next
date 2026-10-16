# Battery Readings Jump

Repeated battery reads through HyperFlux could jump between unrelated values, for example
27%, then 50%, then 0%. A battery reply meant for the other device on the same receiver
could be accepted, and an old reading could be replayed as current. The fix shipped in
2.0.2 and passed its hardware check.

Issue: [#44](https://github.com/offalexjackson777-stack/hyperflux-next/issues/44).

## What People Saw

On 2026-08-23, repeated OpenRazer battery reads through HyperFlux were seen to go
27% → 50% → 0%. In 2.0.0, a battery value should only ever come from a successful,
supported reply, and anything else should be reported as unknown. A reported 0% could
therefore be a regression of an earlier battery fix.

## How It Was Tracked Down

One receiver serves both the keyboard and the mouse through a single control interface,
so replies for both children arrive on the same path. Each request carries a transaction
ID. The question was what on the path checks that a reply belongs to the request that is
waiting for it.

Following an untranslated battery exchange from OpenRazer to the receiver showed that
nothing did:

- the receiver profile set the transaction-ID `match_mask` to `0`, so the kernel driver
  accepted a reply with any transaction ID;
- stock OpenRazer checks only the command class and command ID.

A second path produced values that were simply old. When a retained route timed out, the
relay replayed the last confirmed battery reply as if it had just been read.

The logs could not settle which path produced the reported numbers: the relay's battery
log line recorded the request's transaction ID, not the reply's. One explanation, not yet
confirmed on hardware, is that 27% and 50% were the keyboard's and the mouse's real levels,
read alternately.

## The Cause

- A late battery reply for the other lane (`0x1f` versus `0x9f`) could be reported as this
  device's level.
- A timeout could return a stale level instead of "unknown".

Either one is enough to make the reported value jump.

## The Fix

[0642d7e](https://github.com/offalexjackson777-stack/hyperflux-next/commit/0642d7eca2c5d9a1bb539f0d96005b796f01a096)

**Match the transaction ID in the kernel.** Receiver profile revision 5 sets the
transaction-ID `match_mask` to `0xff`, so a reply for another transaction is not matched
and the driver keeps polling within its existing attempt limit and deadline. This relies on
the receiver echoing the on-wire transaction ID. The 2026-07-27 read-parity evidence
supports that: every battery and read check passed through translated exchanges, which the
kernel already rejects unless they echo the routed lane.

**Accept only exact battery replies in the relay.** The relay passes a battery reply
through only when its status is SUCCESS and its transaction ID, command class, and command
ID all match the request. A reply for another transaction is retried within the existing
bound. Every other outcome, including BUSY, FAILURE, NOT_SUPPORTED, and TIMEOUT, becomes
the existing "may have been sent" transport error, which OpenRazer reports as an error
instead of a number.

**Stop replaying old readings.** The retained confirmed battery read is removed. A battery
timeout still withdraws the endpoint as before, and the read in flight completes as
unknown.

We chose to report "unknown" rather than the last good value. The cost is that a battery
level can briefly disappear; the benefit is that the number on screen is never wrong.

Non-battery replies are unchanged. The `HFX-OPENRAZER-RELAY-BATTERY` journal line now
records `reply_tx` and `outcome=value|unknown`, so a hardware log shows which reply was
accepted.

## How It Was Checked

- Relay endpoint tests check that a reply for another lane or command is never reported as
  a value, that a mismatched reply is retried until the matching one arrives, that failure
  statuses are reported as unknown without retry, and that a retained-route timeout
  completes the read as unknown instead of stale.
- Kernel driver tests and the regenerated profile fixtures carry the revision 5 receiver
  profile.

## What Is Still Open

The fix shipped in 2.0.2. In the 2.0.2 hardware check, with the keyboard and the mouse
connected to the same receiver, each kept its own battery level and no reading was
borrowed from the other device, so #44 is closed. The `reply_tx` field in the battery log
line shows whether a reply for the other device was ever accepted.

Where the reported 0% came from is not proven. Whether a successful reply that carries 0%
should count as a real level is a separate question: the
[bridge core](../architecture/bridge-core.md) currently treats a reported zero as a real
value.
