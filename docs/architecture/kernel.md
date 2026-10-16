# Kernel Boundary

The receiver is one USB device with several HID interfaces, and the keyboard and mouse
behind it send their ordinary input through those interfaces. Kernel code that talks to the
receiver can break typing and pointing for everything on it, and a command sent twice may
change a device twice. This page describes the small kernel module that gives userspace one
narrow way to exchange reports with the receiver while leaving input alone.

The HyperFlux Next kernel module is a deliberately small Linux HID boundary. It
binds only receiver identities generated from qualified receiver profiles. It
does not contain child product names, device layouts, effects, application
policy, support-list guesses, or active information queries.

We kept in the kernel only the work that needs kernel access, so route, ownership, and
diagnostic decisions can be tested as ordinary userspace code. The cost is that every
exchange has to cross a fixed, versioned interface between the bridge and the module.

## Terms Used Here

- **Generation**: one physical connection of the receiver. Replugging the receiver starts a
  new generation, and nothing carries over from the old one.
- **Writer session**: the single open file allowed to send to one receiver generation. It
  expires, and it is revoked on suspend, removal, and the other events listed under
  Lifecycle.
- **Native exchange**: one fixed-size Feature report built by OpenRazer or OpenRGB, sent to
  a device, and its matching response.
- **Certainty**: what the kernel can say about whether a report was sent: `not-sent`,
  `may-have-been-sent`, or `matched-response`.
- **Migration fallback**: the older version-1 path, in which HyperFlux encodes the receiver
  frames itself. It is kept for rollback.

## Responsibilities

The module owns seven things that require kernel proximity:

1. preserve ordinary HID input with `HID_CONNECT_DEFAULT` and never consume or
   rewrite input reports;
2. represent one physical USB receiver as one generation shared by all of its
   HID interfaces;
3. publish bounded, sequenced passive observations without interpreting them as
   retail-device state;
4. admit many read-only observers but exactly one `O_RDWR` writer file and one
   expiring generation-bound authorization session;
5. execute one qualified fixed-size native Feature exchange at a time, with one
   SET attempt, bounded response acquisition, declarative matching, and exact
   certainty;
6. retain the version-1 bounded receiver-frame transport and terminal journal
   as an isolated migration fallback;
7. publish bridge-forwarded child battery state in `power_supply`; never query
   the device for it.

For the native path, the bridge remains responsible for exact route identity,
profile qualification, generation safety, NativeControl leases, bounded fair
admission, deadlines, and user-facing diagnostics. OpenRazer and OpenRGB remain
responsible for device commands, settings, layouts, effects, and persistence.
The kernel treats command content as opaque except for the declarative report
validation and response-correlation fields needed to transport it safely.

## Userspace Adapter

The active native path resolves an exact qualified route, fixed report family,
endpoint slot, route-token digest, generation, and deadline before entering the
kernel adapter. The adapter submits one 90-byte upstream Feature report through
UAPI v2 and projects only the kernel's `not-sent`, `may-have-been-sent`, or
`matched-response` result. It neither constructs device commands nor translates
LED slots. A receiver profile may declare one route-selector field; the kernel
applies that receiver-only addressing rule to a clone and preserves the
application request.

The retained UAPI-v1 migration path still selects a receiver encoder, resolves
each child independently, applies its qualified application-slot-to-carrier map,
and emits bounded physical frames. That behavior remains available for rollback
but is prevented from competing with a child-local NativeControl lease. Unknown
children receive neither a native route nor semantic write authority.

In the migration fallback, the qualified HW001 encoder preserves the observed
two-pass receiver sequence, inter-frame timing, and mouse carrier correction. It
also records which physical reports belong to each semantic device frame.
Terminal kernel progress can therefore be projected conservatively into
bridge-level delivery facts without confusing a partially delivered keyboard row
sequence with a complete keyboard update.

Native request digests bind the route, generation, endpoint, report, deadline,
and dispatch identity. Legacy request digests continue to bind the complete
semantic dispatch and every encoded frame field. The only unsafe Rust is the
private, generated-type ioctl capsule; route and profile resolution, encoding,
observation decoding, session renewal, and result interpretation remain safe
and independently testable through an injected I/O port.

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

## Child Battery Publication

The module publishes bridge-forwarded battery state in `power_supply`; never
queries. The bridge decodes passive battery reports as before and forwards each
changed lane state through `HFX_UAPI_IOCTL_REPORT_CHILD_BATTERY`, a write-only
64-byte record defined by `uapi/power-supply-v3.json`. Only the current writer
file may forward, and only for its exact receiver generation; no authorization
session is needed, so a battery survives session expiry and suspend
revocation. A record names one child lane (pointer or keyboard) and carries
presence, capacity (0-100), status, and a printable, NUL-terminated model name
of at most 31 bytes. Malformed headers fail with `EPROTO`, invalid values with
`EINVAL`, another generation with `ESTALE`, and a lane whose HID interface is
not bound with `EHOSTDOWN`.

A present record registers one `power_supply` per lane named
`<session-name>-mouse` or `<session-name>-keyboard`, parented to that lane's
HID interface, with `SCOPE=Device`, `PRESENT`, `CAPACITY`, `STATUS`,
`MODEL_NAME`, and `MANUFACTURER=Razer`. Every property is answered from the
stored forwarded value under a per-lane spinlock; the kernel holds no model
name table and never sends a report to the receiver for a property read. A
changed model name re-registers the supply instead of rewriting a string a
reader may hold.

Each supply is unregistered when the bridge forwards an absent lane (child
unpaired, gone, stale, or its battery unavailable), when the writer file that
published it is released, when the lane's interface is removed (before
`hid_hw_stop`), and when the receiver generation is destroyed (before its
character device is deregistered). Registration and teardown are serialized by
a per-receiver `battery_lock`, taken before `state_lock` and
`authorization_lock` and never with `registry_lock` or `transport_lock` held.
Kernels built without `CONFIG_POWER_SUPPLY` reject the operation with
`EOPNOTSUPP`.

The forwarded status comes from the bridge, not the module. The bridge decodes
the raw charge field only for a child profile that declares a qualified charge
decoder; today that is the exact mouse profile `0x00cd`. Every other child
forwards `Unknown`. A charging value becomes `Charging`, or `Full` when the
level of the same lane reads 100 percent, whatever byte 3 of the report reads.
A not-charging value becomes `Discharging` only when byte 3 of the same report,
which the module emits as the contact field just before the charge field,
reads its proven off-the-mat value `0`. Byte 3 is not only a mat contact: it
read `6` on a USB cable. Every other byte 3 value, a missing or older one, and
any charge value the decoder does not list stay `Unknown`. A vendor charge
limit is not inferred; the mouse itself keeps reporting charging while it
holds at its limit, and the bridge forwards that. The charge only annotates a
lane that has a forwarded level, and the bridge forgets it whenever that lane
is forwarded absent, when the writer file is released, and with each new
receiver generation, so an old charge value is never reused. The UAPI carries
no not-charging status, and the bridge does not add one without evidence.

## Transport And Outcomes

### Native Exchange V2

Userspace supplies exactly one 90-byte report produced by a qualified upstream
Razer implementation, plus generation, authorization epoch, dispatch nonce,
deadline, endpoint slot, route-token digest, and request digest. The kernel
validates the additive UAPI-v2 structure and the receiver profile's fixed report
shape before reserving a sequence. A per-receiver transport lock permits one
active exchange and prevents a native operation from interleaving with the
legacy carrier path.

The SET begins at most once. Before it begins, the kernel may replace only the
receiver profile's declared route selector in a cloned request. After SET begins,
only bounded GET acquisition may repeat according to declarative profile timing
and response-matching fields. The SET is never replayed. A complete matching
90-byte response has the application's original selector restored and is then
returned without changing the device's own status; HyperFlux does not
reinterpret Busy, Failure, or Not Supported as transport success.

The native journal retains 128 complete results and 128 digest-bearing
tombstones. Exact replay resolves the retained outcome without another SET.
Generation, route, endpoint, nonce, or digest conflicts fail closed. The three
certainty classes mean:

- `not-sent`: transport did not begin and an exact retry may be considered by
  userspace;
- `may-have-been-sent`: SET may have begun, so no layer may repeat it
  automatically;
- `matched-response`: one full response passed declarative correlation and is
  returned with only the declared receiver selector restored to the caller's
  original value.

### Semantic Envelope V1 Migration Fallback

Userspace supplies only frames produced by a qualified fallback encoder. The
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

On startup, the migration runtime reconciles durable nonterminal semantic work
against this version-1 journal before admitting fallback writes. This ordering
prevents unrelated traffic from aging recoverable outcomes out of the bounded
journal during crash recovery.

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
receiver, issue a query, or write hardware. Live qualification is a separate
physical evidence step on a real receiver.
