# Design Book Chapter VIII: Simulation And Verification

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

Most of HyperFlux cannot be tried on a real receiver in CI, so a mistake has to be caught in
software or it reaches a hardware check, or a user. This chapter describes how a simulated
receiver, recorded replays, and one verification entry point catch mistakes in software, and
what is left for hardware checks.

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

Developers and CI use one software-verification entry point:

```bash
./hfx verify --full
```

This command runs a typed dependency graph of software checks rather than one
enormous script. Each node declares the tools, builds, schemas, fixtures,
packages, and applications it covers, and `--changed-from <revision>` selects
only the nodes whose inputs changed; a changed path that no node owns selects
the whole lane. The command refuses any node that needs hardware or writes to
it, so physical qualification stays a separate step. A failed OpenRGB contract
does not erase successful kernel results: only the nodes that depend on a failed
node are blocked. Every selected node runs on each invocation. The run records a
digest of each node's inputs, but it does not reuse earlier results.

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
charging behavior, or receiver-specific lifecycle behavior. It runs outside
`./hfx verify`, and each physical run is chosen for the claim it has to prove. A
documentation or widget-layout change does not call for a physical run.

## 42. Watched-Test Application

**Future work:** any reusable physical test coordinator must show visible
checkpoints and check the source, artifacts, module state, receiver generation,
configuration, and operator confirmation before a write. One-off bench tooling
is not shipped in the product.

## 43. Typed Test Metadata

Every test declares its identity, owned domain, required capabilities, hardware
requirement, whether it performs writes, expected duration, timeout,
dependencies, isolation needs, cache inputs, produced evidence, and resume
policy. `./hfx test plan` lists the selected nodes in order with the selection
mode, dependencies, and hardware requirement, and a changed-path run records the
changed paths it was selected from. Cache inputs drive change selection; the
resume policy is recorded metadata, not an artifact cache. This replaces
hard-coded test lists spread across workflows and shell scripts.

## 44. Verification Outputs

Each run writes a short human summary and structured files: JSON results, JUnit,
GitHub annotations, and an evidence manifest. The manifest binds the results to
the source revision and working-tree state, digests of the schemas, profiles,
dependency policy, lock file, and replay fixtures, the pinned toolchain
versions, the upstream revisions, any package hashes, and the duration of each
node. It also records that no hardware was queried or written. A green badge
then represents a specific source candidate rather than an ambiguous workflow
completion.

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
