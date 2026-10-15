# Design Book Chapter X: Updates, Packaging And Operations

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 52. Configuration Migrations

Every persisted configuration carries a schema version. An upgrade computes a
migration plan, writes the new version transactionally, validates it, and
retains a rollback copy until the new service starts successfully. Unsupported
downgrades are explained before damage occurs. Integrations store their own
presentation settings separately from bridge safety policy so updating OpenRGB
does not rewrite receiver configuration.

## 53. Package Lifecycle

Package qualification covers fresh installation, reinstall, compatible update,
driver-changing update, explicit service activation, configuration preservation,
rollback, and uninstall. Install hooks are registration-only and do not start
services, bind devices, or write hardware.

## 54. Kernel Activation

Linux cannot replace an in-use kernel module invisibly. The normal receiver-only
desktop path keeps the receiver connected, installs the packages, enables
documented boot services, and reboots. Disconnected activation is an advanced
path that requires alternate input or SSH.

## 55. Reproducible Development Environment

Compiler, Rust, Python, CMake, schema compiler, OpenRGB source, OpenRazer
source, and verification-tool identities are pinned. The network-disabled
development container and committed lockfiles reproduce release inputs
independently of one workstation.

## 56. Supply-Chain Security

Dependencies are reviewed, pinned, and license-checked, and CodeQL scans the
code. The repository keeps an
[SPDX inventory](../../../assurance/generated/hyperflux-next.spdx.json) of its
source and build dependencies; packages do not yet ship their own SBOM.
Verification evidence records the source revision, pinned toolchains, and
upstream revisions behind each run. GitHub Actions are pinned by commit. Arch
packages and the package repository database are signed with the project key.
Tags and commits are not signed, and there are no Sigstore attestations or SLSA
provenance yet. Imported upstream catalogs record source hashes so an unnoticed
upstream change cannot silently alter supported devices.

## 57. Performance Budgets

Performance becomes a tested contract. The repository defines acceptable bridge
startup time, transaction latency, effect-frame delay, reconnect recovery, queue
depth, CPU use, memory use, binary growth, and package size. A change that
technically passes but makes Rainbow Wave visibly stutter is not considered
successful. Historical benchmark trends help distinguish noise from meaningful
regression.

---

- Previous:
  [Chapter IX: User Experience And Diagnostics](09-user-experience-and-diagnostics.md)
- [Design Book index](../design-book.md)
- Next:
  [Chapter XI: Automation, Documentation And GitHub](11-automation-documentation-and-github.md)
