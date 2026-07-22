# HyperFlux Next

**Evidence-bound Linux support for devices paired through Razer HyperFlux V2.**

[![Verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
![Development state](docs/assets/badge-state.svg)
![License](docs/assets/badge-license.svg)

> [!IMPORTANT]
> **Unreleased and evidence-bound.** Public source is available for review; no supported product release or package channel exists.

## Choose A Path

| I need to... | Go to... |
| --- | --- |
| Understand the project | [Project overview](docs/user/overview.md) |
| Check installation availability | [Installation status](docs/generated/installation.md) |
| See hardware evidence | [Supported hardware](docs/generated/supported-hardware.md) and [device knowledge](docs/generated/device-knowledge.md) |
| Inspect an installed candidate | [Local Device Qualification Console](apps/device-qualification/README.md) |
| Understand or change the code | [Architecture](docs/architecture/design-book.md), [Repository Atlas](docs/generated/repository-atlas.md), and [Contributing](CONTRIBUTING.md) |
| Review blockers and evidence | [Release gates](docs/generated/release-gates.md), [verification graph](docs/generated/verification.md), and [Roadmap](https://github.com/users/offalexjackson777-stack/projects/1) |
| Get help or report a bug | [Support](SUPPORT.md) and [issue forms](https://github.com/offalexjackson777-stack/hyperflux-next/issues/new/choose) |
| Report a vulnerability | [Security policy](SECURITY.md) and [private reporting](https://github.com/offalexjackson777-stack/hyperflux-next/security/advisories/new) |

## Architecture

```mermaid
flowchart LR
    Applications["Applications"] --> SDK["Versioned SDK"]
    SDK --> Bridge["Bridge and policy authority"]
    Bridge --> Kernel["Minimal HID transport"]
    Kernel --> Receiver["HyperFlux receiver and paired devices"]
```

Applications own user interaction, layouts, and effects. The SDK owns the typed
application boundary. The bridge is the sole userspace writer and owns policy,
qualification, scheduling, restoration, and outcomes. The kernel preserves
ordinary HID input and transports bounded generation-bound envelopes.

## Current Readiness

<!-- Generated from generated/public-readiness.json. -->

| Surface | Plain-language state |
| --- | --- |
| Product | **Unreleased**: Public source is available for review; no supported product release or package channel exists. |
| Software | 5 of 10 release gates are ready in software. |
| Hardware | 2 receiver routes have bounded physical evidence; 10 candidates remain research only. |
| Remaining evidence | 3 hardware gate and 1 lifecycle gate remain; known gaps stay explicit. |
| Local qualification | Installed, loopback-only identity and telemetry checks; hardware-changing runners remain explicitly unavailable |

This compact status is generated from [`generated/public-readiness.json`](generated/public-readiness.json).
Detailed claims remain in the linked evidence ledgers rather than being repeated here.

## Repository Map

| Area | Primary locations |
| --- | --- |
| User and contributor guidance | [`docs/`](docs/), [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SUPPORT.md`](SUPPORT.md) |
| Installed experiences | [`apps/`](apps/), [`integrations/`](integrations/) |
| Runtime and kernel | [`crates/`](crates/), [`runtime/`](runtime/), [`driver/`](driver/), [`uapi/`](uapi/) |
| Public interfaces | [`sdk/`](sdk/), [`protocol/`](protocol/), [`schemas/`](schemas/), [`errors/`](errors/) |
| Hardware knowledge | [`profiles/`](profiles/), [`knowledge/`](knowledge/) |
| Evidence and delivery | [`tests/`](tests/), [`verification/`](verification/), [`assurance/`](assurance/), [`packaging/`](packaging/) |
| Repository authority | [`architecture/`](architecture/), [`governance/`](governance/), [`tools/`](tools/) |

The generated [Repository Atlas](docs/generated/repository-atlas.md) is the canonical ownership and dependency map.

## Verify A Change

```sh
./hfx generate
./hfx verify --changed-from <commit>
./hfx verify --all
```

Generated files must be reproducible in one pass. Verification is change-aware,
networkless after exact upstream preparation, device-free, and incapable of
granting hardware or release authority.

<details>
<summary>Engineering and publication boundaries</summary>

- Unknown devices expose safe identity and passive observations but receive no
  writable capability.
- Imported upstream catalogs contribute provenance-bound knowledge, never raw
  transport authority.
- Release, package, tag, and hardware-writing workflows are absent until a
  separate authorization changes their canonical interlocks.
- The [Repository Atlas](docs/generated/repository-atlas.md) is the authoritative directory and
  ownership map; folder READMEs are generated projections.

</details>

Project-owned kernel and core work is `GPL-2.0-only`. SDKs and integrations use
declared compatible licenses. See [licensing policy](docs/legal/licensing.md).
