# Repository Documentation And Local Hardware Authority

**Status:** Accepted

## Context

HyperFlux Next documentation has two kinds of reader. Most readers need to
install, use, or change the project. A smaller group qualifies hardware, which
needs facts about one machine: its installed driver, receiver, and paired
devices.

A public website can host the first kind of document, but it cannot see a
visitor's driver or receiver. Hardware facts have to come from the machine
itself.

## Decision

Documentation is Markdown in this repository, read on GitHub. The root README
and the documentation index route each audience to its guides. Generated
reference pages are rebuilt from canonical repository data by
`./hfx generate`, and each names its sources.

Hardware inventory and qualification use a source-only, loopback-only console
that reads the local HyperFlux Next bridge. It does not use a remote device
database, does not upload evidence silently, never uses WebHID, and never opens
USB directly. All hardware access stays in the installed bridge and kernel path.

## Consequences

- Readers have one set of documents with stable repository-relative links.
- Generated reference pages stay reproducible, and a stale page fails
  verification.
- Hardware evidence comes from the machine under test, not from a website.
- A documentation change cannot change what the bridge is allowed to write.

## Scope

This decision covers where documentation lives and where hardware facts come
from. It does not cover device support decisions, which follow the evidence
rules in the [Design Book](../design-book.md).

## Alternatives Rejected

- A separate documentation site would duplicate repository guidance without
  adding access to local hardware.
- Browser-side hardware access would bypass the bridge and kernel boundary.
