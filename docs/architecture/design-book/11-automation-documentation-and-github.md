# Design Book Chapter XI: Automation, Documentation And GitHub

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 58. Change-Aware CI

CI derives affected domains from a maintained dependency graph. Schema changes
select generated bindings and adapter contracts; kernel changes select builds,
UAPI compatibility, package, and lifecycle checks; documentation changes select
generation, links, privacy, and rendering checks. Required checks remain
explicit.

## 59. Dependency Updates

Dependabot opens weekly grouped pull requests for GitHub Actions, Rust crates,
and the Python SDK, and Dependabot security updates are enabled. These pull
requests run the same verification as any other change and are reviewed before
they merge. OpenRGB, OpenRazer, and Polychromatic are pinned to exact upstream
revisions, so their metadata changes only through a reviewed pin update rather
than silently entering production.

## 60. Documentation Portal

GitHub-native Markdown serves users, contributors, and maintainers without
requiring a separate Pages site. The documentation index routes each audience to
reviewed guidance, while generated tables derive from canonical registries so
public facts do not drift.

## 61. GitHub Repository Experience

The repository uses meaningful navigation, concise architecture decisions, issue
forms, a focused pull-request template, CODEOWNERS, protected linear history,
pinned workflows, and private vulnerability reporting. Public status and
compatibility surfaces communicate verified facts rather than internal process.

---

- Previous:
  [Chapter X: Updates, Packaging And Operations](10-updates-packaging-and-operations.md)
- [Design Book index](../design-book.md)
- Next: [Chapter XII: Release Boundaries](12-release-boundaries.md)
