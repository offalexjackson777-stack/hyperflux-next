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

Restore scenarios declare all targets before delivery begins. A restore completes only after every declared target has one terminal delivery. Disconnect, transport failure, duplicate delivery, and unknown targets prevent a false complete result.

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
