# HyperFlux Next Design Book

This book records the architecture shipped by HyperFlux Next 2.0. Each subject
states a current boundary or invariant. Explicitly labeled future work is
non-shipping direction, not a promise of support.

Each chapter is a separate page. Subject numbers are stable, and the
[design coverage ledger](../generated/design-coverage.md) links every subject to
its implementation evidence.

## Chapter I: Product Foundations

- [1. Purpose](design-book/01-product-foundations.md#1-purpose)
- [2. Relationship To Earlier Implementations](design-book/01-product-foundations.md#2-relationship-to-earlier-implementations)
- [3. Knowledge That Must Be Preserved](design-book/01-product-foundations.md#3-knowledge-that-must-be-preserved)
- [4. Complexity That Must Not Be Preserved](design-book/01-product-foundations.md#4-complexity-that-must-not-be-preserved)

## Chapter II: The Architectural Foundation

- [5. Primary Architecture](design-book/02-architectural-foundation.md#5-primary-architecture)
- [6. Kernel Responsibility](design-book/02-architectural-foundation.md#6-kernel-responsibility)
- [7. Bridge Responsibility](design-book/02-architectural-foundation.md#7-bridge-responsibility)
- [8. Strong Domain Types](design-book/02-architectural-foundation.md#8-strong-domain-types)

## Chapter III: Devices, Routes And Truthful State

- [9. Inventory And Routes](design-book/03-devices-routes-and-truthful-state.md#9-inventory-and-routes)
- [10. Presence State Model](design-book/03-devices-routes-and-truthful-state.md#10-presence-state-model)
- [11. Wired And Wireless Duplicate Handling](design-book/03-devices-routes-and-truthful-state.md#11-wired-and-wireless-duplicate-handling)
- [12. Composable Hardware Profiles](design-book/03-devices-routes-and-truthful-state.md#12-composable-hardware-profiles)
- [13. Hard And Cloth Editions](design-book/03-devices-routes-and-truthful-state.md#13-hard-and-cloth-editions)
- [14. Mat And Receiver Presentation](design-book/03-devices-routes-and-truthful-state.md#14-mat-and-receiver-presentation)

## Chapter IV: Universality And Qualification

- [15. Support Levels](design-book/04-universality-and-qualification.md#15-support-levels)
- [16. Unknown Device Behavior](design-book/04-universality-and-qualification.md#16-unknown-device-behavior)
- [17. Declarative Transport Profiles](design-book/04-universality-and-qualification.md#17-declarative-transport-profiles)

## Chapter V: Application Integrations

- [18. OpenRGB Integration](design-book/05-application-integrations.md#18-openrgb-integration)
- [19. OpenRGB Effects](design-book/05-application-integrations.md#19-openrgb-effects)
- [20. Application-Owned Persistence](design-book/05-application-integrations.md#20-application-owned-persistence)
- [21. OpenRazer Native Route](design-book/05-application-integrations.md#21-openrazer-native-route)
- [22. Stock OpenRazer Userspace Compatibility](design-book/05-application-integrations.md#22-stock-openrazer-userspace-compatibility)
- [23. Stock Polychromatic Through OpenRazer](design-book/05-application-integrations.md#23-stock-polychromatic-through-openrazer)
- [24. Upstream Device Settings](design-book/05-application-integrations.md#24-upstream-device-settings)

## Chapter VI: Application Ownership

- [25. Native Control Ownership](design-book/06-application-ownership.md#25-native-control-ownership)
- [26. Ownership User Experience](design-book/06-application-ownership.md#26-ownership-user-experience)
- [27. Atomic Native Exchanges](design-book/06-application-ownership.md#27-atomic-native-exchanges)

## Chapter VII: Schemas, SDKs And Reuse

- [28. Schema-First Development](design-book/07-schemas-sdks-and-reuse.md#28-schema-first-development)
- [29. Generated Code](design-book/07-schemas-sdks-and-reuse.md#29-generated-code)
- [30. Stable HyperFlux SDK](design-book/07-schemas-sdks-and-reuse.md#30-stable-hyperflux-sdk)
- [31. Feature Negotiation](design-book/07-schemas-sdks-and-reuse.md#31-feature-negotiation)
- [32. Interfaces And Dependency Injection](design-book/07-schemas-sdks-and-reuse.md#32-interfaces-and-dependency-injection)
- [33. Explicit State Machines](design-book/07-schemas-sdks-and-reuse.md#33-explicit-state-machines)
- [34. Event Subscriptions](design-book/07-schemas-sdks-and-reuse.md#34-event-subscriptions)
- [35. Bounded Concurrency And Backpressure](design-book/07-schemas-sdks-and-reuse.md#35-bounded-concurrency-and-backpressure)

## Chapter VIII: Simulation And Verification

- [36. Virtual Receiver](design-book/08-simulation-and-verification.md#36-virtual-receiver)
- [37. Deterministic Replay](design-book/08-simulation-and-verification.md#37-deterministic-replay)
- [38. One Verification Entry Point](design-book/08-simulation-and-verification.md#38-one-verification-entry-point)
- [39. Fast Verification](design-book/08-simulation-and-verification.md#39-fast-verification)
- [40. Full Software Verification](design-book/08-simulation-and-verification.md#40-full-software-verification)
- [41. Hardware Qualification](design-book/08-simulation-and-verification.md#41-hardware-qualification)
- [42. Watched-Test Application](design-book/08-simulation-and-verification.md#42-watched-test-application)
- [43. Typed Test Metadata](design-book/08-simulation-and-verification.md#43-typed-test-metadata)
- [44. Verification Outputs](design-book/08-simulation-and-verification.md#44-verification-outputs)
- [45. Advanced Testing Methods](design-book/08-simulation-and-verification.md#45-advanced-testing-methods)
- [46. Formal Model Checking](design-book/08-simulation-and-verification.md#46-formal-model-checking)
- [47. Language-Specific Tooling](design-book/08-simulation-and-verification.md#47-language-specific-tooling)

## Chapter IX: User Experience And Diagnostics

- [48. Declarative Application UI](design-book/09-user-experience-and-diagnostics.md#48-declarative-application-ui)
- [49. Central Error Catalog](design-book/09-user-experience-and-diagnostics.md#49-central-error-catalog)
- [50. Doctor](design-book/09-user-experience-and-diagnostics.md#50-doctor)
- [51. Privacy-Safe Support Bundles](design-book/09-user-experience-and-diagnostics.md#51-privacy-safe-support-bundles)

## Chapter X: Updates, Packaging And Operations

- [52. Configuration Migrations](design-book/10-updates-packaging-and-operations.md#52-configuration-migrations)
- [53. Package Lifecycle](design-book/10-updates-packaging-and-operations.md#53-package-lifecycle)
- [54. Kernel Activation](design-book/10-updates-packaging-and-operations.md#54-kernel-activation)
- [55. Reproducible Development Environment](design-book/10-updates-packaging-and-operations.md#55-reproducible-development-environment)
- [56. Supply-Chain Security](design-book/10-updates-packaging-and-operations.md#56-supply-chain-security)
- [57. Performance Budgets](design-book/10-updates-packaging-and-operations.md#57-performance-budgets)

## Chapter XI: Automation, Documentation And GitHub

- [58. Change-Aware CI](design-book/11-automation-documentation-and-github.md#58-change-aware-ci)
- [59. Dependency Updates](design-book/11-automation-documentation-and-github.md#59-dependency-updates)
- [60. Documentation Portal](design-book/11-automation-documentation-and-github.md#60-documentation-portal)
- [61. GitHub Repository Experience](design-book/11-automation-documentation-and-github.md#61-github-repository-experience)

## Chapter XII: Release Boundaries

- [62. Compatibility Transitions](design-book/12-release-boundaries.md#62-compatibility-transitions)
- [63. Read-Only Comparison](design-book/12-release-boundaries.md#63-read-only-comparison)
- [64. Safe Change Order](design-book/12-release-boundaries.md#64-safe-change-order)
- [65. Release Channels](design-book/12-release-boundaries.md#65-release-channels)
- [66. Limits And Epistemic Honesty](design-book/12-release-boundaries.md#66-limits-and-epistemic-honesty)
- [67. Final Definition Of Success](design-book/12-release-boundaries.md#67-final-definition-of-success)
