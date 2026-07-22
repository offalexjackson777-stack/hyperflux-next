# GitHub-Native Documentation And Local Qualification

**Status:** Accepted

## Context

The imported Design Book proposes a GitHub Pages documentation portal. The implemented portal duplicated repository documentation, split navigation across two surfaces, and could not safely inspect a visitor's installed driver or hardware. It made source history and live qualification look like one product even though they have different trust boundaries.

## Decision

GitHub Markdown is the public documentation surface. The root README routes each audience to concise reviewed guides, generated repository-relative references, issue forms, support, and security reporting.

Hardware inventory and qualification run only in the installed loopback console. That console reads the local HyperFlux Next bridge, performs no cloud upload, does not use a remote device database, and never substitutes sample hardware for the machine's actual state.

GitHub Pages remains disabled. Generated technical views stay in the repository and identify their canonical sources. A future deployed site requires a separate reviewed decision with a unique purpose; it may not duplicate GitHub documentation or bypass the installed local authority.

## Consequences

- Visitors get one documentation graph with stable repository-relative links.
- Hardware testers use the package-matched local console instead of a public website that cannot access the driver safely.
- Generated Markdown remains reproducible from canonical repository data.
- Removing the portal does not authorize a release, package, tag, or hardware write.
