# Design Book Chapter VIII: Simulation And Verification

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 36. Virtual Receiver

The simulator implements the same transport interface as the kernel-backed path.
It creates receiver generations, pairs children, emits battery reports,
withdraws and returns routes, delays or mismatches responses, corrupts reports,
and simulates suspend without turning a fixture observation into a production
claim.

## 37. Deterministic Replay

Privacy-safe structured evidence from physical runs becomes immutable replay
fixtures. A fixture can reproduce the exact order of observations that once
caused a mouse wake failure or a reconnect-generation bug. Simulated time and
deterministic random seeds make failures reproducible. When a defect is fixed,
its replay becomes a permanent regression test. Raw serials and unnecessary
payloads are excluded so fixtures can safely live in the repository.

## 38. One Verification Entry Point

Developers and CI use one entry point:

```bash
./hfx verify --all
```

This command invokes a typed dependency graph rather than one enormous script.
It understands which tools, builds, schemas, fixtures, packages, applications,
and hardware stages are relevant. A failed OpenRGB contract does not erase
successful kernel results, and rerunning an unchanged stage reuses its verified
artifact.

## 39. Fast Verification

The fast lane runs formatting, linting, schema validation, generated-file
freshness, unit tests, documentation examples, and representative simulator
coverage. It is the ordinary pre-commit lane.

## 40. Full Software Verification

The full software lane builds supported kernel configurations, Rust services,
C++ integrations, Python clients, packages, documentation, and simulator
scenarios. It runs application contracts, lifecycle replay, installation,
upgrade, rollback, security, license, compatibility, and performance checks
without claiming physical proof.

## 41. Hardware Qualification

Physical testing is reserved for claims software cannot prove: a new command’s
visible effect, an unknown PID, LED ordering, sleep timing, battery meaning,
charging behavior, or receiver-specific lifecycle behavior. The test planner
compares changed source domains with capability evidence and selects only
affected physical scenarios. A documentation or widget-layout change must never
trigger a twenty-minute passive mouse proof.

## 42. Watched-Test Application

**Future work:** any reusable physical coordinator must present visible
checkpoints and validate source, artifacts, module state, receiver generation,
configuration, and explicit authorization before a write. Mission-specific
laboratory tooling is not shipped as runtime product machinery.

## 43. Typed Test Metadata

Every test declares its identity, owned domain, required capabilities, hardware
requirement, whether it performs writes, expected duration, timeout,
dependencies, isolation needs, cache inputs, produced evidence, and resume
policy. The orchestrator can then answer why a test is running and what
invalidated its cache. This replaces hard-coded test lists spread across
workflows and shell scripts.

## 44. Verification Outputs

Each run produces a concise human report and structured machine artifacts: JSON
results, JUnit, GitHub annotations, performance measurements, generated-file
checks, package hashes, and an evidence manifest. The manifest binds results to
source, schemas, profiles, dependencies, simulator fixtures, compiler versions,
and hardware generations where applicable. A green badge then represents a
specific reproducible candidate rather than an ambiguous workflow completion.

## 45. Advanced Testing Methods

Unit tests verify local behavior. Integration tests verify real components
together. Contract tests hold every adapter to the same SDK rules. Golden and
snapshot tests detect layout or generated-data drift. Property tests generate
unexpected combinations. Fuzzers attack parsers and migrations. Mutation testing
deliberately changes conditions to prove tests notice. Fault injection
introduces crashes, stale sockets, permission errors, delayed responses, disk
failures, and interrupted package hooks.

## 46. Formal Model Checking

The formal model covers high-risk ordering around lease acquisition, receiver
reconnect, frame delivery, and application failure. Formal methods target
concurrency invariants where state-space exploration adds evidence; they do not
replace implementation tests or physical qualification.

## 47. Language-Specific Tooling

Rust can use Clippy, nextest, proptest, cargo-fuzz, cargo-mutants, cargo-deny,
and API compatibility checks. Python can use uv, Ruff, mypy, pytest, and
Hypothesis. C++ can use CMake presets, clang-tidy, sanitizers, and
Include-What-You-Use. Kernel code can use sparse, Smatch, Coccinelle, KUnit,
KASAN, and KCSAN. Tools are adopted because they catch a defined failure class,
not merely because they look advanced.

---

- Previous: [Chapter VII: Schemas, SDKs And Reuse](07-schemas-sdks-and-reuse.md)
- [Design Book index](../design-book.md)
- Next:
  [Chapter IX: User Experience And Diagnostics](09-user-experience-and-diagnostics.md)
