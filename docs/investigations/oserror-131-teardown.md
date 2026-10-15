# OSError 131 Around Sleep And Wake

OpenRazer could raise `OSError 131` when a device went to sleep or came back. The relay was
destroying a virtual device while a request to it had already been answered but not yet
delivered. The fix is on `dev` for 2.0.2 and still needs a hardware check.

Issue: [#37](https://github.com/offalexjackson777-stack/hyperflux-next/issues/37).

## What People Saw

Across sleep and return cycles, an OpenRazer read through HyperFlux could fail with
`OSError 131` instead of returning a value.

## How It Was Tracked Down

Error 131 is `ENOTRECOVERABLE`. HyperFlux's OpenRazer driver patch uses that errno for one
meaning: the request may have been sent, and nothing may repeat it (see
[OpenRazer patch removal routes](../architecture/decisions/openrazer-patch-removal.md) for
the patch). So the question was which request was being reported as uncertain, and why.

Sleep and return are exactly when the relay withdraws a virtual device and later creates a
new one, so its teardown was the place to look. The relay talks to the kernel through
uhid: a Feature GET from the OpenRazer driver waits in the kernel until the relay writes
the reply. The window that mattered was between the
relay writing that reply and the waiting driver thread resuming.

## The Cause

If the relay sends `UHID_DESTROY` inside that window, uhid stops the device and fails the
waiting GET with `EIO`, even though its answer has already arrived. The driver cannot tell
that from a lost request, so OpenRazer surfaces the uncertain result as `OSError 131`.

The relay's teardown order was the problem: it destroyed the device without first letting
in-flight requests finish.

## The Fix

[b7ce9a4](https://github.com/offalexjackson777-stack/hyperflux-next/commit/b7ce9a4e10a83c369803e8efbef5d54c3f83ed3e)

Before `UHID_DESTROY`, the relay now detaches the OpenRazer driver from its own device:

1. It finds its device in the route's OpenRazer driver directory by matching `HID_PHYS`.
2. On a worker thread, it writes that device to the driver's `unbind` file. The endpoint
   keeps answering requests while this happens.
3. The kernel drains in-flight requests before the unbind returns, so an answered request
   completes with its answer.
4. The relay then destroys the device, once.

If no driver is bound, the unbind fails, or 5 seconds pass, the destroy goes ahead as before
and a typed `HFX-OPENRAZER-RELAY-TEARDOWN` journal line records what happened.

We chose an orderly unbind over keeping the device alive longer, because the kernel already
knows how to drain a driver's requests on unbind. The cost is that the relay now needs to
write to `/sys`. The relay unit keeps `ProtectKernelTunables` and gains write access only
to each OpenRazer driver's `unbind` file.

The teardown lives in `crates/hfx-uhid-relay/src/runtime/teardown.rs`.

## How It Was Checked

- Teardown unit tests check that only the device with this endpoint's phys is unbound,
  that a missing or foreign binding falls back to an immediate destroy, that an unwritable
  unbind is a typed failure, and that a blocked unbind is bounded by the deadline.
- A runtime test checks that an in-flight GET is answered before the unbind and destroy.
- An integration test checks that the generated relay unit keeps `ProtectKernelTunables`
  and that its only writable `/sys` paths are the drivers' `unbind` files.

## What Is Still Open

The fix ships in 2.0.2. #37 is done when an answered request always completes before its
endpoint is destroyed across repeated sleep and return cycles. That hardware check is part
of the 2.0.2 release checks, and the issue stays open until it is recorded.
