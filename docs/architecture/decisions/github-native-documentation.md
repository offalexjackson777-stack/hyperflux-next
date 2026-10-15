# GitHub-Native Documentation And Local Qualification

**Status:** Accepted

## Context

A second documentation site would duplicate repository guidance and still could
not inspect a visitor's installed driver or hardware. Public documentation and
local qualification have different trust boundaries.

## Decision

GitHub Markdown is the public documentation surface. The root README routes each
audience to concise reviewed guides, generated repository-relative references,
issue forms, support, and security reporting.

Hardware inventory and qualification run only in the installed loopback console.
That console reads the local HyperFlux Next bridge, performs no cloud upload,
does not use a remote device database, and never substitutes sample hardware for
the machine's actual state.

Generated technical views stay in the repository and identify their canonical
sources. A future site requires a distinct user purpose and may not bypass the
installed local authority.

## Consequences

- Visitors get one documentation graph with stable repository-relative links.
- Hardware testers use the package-matched local console instead of a public
  website that cannot access the driver safely.
- Generated Markdown remains reproducible from canonical repository data.
- Documentation changes do not authorize a release or hardware write.

## Alternatives Rejected

- A duplicate Pages portal would split navigation without adding local-device
  capability.
- Browser-side hardware access would cross the installed trust boundary.
