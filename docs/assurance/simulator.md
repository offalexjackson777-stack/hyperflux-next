# Virtual Receiver And Replay

HyperFlux Next treats simulation as executable policy, not physical evidence. The virtual receiver uses the same generated domain types and composable hardware profiles as the production bridge will use, while retaining an explicit `test_fixture=true` and `hardware_claim_authority=false` boundary.

## Model

The simulator supports arbitrary logical children. A scenario can contain only a mouse, only a keyboard, both, neither, or future device kinds represented as unknown. No child requires a sibling, and an unknown or unqualified child receives zero write authority.

Each child tracks independent evidence for:

- pairing;
- receiver route;
- explicit power;
- sleep;
- mat contact where applicable;
- activity;
- freshness;
- battery availability and percentage.

These dimensions are deliberately not collapsed into one online flag. A sleeping paired device, a powered-off device, an unavailable route, and stale evidence have different meanings.

## Generations And Restoration

A receiver reconnect creates a strictly newer generation. Disconnect invalidates generation-scoped observations and partial restore work. Events from an older generation fail closed, and delayed observations cannot replace newer evidence.

Stable restoration uses the production coordinator against simulator implementations of the same persistence and receiver-transport ports. Durable intent, per-device claims, exact dispatch identities, transport terminals, and bounded eviction tombstones survive a simulated bridge-process crash. Sessions, leases, transaction queues, and event buffers do not. A process restart is therefore distinct from a receiver disconnect and does not invent a newer generation.

The fault harness can stop before or after a restore-record compare-and-set, after transport reservation, after the physical write, and after either transport or restore terminal persistence. Tests establish these software invariants:

- only an exact `NotObserved` reconciliation may begin a new physical write;
- retained success completes without a second write;
- reservation, write-start, lookup loss, conflict, and eviction ambiguity fail closed;
- forgotten bounded history never regresses to `NotObserved` within that generation;
- a new receiver generation receives an independent history floor;
- one sleeping child cannot block a ready sibling;
- repeating one trigger is idempotent, while a distinct lifecycle trigger is separately accountable.

The simulator proves policy and crash behavior, not a physical color, battery meaning, report format, or device capability. Those claims still require linked hardware evidence.

## Replay Boundary

Committed replay fixtures conform to `schemas/simulator-scenario.schema.json`. They are bounded, strict, and privacy-safe:

- no hardware serials or stable host identifiers;
- no private filesystem paths;
- no raw HID or USB payloads;
- no more than 32 logical children or 4096 events;
- no authority to establish a physical hardware claim.

Replay uses virtual time and stable ordering for delayed events. The result includes deterministic state and trace SHA-256 digests, allowing the same fixture to become a permanent regression test.

## Verification

Run the complete typed graph:

```console
./hfx verify --all
```

Inspect why each stage runs, its dependencies, timeout, isolation, and hardware authority:

```console
./hfx test plan --all
```

The current graph is software-only and cannot write hardware. Physical qualification remains a separate, explicitly authorized stage for claims a simulator cannot establish.
