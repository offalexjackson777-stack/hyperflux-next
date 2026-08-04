# Verification

The verification graph maps source ownership to bounded checks. A passing test
proves only the behavior it exercises; software results do not substitute for
physical device results.

## Development Lanes

Use focused tests while editing, changed-path verification for ordinary pull
requests, and the complete software graph for release candidates. Hardware work
is separate and never runs in hosted CI.

## References

The generated [verification graph](../generated/verification.md),
[design coverage](../generated/design-coverage.md), and
[release gates](../generated/release-gates.md) provide the current factual view.
