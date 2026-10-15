# Design Book Chapter X: Updates, Packaging And Operations

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

An update touches the kernel module, the services, and saved configuration on a user's
machine, and a mistake in any of them can break a setup that worked. This chapter covers how
configuration, packages, and the kernel module are updated, how builds stay reproducible,
and what the project does and does not check about its supply chain and performance.

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

Performance budgets are declared in
[`assurance/performance-budgets.json`](../../../assurance/performance-budgets.json)
and listed on the [performance budgets](../../generated/performance-budgets.md)
page. They fall into two groups.

Ten software budgets are checked by executable contracts. The bridge timeout,
queue, event, and message limits are checked by `assurance-contracts` in the
fast and full lanes. Installed binary and payload sizes are checked by
`package-contracts` in the full lane.

Five physical budgets cover visible effect-frame delay, reconnect recovery,
bridge readiness after service start, steady CPU use, and steady memory use.
They need measurement on an installed system with real hardware. No
measurement is retained in the repository yet, so these budgets are declared
but unmeasured, and they do not block a release.

The repository stores no benchmark history, so it cannot yet tell noise from a
gradual regression. Trend analysis needs retained measurements first.

---

- Previous:
  [Chapter IX: User Experience And Diagnostics](09-user-experience-and-diagnostics.md)
- [Design Book index](../design-book.md)
- Next:
  [Chapter XI: Automation, Documentation And GitHub](11-automation-documentation-and-github.md)
