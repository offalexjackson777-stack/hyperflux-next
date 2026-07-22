# Device-knowledge integration decision

Status: accepted for local reconstruction

Reviewed destination: `78bd60106f9ce026d21895f2c1baefefb3519a5d`

Reviewed donor: `665f33bb2868358a34bf1940fca170d59f54fff4`

Donor comparison base: `8a1a32c`

## Decision

HyperFlux Next will port the donor's reviewed device facts, semantic capability
rules, candidate links, bounded metadata extraction behavior and presentation
ideas into Main's existing profile, integration, generation, portal and
verification authorities. The donor commits will not be cherry-picked and its
generated files will not be copied into Main.

One fact has one owner:

- reviewed product facts and explicit gaps live under `knowledge/`;
- dated candidate membership lives under `profiles/candidates/`;
- immutable upstream revisions remain owned by `integrations/catalog.json`;
- normalized upstream catalogs are reproducible generated imports;
- profile identity and public physical evidence remain the only source of
  receiver write authority;
- Device Lab and Repository Atlas are generated, read-only views of those
  authorities.

The 2026-07-13 candidate snapshot remains immutable historical evidence. The
2026-07-21 snapshot is added as a distinct successor and is selected explicitly
by the knowledge-link authority. Repeated candidate IDs across dated snapshots
do not merge or rewrite history.

## Safety boundary

An official model name, an OpenRazer method, an OpenRGB registry entry or a
candidate-list match can describe a device but cannot authorize a receiver
write. Unknown, conflicting and candidate-only records remain visible and
non-writable. Integrations continue to use the typed SDK; receiver transport
remains exclusively in the kernel and bridge.

## Donor classification

Every path in the 54-file donor delta is classified below. "Adapt" means port
the behavior or information through the named Main authority; it never means
copy the donor file blindly.

| # | Donor path | Class | Integration decision |
| -: | --- | --- | --- |
| 1 | `README.md` | conflict | Rewrite from current Main after the portal and support boundary are generated. |
| 2 | `applications/control-center/README.md` | UI | Use as Device Lab interaction input; do not retain a standalone application. |
| 3 | `applications/control-center/app.js` | UI | Rework into generated portal behavior. |
| 4 | `applications/control-center/control-center.json` | UI | Replace with generated Device Lab data from the knowledge compiler. |
| 5 | `applications/control-center/index.html` | UI | Replace with the portal shell and `/devices/` route. |
| 6 | `applications/control-center/knowledge.css` | UI | Re-express with the portal visual tokens; do not copy the theme. |
| 7 | `applications/control-center/lib/catalog.js` | UI | Replace runtime catalog assumptions with embedded generated data. |
| 8 | `applications/control-center/lib/dom.js` | UI | Port only bounded accessible DOM helpers that the portal needs. |
| 9 | `applications/control-center/styles.css` | UI | Do not create a second CSS system. |
| 10 | `applications/control-center/views/capabilities.js` | UI | Adapt as the generated capability heatmap. |
| 11 | `applications/control-center/views/evidence.js` | UI | Adapt as provenance and evidence views. |
| 12 | `applications/control-center/views/overview.js` | UI | Adapt as Device Lab inventory and comparison views. |
| 13 | `applications/control-center/views/sidebar.js` | UI | Integrate into portal navigation and filters. |
| 14 | `crates/hfx-profiles/src/generated.rs` | generated output | Discard donor copy and regenerate with Main's profile compiler. |
| 15 | `docs/architecture/device-knowledge.md` | canonical source | Adapt the architecture explanation to Main's current boundaries. |
| 16 | `docs/generated/device-knowledge.md` | generated output | Regenerate from compiled knowledge. |
| 17 | `docs/generated/supported-hardware.md` | generated output | Regenerate from current profiles and knowledge. |
| 18 | `driver/kernel/generated/hyperflux_receiver_profiles.inc` | generated output | Regenerate; knowledge must not alter kernel authority. |
| 19 | `generated/knowledge/catalog.json` | generated output | Regenerate from reviewed facts, links, rules and pinned imports. |
| 20 | `generated/profiles/catalog.json` | generated output | Regenerate with Main's profile compiler. |
| 21 | `knowledge/candidate-links.json` | canonical source | Port as explicit reviewed links to one selected dated snapshot. |
| 22 | `knowledge/capability-map.json` | canonical source | Port as application-neutral semantic mapping rules. |
| 23 | `knowledge/reviewed-facts.json` | canonical source | Port as the reviewed product-fact and explicit-gap authority. |
| 24 | `knowledge/upstreams/openrazer.json` | generated output | Reproduce with the pinned, non-executing Main importer. |
| 25 | `knowledge/upstreams/openrgb.json` | generated output | Reproduce with the pinned, non-executing Main importer. |
| 26 | `migration/sources.json` | conflict | Extend Main's source registry and bind the reviewed-facts digest. |
| 27 | `profiles/candidates/razer-hyperflux-v2-2026-07-21.json` | conflict | Add beside, not instead of, the 2026-07-13 historical snapshot. |
| 28 | `profiles/evidence/claims.json` | conflict | Preserve the old claim and add a distinct July 21 claim. |
| 29 | `schemas/control-center-app.schema.json` | obsolete duplicate | Replace with Device Lab fields in the existing portal authority. |
| 30 | `schemas/device-capability-map.schema.json` | canonical source | Port and tighten for deterministic semantic rules. |
| 31 | `schemas/device-knowledge-links.schema.json` | canonical source | Port and require exact snapshot coverage. |
| 32 | `schemas/reviewed-device-facts.schema.json` | canonical source | Port with bounded source, fact and gap vocabularies. |
| 33 | `schemas/upstream-device-catalog.schema.json` | canonical source | Port with provenance, digest and record bounds. |
| 34 | `sdk/cpp/include/hyperflux/generated/profile_catalog.hpp` | generated output | Regenerate. |
| 35 | `sdk/python/hyperflux_sdk/generated/profile_catalog.py` | generated output | Regenerate. |
| 36 | `tests/fixtures/generated/profile-compositions.json` | generated output | Regenerate. |
| 37 | `tests/fixtures/upstreams/openrazer/daemon/openrazer_daemon/hardware/keyboards.py` | test | Port as a bounded non-executing parser fixture. |
| 38 | `tests/fixtures/upstreams/openrazer/daemon/openrazer_daemon/hardware/mouse.py` | test | Port as a bounded non-executing parser fixture. |
| 39 | `tests/fixtures/upstreams/openrgb/Controllers/RazerController/RazerControllerDetect.cpp` | test | Port as a bounded parser fixture. |
| 40 | `tests/fixtures/upstreams/openrgb/Controllers/RazerController/RazerDevices.cpp` | test | Port as a bounded parser fixture. |
| 41 | `tests/fixtures/upstreams/openrgb/Controllers/RazerController/RazerDevices.h` | test | Port as a bounded parser fixture. |
| 42 | `tests/test_control_center.py` | test | Replace with Device Lab portal, accessibility and offline tests. |
| 43 | `tests/test_device_knowledge.py` | test | Adapt to Main's profile and upstream authorities. |
| 44 | `tests/test_foundation.py` | conflict | Extend current foundation tests without replacing newer coverage. |
| 45 | `tests/test_profiles.py` | conflict | Add snapshot-supersession checks to current profile tests. |
| 46 | `tools/hfxdev/cli.py` | conflict | Extend current CLI commands and preserve every newer command. |
| 47 | `tools/hfxdev/importers/__init__.py` | obsolete duplicate | Do not create a competing upstream-management package. |
| 48 | `tools/hfxdev/importers/openrazer.py` | obsolete duplicate | Port full-catalog behavior into Main's `openrazer.py`. |
| 49 | `tools/hfxdev/importers/openrgb.py` | conflict | Add a bounded parser beside Main's upstream authority, not a second manager. |
| 50 | `tools/hfxdev/knowledge.py` | canonical source | Adapt as the knowledge compiler using Main loaders and generators. |
| 51 | `tools/hfxdev/knowledge_markdown.py` | canonical source | Adapt as a deterministic documentation renderer. |
| 52 | `tools/hfxdev/knowledge_review.py` | canonical source | Port reviewed-fact validation and assurance layering. |
| 53 | `tools/hfxdev/render.py` | conflict | Register knowledge outputs in Main's current render graph. |
| 54 | `verification/tests.json` | conflict | Add focused nodes and change impact to Main's current verification graph. |

Classification totals: 11 canonical sources, 11 generated outputs, 12 UI
inputs, 7 tests or fixtures, 3 obsolete duplicates and 10 Main conflicts.

## Consequences

- Upstream catalog refresh is deterministic and bound to exact integration
  commits, source paths, digests and licenses.
- Device research is useful before physical qualification without becoming a
  write-authority shortcut.
- Portal views can evolve independently from the knowledge model because they
  consume one compiled catalog.
- Updating a candidate snapshot preserves previous evidence and requires an
  explicit link-selection change.
- Generated language bindings, kernel tables, docs and fixtures continue to be
  refreshed by Main's single render graph.
