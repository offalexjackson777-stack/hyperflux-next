# Kernel Boundary

The HyperFlux Next kernel module is a deliberately small Linux HID boundary. It
binds only receiver identities generated from qualified receiver profiles. It
does not contain child product names, device layouts, effects, application
policy, support-list guesses, or active information queries.

## Responsibilities

The module owns five things that require kernel proximity:

1. preserve ordinary HID input with `HID_CONNECT_DEFAULT` and never consume or
   rewrite input reports;
2. represent one physical USB receiver as one generation shared by all of its
   HID interfaces;
3. publish bounded, sequenced passive observations without interpreting them as
   retail-device state;
4. admit many read-only observers but exactly one `O_RDWR` writer file and one
   expiring generation-bound authorization session;
5. validate and deliver a bounded set of receiver frames while retaining an
   exact terminal result journal.

The bridge remains responsible for profile selection, capability qualification,
logical devices, carrier maps, effects, scheduling policy, leases, persistence,
retry decisions, and user-facing meaning.

## Userspace Adapter

The Rust kernel adapter selects an encoder from the qualified receiver
profile's transport-backend identifier. It resolves each logical child profile
independently, applies that profile's application-slot-to-carrier map, and
emits only the bounded physical frames accepted by the kernel UAPI. A receiver
may therefore carry a mouse, a keyboard, or both without an exact-pair profile.
Unknown children receive no encoder or write authority.

The qualified HW001 encoder preserves the observed two-pass receiver sequence,
inter-frame timing, and mouse carrier correction. It also records which
physical reports belong to each semantic device frame. Terminal kernel progress
can therefore be projected conservatively into bridge-level delivery facts
without confusing a partially delivered keyboard row sequence with a complete
keyboard update.

Every request digest binds the complete semantic dispatch and every encoded
frame field. The only unsafe Rust is the private, generated-type ioctl capsule;
profile resolution, encoding, observation decoding, session renewal, and result
interpretation remain safe and independently testable through an injected I/O
port.

## Lifecycle

All HID interfaces belonging to the same `usb_device` share one receiver
object. The first bound interface creates a monotonically increasing receiver
generation and a mode `0600` character device. Additional interfaces join that
generation. The last removal retires it.

Suspend, physical removal, writer close, timeout, identity conflict, transport
failure, and explicit shutdown revoke the live writer session. A session is
bound to its exact open file, receiver generation, authorization epoch,
profile digest, capability digest, daemon nonce, and Linux boottime deadline.
Reconnecting the USB receiver creates a new generation; no authority or pending
claim crosses that boundary.

## Passive Observation

The module records raw facts from already-arriving HID reports:

- receiver availability and suspend transitions;
- pointer-lane and keyboard-lane activity;
- observed child product IDs;
- raw route, battery, contact, charge, and status fields.

Observations use a 256-entry sequence ring. Readers supply their last sequence
and receive at most 32 records per call plus an explicit cursor-gap flag. An
identity change within one generation is terminally conflicting: the event is
recorded, the writer is revoked, and no new session can begin for that
generation. The module never sends an active query to fill missing identity or
battery data.

## Transport And Outcomes

Userspace supplies only frames produced by a qualified backend encoder. The
kernel validates backend, report kind, exact payload length, reserved bytes,
geometry, unused bytes, checksum, per-frame delay, aggregate delay, and a zero
delay after the final frame. One transaction contains at most 16 frames and at
most one second of bounded inter-frame delay.

Each accepted dispatch nonce is reserved before the first possible USB write.
The module retains 64 full terminal records and 64 digest-bearing tombstones.
An exact retained request is never sent twice. A forgotten old request is
`unavailable`, never guessed to be absent. `not_observed` is returned only for
a nonce strictly above the live session high-water mark and is the sole result
that permits an automatic retry without a possible prior hardware side effect.
Exact generation, nonce, and full-dispatch-digest lookup crosses writer-session
rotation, so a bridge restart can recover a retained result without inheriting
the old writer authorization epoch. Reusing a nonce with different content in
the current kernel session remains a terminal conflict.

On startup, the bridge reconciles durable nonterminal work against this kernel
journal before admitting new writes. This ordering prevents unrelated traffic
from aging recoverable outcomes out of the bounded journal during crash
recovery.

`succeeded` means every frame was delivered through the receiver USB control
transfer. It does not claim that a paired child applied the frame or that a
person observed the intended lighting. That higher-level distinction remains
in the bridge protocol.

## Verification

`./hfx verify --all` always compiles and executes the portable UAPI, passive
parser, checksum, and wire-envelope tests. It also builds the module with
warning-fatal checks against the running kernel headers. Release or CI jobs can
provide an explicit matrix with `HFX_KERNEL_BUILD_DIRS`, using colon-separated
absolute header directories.

These are software-only checks. Building the module does not load it, bind a
receiver, issue a query, or write hardware. Any future live qualification still
requires its own explicit authorization and evidence stage.
