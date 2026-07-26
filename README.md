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
    Applications["OpenRazer and OpenRGB"] --> SDK["Versioned native-route SDK"]
    SDK --> Bridge["Route and exchange authority"]
    Bridge --> Kernel["Bounded receiver transport"]
    Kernel --> Receiver["HyperFlux receiver and paired devices"]
```

Applications own model commands, settings, layouts, effects, profiles, and
persistence. The SDK carries qualified route discovery and exact 90-byte Razer
Feature exchanges. The bridge is the sole userspace receiver writer and owns
route qualification, generation safety, NativeControl arbitration, bounded
dispatch, availability, outcome certainty, and diagnostics. The kernel
preserves ordinary HID input and transports generation-bound exchanges without
interpreting device semantics.

## OpenRazer Route

HyperFlux applies a narrow transport patch to official
[OpenRazer 3.12.4](https://github.com/openrazer/openrazer/commit/1ef8a91712906a89fd332a2ed62bbd105315d6d1).
The upstream daemon, Python client, device classes, ordinary USB tables, and
direct-device behavior remain OpenRazer-owned. The patched DKMS modules accept
only trusted, feature-only HyperFlux UHID routes and advertise
`hyperflux_native_transport=razer-feature-report-v1`.

| Qualified receiver-backed controller | Native route | Direct USB precedence |
| --- | --- | --- |
| Razer Basilisk V3 Pro 35K (Wireless) | `1532:00cd` | `1532:00cc` |
| Razer DeathStalker V2 Pro TKL (Wireless) | `1532:0296` | `1532:0298` |

Distribution release suffixes do not affect compatibility. A newer OpenRazer
source line requires a reviewed rebase and fresh verification; until then,
`hyperflux-next-openrazer-relay.service` remains stopped. See the
[deployment contract](docs/generated/openrazer-native-deployment.md),
[installation contract](docs/generated/installation.md), and
[hardware boundary](docs/generated/supported-hardware.md). Other catalog
devices remain research-only and non-writable.

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
