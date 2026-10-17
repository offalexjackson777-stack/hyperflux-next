# Documentation Render Evidence

**Status:** Accepted

## Context

Every document is rendered in a pinned Chromium at three widths and in light
and dark themes. The render fails on any layout issue or outbound network
request. Its result, the screenshot manifest, and the visual review of the
critical screenshots are committed as
`assurance/documentation-audit.json`.

That record used to be bound to the exact documents of one commit, and tests
pinned the commit it was made from. Any merged change to a rendered document
made every other open pull request stale, even when that pull request changed
no documentation. Refreshing it took a long local render and a hand-edited
record.

## Decision

The committed record describes one release, not every commit.

- Every pull request checks the record's structure: its format, its binding to
  the screenshot manifest, a visual review that matches the manifest, and the
  publication interlock. These checks need no browser.
- Release pull requests into `main` also check that the record describes the
  documents being released. CI sets `HFX_RELEASE_EVIDENCE=required` for them,
  and `./hfx verify --release-evidence required` does the same locally.
- The **Documentation render** check renders every document offline in the CI
  image whenever a pull request changes a document or a render input. It fails
  on any issue and uploads the screenshots for review. Layout and network
  problems are still caught on the pull request that causes them.
- `./hfx docs bind` writes the record from a checked render. It keeps the
  existing visual review.
- `./hfx docs review` lists the critical screenshots that changed. It records a
  review only for the files named with `--confirm` after someone has looked at
  them, and never marks a screenshot reviewed by itself.

The CI image pins Node.js, Chromium, and the Noto fonts. The npm packages are
locked in `toolchains/documentation-render`. They are installed with install
scripts disabled, and the render runs with no network.

## Consequences

- Merging one pull request no longer makes the others stale.
- Each release still ships a record that matches its documents exactly, with a
  person's review of every changed critical screenshot.
- The CI image is larger, because it carries Chromium and Node.js.
- Between releases, the committed screenshots may lag the documents. The
  **Documentation render** artifact holds the current screenshots.

## Scope

This decision covers when the render record must match the tree and how it is
refreshed. It does not change what the render checks or which documents are
critical.

## Alternatives Rejected

- Re-rendering and committing evidence on every pull request keeps the stale
  pull request problem and adds a long render to each change.
- Dropping the committed record would leave a release with no reviewed record
  of how its documents looked.
