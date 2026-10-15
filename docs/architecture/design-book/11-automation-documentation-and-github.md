# Design Book Chapter XI: Automation, Documentation And GitHub

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 58. Change-Aware CI

CI derives affected domains from a maintained dependency graph. Schema changes
select generated bindings and adapter contracts; kernel changes select builds,
UAPI compatibility, package, and lifecycle checks; documentation changes select
generation, links, privacy, and rendering checks. Required checks remain
explicit.

## 59. Dependency Updates

Renovate or Dependabot can group related updates and open reviewable pull
requests. Each update reports API compatibility, schema changes, license
changes, vulnerabilities, binary-size movement, performance impact, and selected
tests. Automatic merging is reserved for narrowly defined low-risk updates with
complete green evidence. Upstream OpenRGB or OpenRazer metadata changes generate
a proposed diff rather than silently entering production.

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
