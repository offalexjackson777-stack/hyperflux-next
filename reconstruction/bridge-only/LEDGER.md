# Bridge-Only Reconstruction Ledger

This ledger records the approved bridge-only reconstruction from the isolated
`reconstruct/bridge-only-runtime` worktree. It is evidence for offline review;
it does not authorize installation or hardware activity.

## Baseline

| Field | Value |
| --- | --- |
| Plan SHA-256 | `c261496ed647a20b557d3bc64deae05029277554c4233d92741cd9dcd273d399` |
| Base revision | `c105a72666c054bbaef604cba69a7d2c6cf84eb9` |
| Base tree inventory SHA-256 | `e5d25aaf3deab375c04f32b25bd9fd01a70363417a46b25730ad050bb38c4a75` |
| Base tracked files | `834` |
| Reconstruction branch | `reconstruct/bridge-only-runtime` |
| Reconstruction worktree | `hyperflux-next-bridge-only-runtime` |
| Original branch preserved | `fix/disconnected-driver-activation` at `65e212212df0dc2b35bd3daee93181ecb5e0fb72` |

Other worktrees present at baseline capture:

| Worktree | Branch | Revision |
| --- | --- | --- |
| `hyperflux-next-unified` | `feat/supervised-hardware-qualification` | `9e1c7b6724302b3ef8742c32e0a68bb43a2e9499` |
| `hyperflux-next-cable-route-toolchain` | `fix/qualification-access-diagnostics` | `307fdf0cab5a4a1c1ffe5a1e445679d4e24805d2` |
| `hyperflux-next-native-tunnel-parity` | `fix/disconnected-driver-activation` | `65e212212df0dc2b35bd3daee93181ecb5e0fb72` |

### Baseline authority hashes

| Authority | SHA-256 |
| --- | --- |
| `integrations/catalog.json` | `e0cc279a0e6a2977db3bfe82c2617728f1aa91f201680cc803b9c5dbc2c5ce55` |
| `architecture/repository-atlas.json` | `f12cf351aebacca62d5c33d4b9158bae72e8356476636174466fb75e4847c36f` |
| `assurance/design-coverage.json` | `92e0469e4a75cdcefa5873085ea98cf8e6dad5f470cf61d9df30d46ecc4f4e46` |
| `verification/tests.json` | `f5ee42f945325d2d9ce783fcdae0efe7e6b8cb930caeba70359197e10c037725` |
| `packaging/install.json` | `f863b8fc9b05541fa4d43461518a467a647cb1e3bf02baae5013bf4a077d6075` |
| `packaging/distributions.json` | `36cf23c9db6e8ab2285ce16c78b4a7ac6e97e20e8a39546ec30d4166536d67fd` |
| `runtime/linux.json` | `6f2f7cba6c66679f0f3b6093c15f13a73a449a7f612040304c29cc132b79a723` |
| `integrations/native-route/source-validated.json` | `9b3a5452bbc9e6b88b86748d9766dd9b5619584dab699ea6c65482e06cbe294e` |
| `profiles/candidates/razer-hyperflux-v2-2026-07-21.json` | `8da21f02808e2cd3c33f447535b6329c50ec5d8ae107b98b98483f590e59f197` |

### Baseline verification

`./hfx verify --fast --output build/reconstruction-baseline-fast` produced run
`hfxv-3dd36bb277c2f93bbe30`. Fifteen nodes passed. The run then failed at
`device-knowledge-contracts` because the isolated worktree had no prepared
pinned OpenRazer checkout; dependent nodes were blocked rather than executed.
The result is retained under `build/reconstruction-baseline-fast` and is not
treated as a source failure or silently rerun.

## Safety Boundary

- No live package, service, module, device, display, KWin, or GitHub state was
  queried or changed during baseline capture.
- No command in this ledger authorizes a later live action.
- The reconstruction stops before the first live or physical gate.

## Gate Status

| Gate | State | Evidence |
| --- | --- | --- |
| A - source and scope | Passed | Takeover removal, deterministic generation, focused tests, and verifier evidence below |
| B - core behavior | Passed | Selective bridge, doctor, restoration, registry, relay, preflight, and explicit activation ports below |
| C - stock OpenRazer and Polychromatic | Pending | Isolated compatibility matrix pending |
| D - packaged OpenRGB plugin | In progress | Qualification harness passed against built plugin; exact split-package DSO rerun pending |
| E - reproducible split artifacts | In progress | Split-package source contract passed; two clean artifact builds pending |
| F - ownership and collision audit | Pending | Package artifacts pending |
| G - Arch transaction simulation | Pending | Split artifacts pending |

## Gate A - Source And Scope

The private OpenRazer compatibility provider and custom Polychromatic backend,
state transport, and patch are absent from canonical integration authority,
generated projections, packaging, tests, and documentation. Remaining old-name
matches are limited to the frozen migration inventory and explicit negative
assertions.

- Two consecutive `./hfx generate` runs produced the same diff digest.
- `git diff --check` passed.
- The focused authority suite passed 68 tests.
- Pinned upstream preparation was rerun with
  `network_access_executed=false` after the three exact source checkouts were
  locally available.
- Verifier run `hfxv-2607f0c72f0232017632` passed every selected node except
  the native-onboarding runner, which hit its stale 60-second process budget.
  Its exact isolated rerun passed all 20 tests in 58.742 seconds. The declared
  expectation is now 60 seconds with a 120-second hard bound; no test behavior
  or assertion was weakened.
- Warm-cache verifier run `hfxv-15d56c9fa295de69e904` then passed all 26
  selected nodes. Its `result.json` SHA-256 is
  `e24389298a0153bd83e04148cda1fb3c93b8816fcf3e2a8b171af8266f41e8a1`
  and its `evidence.json` SHA-256 is
  `4da4dc9ff926f80189ef36cb5206ab177ffb96d79c2d41c2f5be028c69a1cc29`.

The bridge at `c105a72` compiles and tests after both application takeovers are
removed. A restart from `4adf8ff` is therefore unnecessary.

## Gate B - Twelve-Device Authority Milestone

Selective changes from `a40e136` establish the official twelve-device release
inventory without granting authority from application names or hardware
assurance. The generated catalog contains ten operational routes and two
explicitly backend-required routes. Exact source-derived command policies,
device kind, product identity, report family, and direct-USB serial precedence
remain fail-closed; unrelated Razer identities do not enter the bridge.

Rejected hunks include the managed OpenRazer application session, global user
runtime lifecycle changes, and custom Polychromatic assertions. Two narrow
dependencies from `0d39993` were accepted: unsigned clamp-helper recognition in
the pinned-source parser, and the native OpenRGB application's use of the same
optional UID/user peer contract as the already-reviewed desktop socket port.

Focused offline evidence:

- 85 Python profile, integration, source-universality, kernel, and direct-route
  tests passed against pinned OpenRazer/OpenRGB checkouts.
- Affected Rust package suites passed, including 19 bridge tests, 14
  integration-model tests, 44 UHID relay tests, and the core/simulator suites.
- Kernel profile, UAPI, and feature-protocol smoke binaries compiled with
  warnings-as-errors and passed.
- The stock OpenRGB adapter build passed 24 CTest contracts; the prepared
  native-route build passed 32. Its private transform result SHA-256 is
  `1e5711d7fa3cba5770a252f467932eff654d889bf8efb568ae4de73db9a3720f`
  and records no network access, hardware query, or hardware write.
- A second generation pass was byte-identical for the source-validated catalog
  (`73cd838cb8c838bcee8b684f7fba29d9fea06d7fc4d29a7131c9289a0b726be5`),
  native command catalog
  (`ce7fc9b43ade572b59cb7f3577fd62a517faf3d86056a461ec0fcd1a43bbb435`),
  generated profile catalog
  (`1f10e1c51547a76954ab99ebd0dc00fee719b2bfbc02f9f51b62498c2942e607`),
  and supported-hardware projection
  (`b5e6250526245f3b2594dc1ef48a0a16e5808b36f0fecae588bb998c75a2fb36`).

### Read-Only Doctor Milestone

Selective changes from `cdd46ca`, `fbeac4e`, and `31bb0ed` preserve Doctor's
read-only behavior without carrying their managed OpenRazer-session premise.
The bridge configuration directory is world-traverse-only (`2751`) while the
configuration file remains public-read-only (`0644`); directory listing and
writes remain forbidden to ordinary users.

For an ordinary desktop user with stock OpenRazer installed, Doctor now uses
one bounded typed `org.freedesktop.DBus.NameHasOwner("org.razer")` query. It
does not enumerate the bus, start a service, or own the daemon lifecycle.
Malformed, failed, timed-out, and oversized command output fails closed. A root
Doctor marks desktop OpenRazer access not applicable rather than claiming a
user-session owner. The complete `hfx-ops` suite passed 102 tests, and the
error/runtime Python suites passed 15 tests.

### Restoration Burst-Suppression Milestone

The complete bridge-owned change from `9abfe0b` moves stable-intent/profile
compatibility ahead of durable restore-claim creation. Known receiver or child
profile incompatibility now yields `NoCompatibleStableIntents`; temporarily
unavailable profile authority remains a separate bounded-retry outcome. A
known-incompatible device-return epoch is consumed without a claim, dispatch,
event, or write while its generation-bound sequence watermark remains in place
to reject stale replay evidence.

The source commit contains no activation, package, desktop-session, or
application-lifecycle hunk. Its core, scheduler, and file-persistence tests were
therefore ported together as one bridge-owned behavior. Focused offline proof:

- all 39 core restoration integration tests passed, including the exact
  twelve-attempt stale receiver-digest regression;
- all 33 daemon restoration/service tests passed, including bounded epoch
  consumption and unavailable-authority retry preservation;
- all 14 file-persistence tests passed, including byte-identical durable state
  across both twelve-attempt endpoint envelopes;
- the complete `hfx-core`, `hfx-daemon`, and `hfx-bridge` package test suites
  passed after the focused selectors;
- formatting and `git diff --check` passed.

### OpenRGB Client-Uniqueness Milestone

Selective runtime changes from `c7c3a47` replace the process-wide fixed
OpenRGB client names with fresh 256-bit OS-entropy identities scoped by role.
Discovery receives its own identity and every published feature controller
receives a distinct identity, so two routes, a generation replacement, plugin
reload, or a post-fork child cannot collide in the bridge client registry.
Entropy or factory failure remains fail-closed. The one-line test correction
from `91af5d2` binds the runtime contract to the allocator's actual 64-hex nonce
rather than the stale pre-commit counter format.

The commit's packaged SDK harness, qmake harness, and large qualification runner
were deliberately not ported here; they remain part of the later plugin-only
Gate D review. No stock OpenRGB executable, resource, configuration, or lifecycle
is owned by this milestone.

Focused offline evidence:

- all seven identity/session/worker/registry/coordinator/plugin contracts passed;
- the native-route configuration built the plugin DSO and passed all 33 CTest
  contracts;
- the stock-adapter configuration independently built the plugin DSO and passed
  all 25 CTest contracts;
- both builds used warnings-as-errors and the pinned OpenRGB checkout; no
  installed or live application was invoked.

### Relay Failure-Bounds Milestone

Selective changes from `ae966c9` make the root/UHID relay a one-start service:
`Restart=no`, no `RestartSec`, no unlimited start window, and an executable
condition for the stock `/usr/bin/openrazer-daemon`. The bridge only wants the
relay; it does not require it, while the relay remains `Requires`/`PartOf` the
bridge. OpenRazer driver state no longer blocks the independent bridge startup
path, and package compatibility reporting no longer performs `try-restart` or
any other relay mutation. Dropping a live relay endpoint now has a direct test
that proves one UHID destroy follows its one create.

The source commit's module-inspecting `ExecCondition` was explicitly rejected:
the relay remains sealed with `ProtectKernelModules=yes`, so that condition is
both architecturally misplaced and known to fail deterministically. The final
metadata-only preflight will be ported as a separate least-privilege oneshot
from `65e2122`. Managed OpenRazer session changes and the synthetic failure
qualification framework were also rejected. The current report-only package
hook is transitional and will be removed with the split-package safe-registration
hook in the packaging milestone.

Offline evidence:

- generation was byte-identical across two passes (diff digest
  `4069cb9b96ab2f5c1a7117e53de0a267d07d849892ecf28d76a00189a878555f`);
- deployment authority SHA-256 is
  `15a8d0fcf42c6966e047beac836fcdee646f269322b505413e962df096aca3dc`;
- generated relay unit SHA-256 is
  `92274cd5e836e65555b02f42c991694c6278d4ab7149274e43e07d3ac6ef6dfb`;
- `systemd-analyze --man=no verify` passed for bridge, relay, and both desktop
  proxy units; offline relay exposure was `1.9 OK`;
- all 101 `hfx-ops` tests, 45 relay tests, and 48 broader Python contracts
  passed (six source-checkout-dependent tests skipped as declared).

### Packaged OpenRGB Qualification Milestone

Selective verification changes from `91af5d2` through `9f5863c` add a pinned
stock-OpenRGB `PluginManager`/SDK harness without packaging or patching the
OpenRGB application. The harness loads only the supplied HyperFlux plugin in a
private configuration root and exercises publication through a synthetic native
bridge. Its outer runner creates separate user, mount, network, and PID
namespaces, presents an empty tmpfs `/proc`, hides real hidraw/UHID and system
plugin paths, drains bounded diagnostics, and fails closed on protocol,
teardown, overflow, or forced-cleanup errors.

The system-socket and UID-1000 desktop-socket runs each passed four cases:
positive exact-set convergence, rejection of the historical constant client
identity, missing ownership leases, and missing native receiver tunnel. The
current smoke proof used the previously built bridge-only plugin; the split
package gate will rerun the same authority against the staged package DSO.

Commit `5d8e274` contributes only the exact `mbedtls=3.6.5-1` build-environment
pin required by the stock SDK harness. Commit `8a26db3` contributes only
verification-image reproducibility: no implicit image pull, immutable image-ID
binding, exact source-declared package inventory, bounded inspection output,
and unconditional temporary-container cleanup. The ordinary-user lifecycle
model and relay-failure qualification mixed into this source range remain
rejected.

Offline evidence:

- 42 focused governance, development-environment, and qualification-runner
  tests passed;
- the pinned OpenRGB `6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0`
  PluginManager harness compiled successfully with SHA-256
  `cc5cb48f30dbd9b8ffeec39a1991dae8fa2193378c31eae9a37f869bac46726c`;
- the system qualification evidence SHA-256 is
  `c4c88c56c5943e11b329c7c87314141d42636018f341b2d58786db51a6d2f87e`;
- the UID-1000 qualification evidence SHA-256 is
  `5b4fee599e9a66f8ec7971ea9e4f42eaa5a8e5e2c71c759f255727e037e01164`;
- both evidence records bind plugin SHA-256
  `7186189dbde469a6596eef7e5e2306e1f9869cf32616bbbfc855cf1cabfee09d`
  and report all four cases passed;
- generated development and verification projections are current and
  `git diff --check` passes.

### Explicit Driver Activation And Metadata-Preflight Milestone

Selective changes from `3aa5a39` add one explicit root-only core-driver
activation command. With every supported receiver disconnected, it settles
udev, proves receiver absence, bounds module references and bindings, replaces
only `hid_hyperflux_next`, and requires stable matching installed/loaded source
identities before reporting success. It does not own bridge stop/start,
update-intent, package, or confirmation state. The operator remediation names
this command only for the core module; stale OpenRazer route modules remain a
reboot boundary.

Selective changes from `65e2122` move OpenRazer module inspection out of the
sealed relay into a stateless metadata-only oneshot. The preflight uses
`DynamicUser=yes`, denies every socket family and device, hides the bridge
runtime, exposes `/usr/lib/modules` read-only, has no capabilities, and blocks
module syscalls. Its checker invokes exactly six bounded `modinfo` reads plus
read-only loaded-module `srcversion` files; it does not call systemd, D-Bus,
the bridge, USB inventory, or hardware. The relay retains
`ProtectKernelModules=yes`, no module paths or checks, `Restart=no`, and its
UHID-only boundary.

Relay startup now retries only an immediately returned
`SocketUnavailable` result for 25 attempts separated by 100 milliseconds.
Successful streams retain the normal three-second production I/O timeout;
configuration, SDK negotiation, identity, route, and protocol failures remain
single-attempt errors, and termination exits cleanly. Managed OpenRazer
sessions, package activation hooks, synthetic dependency-failure evidence, and
the old activation/confirmation lifecycle were explicitly rejected.

Focused offline evidence:

- all 112 `hfx-ops` library tests and four activation-binary tests passed;
- all 49 relay-library, two qualifier, and four relay-CLI tests passed;
- 54 integration, install, Linux-runtime, and testgraph tests passed, with six
  explicitly source-checkout-dependent cases skipped;
- the native-route verifier rebuilt all four transformed OpenRazer modules
  against Linux `6.18.37-1-lts` and passed the exact install/unit contracts;
- `systemd-analyze --man=no verify` passed for bridge, preflight, relay, and
  both desktop proxy units; offline exposure was `1.6 OK` for preflight and
  `1.9 OK` for relay;
- deployment authority SHA-256 is
  `b744f6748cb76f325c8e6c55fb8363418e979aa9079bb324f97e2d8ba2805ce2`;
- generated preflight SHA-256 is
  `833ecc773610c0e0acf61b02c13fb084e6e894a850e23ac1070324b1d3da4ad3`;
- generated relay SHA-256 is
  `d5cd8e3a19349b43825c87fb89a724f819c474ba7c95b83218033dfa10609bec`;
- generation, Rust formatting, and `git diff --check` are current.

## Bridge-Only Lifecycle And Split-Package Source Milestone

The package lifecycle now performs registration only: sysusers and tmpfiles
creation, a system-manager reload, and a udev-rules reload. It has no pre-update
hook and never starts, stops, restarts, enables, disables, masks, or unmasks a
service; it performs no module operation, hardware probe, device trigger, or
application-session write. The activation binary exposes only the explicit
receiver-disconnected core-driver operation and the read-only OpenRazer module
metadata check. Activation, confirmation, and update-intent orchestration are
absent from the runtime authority, units, payload, and operations API.

The component-aware install and distribution authorities now describe one Arch
`pkgbase=hyperflux-next` with three `0.0.0_dev.1-2` packages:

- `hyperflux-next-linux` owns the bridge, CLI, explicit activation utility,
  relay, patched OpenRazer DKMS source, HyperFlux DKMS source, units, rules,
  configuration, license, and minimal troubleshooting document;
- `hyperflux-next-openrgb` owns only the plugin DSO and its license;
- `hyperflux-next-full` owns no payload and depends on the exact sibling
  packages plus stock OpenRazer daemon/client and Polychromatic packages.

The core replaces only `openrazer-driver-dkms`, and no component has a
provide/conflict/replace relationship with Polychromatic, OpenRGB,
`openrazer-daemon`, or `python-openrazer`. Debian and RPM are intentionally
inactive for this reconstruction. Python SDK, public headers, schemas,
qualification console, assurance bundles, compiled catalogs, and broad design
documentation remain development-source material rather than runtime payload.
Final component stages are compared after source-tree expansion and fail closed
if any sibling payload file overlaps.

The shipped default configuration has restoration disabled. The static service
graph is bridge-Wants-relay, relay-Requires/After-bridge-and-preflight, and an
explicit per-UID desktop socket/proxy; no global user-runtime drop-in or managed
OpenRazer session is present.

The installed operations CLI is now limited to `status`, read-only `doctor`,
and bounded support-bundle output. Runtime dispatch for qualification,
native-device observation, configuration repair, and service restart is absent;
those verification facilities remain source-side only. The CLI contract tests
also prove each removed command is rejected before a system probe is
constructed.

An ELF/package-owner audit of the first split-package inspection build found
that Arch's `libgcc` package owns the Rust binaries' direct `libgcc_s.so.1`
dependency, while the plugin additionally links `libstdc++`, `qt5-base`, and
`hidapi`. The canonical component metadata names those direct package owners
rather than the broader `gcc-libs` umbrella. The inspected package set from
commit `3efddeb` is therefore retained only as pre-candidate evidence and will
not be promoted or reused as the final candidate.

The same archive-level audit found no split-payload file collision with stock
Polychromatic, OpenRGB, OpenRazer daemon, or Python client packages. The only
non-directory overlap is the declared OpenRazer DKMS-driver replacement. For
comparison, the retired monolith overlaps 319 stock Polychromatic files: 314
are byte-identical and five are modified. The final transaction gate must prove
that upgrading the same core package removes those takeover files before stock
Polychromatic becomes their sole owner.

Offline source evidence before the immutable package-build commit:

- two consecutive generation states had identical diff SHA-256
  `3419d5367a43968603718bf8ed00e92ffde70ee0c067539f8fae2ffc0cc1b4a4`;
- 76 focused distribution, install, staging, runtime, verifier, and governance
  tests passed after the final sibling-overlap check;
- the full Python discovery run passed 344 tests with seven declared skips;
- all 107 `hfx-ops` library tests and four activation-binary tests passed;
- all 49 relay-library, two qualifier, and four relay-CLI tests passed;
- `cargo fmt --all -- --check`, `git diff --check`, and
  `systemd-analyze --man=no verify` for all five runtime units passed;
- install authority SHA-256 is
  `50b98214d15a55b119b3edc0c3e0969c49a9f23faf92d5691435982c85a57f54`;
- distribution authority SHA-256 after the direct ELF-owner correction is
  `24a76d0f30d76bf5d492abdade7efd920518f405d989af594621b00af5288818`;
- Linux runtime authority SHA-256 is
  `0a7f02e0495addbc6071efe2da07c20a5444e84eef8be0fe091304c393ecad96`;
- packaged default configuration SHA-256 is
  `b562bbb73af82371938c84df040e706a21f0041701d522c131e23c8ad03dd299`.

## Stock-Application, Ownership, And Transaction Gate Harnesses

The release gates now have three bounded, source-controlled qualifiers. The
stock-application qualifier extracts exact OpenRazer daemon/client and stock
Polychromatic archives into a temporary root, runs the public `org.razer` API
on a private D-Bus session against OpenRazer's own test sysfs, and proves the
two assurance fixtures enumerate, accept their supported public controls, and
survive a `2 -> 0 -> 2` reconnect without any private HyperFlux client. The
same qualifier is parameterized for both the distribution `3.12.2` and local
`3.12.4` userspace archives.

The ownership qualifier reads `.PKGINFO` and `.MTREE` directly from every
candidate and stock archive. It enforces the three component relationships,
zero sibling overlap, plugin-only OpenRGB payload, zero application-package
collision, the exact three-file OpenRazer driver replacement, and the retired
monolith's independently observed 319-file Polychromatic takeover.

The Arch transaction qualifier runs only in the pinned rootless Podman image
with networking disabled. It copies `/var/lib/pacman/local` read-only into a
private pacman database, extracts the matching old monolith only in the
disposable overlay, and then executes the real monolith-to-split transaction.
Its package-script shims admit only sysusers, tmpfiles, daemon reload, and udev
rules reload; standard DKMS hooks remain real. It proves stock Polychromatic
becomes sole owner of the takeover paths, obsolete private runtime files are
removed, the modified live-style restoration setting is preserved while the
disabled packaged default becomes `.pacnew`, and the old monolith rollback can
be resolved without executing its retired activation scriptlet. Container and
inner diagnostics are time-, file-, and aggregate-size bounded.

Pre-candidate checks:

- stock OpenRazer `3.12.2` plus Polychromatic `0.9.7` passed against the first
  inspection driver archive;
- stock OpenRazer `3.12.4` plus Polychromatic `0.9.7` passed against the same
  inspection driver archive;
- the ownership qualifier rejected the first inspection packages because
  their direct dependency metadata predated the ELF-owner correction;
- four inert qualifier-contract tests, Python compilation, `bash -n`,
  ShellCheck warning level, and `git diff --check` pass.

These pre-candidate runs validate the harnesses only. Gates C, F, and G remain
open until the immutable final source commit is built and the qualifiers bind
the resulting package and driver bytes.
