# Virtual Receiver And Replay

Many of the problems that reached users happened when something changed underneath the
bridge: a device slept, the receiver went away, or a process stopped halfway through its
work (see [Investigations](../investigations/README.md)). Those moments are hard to
reproduce on demand with a real receiver, and CI has no receiver at all. The virtual
receiver lets tests create those moments and replay them exactly, so the bridge's rules can
be checked in software.

HyperFlux Next treats simulation as executable policy, not physical evidence. The virtual
receiver uses the same generated domain types and composable hardware profiles as the
production bridge, while keeping an explicit `test_fixture=true` and
`hardware_claim_authority=false` marker, so a simulated result can never count as proof that
real hardware behaves the same way.

## Terms Used Here

- **Child**: a device paired with the receiver, such as a keyboard or a mouse.
- **Generation**: one physical connection of the receiver. A reconnect starts a strictly
  newer one.
- **Restoration**: the bridge reapplying saved lighting after a restart, on the retained
  migration path.
- **Replay fixture**: a recorded scenario that runs the same way every time and becomes a
  permanent regression test.

## Model

The simulator supports arbitrary logical children. A scenario can contain only a
mouse, only a keyboard, both, neither, or future device kinds represented as
unknown. No child requires a sibling, and an unknown or unqualified child
receives zero write authority.

Each child tracks independent evidence for:

- pairing;
- receiver route;
- explicit power;
- sleep;
- mat contact where applicable;
- activity;
- freshness;
- battery availability and percentage.

These dimensions are deliberately not collapsed into one online flag. A sleeping
paired device, a powered-off device, an unavailable route, and stale evidence
have different meanings.

## Generations And Restoration

A receiver reconnect creates a strictly newer generation. Disconnect invalidates
generation-scoped observations and partial restore work. Events from an older
generation fail closed, and delayed observations cannot replace newer evidence.

Stable restoration uses the production coordinator against simulator
implementations of the same persistence and receiver-transport ports. Durable
intent, per-device claims, exact dispatch identities, transport terminals, and
bounded eviction tombstones survive a simulated bridge-process crash. Sessions,
leases, transaction queues, and event buffers do not. A process restart is
therefore distinct from a receiver disconnect and does not invent a newer
generation.

The fault harness can stop before or after a restore-record compare-and-set,
after transport reservation, after the physical write, and after either
transport or restore terminal persistence. Tests establish these software
invariants:

- only an exact `NotObserved` reconciliation may begin a new physical write;
- retained success completes without a second write;
- reservation, write-start, lookup loss, conflict, and eviction ambiguity fail closed;
- forgotten bounded history never regresses to `NotObserved` within that generation;
- a new receiver generation receives an independent history floor;
- one sleeping child cannot block a ready sibling;
- repeating one trigger is idempotent, while a distinct lifecycle trigger is separately accountable.

The simulator proves policy and crash behavior, not a physical color, battery
meaning, report format, or device capability. Those claims still require linked
hardware evidence.

## Replay Boundary

Committed replay fixtures conform to `schemas/simulator-scenario.schema.json`.
They are bounded, strict, and privacy-safe:

- no hardware serials or stable host identifiers;
- no private filesystem paths;
- no raw HID or USB payloads;
- no more than 32 logical children or 4096 events;
- no authority to establish a physical hardware claim.

Replay uses virtual time and stable ordering for delayed events. The result
includes deterministic state and trace SHA-256 digests, allowing the same
fixture to become a permanent regression test.

## Verification

Run the complete software graph, which includes the simulator and replay tests:

```console
./hfx verify --full
```

To see why each stage runs, with its dependencies, timeout, isolation, and whether it may
touch hardware:

```console
./hfx test plan --full
```

The current graph is software-only and cannot write hardware. Physical
qualification is a separate step on real hardware for claims a simulator cannot
establish.
